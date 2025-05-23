#!/usr/bin/env python3

import time, os, sys
import numpy as np
import jax
import jax.numpy as jnp

import rclpy
from rclpy.node import Node
import tf_transformations
from geometry_msgs.msg import Point, PoseStamped
from sensor_msgs.msg import LaserScan
from nav_msgs.msg import Odometry, OccupancyGrid
from ackermann_msgs.msg import AckermannDriveStamped
from std_msgs.msg import Float32MultiArray, MultiArrayDimension
from parallel_parking_interfaces.msg import Traj
from utils.ros_np_multiarray import to_multiarray_f32, to_numpy_f32
from utils.utils import pose_to_xyyaw, poses_to_xyyaw, wrap_to_2pi, angle_diff
from ament_index_python.packages import get_package_share_directory

from infer_env import InferEnv
from mppi_tracking import MPPI
import utils.utils as utils
from utils.jax_utils import numpify
import utils.jax_utils as jax_utils
from utils.Track import Track

from visualization_msgs.msg import Marker

# jax.config.update("jax_compilation_cache_dir", "/home/nvidia/jax_cache") 
jax.config.update("jax_compilation_cache_dir", "/cache/jax")

## This is a demosntration of how to use the MPPI planner with the Roboracer
## Zirui Zang 2025/04/07

