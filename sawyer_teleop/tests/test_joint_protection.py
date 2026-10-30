"""Independent A/B checks, lifecycle and offline fault injection. No robot SDK."""
from pathlib import Path
import threading

import numpy as np
import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.client import RobotClient
from sawyer_teleop.joint_protection import (CAPABILITY, JointJumpError, check_jump,
    protection_options, parse_options)
from sawyer_teleop.protocol import ProtocolError, encode
from sawyer_teleop.server import BridgeServer
from sawyer_teleop.transport import read_one
from test_bridge import Clock, target
from test_intera_cartesian import Native

ON, OFF = protection_options(), protection_options(False)


def rig(mode='direct', enabled=True):
    clock = Clock()
    backend = FakeBackend(clock)
    options = dict(joint_protection=protection_options(enabled))
    if isinstance(mode, int):
        backend.name = 'intera'
        backend.cartesian = Native()
        backend.cartesian_capability = dict(available=True, schema=1, send_hz_choices=[10,20,50,100])
        options.update(execution_mode='intera_cartesian', cartesian_send_hz=mode)
    elif mode != 'direct':
        options['trajectory'] = dict(enabled=True, profile=mode)
    core = BridgeCore(backend, clock=clock)
    assert core.connect()['joint_protection'] == CAPABILITY
    result = core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'], **options))
    assert result['joint_protection'] == protection_options(enabled)
    return clock, backend, core


@pytest.mark.parametrize('index', range(7))
@pytest.mark.parametrize('sign', [-1, 1])
def test_per_joint_boundary_no_wrapping_and_no_accumulated_norm(index, sign):
    reference = [2.]*7
    q = [2+sign*.35]*7
    check_jump(q, reference, ON, 'boundary')
    q[index] += sign*1e-7
    with pytest.raises(JointJumpError) as caught:
        check_jump(q, reference, ON, 'boundary')
    assert caught.value.details['joint'] == 'right_j'+str(index)
    assert caught.value.details['delta_rad'] > .35
    check_jump(q, reference, OFF, 'disabled')
    q[index] = reference[index] + 2*np.pi
    with pytest.raises(JointJumpError): check_jump(q, reference, ON, 'no_wrap')


@pytest.mark.parametrize('options', [True, False, {}, {'enabled':1,'max_step_rad':.35},
    {'enabled':True,'max_step_rad':float('nan')}, {'enabled':True,'max_step_rad':.36},
    {'enabled':True,'max_step_rad':.35,'ignore':True}])
def test_guard_configuration_fails_closed(options):
    with pytest.raises(ProtocolError): parse_options(options)


@pytest.mark.parametrize('mode', ['direct', 'cubic', 'responsive', 'coordinated', 10, 20, 50, 100])
def test_b_rejects_target_even_if_a_did_not_check_and_rearm_anchors_fresh(mode):
    clock, backend, core = rig(mode)
    target(core, 1, [.1]*7)
    before = list(core.joint_target_anchor)
    q = [.1]*7; q[4] = .451
    with pytest.raises(JointJumpError): target(core, 2, q, gripper=1)
    assert core.reason == 'joint_jump' and not core.armed and core.pending is None
    assert core.joint_target_anchor == before
    assert core.submitted == 0
    assert core.pause_details['stage'] == 'B_target'
    assert core.pause_details['joint'] == 'right_j4'
    assert not target(core, 3, [.1]*7)['accepted']
    if isinstance(mode, int):
        assert not backend.cartesian.sent
        backend.cartesian.state = 'stopped'
        core.tick()
    backend.target = [1.]*7  # external repositioning while held
    clock.advance(.01)
    core._refresh()
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'], joint_protection=ON))
    target(core, 4, [1.1]*7)
    assert core.tick()[0]['q'] == [1.1]*7


@pytest.mark.parametrize('mode', ['direct', 10, 20, 50, 100])
def test_skipped_latest_goals_do_not_bypass_submission_check(mode):
    _, backend, core = rig(mode)
    target(core, 1, [.2]*7)
    target(core, 2, [.4]*7)  # accepted steps are small, aggregate submitted step isn't
    receipt, _ = core.tick()
    if isinstance(mode, int):
        backend.cartesian.finish()
        receipt, _ = core.tick()
        assert not backend.cartesian.sent
    assert receipt is None and core.reason == 'joint_jump' and core.submitted == 0
    assert core.joint_output_anchor == [0.]*7
    assert core.pause_details['stage'] in ('B_submit','B_native_submit')


