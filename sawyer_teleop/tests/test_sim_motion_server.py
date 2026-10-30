"""Offline checks for the Gazebo-only simulated Motion Controller planner (no ROS)."""
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from sawyer_teleop.kinematics_model import read_kinematics_model
from sawyer_teleop.local_ik import LocalIK
from sawyer_teleop.protocol import JOINTS
from sawyer_teleop.sim_motion_server import (
    DEFAULT_JOINT_ACCEL, DEFAULT_WAYPOINT, MotionError, SimMotionPlanner, approach,
    joint_speed_limits, parse_trajectory)

ROOT = Path(__file__).resolve().parents[1]
URDF = (ROOT/'tests/fixtures/sideways_lift_1_2_11/robot.urdf').read_text()
HOME = [-.2936, -.9481, .07, 1.6269, -.0524, .8939, -1.6001]
DT = .01


def planner():
    result = SimMotionPlanner(LocalIK(read_kinematics_model(URDF)), joint_speed_limits(URDF))
    result.sync(HOME)
    return result


def cartesian(p, r, **options):
    return dict(DEFAULT_WAYPOINT, kind='CARTESIAN', joints=None, pose=(np.array(p), r),
                max_joint_accel=DEFAULT_JOINT_ACCEL, **options)


def run(plan, limit=1500):
    samples = []
    for _ in range(limit):
        q, dq, ddq, event = plan.tick(DT)
        samples.append((plan.p.copy(), plan.v.copy(), np.array(dq)))
        if event:
            return event, samples
    raise AssertionError('planner did not settle')


def test_speed_limits_come_from_urdf():
    assert joint_speed_limits(URDF) == [1.74, 1.328, 1.957, 1.957, 3.485, 3.485, 4.545]


def test_approach_stops_on_target_without_overshoot():
    x, v, positions = np.array([.1, 0., 0.]), np.zeros(3), []
    for _ in range(500):
        step, new_v = approach(x, v, .6, .6, DT)
        assert np.linalg.norm(new_v-v) <= .6*DT+1e-9 or np.allclose(step, x)
        x, v = x-step, new_v
        positions.append(x[0])
    assert abs(x[0]) < 1e-9 and min(positions) >= -1e-12


def test_cartesian_goal_reaches_pose_within_speed_and_joint_limits():
    plan = planner()
    start = plan.p.copy()
    plan.start([cartesian(start+[.1, .05, -.05], plan.r)])
    event, samples = run(plan)
    assert event == 'reached'
    assert np.linalg.norm(plan.p-(start+[.1, .05, -.05])) < 1e-4
    speeds = [np.linalg.norm(v) for _, v, _ in samples]
    assert max(speeds) <= .6+1e-9
    changes = [np.linalg.norm(b[1]-a[1]) for a, b in zip(samples, samples[1:])]
    assert max(changes[:-2]) <= .6*DT+1e-6
    # The final ticks snap onto the target; the residual jump stays near one accel step.
    assert max(changes) <= 1.2*.6*DT
    limits = np.array(joint_speed_limits(URDF))*DEFAULT_WAYPOINT['max_joint_speed_ratio']
    assert max(np.max(np.abs(dq)/limits) for _, _, dq in samples) <= 1.0001
    # Straight Cartesian path: lateral deviation from the start-goal line stays tiny.
    axis = np.array([.1, .05, -.05])/np.linalg.norm([.1, .05, -.05])
    assert max(np.linalg.norm(np.cross(p-start, axis)) for p, _, _ in samples) < 1e-4


def test_streamed_goals_keep_velocity_continuous():
    plan = planner()
    start, r = plan.p.copy(), plan.r
    previous_v = np.zeros(3)
    for i in range(1, 80):
        # A new single-waypoint goal every 10 ms, as the 100 Hz teleop client sends.
        plan.start([cartesian(start+[.002*i, 0, 0], r)])
        _, _, _, event = plan.tick(DT)
        assert np.linalg.norm(plan.v-previous_v) <= .6*DT+1e-6
        previous_v = plan.v.copy()
    assert plan.v[0] > .1  # Preemption did not restart from rest each tick.


