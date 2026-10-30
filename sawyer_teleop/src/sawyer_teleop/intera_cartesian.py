"""Native Intera Cartesian goals. No ROS imports or device access until discovery.

The B loop owns send/stop. Only FK runs on a single daemon worker; its result
cannot send motion and must be revalidated by BridgeCore before submission.
"""
from concurrent.futures import Future
import math
import threading

from .protocol import JOINTS, pose, vector

MODE = 'intera_cartesian'
SEND_HZ_CHOICES = (10, 20, 50, 100)
DEFAULT_OPTIONS = dict(send_hz=10.0, max_linear_speed=0.6, max_linear_accel=0.6,
                       max_rotational_speed=1.57, max_rotational_accel=1.57,
                       max_joint_speed_ratio=1.0)


def cartesian_options(values=None):
    values = {} if values is None else values
    if not isinstance(values, dict) or set(values)-set(DEFAULT_OPTIONS):
        raise ValueError('Invalid Intera Cartesian options')
    result = dict(DEFAULT_OPTIONS, **values)
    for key, value in result.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid Intera Cartesian option: '+key)
    if not 1 <= result['send_hz'] <= 100 or not .05 <= result['max_joint_speed_ratio'] <= 1:
        raise ValueError('Invalid Intera Cartesian rate or joint speed ratio')
    return result


def validate_send_hz(value):
    if type(value) not in (int, float) or value not in SEND_HZ_CHOICES:
        raise ValueError('intera_cartesian_invalid_send_hz')
    return float(value)


class InteraCartesian:
    def __init__(self, trajectory, waypoint, tip_frame, options=None):
        self.trajectory, self.waypoint, self.tip_frame = trajectory, waypoint, tip_frame
        self.options = cartesian_options(options)
        self.future = None
        self.command_sent = False
        self.stopping = False

    @property
    def busy(self):
        return self.future is not None and not self.future.done()

    def prepare(self, q):
        if self.busy:
            raise RuntimeError('intera_cartesian_fk_busy')
        q = vector(q, 7)
        future = self.future = Future()
        def work():
            try:
                # Use keywords: this SDK's third argument is perform_fk, not names.
                self.waypoint.set_joint_angles(joint_angles=q,
                    active_endpoint=self.tip_frame, perform_fk=True)
                msg = self.waypoint.to_msg()
                p, r = msg.pose.pose.position, msg.pose.pose.orientation
                pose({'p': [p.x, p.y, p.z], 'q': [r.x, r.y, r.z, r.w]})
                # The installed SDK treats a zero component sum as an unset pose,
                # even for valid poses. Reject explicitly rather than let send()
                # unexpectedly run another blocking FK on the control thread.
                if not sum((p.x, p.y, p.z)) or not sum((r.w, r.x, r.y, r.z)):
                    raise ValueError('intera_cartesian_sdk_zero_sum_pose')
                if msg.pose.header.frame_id not in ('', 'base', '/base'):
                    raise ValueError('Unexpected FK frame')
                msg.pose.header.frame_id = 'base'
                if msg.active_endpoint != self.tip_frame or vector(msg.joint_positions, 7) != q:
                    raise ValueError('Unexpected FK joints or endpoint')
                future.set_result(msg)
            except Exception as exc:
                future.set_exception(exc)
        threading.Thread(target=work, name='intera-cartesian-fk', daemon=True).start()
        return future

    def prepare_pose(self, target, bias):
        # Explicit pose: SDK must never fall back to an FK service call.
        from geometry_msgs.msg import PoseStamped
        target, bias = pose(target), vector(bias, 7)
        if not sum(target['p']) or not sum(target['q']):
            raise ValueError('direct_pose_sdk_zero_sum_pose')
        p = PoseStamped()
        p.header.frame_id = 'base'
        p.pose.position.x, p.pose.position.y, p.pose.position.z = target['p']
        p.pose.orientation.x, p.pose.orientation.y, p.pose.orientation.z, p.pose.orientation.w = target['q']
        self.waypoint.set_cartesian_pose(pose=p, active_endpoint=self.tip_frame, joint_angles=bias)
        return self.waypoint.to_msg()

    def send(self, waypoint):
        self.trajectory.clear_waypoints()
        self.trajectory.append_waypoint(waypoint)
        # A valid, already-computed pose prevents SDK _check_options from doing
        # a blocking FK service call in the B control thread.
        self.command_sent = True  # Even an ambiguous send failure requires STOP.
        self.stopping = False
        if not self.trajectory.send_trajectory(wait_for_result=False):
            raise RuntimeError('intera_cartesian_send_failed')

    def stop(self):
        self.command_sent = True
        self.stopping = True
        if not self.trajectory.stop_trajectory(wait_for_result=False):
            raise RuntimeError('intera_cartesian_stop_send_failed')

    def status(self):
        if not self.command_sent:
            return 'idle'
        state = self.trajectory.get_state()
        if state in (0, 1, 6, 7):  # pending, active, preempting, recalling
            return 'running'
        if state != 3:
            raise RuntimeError('intera_cartesian_action_state_'+str(state))
        # Only read a terminal result; never wait for an active motion to finish.
        result = self.trajectory.wait_for_result(timeout=.001)
        if result is None or not result.result:
            raise RuntimeError('intera_cartesian_action_failed:'+str(getattr(result, 'errorId', 'missing_result')))
        return 'stopped' if self.stopping else 'succeeded'


