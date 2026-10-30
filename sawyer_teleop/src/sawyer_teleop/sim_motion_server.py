"""Gazebo-only stand-in for the closed-source Intera Motion Controller.

Serves /motion/motion_command (intera_motion_msgs/MotionCommand) so the official
SDK path (set_cartesian_pose / send_trajectory / stop_trajectory) runs in
simulation. The planner is ours: bounded-acceleration tracking toward each
waypoint, LocalIK warm-started from the previous command, and TRAJECTORY_MODE
output. It never claims Intera's blending, collision checks or feel.

A newer goal replaces the target but keeps the commanded pose and velocity, so
streamed single-waypoint goals continue without restarting from rest.
Refuses to start unless /use_sim_time is true and /clock is published.
"""
import math
import threading
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation

from .protocol import JOINTS

NODE = 'sawyer_sim_motion_server'
ACTION = '/motion/motion_command'
CONTROL_HZ = 100.
# Same defaults as intera_motion_interface.MotionWaypointOptions.
DEFAULT_WAYPOINT = dict(max_linear_speed=.6, max_linear_accel=.6, max_rotational_speed=1.57,
                        max_rotational_accel=1.57, max_joint_speed_ratio=.7)
DEFAULT_JOINT_ACCEL = [3.5, 2.5, 5., 5., 5., 5., 5.]  # SDK 'medium' preset
POSITION_TOLERANCE, ROTATION_TOLERANCE = 1e-4, 1e-3
SETTLED_SPEED = 1e-3
FOLLOW_LIMIT_RAD = .25
FEEDBACK_TIMEOUT_S = .2
# Wall-time guard only. A normal step takes ~5 ms; with Gazebo on the same CPU
# 8 ms aborted reachable goals (a 1 s retry solved them). Unreachable targets
# still return early when LocalIK stops improving.
IK_BUDGET_S = .05


class MotionError(Exception):
    """Carries an official MotionCommandResult errorId."""


