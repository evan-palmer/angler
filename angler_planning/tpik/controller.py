# Copyright 2023, Evan Palmer
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import kinpy
import numpy as np
import rclpy
from geometry_msgs.msg import Transform, Twist
from moveit_msgs.msg import RobotState, RobotTrajectory
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile
from std_msgs.msg import String
from tf2_ros import TransformException  # type: ignore
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener
from tpik.constraint import EqualityTask, SetTask
from tpik.hierarchy import TaskHierarchy
from tpik.tasks import (
    EndEffectorPose,
    JointLimit,
    ManipulatorConfiguration,
    VehicleOrientation,
    VehicleRollPitch,
    VehicleYaw,
)
from trajectory_msgs.msg import JointTrajectoryPoint, MultiDOFJointTrajectoryPoint


def calculate_nullspace(augmented_jacobian: np.ndarray) -> np.ndarray:
    """Calculate the nullspace of the augmented Jacobian.

    Args:
        augmented_jacobian: The augmented Jacobian whose nullspace will be projected
            into.

    Returns:
        The nullspace of the augmented Jacobian.
    """
    return (
        np.eye(augmented_jacobian.shape[0])
        - np.linalg.pinv(augmented_jacobian) @ augmented_jacobian
    )


def construct_augmented_jacobian(jacobians: list[np.ndarray]) -> np.ndarray:
    """Construct an augmented Jacobian matrix.

    The augmented Jacobian matrix is given as:

    J_i^A = [J_1, J_2, ..., J_i]^T

    Args:
        jacobians: The Jacobian matrices which should be used to construct the
            augmented Jacobian.

    Returns:
        The resulting augmented Jacobian.
    """
    return np.vstack(tuple(jacobians))


