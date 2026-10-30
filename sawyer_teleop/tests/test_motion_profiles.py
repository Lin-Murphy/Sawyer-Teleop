"""Motion choices use existing limits and never switch an active generation."""
import copy
import json

import pytest

from sawyer_teleop.demo import SyntheticQuest
from sawyer_teleop.operator import Operator
from sawyer_teleop.motion_profiles import CARTESIAN_RATES
from test_gazebo_tracking import BASE, FAST
from test_ik_schedule import Robot


@pytest.fixture
def op(tmp_path):
    robot = Robot()
    robot.expected_backend = 'gazebo'
    robot.hello = {'backend': 'gazebo'}
    controller = Operator(robot, SyntheticQuest(), [], tmp_path, trajectory=BASE,
        gazebo_max_acceleration_rad_s2=FAST, motion_profile='standard')
    controller._select_trajectory_options()
    yield controller
    controller.executor.shutdown(wait=True, cancel_futures=True)


def test_explicit_standard_and_paused_switch_preserve_limits_without_commands(op):
    assert op.trajectory_options == op.base_trajectory_options
    baseline = copy.deepcopy(op.trajectory_options)
    for name, acceleration in [('fast_follow', FAST), ('standard', BASE['max_acceleration_rad_s2'])]:
        op.request('set_motion_profile', profile=name, request_id=name)
        op.tick(queue_ik=False)
        assert op.view()['motion_profile_result'] == dict(request_id=name, accepted=True, profile=name)
        assert op.motion_profile == name
        assert op.trajectory_options == dict(baseline, max_acceleration_rad_s2=acceleration)
        assert not op.robot.armed and not op.robot.targets and not op.robot.holds
    assert op.base_trajectory_options == baseline


@pytest.mark.parametrize('blocked', ['following', 'resetting', 'reset_requested', 'episode', 'review',
    'finishing', 'auto_recording', 'grip', 'invalid', 'armed', 'wrong_backend', 'wrong_handshake'])
def test_switch_rejected_without_control_side_effects(op, blocked):
    value = op.quest.poll()
    if blocked == 'grip': value['grip'] = 1.
    elif blocked == 'invalid': value['valid'] = False
    elif blocked == 'armed': op.robot.armed = True
    elif blocked == 'wrong_backend': op.robot.expected_backend = 'intera'
    elif blocked == 'wrong_handshake': op.robot.hello['backend'] = 'intera'
    elif blocked == 'reset_requested': op.reset_requested.set()
    else: setattr(op, blocked, object() if blocked in ('episode','review','finishing','resetting') else True)
    with pytest.raises(ValueError):
        op._change_motion_profile('fast_follow', value)
    assert op.motion_profile == op.selected_motion_profile == 'standard'
    assert op.trajectory_options == op.base_trajectory_options
    assert not op.robot.targets and not op.robot.holds


def test_invalid_choice_and_stale_feedback_do_not_change_selection(op):
    for choice in [None, 'payload', 'unknown']:
        with pytest.raises(ValueError): op._change_motion_profile(choice, op.quest.poll())
    def stale(): raise RuntimeError('feedback_stale')
    op.robot.snapshot = stale
    op.request('set_motion_profile', profile='fast_follow', request_id=7)
    op.tick(queue_ik=False)
    assert op.view()['motion_profile_result'] == dict(request_id=7, accepted=False,
        profile='standard', error='feedback_stale')
    assert op.trajectory_options == op.base_trajectory_options