@pytest.mark.parametrize('mode', ['cubic','responsive','coordinated'])
def test_planner_fault_cannot_publish_a_large_reference_or_gripper(mode):
    _, backend, core = rig(mode)
    calls = []
    backend.publish_gripper = calls.append
    target(core, 1, [.2]*7, gripper=1)
    core.trajectory.sample = lambda now: ([.351]*7, [0.]*7, [0.]*7)
    assert core.tick()[0] is None
    assert core.reason == 'joint_jump' and not calls
    assert core.pause_details['stage'] == 'B_trajectory_submit'


def test_disabled_only_removes_new_guard_and_never_freshness():
    clock, backend, core = rig(enabled=False)
    target(core, 1, [.8]*7)
    assert core.tick()[0]['q'] == [.8]*7
    target(core, 2, [1.6]*7)
    clock.advance(.121)
    assert core.tick()[0] is None and core.reason == 'feedback_reference_expired'


def test_neutral_reset_is_exact_named_destination_with_bounded_samples():
    clock, backend, core = rig('cubic')
    core._hold('test')
    neutral = list(backend.neutral_pose['q'])
    assert max(abs(q) for q in neutral) > .35
    opts = dict(type='arm', session=core.session, feedback_seq=core.latest['seq'],
                joint_protection=ON, motion_purpose='neutral_reset', trajectory={'enabled':True})
    core.request(opts)
    target(core, 1, neutral)
    assert core.tick()[0] is not None
    assert core.joint_output_anchor == [0.]*7
    bad = neutral[:]; bad[1] += .001
    with pytest.raises(ProtocolError, match='neutral_reset_target_mismatch'): target(core, 2, bad)
    assert not core.armed
    for trajectory in (None, {'enabled':False}, {'enabled':True,'profile':'responsive'}):
        with pytest.raises(ProtocolError, match='invalid_motion_purpose'):
            core.request(dict(opts, trajectory=trajectory))
    assert not core.armed


def client_stub(enabled=True):
    client = RobotClient('unused')
    client.joint_protection = protection_options(enabled)
    client.joint_target_anchor = [0.]*7
    client.hello = {}
    client.generation = 1
    client.snapshot = lambda *a, **k: dict(armed=True,generation=client.generation,sample={'q':[0.]*7,'seq':1})
    messages, holds = [], []
    class Connection:
        def send(self, msg, **kw): messages.append(msg)
    client.connection = Connection()
    client.hold = holds.append
    return client, messages, holds


def test_a_target_rejects_before_send_and_does_not_advance_anchor():
    client, messages, holds = client_stub()
    client.target([.2]*7,1)
    with pytest.raises(JointJumpError): client.target([.551]*7,1)
    assert len(messages) == 1 and len(holds) == 1
    assert client.joint_target_anchor == [.2]*7


@pytest.mark.parametrize('local', [True, False])
def test_a_checks_local_and_remote_ik_independently(local, monkeypatch):
    client, _, holds = client_stub()
    if local:
        client.ik_mode = 'local'
        class IK:
            def solve(self,*a,**k): return [.351]*7
        client.local_ik = IK()
    else:
        class Sock:
            def settimeout(self,*a): pass
            def sendall(self,*a): pass
            def shutdown(self,*a): pass
            def close(self): pass
        client.ik_socket = Sock()
        monkeypatch.setattr('sawyer_teleop.client.read_one', lambda *a,**k: dict(id=client.ident,ok=True,q=[.351]*7))
    with pytest.raises(JointJumpError, match='A_IK'): client.solve({},[0.]*7,1)
    assert not holds  # Worker leaves pause to the operator's generation-aware dispatch.
    assert client.joint_target_anchor == [0.]*7


def test_old_b_or_missing_guard_confirmation_cannot_silently_arm():
    client, _, holds = client_stub()
    with pytest.raises(ProtocolError, match='Update B'): client.arm(joint_protection=ON)
    client.hello['joint_protection'] = CAPABILITY
    client.request = lambda *a,**k: {'generation':2}
    with pytest.raises(ProtocolError, match='not_confirmed'): client.arm(joint_protection=ON)
    assert holds == ['joint_protection_not_confirmed']


@pytest.mark.parametrize('enabled', [True, False])
def test_real_socket_b_checks_remote_ik_before_reply_even_without_a_guard(enabled):
    backend = FakeBackend()
    backend.solve = lambda *args: [.4]*7
    server = BridgeServer(backend,control_port=0,ik_port=0).start()
    client = RobotClient('127.0.0.1',server.control_port,server.ik_port).connect()
    try:
        client.arm(joint_protection=protection_options(enabled))
        # Deliberately disable A's post-check to prove B enforces its own setting.
        client.joint_protection = OFF
        if enabled:
            with pytest.raises(ProtocolError, match='B_IK'):
                client.solve({'p':[.5,0,.3],'q':[0,0,0,1]},[0.]*7,client.snapshot()['sample']['seq'])
            assert server.core.reason == 'joint_jump' and not server.core.armed
        else:
            assert client.solve({'p':[.5,0,.3],'q':[0,0,0,1]},[0.]*7,client.snapshot()['sample']['seq']) == [.4]*7
            assert server.core.armed
        assert server.core.submitted == 0
    finally:
        client.close(); server.close()


