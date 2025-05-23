#!/usr/bin/env python3

import os, sys
import jax
import jax.numpy as jnp
import numpy as np
from functools import partial
from numba import njit
import utils.jax_utils as jax_utils
from dynamics_models.dynamics_models_jax import *

CUDANUM = 0
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"
os.environ["CUDA_VISIBLE_DEVICES"] = str(CUDANUM)

class InferEnv():
    def __init__(self, track, config, DT,
                 jrng=None, dyna_config=None) -> None:
        self.a_shape = 2
        self.track = track
        self.waypoints = track.waypoints
        self.diff = self.waypoints[1:, 1:3] - self.waypoints[:-1, 1:3]
        self.waypoints_distances = np.linalg.norm(self.waypoints[1:, (1, 2)] - self.waypoints[:-1, (1, 2)], axis=1)
        self.reference = None
        self.DT = DT
        self.config = config
        self.jrng = jax_utils.oneLineJaxRNG(0) if jrng is None else jrng
        self.state_frenet = jnp.zeros(6)
        self.norm_params = config.norm_params
        self.xy_cost = jnp.asarray(self.config.xy_cost, dtype=jnp.float32)
        self.yaw_cost = jnp.asarray(self.config.yaw_cost, dtype=jnp.float32)
        self.obs_cost = jnp.asarray(self.config.obs_cost, dtype=jnp.float32)
        print('MPPI Model:', self.config.state_predictor)
        
        def RK4_fn(x0, u, Ddt, vehicle_dynamics_fn, args):
            # return x0 + vehicle_dynamics_fn(x0, u, *args) * Ddt # Euler integration
            # RK4 integration
            k1 = vehicle_dynamics_fn(x0, u, *args)
            k2 = vehicle_dynamics_fn(x0 + k1 * 0.5 * Ddt, u, *args)
            k3 = vehicle_dynamics_fn(x0 + k2 * 0.5 * Ddt, u, *args)
            k4 = vehicle_dynamics_fn(x0 + k3 * Ddt, u, *args)
            return x0 + (k1 + 2 * k2 + 2 * k3 + k4) / 6 * Ddt
            
        if self.config.state_predictor == 'dynamic_ST':
            @jax.jit
            def update_fn(x, u):
                x1 = x.copy()
                Ddt = 0.05
                def step_fn(i, x0):
                    args = (self.config.friction,)
                    return RK4_fn(x0, u, Ddt, vehicle_dynamics_st, args)
                x1 = jax.lax.fori_loop(0, int(self.DT/Ddt), step_fn, x1)
                # x1 = jnp.nan_to_num(x1, nan=0.0, posinf=1e12, neginf=-1e12)
                return (x1, 0, x1-x)
            self.update_fn = update_fn
            
        elif self.config.state_predictor == 'kinematic_ST':
            @jax.jit
            def update_fn(x, u,):
                x_k = x.copy()[:5]
                Ddt = 0.05
                def step_fn(i, x0):
                    args = ()
                    return RK4_fn(x0, u, Ddt, vehicle_dynamics_ks, args)
                x_k = jax.lax.fori_loop(0, int(self.DT/Ddt), step_fn, x_k)
                x1 = x.at[:5].set(x_k)
                # x1 = jnp.nan_to_num(x1, nan=0.0, posinf=1e12, neginf=-1e12)
                return (x1, 0, x1-x)
            self.update_fn = update_fn
            
    @partial(jax.jit, static_argnums=(0,))
    def step(self, x, u, rng_key=None, dyna_norm_param=None):
        return self.update_fn(x, u * self.norm_params[0, :2]/2)
    
    @partial(jax.jit, static_argnums=(0,))
    def reward_fn_sey(self, s, reference):
        """
        reward function for the state s with respect to the reference trajectory
        """
        sey_cost = -jnp.linalg.norm(reference[1:, 4:6] - s[:, :2], ord=1, axis=1)
        # vel_cost = -jnp.linalg.norm(reference[1:, 3] - s[:, 3])
        # yaw_cost = -jnp.abs(jnp.sin(reference[1:, 4]) - jnp.sin(s[:, 4])) - \
        #     jnp.abs(jnp.cos(reference[1:, 4]) - jnp.cos(s[:, 4]))
            
        return sey_cost
    
    def update_waypoints(self, waypoints):
        self.waypoints = waypoints
        self.diff = self.waypoints[1:, 1:3] - self.waypoints[:-1, 1:3]
        self.waypoints_distances = np.linalg.norm(self.waypoints[1:, (1, 2)] - self.waypoints[:-1, (1, 2)], axis=1)
    
    @partial(jax.jit, static_argnums=(0,))
    def reward_fn_xy(self, state, reference, stage_idx, obstacles=None):
        """
        reward function for the state s with respect to the reference trajectory
        """
        cost = jnp.zeros((state.shape[0],))
        xy_cost = -jnp.linalg.norm(reference[1:, :2] - state[:, :2], ord=1, axis=1)
        cost += xy_cost * self.xy_cost[stage_idx]
        # jax.debug.print("xy_cost: {}", xy_cost)

        vel_cost = -jnp.linalg.norm(reference[1:, 2] - state[:, 3])
        # cost += vel_cost * 1.5
        # jax.debug.print('vel_cost: {}', vel_cost)
        yaw_cost = -jnp.abs(jnp.sin(reference[1:, 3]) - jnp.sin(state[:, 4])) - \
            jnp.abs(jnp.cos(reference[1:, 4]) - jnp.cos(state[:, 4]))
        cost += yaw_cost * self.yaw_cost[stage_idx] 
        # jax.debug.print('yaw_cost: {}', yaw_cost)
        if obstacles is not None:
            pos = state[:, :2]                         # (n_steps, 2)
            diffs = pos[:, None, :] - obstacles[None, :, :]  # (n_steps, n_obs, 2)
            sqd = diffs[..., 0]**2 + diffs[..., 1]**2         # (n_steps, n_obs)
            d2 = jnp.min(sqd, axis=1)                         # (n_steps,)
            obs_cost = -1.0 / (d2**2 + 1e-4)
            # obs_cost = jnp.clip(obs_cost, a_min=-30.0, a_max=0.0)
            cost += obs_cost * self.obs_cost[stage_idx]
            # jax.debug.print('obs_cost: {}', obs_cost)
            
        # return 20*xy_cost + 15*vel_cost + 1*yaw_cost
        return cost


    def calc_ref_trajectory_kinematic(self, state, cx, cy, cyaw, sp):
        """
        calc referent trajectory ref_traj in T steps: [x, y, v, yaw]
        using the current velocity, calc the T points along the reference path
        :param cx: Course X-Position
        :param cy: Course y-Position
        :param cyaw: Course Heading
        :param sp: speed profile
        :dl: distance step
        :pind: Setpoint Index
        :return: reference trajectory ref_traj, reference steering angle
        """

        n_state = 4
        n_steps = 10
        # Create placeholder Arrays for the reference trajectory for T steps
        ref_traj = np.zeros((n_state, n_steps + 1))
        ncourse = len(cx)

        # Find nearest index/setpoint from where the trajectories are calculated
        _, _, _, ind = nearest_point(np.array([state.x, state.y]), np.array([cx, cy]).T)

        # Load the initial parameters from the setpoint into the trajectory
        ref_traj[0, 0] = cx[ind]
        ref_traj[1, 0] = cy[ind]
        ref_traj[2, 0] = sp[ind]
        ref_traj[3, 0] = cyaw[ind]

        # based on current velocity, distance traveled on the ref line between time steps
        travel = abs(state.v) * self.config.DTK
        dind = travel / self.config.dlk
        ind_list = int(ind) + np.insert(
            np.cumsum(np.repeat(dind, self.config.TK)), 0, 0
        ).astype(int)
        ind_list[ind_list >= ncourse] -= ncourse
        ref_traj[0, :] = cx[ind_list]
        ref_traj[1, :] = cy[ind_list]
        ref_traj[2, :] = sp[ind_list]
        cyaw[cyaw - state.yaw > 4.5] = np.abs(
            cyaw[cyaw - state.yaw > 4.5] - (2 * np.pi)
        )
        cyaw[cyaw - state.yaw < -4.5] = np.abs(
            cyaw[cyaw - state.yaw < -4.5] + (2 * np.pi)
        )
        ref_traj[3, :] = cyaw[ind_list]

        return ref_traj
    
    
    def get_refernece_traj(self, state, target_speed=None, n_steps=10, vind=5, speed_factor=1.0, reverse=False):
        '''
        Get the reference trajectory for the given predicted speeds and distances.
        Args:
            state (numpy.ndarray): current state of the vehicle
            target_speed (float): target speed for the vehicle
            n_steps (int): number of steps to predict
            vind (int): index of the speed in the waypoints
            speed_factor (float): factor to scale the speed
        Returns:
            reference (numpy.ndarray): reference trajectory
            ind (int): index of the closest waypoint to start the reference trajectory
        '''
        p, dist, _, _, ind = nearest_point(np.array([state[0], state[1]]), 
                                           self.waypoints[:, (1, 2)].copy())
        
        if target_speed is None:
            # speed = self.waypoints[ind, vind] * speed_factor
            # speed = np.minimum(self.waypoints[ind, vind] * speed_factor, 20.)
            speed = state[3]
        else:
            speed = target_speed
        
        # if ind < self.waypoints.shape[0] - self.n_steps:
        #     speeds = self.waypoints[ind:ind+self.n_steps, vind]
        # else:

        speeds = np.ones(n_steps) * speed

        # reference speed, closest pts dist and ind to current position
        if not reverse:
            reference = get_reference_trajectory(speeds, dist, ind, 
                                                self.waypoints.copy(), int(n_steps),
                                                self.waypoints_distances.copy(), DT=self.DT)
        else:
            reference = get_reference_trajectory_backward(speeds, dist, ind,
                                                    self.waypoints.copy(), int(n_steps),
                                                    self.waypoints_distances.copy(), DT=self.DT)
        orientation = state[4]
        # we care about diff between the reference and the current state so calibrate using the diff that is too far apart
        reference[3, :][reference[3, :] - orientation > 5] = np.abs(
            reference[3, :][reference[3, :] - orientation > 5] - (2 * np.pi))
        reference[3, :][reference[3, :] - orientation < -5] = np.abs(
            reference[3, :][reference[3, :] - orientation < -5] + (2 * np.pi))
        
        # reference[2] = np.where(reference[2] - speed > 5.0, speed + 5.0, reference[2])
        self.reference = reference.T
        # print('reference:', reference)
        return reference.T, ind

    