class MPPI_Node(Node):
    def __init__(self):
        super().__init__('lmppi_node')
        self.config = utils.ConfigYAML()
        config_dir = get_package_share_directory('mppi')
        config_path = os.path.join(config_dir, 'config_park_slot1.yaml')
        self.config.load_file(config_path)
        self.config.norm_params = np.array(self.config.norm_params).T
        if self.config.random_seed is None:
            self.config.random_seed = np.random.randint(0, 1e6)
        self.jrng = jax_utils.oneLineJaxRNG(self.config.random_seed)    
        # map_dir = os.path.join(config_dir, 'waypoints/')
        map_dir = None
        if map_dir is not None:
            map_info = None
            # map_info = np.genfromtxt(map_dir + 'map_info.txt', delimiter='|', dtype='str')]
            map_ind = self.config.map_ind if hasattr(self.config, 'map_ind') else None
            self.track, self.config = Track.load_map(map_dir, map_info, map_ind, self.config)
            # self.track.waypoints[:, 3] += 0.5 * np.pi
            self.infer_env = InferEnv(self.track, self.config, DT=self.config.sim_time_step)
            self.mppi = MPPI(self.config, self.infer_env, self.jrng)
            # Do a dummy call on the MPPI to initialize the variables
            state_c_0 = np.asarray([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            self.control = np.asarray([0.0, 0.0]) # [steering angle, speed]
            reference_traj, waypoint_ind = self.infer_env.get_refernece_traj(state_c_0.copy(), self.config.ref_vel, self.config.n_steps)
            self.mppi.update(jnp.asarray(state_c_0), jnp.asarray(reference_traj))
            self.get_logger().info('MPPI initialized')
        else:
            self.track = None
            self.infer_env = None
            self.mppi = None
        
        qos = rclpy.qos.QoSProfile(history=rclpy.qos.QoSHistoryPolicy.KEEP_LAST,
                                   depth=10,
                                   reliability=rclpy.qos.QoSReliabilityPolicy.RELIABLE,
                                   durability=rclpy.qos.QoSDurabilityPolicy.VOLATILE)
        self.declare_parameter('extrapolated_path_topic', '/extrapolated_path')
        extrapolated_path_topic = self.get_parameter('extrapolated_path_topic').get_parameter_value().string_value
        # create subscribers
        self.grid_sub = self.create_subscription(OccupancyGrid, "/occupancy_grid", self.grid_callback, qos)        

        if self.config.is_sim:
            self.pose_sub = self.create_subscription(Odometry, "/ego_racecar/odom", self.pose_callback, qos)
        else:
            self.pose_sub = self.create_subscription(Odometry, "/pf/pose/odom", self.pose_callback, qos)

        self.traj_sub = self.create_subscription(Traj, extrapolated_path_topic, self.traj_callback, qos)

        # publishers
        self.drive_pub = self.create_publisher(AckermannDriveStamped, "/drive", qos)
        self.reference_pub = self.create_publisher(Float32MultiArray, "/reference_arr", qos)
        self.opt_traj_pub = self.create_publisher(Float32MultiArray, "/opt_traj_arr", qos)
        
        self.obstacle_marker_pub = self.create_publisher(Marker, "/obstacle_points_marker", 10)
        self.point_marker_pub = self.create_publisher(Marker, "/point_marker", 10)
        self.point_marker_pub_rev = self.create_publisher(Marker, "/point_marker_rev", 10)
        
        self.traj_marker_pub = self.create_publisher(Marker, "/traj_marker", 10)

        # map info
        self.map_received = False
        self.width = None
        self.height = None
        self.resolution = None
        self.origin = None
        self.occup_pos = None

        # Traj thres
        self.filtered_thres_forward = 0.2 # m
        self.filtered_thres_reverse = 0.1 # m
        self.pos_thres = 0.4 # m
        self.yaw_thres = 0.3 # rad
        self.waiting_for_traj = False
        self.max_steering_angle_forward = 0.5 # rad
        self.max_steering_angle_reverse = 0.25 # rad

        self.stage_cnt = None

    def traj_callback(self, traj_msg):
        if self.track is None or self.infer_env is None or self.mppi is None:
            trajectory = traj_msg.traj
            end_pose = traj_msg.end_pose
            # Convert the trajectory to a numpy array
            trajectory = poses_to_xyyaw(trajectory)
            self.end_pose = pose_to_xyyaw(end_pose)
            self.publish_traj_marker(trajectory, frame_id="map")
            if self.stage_cnt is None:
                self.stage_cnt = 0
            else:
                self.stage_cnt += 1
            self.track, self.config = Track.load_traj(trajectory, self.config)
            self.infer_env = InferEnv(self.track, self.config, DT=self.config.sim_time_step)
            self.mppi = MPPI(self.config, self.infer_env, self.jrng)
            # Do a dummy call on the MPPI to initialize the variables
            state_c_0 = np.asarray([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            self.control = np.asarray([0.0, 0.0])
            reference_traj, waypoint_ind = self.infer_env.get_refernece_traj(state_c_0.copy(), self.config.ref_vel, self.config.n_steps)
            self.mppi.update(jnp.asarray(state_c_0), jnp.asarray(reference_traj), self.stage_cnt)
            self.waiting_for_traj = False
            self.get_logger().info(f"Received {self.stage_cnt} trajectories with {trajectory.shape[0]} points.")
            self.get_logger().info('MPPI initialized')

    def publish_traj_marker(self, traj: np.ndarray, frame_id="map"):
        """
        Publishes a trajectory as a Marker line strip.

        Args:
            traj (np.ndarray): Shape (N, 2) or (N, 3), where each row is (x, y) or (x, y, z)
            frame_id (str): Frame to visualize in RViz
        """
        marker = Marker()
        marker.header.frame_id = frame_id
        marker.header.stamp = self.get_clock().now().to_msg()
        marker.ns = "traj"
        marker.id = 0
        marker.type = Marker.LINE_STRIP
        marker.action = Marker.ADD
        marker.scale.x = 0.1  # line width

        marker.color.a = 1.0
        marker.color.r = 1.0
        marker.color.g = 0.0
        marker.color.b = 0.0

        marker.pose.orientation.w = 1.0  # identity quaternion

        # fill in points
        for p in traj[:, :2]:
            pt = Point()
            pt.x = float(p[0])
            pt.y = float(p[1])
            pt.z = float(p[2]) if p.shape[0] > 2 else 0.0
            marker.points.append(pt)

        self.traj_marker_pub.publish(marker)

    def grid_callback(self, grid_msg):
        if self.width is None or self.height is None or self.resolution is None or self.origin is None:
            self.width = grid_msg.info.width
            self.height = grid_msg.info.height
            self.resolution = grid_msg.info.resolution
            self.origin = grid_msg.info.origin.position

        grid = np.array(grid_msg.data).reshape((self.height, self.width))
        occupied_indices = np.argwhere(grid >= 100)[::5]
        occupied_pos = self.grid_to_world_batch(occupied_indices, self.origin, self.resolution)
        self.occup_pos = occupied_pos

        self.map_received = True
        
    def grid_to_world_batch(self, occupied_indices, origin, resolution):
        i = occupied_indices[:, 0]
        j = occupied_indices[:, 1]

        x = origin.x + j * resolution + resolution / 2.0
        y = origin.y + i * resolution + resolution / 2.0

        return np.stack((x, y), axis=-1)  # shape: [N, 2]

    def uniform_resample(self, obstacles: np.ndarray, max_obstacles: int) -> np.ndarray:
        """
        If len(obs) > max_obstacles: uniformly subsample.
        If len(obs) < max_obstacles: randomly replicate existing points
        (sampling with replacement) until you have exactly max_obstacles.
        Returns an array of shape (max_obstacles,2).
        """
        M = obstacles.shape[0]
        if M == 0:
            return np.zeros((max_obstacles, 2), dtype=obstacles.dtype)

        if M >= max_obstacles:
            # down-sample uniformly
            idxs = np.linspace(0, M - 1, max_obstacles, dtype=int)
            return obstacles[idxs]

        # up-sample by random sampling WITH replacement
        # keep all original M, then sample (max_obstacles - M) extras
        extra_n = max_obstacles - M
        # choose random indices from [0..M-1]
        extra_idxs = np.random.choice(M, size=extra_n, replace=True)
        extras = obstacles[extra_idxs]
        # concatenate original + extras
        out = np.vstack([obstacles, extras])

        return out
    
    def is_goal_in_front(self, state, end_pos):
        current_pos = state[:2]
        current_yaw = state[4]
        to_goal = np.array(end_pos[:2]) - np.array(current_pos[:2])
        to_goal_norm = to_goal / (np.linalg.norm(to_goal) + 1e-6)
        # to car coordinates
        heading = np.array([np.cos(current_yaw), np.sin(current_yaw)])
        dot = heading @ to_goal_norm

        return dot > 0  # True = in front, False = behind

    def filtering_roi_obstacles(self, state, obstacle_world_coords, roi_area=(2.0, 2.0), max_obstacles=200):
        '''
        Filters obstacles within a rectangular ROI in front of the car.
        
        Args:
            state (np.ndarray): current vehicle state [x, y, steering, v, yaw, ..., ...]
            obstacle_world_coords (np.ndarray): shape [N, 2] array of world-frame (x, y) obstacle positions
            roi_area (tuple): ROI dimensions (length, width), in meters

        Returns:
            np.ndarray: filtered (x, y) world coordinates of obstacles in ROI
        '''
        x, y, yaw = state[0], state[1], state[4]
        dx = obstacle_world_coords[:, 0] - x
        dy = obstacle_world_coords[:, 1] - y

        # Transform to robot frame
        cos_yaw = np.cos(-yaw)
        sin_yaw = np.sin(-yaw)
        x_local = cos_yaw * dx - sin_yaw * dy
        y_local = sin_yaw * dx + cos_yaw * dy

        length, width = roi_area
        x_min, x_max = 0.0, length
        y_min, y_max = -width / 2.0, width / 2.0

        # Filter points inside ROI
        mask = (x_local >= x_min) & (x_local <= x_max) & \
            (y_local >= y_min) & (y_local <= y_max)
        filtered_obstacles = obstacle_world_coords[mask]

        filtered_obstacles = self.uniform_resample(filtered_obstacles, max_obstacles)    

        return filtered_obstacles

    def publish_obstacle_points(self, obstacle_world_coords):
        marker = Marker()
        marker.header.frame_id = "map"
        marker.header.stamp = self.get_clock().now().to_msg()
        marker.ns = "obstacles"
        marker.id = 0
        marker.type = Marker.POINTS
        marker.action = Marker.ADD

        # Visual appearance
        marker.scale.x = 0.1  # point width
        marker.scale.y = 0.1  # point height
        marker.color.a = 1.0
        marker.color.r = 1.0
        marker.color.g = 0.0
        marker.color.b = 0.0

        # Add points
        for x, y in obstacle_world_coords:
            p = Point()
            p.x = float(x)
            p.y = float(y)
            p.z = 0.0
            marker.points.append(p)

        self.obstacle_marker_pub.publish(marker)

    def publish_point_marker(self, x, y, rev=False):
        marker = Marker()
        marker.header.frame_id = "map"
        marker.header.stamp = self.get_clock().now().to_msg()
        marker.ns = "point_marker"
        marker.id = 0
        marker.type = Marker.SPHERE
        marker.action = Marker.ADD

        # Visual appearance
        marker.scale.x = 0.1
        marker.scale.y = 0.1
        marker.scale.z = 0.1
        marker.color.a = 1.0
        marker.color.r = 0.0
        marker.color.g = 1.0
        marker.color.b = 0.0
        # Set the position of the point
        marker.pose.position.x = x
        marker.pose.position.y = y
        marker.pose.position.z = 0.0
        marker.pose.orientation.x = 0.0
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 1.0
        if rev:
            marker.id = 1
            marker.color.r = 1.0
            marker.color.g = 0.0
            marker.color.b = 0.0
            self.point_marker_pub_rev.publish(marker)
        else:
            self.point_marker_pub.publish(marker)

    def check_is_traj_done(self, state: np.ndarray, end_pose: np.ndarray) -> bool:
        """
        Check if the trajectory is done by checking the distance and yaw difference
        between the current state and the end pose.

        Args:
            state (np.ndarray): current vehicle state [x, y, steering, v, yaw, ..., ...]
            end_pose (np.ndarray): end pose [x, y, yaw]

        Returns:
            bool: True if the trajectory is done, False otherwise
        """
        yaw   = wrap_to_2pi(state[4])          # current heading in [0,2π)
        yaw_r = self.end_pose[2]
        # Smallest absolute yaw error (0 … π)
        yaw_error = np.abs(angle_diff(yaw, yaw_r))
        pos_error = np.linalg.norm(state[:2] - self.end_pose[:2])
        self.get_logger().info(f"x, y, yaw: {state[:2]}, end_pose: {self.end_pose[:2]}", throttle_duration_sec=1.0)
        self.get_logger().info(f"yaw: {yaw}, end_pose: {yaw_r}", throttle_duration_sec=1.0)
        self.get_logger().info(f"pos_error: {pos_error}, yaw_error: {yaw_error}", throttle_duration_sec=1.0)
        if pos_error < self.pos_thres and yaw_error < self.yaw_thres:
            return True
        return False

    def pose_callback(self, pose_msg):
        """
        Callback function for subscribing to particle filter's inferred pose.
        This funcion saves the current pose of the car and obtain the goal
        waypoint from the pure pursuit module.

        Args: 
            pose_msg (PoseStamped): incoming message from subscribed topic
        """
        if not self.map_received:
            self.get_logger().warning("Waiting for map data...")
            return
        elif self.mppi is None:
            self.get_logger().warning("Waiting for the trajectory.")
            return
        
        pose = pose_msg.pose.pose
        twist = pose_msg.twist.twist

        # Beta calculated by the arctan of the lateral velocity and the longitudinal velocity
        beta = np.arctan2(twist.linear.y, twist.linear.x)

        # For demonstration, let’s assume we have these quaternion values
        quaternion = [pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w]

        # Convert quaternion to Euler angles
        euler = tf_transformations.euler_from_quaternion(quaternion)

        # Extract the Z-angle (yaw)
        theta = euler[2]

        state_c_0 = np.asarray([
            pose.position.x,
            pose.position.y,
            self.control[0],
            max(twist.linear.x, self.config.init_vel),
            theta,
            twist.angular.z,
            beta,
        ])
        if self.check_is_traj_done(state_c_0, self.end_pose):
            self.mppi = None
            self.control = np.asarray([0.0, 0.0])
            max_steering_angle = 0.0
            self.get_logger().info("Trajectory done, stopping the MPPI.")

        else:
            filtered_obstacles = self.filtering_roi_obstacles(state_c_0, self.occup_pos)
            self.publish_obstacle_points(filtered_obstacles)
            curr_speed = twist.linear.x
            if self.is_goal_in_front(state_c_0, self.end_pose):
                filtered_thres = self.filtered_thres_forward
                find_waypoint_vel = max(self.config.ref_vel, twist.linear.x)
                find_waypoint_vel =  np.clip(find_waypoint_vel, 0.0, self.config.ref_vel)
                reference_traj, waypoint_ind = self.infer_env.get_refernece_traj(
                    state_c_0.copy(), find_waypoint_vel, self.config.n_steps)
                max_steering_angle = self.max_steering_angle_forward
                # curr_speed = twist.linear.x
            else:
                filtered_thres = self.filtered_thres_reverse
                state_c_0[3] = min(twist.linear.x, -self.config.init_vel)
                find_waypoint_vel = min(-self.config.ref_vel, twist.linear.x)
                find_waypoint_vel =  np.clip(find_waypoint_vel, -self.config.ref_vel, 0.0)
                reference_traj, waypoint_ind = self.infer_env.get_refernece_traj(
                    state_c_0.copy(), find_waypoint_vel, self.config.n_steps, reverse=True)
                max_steering_angle = self.max_steering_angle_reverse

                # curr_speed = -twist.linear.x

            # Doing filtering and help mppi to not get stucked in oscillations
            distances = np.linalg.norm(reference_traj[:, :2] - self.end_pose[:2], axis=1)
            num_within = np.sum(distances < filtered_thres)
            # If at least half are close to the goal
            if num_within >= (self.config.n_steps // 2):
                reference_traj = np.tile(self.end_pose, (self.config.n_steps, 1))
            if np.any(distances < filtered_thres):
                reference_traj = np.tile(self.end_pose, (self.config.n_steps + 1, 1))
            
            # self.get_logger().info(f"reference_traj yaw: {reference_traj[:, 3]}")
            ## MPPI call
            self.mppi.update(jnp.asarray(state_c_0), jnp.asarray(reference_traj), self.stage_cnt, jnp.asarray(filtered_obstacles))
            # self.mppi.update(jnp.asarray(state_c_0), jnp.asarray(reference_traj))
            mppi_control = numpify(self.mppi.a_opt[0]) * self.config.norm_params[0, :2]/2
            # print(f"mppi_control: {mppi_control}")
            self.control[0] = float(mppi_control[0]) * self.config.sim_time_step + self.control[0]
            self.control[1] = float(mppi_control[1]) * self.config.sim_time_step + curr_speed
            
            if self.reference_pub.get_subscription_count() > 0:
                ref_traj_cpu = numpify(reference_traj)
                arr_msg = to_multiarray_f32(ref_traj_cpu.astype(np.float32))
                self.reference_pub.publish(arr_msg)

            if self.opt_traj_pub.get_subscription_count() > 0:
                opt_traj_cpu = numpify(self.mppi.traj_opt)
                arr_msg = to_multiarray_f32(opt_traj_cpu.astype(np.float32))
                self.opt_traj_pub.publish(arr_msg)

            # if twist.linear.x < self.config.init_vel:
            #     self.control = [0.0, self.config.init_vel * 2]

            if np.isnan(self.control).any() or np.isinf(self.control).any():
                self.control = np.array([0.0, 0.0])
                self.mppi.a_opt = np.zeros_like(self.mppi.a_opt)

        # Publish the control command
        drive_msg = AckermannDriveStamped()
        drive_msg.header.stamp = self.get_clock().now().to_msg()
        drive_msg.header.frame_id = "base_link"
        drive_msg.drive.steering_angle = np.clip(self.control[0], -max_steering_angle, max_steering_angle)
        drive_msg.drive.speed = np.clip(self.control[1], -self.config.ref_vel, self.config.ref_vel)
        # self.get_logger().info(f"Steering Angle: {drive_msg.drive.steering_angle}, Speed: {drive_msg.drive.speed}")
        self.drive_pub.publish(drive_msg)

        if self.mppi == None:
            pos_error = np.linalg.norm(state_c_0[:2] - self.end_pose[:2])
            self.get_logger().info(f"The pos_error is {pos_error:.2f}, the pose is {state_c_0[:2]}, the end pose is {self.end_pose[:2]}")
            yaw_error  = np.abs(state_c_0[4] - self.end_pose[2])
            self.get_logger().info(f"The yaw_error is {yaw_error:.2f}, the yaw is {state_c_0[4]}, the end pose is {self.end_pose[2]}")
            self.waiting_for_traj = True
        

def main(args=None):
    rclpy.init(args=args)
    mppi_node = MPPI_Node()
    rclpy.spin(mppi_node)

    mppi_node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()