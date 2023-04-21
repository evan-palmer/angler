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

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import (
    LaunchConfiguration,
    PathJoinSubstitution,
    PythonExpression,
)
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    """Generate a launch description for the localization package.

    Returns:
        The localization ROS 2 launch description.
    """
    args = [
        DeclareLaunchArgument(
            "config_filepath",
            default_value=None,
            description="The path to the configuration YAML file",
        ),
        DeclareLaunchArgument(
            "source",
            default_value="qualisys_mocap",
            choices=["qualisys_mocap", "camera"],
            description="The localization source to stream from.",
        ),
    ]

    source = LaunchConfiguration("source")

    nodes = [
        Node(
            package="angler_localization",
            executable="camera",
            name="camera",
            output="screen",
            parameters=[LaunchConfiguration("config_filepath")],
            condition=IfCondition(PythonExpression(["'", source, "' == 'camera'"])),
        ),
        Node(
            package="angler_localization",
            executable="aruco_marker_localizer",
            name="aruco_marker_localizer",
            output="screen",
            parameters=[LaunchConfiguration("config_filepath")],
            condition=IfCondition(PythonExpression(["'", source, "' == 'camera'"])),
        ),
        Node(
            package="angler_localization",
            executable="qualisys_mocap",
            name="qualisys_mocap",
            output="screen",
            parameters=[LaunchConfiguration("config_filepath")],
            condition=IfCondition(
                PythonExpression(["'", source, "' == 'qualisys_mocap'"])
            ),
        ),
        Node(
            package="angler_localization",
            executable="qualisys_localizer",
            name="qualisys_localizer",
            output="screen",
            parameters=[LaunchConfiguration("config_filepath")],
            condition=IfCondition(
                PythonExpression(["'", source, "' == 'qualisys_mocap'"])
            ),
        ),
    ]

    includes = [
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                PathJoinSubstitution(
                    [FindPackageShare("angler_localization"), "markers.launch.py"]
                )
            ),
            condition=IfCondition(PythonExpression(["'", source, "' == 'camera'"])),
        )
    ]

    return LaunchDescription(args + nodes + includes)