@njit(cache=True)
def nearest_point(point, trajectory):
    """
    Return the nearest point along the given piecewise linear trajectory.
    Args:
        point (numpy.ndarray, (2, )): (x, y) of current pose
        trajectory (numpy.ndarray, (N, 2)): array of (x, y) trajectory waypoints
            NOTE: points in trajectory must be unique. If they are not unique, a divide by 0 error will destroy the world
    Returns:
        nearest_point (numpy.ndarray, (2, )): nearest point on the trajectory to the point
        nearest_dist (float): distance to the nearest point
        t (float): nearest point's location as a segment between 0 and 1 on the vector formed by the closest two points on the trajectory. (p_i---*-------p_i+1)
        i (int): index of nearest point in the array of trajectory waypoints
    """
    diffs = trajectory[1:, :] - trajectory[:-1, :]
    l2s = diffs[:, 0] ** 2 + diffs[:, 1] ** 2
    dots = np.empty((trajectory.shape[0] - 1,))
    for i in range(dots.shape[0]):
        dots[i] = np.dot((point - trajectory[i, :]), diffs[i, :])
    t = dots / (l2s + 1e-8)
    t[t < 0.0] = 0.0
    t[t > 1.0] = 1.0
    projections = trajectory[:-1, :] + (t * diffs.T).T
    dists = np.empty((projections.shape[0],))
    for i in range(dists.shape[0]):
        temp = point - projections[i]
        dists[i] = np.sqrt(np.sum(temp * temp))
    min_dist_segment = np.argmin(dists)
    dist_from_segment_start = np.linalg.norm(diffs[min_dist_segment] * t[min_dist_segment])
    return projections[min_dist_segment], dist_from_segment_start, dists[min_dist_segment], t[
        min_dist_segment], min_dist_segment