def joint_speed_limits(urdf):
    limits = {}
    for joint in ET.fromstring(urdf).findall('joint'):
        limit = joint.find('limit')
        if joint.get('name') in JOINTS and limit is not None:
            limits[joint.get('name')] = float(limit.get('velocity'))
    values = [limits.get(name) for name in JOINTS]
    if any(v is None or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('URDF lacks joint velocity limits')
    return values


def approach(error, velocity, vmax, amax, dt):
    """One bounded-acceleration step toward zero error; returns (step, velocity).

    The desired speed is the discrete-time braking speed that stops on the target
    while decelerating by amax per tick; the velocity change per tick is capped,
    so preemption keeps the commanded velocity continuous.
    """
    distance = float(np.linalg.norm(error))
    desired = np.zeros_like(error)
    if distance > 1e-12:
        brake = amax*dt*(math.sqrt(.25+2*distance/(amax*dt*dt))-.5)
        desired = error/distance*min(vmax, brake, distance/dt)
    change = desired-velocity
    norm = float(np.linalg.norm(change))
    if norm > amax*dt:
        change *= amax*dt/norm
    velocity = velocity+change
    step = velocity*dt
    if distance > 1e-12 and float(np.linalg.norm(step)) > distance:
        step, velocity = error.copy(), error/dt
    return step, velocity


def parse_trajectory(trajectory, tip_frame):
    """Convert a Trajectory message into plain waypoints or raise MotionError."""
    options = trajectory.trajectory_options
    kind = options.interpolation_type or 'JOINT'
    if kind not in ('CARTESIAN', 'JOINT'):
        raise MotionError('INVALID_TRAJECTORY_MESSAGE')
    if options.interaction_control:
        raise MotionError('INVALID_TRAJECTORY_MESSAGE')  # not simulated
    if trajectory.joint_names and list(trajectory.joint_names) != JOINTS:
        raise MotionError('INVALID_TRAJECTORY_MESSAGE')
    if not trajectory.waypoints:
        raise MotionError('INVALID_TRAJECTORY_MESSAGE')
    result = []
    for waypoint in trajectory.waypoints:
        if waypoint.active_endpoint not in ('', tip_frame):
            raise MotionError('ENDPOINT_DOES_NOT_EXIST')
        item = dict(kind=kind, joints=None, pose=None)
        o = waypoint.options
        for key, default in DEFAULT_WAYPOINT.items():
            value = getattr(o, key)
            item[key] = value if math.isfinite(value) and value > 0 else default
        item['max_joint_speed_ratio'] = min(1., item['max_joint_speed_ratio'])
        accel = list(o.max_joint_accel)
        item['max_joint_accel'] = (accel if len(accel) == 7 and all(math.isfinite(a) and a > 0 for a in accel)
                                   else DEFAULT_JOINT_ACCEL)
        if len(waypoint.joint_positions) == 7:
            item['joints'] = [float(x) for x in waypoint.joint_positions]
        elif waypoint.joint_positions:
            raise MotionError('INVALID_TRAJECTORY_MESSAGE')
        if kind == 'CARTESIAN':
            if waypoint.pose.header.frame_id.lstrip('/') not in ('', 'base'):
                raise MotionError('INVALID_TRAJECTORY_MESSAGE')
            p, q = waypoint.pose.pose.position, waypoint.pose.pose.orientation
            quaternion = np.array([q.x, q.y, q.z, q.w])
            position = np.array([p.x, p.y, p.z])
            norm = float(np.linalg.norm(quaternion))
            if not np.all(np.isfinite(position)) or not math.isfinite(norm) or abs(norm-1) > 1e-3:
                raise MotionError('INVALID_TRAJECTORY_MESSAGE')
            item['pose'] = (position, Rotation.from_quat(quaternion/norm))
        elif item['joints'] is None:
            raise MotionError('INVALID_TRAJECTORY_MESSAGE')
        result.append(item)
    return result


class SimMotionPlanner:
    """Pure planner state; the ROS layer owns timing, locks and action status."""

    def __init__(self, ik, speed_limits):
        self.ik = ik
        self.speed_limits = np.array(speed_limits, dtype=float)
        self.q = self.dq = None
        self.waypoints, self.index, self.stopping = [], 0, False
        self.v = self.w = np.zeros(3)

    @property
    def moving(self):
        return bool(self.waypoints) or self.stopping

    def sync(self, q):
        """Adopt measured joints; only valid while not moving."""
        self.q, self.dq = np.array(q, dtype=float), np.zeros(7)
        self.v, self.w = np.zeros(3), np.zeros(3)
        self._update_pose()

    def _update_pose(self):
        transform = self.ik.forward(self.q.tolist())
        self.p, self.r = transform[:3, 3].copy(), Rotation.from_matrix(transform[:3, :3])

    def start(self, waypoints):
        for item in waypoints:
            joints = item['joints']
            if item['kind'] == 'JOINT' and (np.any(np.array(joints) < self.ik.lower)
                                            or np.any(np.array(joints) > self.ik.upper)):
                raise MotionError('INVALID_TRAJECTORY_MESSAGE')
        self.waypoints, self.index, self.stopping = list(waypoints), 0, False

    def stop(self):
        self.waypoints, self.stopping = [], True

    def tick(self, dt):
        """Advance one control period. Returns (q, dq, ddq, event).

        event is None while moving, 'reached' after the final waypoint and
        'stopped' after a stop request decelerates to rest.
        """
        previous = self.dq.copy()
        if self.stopping:
            event = self._stop_step(dt)
        else:
            event = self._waypoint_step(dt)
        return self.q.tolist(), self.dq.tolist(), ((self.dq-previous)/dt).tolist(), event

    def _waypoint_step(self, dt):
        target = self.waypoints[self.index]
        if target['kind'] == 'CARTESIAN':
            settled = self._cartesian_step(target, dt)
        else:
            settled = self._joint_step(target, dt)
        if not settled:
            return None
        self.index += 1
        if self.index < len(self.waypoints):
            return None
        self.waypoints = []
        return 'reached'

    def _stop_step(self, dt):
        if np.linalg.norm(self.v) > 0 or np.linalg.norm(self.w) > 0:
            # Decelerate along the current Cartesian velocity at default limits.
            target = dict(max_linear_speed=np.linalg.norm(self.v), max_rotational_speed=np.linalg.norm(self.w),
                          max_linear_accel=DEFAULT_WAYPOINT['max_linear_accel'],
                          max_rotational_accel=DEFAULT_WAYPOINT['max_rotational_accel'],
                          max_joint_speed_ratio=1.)
            self._move_cartesian(target, np.zeros(3), np.zeros(3), dt)
        else:
            dq = self.dq
            amax = np.array(DEFAULT_JOINT_ACCEL)
            dq = np.sign(dq)*np.maximum(0., np.abs(dq)-amax*dt)
            self.q, self.dq = self.q+dq*dt, dq
            self._update_pose()
        if (np.linalg.norm(self.v) < SETTLED_SPEED and np.linalg.norm(self.w) < SETTLED_SPEED
                and np.max(np.abs(self.dq)) < SETTLED_SPEED):
            self.v, self.w, self.dq = np.zeros(3), np.zeros(3), np.zeros(7)
            self.stopping = False
            self._update_pose()
            return 'stopped'
        return None

    def _cartesian_step(self, target, dt):
        goal_p, goal_r = target['pose']
        error_p = goal_p-self.p
        error_r = (goal_r*self.r.inv()).as_rotvec()
        self._move_cartesian(target, error_p, error_r, dt)
        return (np.linalg.norm(goal_p-self.p) < POSITION_TOLERANCE
                and np.linalg.norm((goal_r*self.r.inv()).as_rotvec()) < ROTATION_TOLERANCE
                and np.linalg.norm(self.v) < SETTLED_SPEED and np.linalg.norm(self.w) < SETTLED_SPEED)

    def _move_cartesian(self, target, error_p, error_r, dt):
        step_p, v = approach(error_p, self.v, target['max_linear_speed'], target['max_linear_accel'], dt)
        step_r, w = approach(error_r, self.w, target['max_rotational_speed'], target['max_rotational_accel'], dt)
        limit = self.speed_limits*target['max_joint_speed_ratio']
        for attempt in range(2):
            p = self.p+step_p
            r = Rotation.from_rotvec(step_r)*self.r
            try:
                q = self.ik.solve({'p': p.tolist(), 'q': r.as_quat().tolist()}, self.q.tolist(),
                                  timeout=IK_BUDGET_S)
            except ValueError:
                q = None
            if q is None:
                raise MotionError('CARTESIAN_INTERPOLATION_FAILED')
            q = np.array(q)
            ratio = float(np.max(np.abs(q-self.q)/(limit*dt)))
            if ratio <= 1.0001 or attempt:
                break
            # Joint speed limit: slow the Cartesian motion along the same path.
            step_p, step_r, v, w = step_p/ratio, step_r/ratio, v/ratio, w/ratio
        self.dq = (q-self.q)/dt
        self.q, self.p, self.r, self.v, self.w = q, p, r, v, w

    def _joint_step(self, target, dt):
        limit = self.speed_limits*target['max_joint_speed_ratio']
        accel = np.array(target['max_joint_accel'])
        # Normalized coordinates give a straight joint-space path under per-joint limits.
        error = (np.array(target['joints'])-self.q)/limit
        step, velocity = approach(error, self.dq/limit, 1., float(np.min(accel/limit)), dt)
        self.dq = velocity*limit
        self.q = self.q+step*limit
        self.v, self.w = np.zeros(3), np.zeros(3)
        self._update_pose()
        return (float(np.max(np.abs(np.array(target['joints'])-self.q))) < 1e-4
                and float(np.max(np.abs(self.dq))) < SETTLED_SPEED)


class MotionServer:
    """actionlib wrapper. Lock order everywhere: action server locks, then self.lock."""

    def __init__(self, rospy, tip_frame='right_hand'):
        import actionlib
        from intera_core_msgs.msg import JointCommand
        from intera_motion_msgs.msg import MotionCommandAction, MotionCommandFeedback, MotionCommandResult
        from sensor_msgs.msg import JointState
        from .kinematics_model import read_kinematics_model
        from .local_ik import LocalIK
        self.ros, self.tip_frame = rospy, tip_frame
        if rospy.get_param('/use_sim_time', False) is not True:
            raise RuntimeError('Simulated Motion Controller requires /use_sim_time=true')
        urdf = rospy.get_param('/robot_description')
        model = read_kinematics_model(urdf, tip_frame)
        if model.get('available') is not True:
            raise RuntimeError(model.get('reason'))
        self.planner = SimMotionPlanner(LocalIK(model), joint_speed_limits(urdf))
        self.JointCommand, self.Feedback, self.Result = JointCommand, MotionCommandFeedback, MotionCommandResult
        self.lock = threading.Lock()
        self.measured = None  # (q, stamp)
        self.mode = None  # 'start' | 'stop' while a goal is active
        self.label = ''
        self.last_tick = None
        self.last_command = None  # sim time of the last published command
        self.publisher = rospy.Publisher('/robot/limb/right/joint_command', JointCommand,
                                         queue_size=1, tcp_nodelay=True)
        self.subscriber = rospy.Subscriber('/robot/joint_states', JointState, self._joints,
                                           queue_size=1, tcp_nodelay=True)
        self.server = actionlib.SimpleActionServer(ACTION, MotionCommandAction, auto_start=False)
        self.server.register_goal_callback(self._goal)
        self.server.register_preempt_callback(self._preempt)
        self.server.start()
        rospy.set_param(ACTION+'/simulated', True)
        self.timer = rospy.Timer(rospy.Duration(1./CONTROL_HZ), self._tick)

    def _locks(self):
        return _Locks(self.server.action_server.lock, self.server.lock, self.lock)

    def _joints(self, msg):
        try:
            positions = dict(zip(msg.name, msg.position))
            q = [float(positions[name]) for name in JOINTS]
        except (KeyError, TypeError, ValueError):
            return
        with self.lock:
            self.measured = (q, msg.header.stamp.to_sec())

    def _fresh_q(self):
        if self.measured is None or self.ros.Time.now().to_sec()-self.measured[1] > FEEDBACK_TIMEOUT_S:
            raise MotionError('CONTROLLER_NOT_FOLLOWING')
        return self.measured[0]

    def _finish(self, success, error_id='', preempted=False, last=None):
        result = self.Result()
        result.result, result.errorId = success, error_id
        # The planner index counts waypoints reached by the current start goal.
        index = self.planner.index-1 if last is None else last
        result.last_successful_waypoint = index if index >= 0 else result.HAVE_NOT_REACHED_FIRST_WAYPOINT
        self.mode = None
        if preempted:
            self.server.set_preempted(result)
        elif success:
            self.server.set_succeeded(result)
        else:
            self.ros.logwarn('Simulated Motion Controller aborted goal: %s', error_id)
            self.server.set_aborted(result, error_id)

    def _goal(self):
        with self.lock:
            goal = self.server.accept_new_goal()
            try:
                if goal.command == goal.MOTION_STOP:
                    self.mode = 'stop'
                    if self.planner.moving:
                        self.planner.stop()
                    else:
                        self._finish(True, last=-1)
                    return
                if goal.command not in (goal.MOTION_START, goal.MOTION_GENERATE):
                    raise MotionError('INVALID_TRAJECTORY_MESSAGE')
                waypoints = parse_trajectory(goal.trajectory, self.tip_frame)
                if goal.command == goal.MOTION_GENERATE:
                    self.mode = 'start'
                    self._finish(True, last=len(waypoints)-1)  # validated only; no motion
                    return
                if not self.planner.moving:
                    q = self._fresh_q()
                    # Streamed goals often arrive just after the previous one settled.
                    # Continue from the recent command; measured joints lag it in Gazebo.
                    recent = (self.last_command is not None and self.planner.q is not None
                              and self.ros.Time.now().to_sec()-self.last_command <= FEEDBACK_TIMEOUT_S
                              and float(np.max(np.abs(np.array(q)-self.planner.q))) <= FOLLOW_LIMIT_RAD)
                    if not recent:
                        self.planner.sync(q)
                self.planner.start(waypoints)
                self.label = goal.trajectory.label
                self.mode = 'start'
            except MotionError as exc:
                self.mode = 'start'
                self._finish(False, str(exc), last=-1)

    def _preempt(self):
        with self.lock:
            # A newer goal is handled by _goal; only a bare cancel decelerates here.
            if not self.server.is_new_goal_available() and self.planner.moving:
                self.planner.stop()
                self.mode = 'cancel'

    def _tick(self, event):
        with self._locks():
            now = self.ros.Time.now().to_sec()
            previous, self.last_tick = self.last_tick, now
            if not self.planner.moving or previous is None or now <= previous:
                return
            dt = min(now-previous, 2./CONTROL_HZ)
            try:
                q_meas = self._fresh_q()
                if float(np.max(np.abs(np.array(q_meas)-self.planner.q))) > FOLLOW_LIMIT_RAD:
                    raise MotionError('CONTROLLER_NOT_FOLLOWING')
                q, dq, ddq, outcome = self.planner.tick(dt)
            except MotionError as exc:
                self.planner.waypoints, self.planner.stopping = [], False
                self._publish(self.planner.q.tolist(), [0.]*7, [0.]*7)
                if self.server.is_active():
                    self._finish(False, str(exc))
                return
            self._publish(q, dq, ddq)
            if not self.server.is_active():
                return
            feedback = self.Feedback()
            status = feedback.status
            status.header.stamp = self.ros.Time.now()
            status.current_trajectory = self.label
            status.current_waypoint = self.planner.index
            status.motion_status = status.MOTION_STOPPING if self.mode in ('stop', 'cancel') else status.MOTION_RUNNING
            if outcome is None:
                self.server.publish_feedback(feedback)
            elif self.mode == 'cancel':
                self._finish(False, preempted=True)
            else:
                self._finish(True)

    def _publish(self, q, dq, ddq):
        msg = self.JointCommand()
        msg.header.stamp = self.ros.Time.now()
        msg.mode, msg.names = msg.TRAJECTORY_MODE, list(JOINTS)
        msg.position, msg.velocity, msg.acceleration = q, dq, ddq
        self.publisher.publish(msg)
        self.last_command = msg.header.stamp.to_sec()


class _Locks:
    def __init__(self, *locks):
        self.locks = locks

    def __enter__(self):
        for lock in self.locks:
            lock.acquire()

    def __exit__(self, *exc):
        for lock in reversed(self.locks):
            lock.release()


def main():
    import rospy
    rospy.init_node(NODE)
    rospy.wait_for_message('/clock', __import__('rosgraph_msgs.msg', fromlist=['Clock']).Clock, timeout=30)
    MotionServer(rospy, rospy.get_param('/robot/limb/right/tip_name', 'right_hand'))
    rospy.loginfo('Simulated Intera Motion Controller ready on %s (Gazebo only)', ACTION)
    rospy.spin()


if __name__ == '__main__':
    main()