def test_same_tick_recording_has_selected_profile_and_its_own_parameters(op):
    op.tick(queue_ik=False)
    op.request('calibrate'); op.tick(queue_ik=False)
    # Parameter dictionaries must not overwrite another action in the same GUI batch.
    op.request('start_recording', name='selected_profile', count=1, seconds=20, pause=0)
    op.request('set_motion_profile', profile='fast_follow', request_id=1)
    op.tick(queue_ik=False)
    ep = op.episode
    assert ep is not None
    assert op.batch['name'] == 'selected_profile'
    assert ep.metadata['motion_profile'] == 'fast_follow'
    assert ep.metadata['motion_profile_status'] == 'experimental'
    assert ep.metadata['trajectory']['max_acceleration_rad_s2'] == FAST
    op._end_recording()
    assert ep.finish(True, {'capture_closed': True}).wait(5)
    saved = json.loads((ep.result/'manifest.json').read_text())
    assert saved['motion_profile'] == 'fast_follow'


@pytest.mark.parametrize('backend', ['fake'])
def test_explicit_fast_choice_rejects_unsupported_backend(op, backend):
    op.selected_motion_profile = 'fast_follow'
    op.robot.expected_backend = op.robot.hello['backend'] = backend
    op._select_trajectory_options()
    assert op.motion_profile == 'standard'
    assert op.trajectory_options == op.base_trajectory_options


def test_real_fast_has_independent_double_acceleration_and_same_reset(op):
    op.robot.expected_backend = op.robot.hello['backend'] = 'intera'
    reset = copy.deepcopy(op.reset_trajectory_options)
    baseline = copy.deepcopy(op.base_trajectory_options)
    # Deliberately extreme simulation values must not leak into Real Sawyer.
    op.gazebo_trajectory_options['max_acceleration_rad_s2'] = [999.]*7
    op._change_motion_profile('fast_follow', op.quest.poll())
    assert op.motion_profile == 'fast_follow'
    assert op.trajectory_options['max_acceleration_rad_s2'] == [2*a for a in baseline['max_acceleration_rad_s2']]
    assert op.trajectory_options['max_velocity_rad_s'] == baseline['max_velocity_rad_s']
    assert op.reset_trajectory_options == reset and op.base_trajectory_options == baseline
    assert not op.robot.targets and not op.robot.armed
    op._change_motion_profile('standard', op.quest.poll())
    assert op.trajectory_options == baseline


def test_real_fast_does_not_require_gazebo_override(tmp_path):
    robot = Robot()
    robot.expected_backend = 'intera'
    robot.hello = {'backend':'intera'}
    op = Operator(robot,SyntheticQuest(),[],tmp_path,trajectory=BASE,motion_profile='fast_follow')
    try:
        op._select_trajectory_options()
        assert op.motion_profile == 'fast_follow'
        assert op.trajectory_options['max_acceleration_rad_s2'] == [2*a for a in BASE['max_acceleration_rad_s2']]
    finally:
        op.executor.shutdown(wait=True,cancel_futures=True)


def native_ready(op):
    op.robot.expected_backend = 'intera'
    op.robot.hello = dict(backend='intera', intera_cartesian=dict(available=True, schema=1,
                                                               options=dict(send_hz=10), send_hz_choices=[10, 20, 50, 100]))


def test_native_selection_bypasses_joint_trajectory_and_preserves_standard(op):
    native_ready(op)
    baseline = copy.deepcopy(op.base_trajectory_options)
    op._change_motion_profile('intera_cartesian', op.quest.poll())
    assert op.motion_profile == 'intera_cartesian'
    assert not op.trajectory_options['enabled']
    assert not op.robot.targets and not op.robot.holds and not op.robot.armed
    op._change_motion_profile('standard', op.quest.poll())
    assert op.trajectory_options == baseline
    assert op.motion_profile == 'standard'


@pytest.mark.parametrize('blocked', ['following', 'resetting', 'reset_requested', 'episode', 'review',
    'finishing', 'auto_recording', 'grip', 'invalid', 'armed', 'native_stopping', 'native_stop_failed'])