def test_stop_decelerates_to_rest():
    plan = planner()
    plan.start([cartesian(plan.p+[.3, 0, 0], plan.r)])
    for _ in range(40):
        plan.tick(DT)
    assert np.linalg.norm(plan.v) > .1
    plan.stop()
    event, samples = run(plan)
    assert event == 'stopped' and not plan.moving
    assert max(np.linalg.norm(b[1]-a[1]) for a, b in zip(samples, samples[1:])) <= .6*DT+1e-6
    assert np.allclose(plan.dq, 0)


def test_rotation_goal_reaches_orientation():
    plan = planner()
    goal = Rotation.from_rotvec([0, 0, .3])*plan.r
    plan.start([cartesian(plan.p.copy(), goal)])
    assert run(plan)[0] == 'reached'
    assert np.linalg.norm((goal*plan.r.inv()).as_rotvec()) < 1e-3


def test_joint_goal_and_limits():
    plan = planner()
    goal = list(np.array(HOME)+[.2, -.1, 0, .1, 0, 0, .3])
    plan.start([dict(DEFAULT_WAYPOINT, kind='JOINT', joints=goal, pose=None, max_joint_accel=DEFAULT_JOINT_ACCEL)])
    assert run(plan)[0] == 'reached'
    assert np.allclose(plan.q, goal, atol=1e-4)
    with pytest.raises(MotionError):
        plan.start([dict(DEFAULT_WAYPOINT, kind='JOINT', joints=[9.]*7, pose=None,
                         max_joint_accel=DEFAULT_JOINT_ACCEL)])


def test_unreachable_cartesian_goal_fails_closed():
    plan = planner()
    plan.start([cartesian([3., 0, 0], plan.r, max_linear_speed=50., max_linear_accel=5000.)])
    with pytest.raises(MotionError, match='CARTESIAN_INTERPOLATION_FAILED'):
        run(plan)


def message(kind='CARTESIAN', endpoint='right_hand', quaternion=(0, 0, 0, 1), interaction=False, joints=()):
    pose = NS(header=NS(frame_id='base'), pose=NS(position=NS(x=.5, y=0, z=.3),
              orientation=NS(**dict(zip('xyzw', quaternion)))))
    options = NS(max_linear_speed=0., max_linear_accel=.3, max_rotational_speed=0., max_rotational_accel=0.,
                 max_joint_speed_ratio=1., max_joint_accel=[])
    waypoint = NS(active_endpoint=endpoint, pose=pose, options=options, joint_positions=list(joints))
    return NS(joint_names=JOINTS, waypoints=[waypoint],
              trajectory_options=NS(interpolation_type=kind, interaction_control=interaction))


def test_parse_trajectory_fills_sdk_defaults():
    [item] = parse_trajectory(message(joints=HOME), 'right_hand')
    assert item['max_linear_speed'] == .6 and item['max_linear_accel'] == .3
    assert item['joints'] == HOME and np.allclose(item['pose'][0], [.5, 0, .3])


@pytest.mark.parametrize('kwargs, error', [
    (dict(endpoint='right_l6'), 'ENDPOINT_DOES_NOT_EXIST'),
    (dict(interaction=True), 'INVALID_TRAJECTORY_MESSAGE'),
    (dict(quaternion=(0, 0, 0, 0)), 'INVALID_TRAJECTORY_MESSAGE'),
    (dict(kind='JOINT'), 'INVALID_TRAJECTORY_MESSAGE'),
    (dict(kind='SPLINE'), 'INVALID_TRAJECTORY_MESSAGE'),
])
def test_parse_trajectory_rejects(kwargs, error):
    with pytest.raises(MotionError, match=error):
        parse_trajectory(message(**kwargs), 'right_hand')