# @njit(cache=True)
def get_reference_trajectory(predicted_speeds, dist_from_segment_start, idx, 
                             waypoints, n_steps, waypoints_distances, DT):
    '''
    Get the reference trajectory for the given predicted speeds and distances.
    Args:
        predicted_speeds (numpy.ndarray): predicted speeds for each step
        dist_from_segment_start (float): distance from the start of the segment
        idx (int): index of the current segment
        waypoints (numpy.ndarray): waypoints of the track
        n_steps (int): number of steps to predict
        waypoints_distances (numpy.ndarray): distances between waypoints
        DT (float): time step size
    Returns:
        reference (numpy.ndarray): reference trajectory for the given predicted speeds and distances
    '''
    # distance from the start of the segment for every interval starting from closest point to current position
    # st = init_pos + sum(v * dtime_step)
    s_relative = np.zeros((n_steps + 1,))
    s_relative[0] = dist_from_segment_start
    s_relative[1:] = predicted_speeds * DT
    s_relative = np.cumsum(s_relative)
    # print(f's_relative: {s_relative}')

    # idx += 5
    # np.roll(..., -idx) shifts the array left so that the segment starting at your "idx" becomes the start
    # waypoints_distances_relative is cumulative distance from the start of the "idx"
    waypoints_distances_relative = np.cumsum(np.roll(waypoints_distances, -idx))
    # how much indices to travel using s_relative starting from "idx"
    index_relative = np.int_(np.ones((n_steps + 1,)))
    for i in range(n_steps + 1):
        index_relative[i] = (waypoints_distances_relative <= s_relative[i]).sum()
    index_absolute = np.clip(idx + index_relative, 0, waypoints.shape[0] - 2)
    index_relative = np.clip(index_relative, 0, waypoints.shape[0] - 2)
    # waypoints_distances_relative[index_relative] - waypoints_distances[index_absolute] is the distance to the start of the segment
    # This expression gives the cumulative distance from the current position to the start of the segment you're currently in at each timestep.
    segment_part = s_relative - (
            waypoints_distances_relative[index_relative] - waypoints_distances[index_absolute])

    t = (segment_part / waypoints_distances[index_absolute])
    # print(np.all(np.logical_and((t < 1.0), (t > 0.0))))

    position_diffs = (waypoints[np.clip(index_absolute + 1, 0, waypoints.shape[0] - 2)][:, (1, 2)] -
                        waypoints[index_absolute][:, (1, 2)])
    position_diff_s = (waypoints[np.clip(index_absolute + 1, 0, waypoints.shape[0] - 2)][:, 0] -
                        waypoints[index_absolute][:, 0])
    orientation_diffs = (waypoints[np.clip(index_absolute + 1, 0, waypoints.shape[0] - 2)][:, 3] -
                            waypoints[index_absolute][:, 3])
    speed_diffs = (waypoints[np.clip(index_absolute + 1, 0, waypoints.shape[0] - 2)][:, 5] -
                    waypoints[index_absolute][:, 5])

    interpolated_positions = waypoints[index_absolute][:, (1, 2)] + (t * position_diffs.T).T
    interpolated_s = waypoints[index_absolute][:, 0] + (t * position_diff_s)
    interpolated_s[np.where(interpolated_s > waypoints[-1, 0])] -= waypoints[-1, 0]
    interpolated_orientations = waypoints[index_absolute][:, 3] + (t * orientation_diffs)
    interpolated_orientations = (interpolated_orientations + np.pi) % (2 * np.pi) - np.pi
    interpolated_speeds = waypoints[index_absolute][:, 5] + (t * speed_diffs)
    
    reference = np.array([
        # Sort reference trajectory so the order of reference match the order of the states
        interpolated_positions[:, 0],
        interpolated_positions[:, 1],
        interpolated_speeds,
        interpolated_orientations,
        # Fill zeros to the rest so number of references mathc number of states (x[k] - ref[k])
        interpolated_s,
        np.zeros(len(interpolated_speeds)),
        np.zeros(len(interpolated_speeds))
    ])
    return reference