class TPIK(Node):
    """Task-priority inverse kinematic (TPIK) controller.

    The TPIK controller is responsible for calculating kinematically feasible system
    velocities subject to equality and set-based constraints.
    """

    def __init__(self) -> None:
        """Create a new TPIK node."""
        super().__init__("tpik")

        self.declare_parameter("constraint_config_path", "")
        self.declare_parameter("manipulator_base_link", "alpha_base_link")
        self.declare_parameter("manipulator_end_link", "alpha_ee_base_link")
        self.declare_parameter("control_rate", 30.0)

        # Keep track of the robot state for the tasks
        self.state = RobotState()
        self._description_received = False
        self._init_state_received = False

        # Get the constraints
        self.hierarchy = TaskHierarchy.load_tasks_from_path(
            self.get_parameter("constraint_config_path")
            .get_parameter_value()
            .string_value
        )

        # TF2
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        # Subscribers
        self.robot_description_sub = self.create_subscription(
            String,
            "/robot_description",
            self.read_robot_description_cb,
            QoSProfile(depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL),
        )
        self.robot_state_sub = self.create_subscription(
            RobotState, "/angler/state", self.robot_state_cb, 1
        )
        self.joint_trajectory_sub = self.create_subscription(
            RobotTrajectory,
            "/angler/reference_trajectory",
            self.update_trajectory_cb,
            1,
        )

        # Publishers
        self.robot_trajectory_pub = self.create_publisher(
            RobotTrajectory, "/angler/robot_trajectory", 1
        )

        # Create a new callback group for the control loop timer
        self.timer_cb_group = MutuallyExclusiveCallbackGroup()

        # Create a timer to run the TPIK controller
        self.control_timer = self.create_timer(
            1 / self.get_parameter("control_rate").get_parameter_value().double_value,
            self.update,
            self.timer_cb_group,
        )

    @property
    def initialized(self) -> bool:
        """Check whether or not the controller has been fully initialized.

        Returns:
            Whether or not the controller has been initialized.
        """
        return self._description_received and self._init_state_received

    def read_robot_description_cb(self, robot_description: String) -> None:
        """Create a kinpy serial chain from the robot description.

        Args:
            robot_description: The robot description message.
        """
        end_link = (
            self.get_parameter("manipulator_end_link")
            .get_parameter_value()
            .string_value
        )
        start_link = (
            self.get_parameter("manipulator_base_link")
            .get_parameter_value()
            .string_value
        )

        self.serial_chain = kinpy.build_serial_chain_from_urdf(
            robot_description.data,
            end_link_name=end_link,
            root_link_name=start_link,
        )

        self.n_manipulator_joints = len(self.serial_chain.get_joint_parameter_names())

        # Set the relevant task properties while we are here so that we don't have to
        # do it later
        for task in self.hierarchy.tasks:
            if hasattr(task, "n_manipulator_joints"):
                task.n_manipulator_joints = self.n_manipulator_joints  # type: ignore

            if hasattr(task, "serial_chain"):
                task.serial_chain = self.serial_chain  # type: ignore

        self._description_received = True

    def update_context(self) -> None:
        """Update the current state variables for each task."""
        print(self.state)

        for task in self.hierarchy.active_task_hierarchy:
            # As a brief note, the joint state includes the linear jaws joint angle.
            # We exclude this, because we aren't controlling it within this specific
            # framework.
            if isinstance(task, JointLimit):
                # Joint limits appear the most frequently so update those first
                joint_angle = np.array(self.state.joint_state.position)[1:][task.joint]  # type: ignore # noqa
                task.update(joint_angle)
                task.set_task_active(joint_angle)
            elif isinstance(task, VehicleRollPitch):
                vehicle_pose: Transform = self.state.multi_dof_joint_state.transforms[0]  # type: ignore # noqa
                task.update(vehicle_pose.rotation)
            elif isinstance(task, ManipulatorConfiguration):
                joint_angles = np.array(self.state.joint_state.position)[1:]  # type: ignore # noqa
                task.update(joint_angles)
            elif isinstance(task, VehicleYaw):
                vehicle_pose: Transform = self.state.multi_dof_joint_state.transforms[0]  # type: ignore # noqa
                task.update(vehicle_pose.rotation)
            elif isinstance(task, VehicleOrientation):
                vehicle_pose: Transform = self.state.multi_dof_joint_state.transforms[0]  # type: ignore # noqa
                task.update(vehicle_pose.rotation)
            elif isinstance(task, EndEffectorPose):
                joint_angles = np.array(self.state.joint_state.position)[1:]  # type: ignore # noqa
                vehicle_pose: Transform = self.state.multi_dof_joint_state.transforms[0]  # type: ignore # noqa

                # Get the necessary transforms
                try:
                    tf_manipulator_base_to_base = self.tf_buffer.lookup_transform(
                        "alpha_base_link", "base_link", self.get_clock().now()
                    )
                except TransformException as e:
                    self.get_logger().error(
                        "Unable to get the transformation from the manipulator base"
                        f" to the vehicle base: {e}"
                    )
                    continue
                try:
                    tf_map_to_ee = self.tf_buffer.lookup_transform(
                        "map", "alpha_ee_base_link", self.get_clock().now()
                    )
                except TransformException as e:
                    self.get_logger().error(
                        "Unable to get the transformation from the manipulator base"
                        f" to the vehicle base: {e}"
                    )
                    continue

                task.update(
                    joint_angles,
                    vehicle_pose,
                    tf_manipulator_base_to_base.transform,
                    tf_map_to_ee.transform,
                )

    def robot_state_cb(self, state: RobotState) -> None:
        """Update the current robot state.

        Args:
            state: The current robot state.
        """
        self.state = state
        self.update_context()
        self._init_state_received = True

    def update_trajectory_cb(self, trajectory: RobotTrajectory) -> None:
        ...

    def calculate_system_velocity(
        self,
        hierarchy: list[EqualityTask | SetTask],
        task: int = 0,
        system_velocities: np.ndarray | None = None,
        prev_jacobians: list[np.ndarray] | None = None,
        nullspace: np.ndarray | None = None,
    ):
        """Calculate the system velocities using TPIK control.

        This method calculates the system velocities recursively. This is based off of
        the formula provided in "Underwater Intervention With Remote Supervision via
        Satellite Communication: Developed Control Architecture and Experimental Results
        Within the Dexrov Project" by Di Lillo, et. al.

        Args:
            hierarchy: The task hierarchy that should be used when calculating the
                system velocities.
            task: The current task index in the activated task hierarchy. Defaults to 0.
            system_velocities: The calculated system velocities. Defaults to None.
            prev_jacobians: The list of previous Jacobians. Defaults to None.
            nullspace: The nullspace which the task Jacobian should be projected into.
                Defaults to None.

        Returns:
            The calculated system velocities.
        """
        # Get the task
        t = hierarchy[task]

        J = t.jacobian
        r = t.error
        K = t.gain * np.eye(r.shape[0])

        # Use the desired time derivative if the task is time-varying, otherwise use
        # a regularization task
        if t.desired_value_dot is not None:
            t_dot = t.desired_value_dot
        else:
            t_dot = np.zeros(r.shape)

        # Calculate the system velocities
        if task == 0 or None in [system_velocities, prev_jacobians, nullspace]:
            # Set the initial system velocities to 0
            system_velocities = np.zeros((6 + self.n_manipulator_joints, 1))

            if prev_jacobians is None:
                prev_jacobians = []

            # The nullspace for the first task is the identity matrix
            updated_system_velocities = (
                t_dot + np.linalg.pinv(J) * K @ r - J * system_velocities
            )
        else:
            updated_system_velocities = (
                t_dot + np.linalg.pinv(J @ nullspace) * K @ r - J @ system_velocities
            )

        # Stop recursion
        if task == len(self.hierarchy.active_task_hierarchy) - 1:
            return updated_system_velocities

        # Now update for the next iteration
        # Note that this will modify the top-level list
        prev_jacobians.append(J)  # type: ignore
        updated_nullspace = calculate_nullspace(
            construct_augmented_jacobian(prev_jacobians)  # type: ignore
        )

        return self.calculate_system_velocity(
            hierarchy,
            task=task + 1,
            prev_jacobians=prev_jacobians,
            system_velocities=updated_system_velocities,
            nullspace=updated_nullspace,
        )

    def update(self) -> None:
        """Send the updated system velocities."""
        # Wait for the system to be initialized before running the controller
        if not self.initialized:
            return

        # Calculate the system velocities for each potential hierarchy
        hierarchies = self.hierarchy.modes

        solutions = []

        for hierarchy in hierarchies:
            system_velocities = self.calculate_system_velocity(hierarchy)

            set_tasks = [
                set_task for set_task in hierarchy if isinstance(set_task, SetTask)
            ]

            # If there are no set tasks, break early and use the equality task solution
            if not set_tasks:
                break

            # Otherwise, we need to check whether or not the solution will drive the
            # system to the safe set
            satisfied = False
            for set_task in set_tasks:
                if (
                    set_task.current_value < set_task.activation_threshold.lower
                    and set_task.jacobian @ system_velocities > 0
                ):
                    satisfied = True
                elif (
                    set_task.current_value > set_task.activation_threshold.upper
                    and set_task.jacobian @ system_velocities < 0
                ):
                    satisfied = True

                # If it is a valid solution, save it for future consideration
                if satisfied:
                    solutions.append(system_velocities)

        # Select the solution with the highest norm (this is the least conservative
        # solution)
        system_velocities = solutions[np.argmax([np.linalg.norm(x) for x in solutions])]

        cmd = RobotTrajectory()

        # Create the vehicle command
        vehicle_cmd = MultiDOFJointTrajectoryPoint()
        vehicle_vel = Twist()

        (
            vehicle_vel.linear.x,
            vehicle_vel.linear.y,
            vehicle_vel.linear.z,
            vehicle_vel.angular.x,
            vehicle_vel.angular.y,
            vehicle_vel.angular.z,
        ) = system_velocities[:6]

        vehicle_cmd.velocities = [vehicle_vel]

        # Create the manipulator command
        arm_cmd = JointTrajectoryPoint()
        arm_cmd.velocities = [0.0] + list(system_velocities[6:])

        # Create the full system command
        cmd.multi_dof_joint_trajectory.joint_names = ["vehicle"]
        cmd.joint_trajectory.joint_names = self.serial_chain.get_joint_parameter_names()
        cmd.multi_dof_joint_trajectory.points = [vehicle_cmd]
        cmd.joint_trajectory.points = [arm_cmd]

        self.robot_trajectory_pub.publish(cmd)


def main(args: list[str] | None = None):
    """Run the TPIK controller."""
    rclpy.init(args=args)

    node = TPIK()
    executor = MultiThreadedExecutor()
    rclpy.spin(node, executor)

    node.destroy_node()
    rclpy.shutdown()