def test_expired_ik_result_cannot_stop_a_new_generation():
    entered, release = threading.Event(), threading.Event()
    backend = FakeBackend()
    def solve(*args):
        entered.set(); release.wait(1); return [2.]*7
    backend.solve = solve
    server = BridgeServer(backend,control_port=0,ik_port=0).start()
    client = RobotClient('127.0.0.1',server.control_port,server.ik_port).connect()
    try:
        client.arm(joint_protection=ON)
        sock = client.ik_socket
        sock.sendall(encode(dict(type='solve',id=123,session=client.session,generation=client.generation,
            pose={'p':[.5,0,.3],'q':[0,0,0,1]},seed=[0.]*7,timeout_s=.2,
            feedback_seq=client.snapshot()['sample']['seq'])))
        assert entered.wait(1)
        client.hold('test'); client.arm(joint_protection=ON)
        generation = server.core.generation
        release.set()
        assert read_one(sock)['error'] == 'ik_expired'
        assert server.core.armed and server.core.generation == generation
    finally:
        release.set(); client.close(); server.close()


def test_entire_lab_fixture_guard_without_skipping_failed_segments():
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('evaluate_joint_protection', root/'scripts/evaluate_joint_protection.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    report = module.evaluate(root/'tests/fixtures/lab_scene_20261022')
    assert report['total_events'] == 12098 and report['event_counts']['quest_input'] == 7177
    assert report['unique_goals'] == 1779 and report['adjacent_steps_rad']['count'] == 1774
    assert len(report['no_submit_segments']) == 3 and report['missing_first_anchor'] == 5
    assert report['candidate_rejections']['0.2'] == 4 and report['candidate_rejections']['0.35'] == 0
    for row in report['comparisons']:
        check_jump(row['delta_rad'], [0.]*7, ON, 'recorded_goal_delta')
    assert report['hypothetical_cadence']['10']['over_035'] == 1


def test_operator_selection_is_atomic_with_existing_pause_guards(tmp_path):
    from sawyer_teleop.operator import Operator
    from sawyer_teleop.demo import SyntheticQuest
    from test_ik_schedule import Robot
    robot = Robot(); robot.expected_backend = 'fake'
    op = Operator(robot, SyntheticQuest(), [], tmp_path)
    try:
        assert op.joint_protection == ON
        value = op.quest.poll()
        op.following = True
        with pytest.raises(ValueError): op._change_motion_profile('standard',value,False)
        assert op.joint_protection == ON
        op.following = False
        op._change_motion_profile('standard',value,False)
        assert op.joint_protection == OFF and not robot.targets and not robot.armed
        with pytest.raises(ProtocolError): op._change_motion_profile('standard',value,'false')
        assert op.joint_protection == OFF
    finally:
        op.executor.shutdown(wait=True)


def test_operator_native_and_direct_use_previous_accepted_seed_with_smoothing_off(tmp_path):
    from sawyer_teleop.operator import Operator
    from sawyer_teleop.demo import SyntheticQuest
    from test_ik_schedule import Robot, ManualExecutor
    robot, quest = Robot(), SyntheticQuest()
    op = Operator(robot, quest, [], tmp_path, motion={'enabled':False}, trajectory={'enabled':False})
    op.executor.shutdown(wait=True); op.executor = ManualExecutor()
    op.tick(); op.request('calibrate'); op.tick()
    quest.grip = 1
    op.tick()
    assert op.following
    op.future.set_result(([.2]*7, op.future_info['started'], op.future_info['started']))
    op.next_ik_ns = 0
    op.tick()
    assert robot.targets[0][0] == [.2]*7
    assert op.executor.jobs[-1][2][1] == [.2]*7
    assert op.future_info['ik_seed'] == [.2]*7


@pytest.mark.parametrize('mode', ['direct', 'cubic', 'responsive', 'coordinated', 10, 20, 50, 100])
def test_every_mode_can_submit_a_nearby_goal_with_protection_enabled(mode):
    clock, backend, core = rig(mode)
    target(core, 1, [.2]*7)
    receipt, state = core.tick()
    if isinstance(mode, int):
        backend.cartesian.finish()
        receipt, state = core.tick()
        assert backend.cartesian.sent == [[.2]*7]
    assert receipt is not None and core.submitted == 1
    assert state['armed'] and state['joint_protection'] == ON
    assert core.joint_output_anchor == receipt['q']
    core.request(dict(type='hold', session=core.session))
    assert not core.armed and core.pending is None