#@ TODO: modify this function to make a backward reference trajectory
# @njit(cache=True)
def get_reference_trajectory_backward(predicted_speeds,
                                      dist_from_segment_start,
                                      idx,
                                      waypoints,
                                      n_steps,
                                      waypoints_distances,
                                      DT):
    """
    Supports both forward and reverse predicted_speeds.
    Returns reference = [x, y, v_ref, yaw_ref, s_abs, 0, 0] shape (7, n_steps+1).
    """
    car_length = 0.32 * 0.2 # to trail behind a little bit
    # assert predicted_speeds <= 0
    # build signed arc-length increments
    delta_s = np.ones((n_steps ,)) * predicted_speeds * DT
    s_relative = np.cumsum(np.concatenate(([0.0], delta_s)))
    s_relative = np.where(s_relative < 0, s_relative, 0.0)
    # print(f's_relative: {s_relative}')
    # np.roll(..., -idx) shifts the array left so that the segment starting at your "idx" becomes the start
    # waypoints_distances_relative is cumulative distance from the start of the "idx"
    N = waypoints.shape[0] - 1
    seg_inds = np.mod((idx - 1 - np.arange(N)), N)
    # 2) pick out those distances in that order
    d_rev = waypoints_distances[seg_inds]      # shape (N,)
    # 3) cumulative sum and negate
    waypoints_distances_relative = -np.cumsum(d_rev)  
    # how much indices to travel using s_relative starting from "idx"
    # now s_relative is negative
    index_relative = np.int_(np.ones((n_steps + 1,)))
    for i in range(n_steps + 1):
        index_relative[i] = (-waypoints_distances_relative <= (-s_relative[i])).sum()
    index_absolute = np.clip(idx + index_relative, 0, waypoints.shape[0] - 2)
    index_relative = np.clip(index_relative, 0, waypoints.shape[0] - 2)
    # waypoints_distances_relative[index_relative] - waypoints_distances[index_absolute] is the distance to the start of the segment
    # This expression gives the cumulative distance from the current position to the start of the segment you're currently in at each timestep.
    segment_part = s_relative - (
            waypoints_distances_relative[index_relative] + waypoints_distances[index_absolute])

    t = (segment_part / -waypoints_distances[index_absolute])
    position_diffs = (waypoints[np.clip(index_absolute + 1, 0, waypoints.shape[0] - 2)][:, (1, 2)] -
                        waypoints[index_absolute][:, (1, 2)])
    position_diff_s = (waypoints[np.clip(index_absolute + 1, 0, waypoints.shape[0] - 2)][:, 0] -
                        waypoints[index_absolute][:, 0])
    orientation_diffs = (waypoints[np.clip(index_absolute + 1, 0, waypoints.shape[0] - 2)][:, 3] -
                            waypoints[index_absolute][:, 3])
    speed_diffs = (waypoints[np.clip(index_absolute + 1, 0, waypoints.shape[0] - 2)][:, 5] -
                    waypoints[index_absolute][:, 5])

    interpolated_positions = waypoints[index_absolute][:, (1, 2)] + (t * position_diffs.T).T
    interpolated_s = waypoints[index_absolute][:, 0] + (t * position_diff_s)
    interpolated_s[np.where(interpolated_s > waypoints[-1, 0])] -= waypoints[-1, 0]
    interpolated_orientations = waypoints[index_absolute][:, 3] + (t * orientation_diffs)
    interpolated_orientations = (interpolated_orientations + np.pi) % (2 * np.pi) - np.pi
    interpolated_speeds = waypoints[index_absolute][:, 5] + (t * speed_diffs)
    
    reference = np.array([
        # Sort reference trajectory so the order of reference match the order of the states
        interpolated_positions[:, 0],
        interpolated_positions[:, 1],
        interpolated_speeds,
        interpolated_orientations,
        # Fill zeros to the rest so number of references mathc number of states (x[k] - ref[k])
        interpolated_s,
        np.zeros(len(interpolated_speeds)),
        np.zeros(len(interpolated_speeds))
    ])
    return reference

