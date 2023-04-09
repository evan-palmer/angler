# Copyright 2023, Evan Palmer
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from typing import Any

import cv2
import numpy as np
import rclpy
import tf_transforms as tf
from geometry_msgs.msg import Pose, PoseStamped
from gi.repository import Gst
from rclpy.node import Node


class ArucoMarkerDetector(Node):
    """
    ArUco marker pose estimator.

    The ArUco marker detector is responsible for detecting any ArUco markers in the
    BlueROV2 camera stream and, if any markers are detected, estimating the pose of the
    BlueROV2 relative to the tag. Once the pose has been estimated, the resulting pose
    is sent to the ArduSub EKF for fusing.
    """

    ARUCO_MARKER_TYPES = [
        cv2.aruco.DICT_4X4_50,
        cv2.aruco.DICT_4X4_100,
        cv2.aruco.DICT_4X4_250,
        cv2.aruco.DICT_4X4_1000,
        cv2.aruco.DICT_5X5_50,
        cv2.aruco.DICT_5X5_100,
        cv2.aruco.DICT_5X5_250,
        cv2.aruco.DICT_5X5_1000,
        cv2.aruco.DICT_6X6_50,
        cv2.aruco.DICT_6X6_100,
        cv2.aruco.DICT_6X6_250,
        cv2.aruco.DICT_6X6_1000,
        cv2.aruco.DICT_7X7_50,
        cv2.aruco.DICT_7X7_100,
        cv2.aruco.DICT_7X7_250,
        cv2.aruco.DICT_7X7_1000,
        cv2.aruco.DICT_ARUCO_ORIGINAL,
    ]

    def __init__(self) -> None:
        super().__init__("aruco_marker_detector")

        self.declare_parameters(
            "",
            [
                ("port", 5600),
                ("camera_matrix", np.zeros((1, 9))),  # Reshaped to 3x3
                ("projection_matrix", np.zeros((1, 12))),  # Reshaped to 3x4
                ("distortion_coefficients", np.zeros((1, 5))),
            ],
        )

        # Get the camera intrinsics
        self.camera_matrix = np.array(
            self.get_parameter("camera_matrix")
            .get_parameter_value()
            .double_array_value,
            np.float32,
        ).reshape(3, 3)

        self.projection_matrix = np.array(
            self.get_parameter("projection_matrix")
            .get_parameter_value()
            .double_array_value,
            np.float32,
        ).reshape(3, 4)

        self.distortion_coefficients = np.array(
            self.get_parameter("distortion_coefficients")
            .get_parameter_value()
            .double_array_value,
            np.float32,
        ).reshape(1, 5)

        self.visual_odom_pub = self.create_publisher(
            PoseStamped, "/mavros/vision_pose/pose", 1
        )

        # Start the Gstreamer stream
        self.video_pipe, self.video_sink = self.init_stream(
            self.get_parameter("port").get_parameter_value().integer_value
        )

    def init_stream(self, port: int) -> tuple[Any, Any]:
        Gst.init(None)

        video_source = f"udpsrc port={port}"
        video_codec = (
            "! application/x-rtp, payload=96 ! rtph264depay ! h264parse ! avdec_h264"
        )
        video_decode = (
            "! decodebin ! videoconvert ! video/x-raw,format=(string)BGR ! videoconvert"
        )
        video_sink_conf = (
            "! appsink emit-signals=true sync=false max-buffers=2 drop=true"
        )

        command = " ".join([video_source, video_codec, video_decode, video_sink_conf])

        video_pipe = Gst.parse_launch(command)
        video_pipe.set_state(Gst.State.PLAYING)

        video_sink = video_pipe.get_by_name("appsink0")

        video_sink.connect("new-sample", self.extract_and_publish_pose_cb)

        return video_pipe, video_sink

    @staticmethod
    def gst_to_opencv(frame: Any) -> np.ndarray:
        buf = frame.get_buffer()
        caps = frame.get_caps()

        return np.ndarray(
            (
                caps.get_structure(0).get_value("height"),
                caps.get_structure(0).get_value("width"),
                3,
            ),
            buffer=buf.extract_dup(0, buf.get_size()),
            dtype=np.uint8,
        )

    def detect_tag(self, frame: np.ndarray) -> tuple[Any, Any] | None:
        # Check each tag type, breaking when we find one that works
        for tag_type in self.ARUCO_MARKER_TYPES:
            aruco_dict = cv2.aruco.Dictionary_get(tag_type)
            aruco_params = cv2.aruco.DetectorParameters_create()

            try:
                # Return the corners and ids if we find the correct tag type
                corners, ids, _ = cv2.aruco.detectMarkers(
                    frame, aruco_dict, parameters=aruco_params
                )

                if len(ids) > 0:
                    return corners, ids

            except Exception:
                continue

        # Nothing was found
        return None

    def get_marker_pose(self, frame: np.ndarray) -> tuple[Pose, int] | None:
        # Convert to greyscale image then try to detect the tag(s)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        detection = self.detect_tag(gray)

        if detection is None:
            return None

        corners, ids = detection

        # If there are multiple markers, get the marker with the "longest" side, where
        # "longest" should be interpretted as the relative size in the image
        side_lengths = [
            abs(corner[0][0][0] - corner[0][2][0])
            + abs(corner[0][0][1] - corner[0][2][1])
            for corner in corners
        ]

        min_side_idx = side_lengths.index(max(side_lengths))

        # Get the estimated pose
        rot_vec, trans_vec, _ = cv2.aruco.estimatePoseSingleMarkers(
            corners[min_side_idx],
            ids[min_side_idx],
            self.camera_matrix,
            self.distortion_coefficients,
        )

        # Convert to a pose msg
        pose = Pose()

        (
            pose.position.x,
            pose.position.y,
            pose.position.z,
        ) = trans_vec.squeeze()

        # Convert the rotation vector to a quaternion
        tf_mat = np.identity(4)
        tf_mat[:3, :3], _ = cv2.Rodrigues(rot_vec)

        (
            pose.orientation.x,
            pose.orientation.y,
            pose.orientation.z,
            pose.orientation.w,
        ) = tf.quaternion_from_matrix(tf_mat)

        return pose, ids[min_side_idx]

    def extract_and_publish_pose_cb(self, sink: Any) -> Any:
        frame = self.gst_to_opencv(sink.emit("pull-sample"))

        # Get the pose of the camera in the `marker` frame
        marker_pose = self.get_marker_pose(frame)

        # If there was no marker in the image, exit early
        if marker_pose is None:
            self.get_logger().debug(
                "An ArUco marker could not be detected in the current frame"
            )
            return Gst.FlowReturn.OK

        # Transform the pose from the `marker` frame to the `map` frame
        pose, marker_id = marker_pose

        # TODO(evan-palmer): Get the transform from the marker ID to the map frame
        # TODO(evan-palmer): then apply the transform. Calculate the estimated velocity
        # TODO(evan-palmer): and publish to the ArduSub EKF

        pose_msg = PoseStamped()

        pose_msg.header.stamp = self.get_clock().now()
        pose_msg.header.frame_id = "map"

        (
            pose_msg.pose.position.x,
            pose_msg.pose.position.y,
            pose_msg.pose.position.z,
        ) = [0, 0, 0]

        (
            pose_msg.pose.orientation.x,
            pose_msg.pose.orientation.y,
            pose_msg.pose.orientation.z,
            pose_msg.pose.orientation.w,
        ) = [0, 0, 0, 0]

        self.visual_odom_pub.publish(pose_msg)

        return Gst.FlowReturn.OK


def main(args=None):
    """Run the ArUco tag detector."""
    rclpy.init(args=args)

    node = ArucoMarkerDetector()
    rclpy.spin(node)

    node.destroy_node()
    rclpy.shutdown()

    return


if __name__ == "__main__":
    main()
