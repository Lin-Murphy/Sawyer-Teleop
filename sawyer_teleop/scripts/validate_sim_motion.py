"""Gazebo-only check of the simulated /motion/motion_command through the official SDK.

Runs inside the simulation container after gazebo-windows-entrypoint.sh is up.
Refuses to run without /use_sim_time and the project simulated-server marker.
Prints one JSON report; it is software/simulation evidence only, never real-robot proof.
"""
import json
import math
import sys
import threading
import time

import rospy
from actionlib_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from intera_core_msgs.msg import JointCommand
from intera_interface import Limb
from intera_motion_interface import MotionTrajectory, MotionWaypoint, MotionWaypointOptions
from intera_motion_msgs.msg import TrajectoryOptions


def pose_msg(p, q):
    msg = PoseStamped()
    msg.header.frame_id = 'base'
    msg.pose.position.x, msg.pose.position.y, msg.pose.position.z = p
    msg.pose.orientation.x, msg.pose.orientation.y, msg.pose.orientation.z, msg.pose.orientation.w = q
    return msg


class Recorder:
    def __init__(self):
        self.lock, self.items = threading.Lock(), []
        rospy.Subscriber('/robot/limb/right/joint_command', JointCommand, self._command, queue_size=100)

    def _command(self, msg):
        with self.lock:
            self.items.append((msg.header.stamp.to_sec(), list(msg.velocity), msg.mode))

    def take(self):
        with self.lock:
            items, self.items = self.items, []
        return items


def max_velocity_step(items):
    steps = [max(abs(a-b) for a, b in zip(x[1], y[1])) for x, y in zip(items, items[1:])
             if len(x[1]) == 7 and len(y[1]) == 7]
    return max(steps) if steps else None