def test_native_switch_retains_all_pause_and_recording_guards(op, blocked):
    native_ready(op)
    value = op.quest.poll()
    if blocked == 'grip': value['grip'] = 1.
    elif blocked == 'invalid': value['valid'] = False
    elif blocked == 'armed': op.robot.armed = True
    elif blocked in ('native_stopping', 'native_stop_failed'):
        snapshot = op.robot.snapshot
        op.robot.snapshot = lambda: dict(snapshot(), **{blocked: True})
    elif blocked == 'reset_requested': op.reset_requested.set()
    else: setattr(op, blocked, object() if blocked in ('episode', 'review', 'finishing', 'resetting') else True)
    with pytest.raises(ValueError): op._change_motion_profile('intera_cartesian', value)
    assert op.motion_profile == 'standard' and not op.robot.targets


def test_native_unavailable_cannot_apply_or_silently_engage_standard(op):
    native_ready(op)
    op.robot.hello['intera_cartesian'] = dict(available=False, reason='Native service missing')
    with pytest.raises(ValueError, match='Native service missing'):
        op._change_motion_profile('intera_cartesian', op.quest.poll())
    op.selected_motion_profile = 'intera_cartesian'
    op._select_trajectory_options()
    op.tick(queue_ik=False)
    op.request('calibrate')
    op.tick(queue_ik=False)
    with pytest.raises(RuntimeError, match='Native service missing'):
        op._engage(op.quest.poll(), op.robot.snapshot())
    assert not op.robot.armed


@pytest.mark.parametrize('profile,send_hz', [(p,hz) for p,hz in CARTESIAN_RATES.items() if p != 'intera_pose_50hz'])
def test_native_engagement_passes_explicit_mode_and_recording_semantics(op, profile, send_hz):
    native_ready(op)
    op._change_motion_profile(profile, op.quest.poll())
    op.tick(queue_ik=False)
    op.request('calibrate')
    op.tick(queue_ik=False)
    calls = []
    arm = op.robot.arm
    def capture(**options):
        calls.append(options)
        return arm(**options)
    op.robot.arm = capture
    op.quest.grip = 1  # Engagement now revalidates actual grip after arm.
    op._engage(op.quest.poll(), op.robot.snapshot())
    assert calls == [dict(execution_mode='intera_cartesian', cartesian_send_hz=send_hz,
                          joint_protection={'enabled':True, 'max_step_rad':.35})]
    op.following = False
    op.robot.armed = False
    op.request('start_recording', name='native_profile', count=1, seconds=20, pause=0)
    op.tick(queue_ik=False)
    ep = op.episode
    assert ep.metadata['motion_profile_status'] == 'experimental'
    assert ep.metadata['command_semantics'] == 'cartesian_goal_joint_bias'
    assert ep.metadata['native_cartesian']['options']['send_hz'] == send_hz
    assert not ep.metadata['trajectory']['enabled']
    op._end_recording()
    assert ep.finish(True, {'capture_closed': True}).wait(5)


@pytest.mark.parametrize('profile', CARTESIAN_RATES)
def test_saved_rate_choice_is_not_silently_changed_on_old_b(op, profile):
    native_ready(op)
    op.robot.hello['intera_cartesian'].pop('send_hz_choices')
    op.selected_motion_profile = profile
    op._select_trajectory_options()
    assert 'Update B' in op.view()['motion_profile_unavailable_reason']
    assert op.selected_motion_profile == profile
    op.tick(queue_ik=False)
    op.request('calibrate')
    op.tick(queue_ik=False)
    with pytest.raises(RuntimeError, match='Update B'):
        op._engage(op.quest.poll(), op.robot.snapshot())
    assert not op.robot.armed


def test_web_keeps_native_options_selectable_until_b_reports_capability():
    # 2026-10-29: greyed before connect, Gazebo users could not pick them at all.
    from sawyer_teleop.web_session import profile_reason
    for profile in CARTESIAN_RATES:
        for backend in ('gazebo', 'intera'):
            assert profile_reason(profile, backend, None) is None
            assert profile_reason(profile, backend, dict(available=False, reason='missing')) == 'missing'
        assert profile_reason(profile, 'fake', None)
