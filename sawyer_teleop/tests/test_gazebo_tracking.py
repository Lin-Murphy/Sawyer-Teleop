"""Simulation acceleration selection must not affect hardware or neutral return."""
import copy
from types import SimpleNamespace

import pytest

from sawyer_teleop.operator import Operator

BASE = {'profile': 'coordinated', 'max_velocity_rad_s': [.8, .8, 1., 1., 1.57, 1.57, 1.57],
        'max_acceleration_rad_s2': [1.5, 1.5, 3., 3., 3., 3., 3.]}
FAST = [6., 6., 12., 12., 12., 12., 12.]


@pytest.mark.parametrize('selected,reported,fast', [
    ('gazebo', 'gazebo', True), ('gazebo', 'intera', False),
    ('intera', 'gazebo', False), ('intera', 'intera', False),
    ('fake', 'fake', False), (None, 'gazebo', False), ('gazebo', None, False),
])
def test_both_selected_and_confirmed_backend_required(tmp_path, selected, reported, fast):
    config = copy.deepcopy(BASE)
    robot = SimpleNamespace(expected_backend=selected, hello={'backend': reported})
    op = Operator(robot, None, [], tmp_path, trajectory=config,
                  gazebo_max_acceleration_rad_s2=FAST)
    try:
        assert op.trajectory_options['max_acceleration_rad_s2'] == BASE['max_acceleration_rad_s2']
        op._select_trajectory_options()
        expected = FAST if fast else BASE['max_acceleration_rad_s2']
        assert op.trajectory_options['max_acceleration_rad_s2'] == expected
        assert op.trajectory_options['max_velocity_rad_s'] == BASE['max_velocity_rad_s']
        assert op.trajectory_options['profile'] == 'coordinated'
        assert config == BASE and op.base_trajectory_options['max_acceleration_rad_s2'] == BASE['max_acceleration_rad_s2']
        # A later selection cannot retain the simulation setting for another backend.
        robot.expected_backend = 'intera'
        robot.hello['backend'] = 'intera'
        op._select_trajectory_options()
        assert op.trajectory_options == op.base_trajectory_options
    finally:
        op.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('value', [[6.]*6, [0.]*7, [-1.]*7, [float('nan')]*7])
def test_invalid_simulation_limits_rejected_before_start(tmp_path, value):
    with pytest.raises(ValueError):
        Operator(None, None, [], tmp_path, trajectory=BASE, gazebo_max_acceleration_rad_s2=value)


def test_old_config_has_no_simulation_override(tmp_path):
    op = Operator(SimpleNamespace(expected_backend='gazebo', hello={'backend':'gazebo'}),
                  None, [], tmp_path, trajectory=BASE)
    try:
        op._select_trajectory_options()
        assert op.trajectory_options == op.base_trajectory_options
    finally:
        op.executor.shutdown(wait=True, cancel_futures=True)


def test_reset_keeps_original_limits_after_fast_teleop_selection(tmp_path):
    from test_reset_arm import Robot
    from sawyer_teleop.demo import SyntheticQuest
    robot = Robot()
    robot.expected_backend = robot.hello['backend'] = 'gazebo'
    robot.hello['scene_reset'] = {'available': True}
    op = Operator(robot, SyntheticQuest(), [], tmp_path, trajectory=BASE,
                  gazebo_max_acceleration_rad_s2=FAST)
    try:
        op._select_trajectory_options()
        assert op.trajectory_options['max_acceleration_rad_s2'] == FAST
        op.request('reset_arm')
        op.tick()
        reset = robot.arms[0]['trajectory']
        assert reset['profile'] == 'cubic'
        assert reset['max_acceleration_rad_s2'] == [a*.25 for a in BASE['max_acceleration_rad_s2']]
        assert reset['max_velocity_rad_s'] == [.2, .2, .25, .25, .3925, .3925, .3925]
    finally:
        op.executor.shutdown(wait=True, cancel_futures=True)


def test_operator_handshake_applies_limits_to_existing_bridge_protocol(tmp_path):
    # A software protocol integration test; this is not physical Gazebo evidence.
    from sawyer_teleop.backends import FakeBackend
    from sawyer_teleop.client import RobotClient
    from sawyer_teleop.demo import SyntheticQuest, wait_until
    from sawyer_teleop.server import BridgeServer
    from test_operator import engage
    backend = FakeBackend()
    backend.name = 'gazebo'
    bridge = BridgeServer(backend, '127.0.0.1', 0, 0).start()
    quest = SyntheticQuest()
    op = Operator(RobotClient('127.0.0.1', bridge.control_port, bridge.ik_port,
                              expected_backend='gazebo'), quest, [], tmp_path,
                  trajectory=BASE, gazebo_max_acceleration_rad_s2=FAST).start()
    try:
        wait_until(lambda: op.epoch is not None)
        op.request('calibrate')
        wait_until(lambda: op.mapping is not None)
        engage(quest, op)
        assert bridge.core.trajectory.options['max_acceleration_rad_s2'] == FAST
        assert bridge.core.trajectory.options['max_velocity_rad_s'] == BASE['max_velocity_rad_s']
        quest.grip = 0.
        wait_until(lambda: not op.following)
        # A cancels immediately; B telemetry confirms the hold separately.
        # Profile switching must retain its existing B-confirmed-hold guard.
        wait_until(lambda: not op.robot.snapshot()['armed'])
        assert bridge.core.trajectory is None
        for number, (profile, acceleration) in enumerate([
                ('standard', BASE['max_acceleration_rad_s2']), ('fast_follow', FAST)], 1):
            op.request('set_motion_profile', profile=profile, request_id=number)
            wait_until(lambda: op.view().get('motion_profile_result', {}).get('request_id') == number)
            assert op.view()['motion_profile_result']['accepted'], op.view()['motion_profile_result']
            assert bridge.core.trajectory is None  # Selection itself must not arm B.
            engage(quest, op)
            assert bridge.core.trajectory.options['max_acceleration_rad_s2'] == acceleration
            quest.grip = 0.
            wait_until(lambda: not op.following)
            wait_until(lambda: not op.robot.snapshot()['armed'])
            assert bridge.core.trajectory is None
    except Exception:
        print('Operator diagnostics on handshake failure:', op.view())
        raise
    finally:
        op.close()
        bridge.close()