def main():
    rospy.init_node('validate_sim_motion', anonymous=True)
    if rospy.get_param('/use_sim_time', False) is not True or not rospy.get_param('/motion/motion_command/simulated', False):
        raise SystemExit('Refusing: not the Gazebo simulated Motion Controller')
    limb = Limb()
    recorder = Recorder()
    rospy.sleep(1.)
    options = TrajectoryOptions(interpolation_type=TrajectoryOptions.CARTESIAN)
    trajectory = MotionTrajectory(trajectory_options=options, limb=limb)
    waypoint = MotionWaypoint(options=MotionWaypointOptions(max_linear_speed=.3, max_linear_accel=.6,
                                                            max_joint_speed_ratio=1.).to_msg(), limb=limb)
    report = {}
    home = limb.joint_ordered_angles()

    def endpoint():
        e = limb.endpoint_pose()
        return [e['position'].x, e['position'].y, e['position'].z], [
            e['orientation'].x, e['orientation'].y, e['orientation'].z, e['orientation'].w]

    def send(p, q, wait, timeout=None):
        waypoint.set_cartesian_pose(pose_msg(p, q), 'right_hand', limb.joint_ordered_angles())
        trajectory.clear_waypoints()
        trajectory.append_waypoint(waypoint.to_msg())
        return trajectory.send_trajectory(wait_for_result=wait, timeout=timeout)

    # 1. Single Cartesian goal, as go_to_cartesian_pose.py does.
    p0, q0 = endpoint()
    goal = [p0[0]+.08, p0[1]-.05, p0[2]+.05]
    recorder.take()
    started = time.time()
    result = send(goal, q0, True, 15.)
    rospy.sleep(1.)
    p1, _ = endpoint()
    commands = recorder.take()
    report['single_goal'] = dict(result=bool(result and result.result), state=trajectory.get_state(),
                                 error_id=getattr(result, 'errorId', None), wall_s=round(time.time()-started, 3),
                                 final_error_m=math.dist(goal, p1), commands=len(commands),
                                 trajectory_mode=all(m == JointCommand.TRAJECTORY_MODE for _, _, m in commands),
                                 max_joint_velocity_step=max_velocity_step(commands))

    # 2. 100 Hz streamed single-waypoint goals (each preempts the last), then STOP.
    p0, q0 = endpoint()
    rate = rospy.Rate(100)
    recorder.take()
    sent = 0
    for i in range(1, 151):
        send([p0[0], p0[1]+.0015*i, p0[2]], q0, False)  # 0.15 m/s target speed
        sent += 1
        rate.sleep()
    streamed = recorder.take()
    stop = trajectory.stop_trajectory(wait_for_result=True, timeout=3.)
    stop_state = trajectory.get_state()
    rospy.sleep(.5)
    after_stop = recorder.take()
    p2, _ = endpoint()
    report['stream_100hz_then_stop'] = dict(
        goals_sent=sent, commands=len(streamed), max_joint_velocity_step=max_velocity_step(streamed),
        moved_y_m=p2[1]-p0[1], stop_result=bool(stop and stop.result), stop_state=stop_state,
        stop_succeeded=stop_state == GoalStatus.SUCCEEDED,
        final_command_velocity=max(abs(v) for v in after_stop[-1][1]) if after_stop else None,
        commands_after_stop=len(after_stop))

    # 3. Cancel without a new goal ends PREEMPTED after deceleration.
    p0, q0 = endpoint()
    send([p0[0]-.1, p0[1], p0[2]], q0, False)
    rospy.sleep(.3)
    trajectory._client._client.cancel_goal()
    trajectory._client._client.wait_for_result(rospy.Duration(3.))
    report['cancel'] = dict(state=trajectory.get_state(), preempted=trajectory.get_state() == GoalStatus.PREEMPTED)

    # 4. Invalid endpoint is aborted with the official error id.
    waypoint.set_cartesian_pose(pose_msg(*endpoint()), 'not_a_frame', limb.joint_ordered_angles())
    trajectory.clear_waypoints()
    trajectory.append_waypoint(waypoint.to_msg())
    trajectory._client.send_trajectory(trajectory.to_msg())
    bad = trajectory.wait_for_result(3.)
    report['invalid_endpoint'] = dict(state=trajectory.get_state(), error_id=getattr(bad, 'errorId', None))

    # 5. Return to the starting joints (JOINT interpolation) so repeated runs stay in the
    # same workspace; without this each run drifts ~0.27 m in y until IK fails at limits.
    joint_options = TrajectoryOptions(interpolation_type=TrajectoryOptions.JOINT)
    back = MotionTrajectory(trajectory_options=joint_options, limb=limb)
    back_waypoint = MotionWaypoint(options=MotionWaypointOptions(max_joint_speed_ratio=.5).to_msg(), limb=limb)
    back_waypoint.set_joint_angles(joint_angles=home, active_endpoint='right_hand')
    back.append_waypoint(back_waypoint.to_msg())
    returned = back.send_trajectory(wait_for_result=True, timeout=20.)
    rospy.sleep(1.)
    report['return_home'] = dict(result=bool(returned and returned.result),
                                 max_joint_error=max(abs(a-b) for a, b in zip(limb.joint_ordered_angles(), home)))

    checks = dict(
        single_goal=report['single_goal']['result'] and report['single_goal']['final_error_m'] < .005,
        stream_moved=report['stream_100hz_then_stop']['moved_y_m'] > .1,
        stop_confirmed=report['stream_100hz_then_stop']['stop_result'] and report['stream_100hz_then_stop']['stop_succeeded'],
        stopped_at_rest=report['stream_100hz_then_stop']['final_command_velocity'] in (None, 0.0)
                        or report['stream_100hz_then_stop']['final_command_velocity'] < 1e-6,
        cancel_preempted=report['cancel']['preempted'],
        invalid_aborted=report['invalid_endpoint']['state'] == GoalStatus.ABORTED
                        and report['invalid_endpoint']['error_id'] == 'ENDPOINT_DOES_NOT_EXIST',
        returned_home=report['return_home']['result'] and report['return_home']['max_joint_error'] < .02)
    report.update(checks=checks, passed=all(checks.values()),
                  limitations=['Project planner in Gazebo; not evidence of Intera blending, feel or real-robot behavior.'])
    print(json.dumps(report, indent=1))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