def discover_cartesian(rospy, tip_frame):
    """Startup-only availability check. Never sends START, STOP or enable."""
    try:
        import rosgraph
        from intera_motion_interface import MotionTrajectory, MotionWaypoint, MotionWaypointOptions
        from intera_motion_msgs.msg import TrajectoryOptions
        from intera_interface import Limb
        master = rosgraph.Master(rospy.get_name())
        publishers, subscribers, _ = [dict(rows) for rows in master.getSystemState()]
        types = dict(master.getTopicTypes())
        owners = []
        for suffix, kind in (('goal', 'intera_motion_msgs/MotionCommandActionGoal'),
                             ('cancel', 'actionlib_msgs/GoalID'),
                             ('status', 'actionlib_msgs/GoalStatusArray'),
                             ('feedback', 'intera_motion_msgs/MotionCommandActionFeedback'),
                             ('result', 'intera_motion_msgs/MotionCommandActionResult')):
            topic = '/motion/motion_command/'+suffix
            nodes = (subscribers if suffix in ('goal', 'cancel') else publishers).get(topic, [])
            if not nodes or types.get(topic) != kind:
                raise RuntimeError('Native Motion Controller service is unavailable')
            owners.append(set(nodes))
        if not set.intersection(*owners):
            raise RuntimeError('Native Motion Controller interfaces have no common server')
        options = cartesian_options(rospy.get_param('/sawyer_teleop/intera_cartesian', {}))
        limb = Limb()
        if limb.joint_names() != JOINTS:
            raise RuntimeError('Unexpected Intera joint order')
        settings = TrajectoryOptions()
        settings.interpolation_type = TrajectoryOptions.CARTESIAN
        settings.nso_start_offset_allowed = False
        trajectory = MotionTrajectory(trajectory_options=settings, limb=limb)
        if rospy.is_shutdown():
            raise RuntimeError('Native Motion Controller connection failed')
        waypoint_options = MotionWaypointOptions(**{k: v for k, v in options.items() if k != 'send_hz'})
        waypoint = MotionWaypoint(options=waypoint_options.to_msg(), limb=limb)
        executor = InteraCartesian(trajectory, waypoint, tip_frame, options)
        return executor, dict(available=True, schema=1, experimental=True, options=options,
                              send_hz_choices=list(SEND_HZ_CHOICES), direct_pose_50hz=True,
                              command_semantics='cartesian_pose_with_joint_nullspace_bias')
    except Exception as exc:
        return None, dict(available=False, reason='Intera Cartesian unavailable: '+str(exc))
