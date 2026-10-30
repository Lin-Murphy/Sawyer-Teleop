import copy
import threading
from types import SimpleNamespace as Obj

import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.demo import SyntheticQuest, wait_until
from sawyer_teleop.gazebo_scene import GazeboScene
from sawyer_teleop.operator import Operator
from sawyer_teleop.protocol import ProtocolError, ns
from test_reset_arm import Robot


def test_scene_restores_startup_pose_and_zero_velocity_without_robot():
    scene = GazeboScene.__new__(GazeboScene)
    scene.capability = {'available': True}
    scene.poses = {'cube': Obj(x=1), 'table': Obj(x=2)}
    scene.world = lambda: Obj(success=True, model_names=['sawyer', 'cube', 'table'])
    scene.model_state = lambda: Obj(twist=[0]*6)
    written = []
    def write(state):
        written.append(copy.deepcopy(state))
        state.pose.x = 99  # Services cannot mutate the stored baseline.
        return Obj(success=True)
    scene.set = write
    assert scene.reset(threading.Event()) == ['cube', 'table']
    assert [(s.model_name, s.pose.x, s.reference_frame, s.twist) for s in written] == [
        ('cube', 1, 'world', [0]*6), ('table', 2, 'world', [0]*6)]
    assert scene.poses['cube'].x == 1
    scene.world = lambda: Obj(success=True, model_names=['sawyer', 'table'])
    with pytest.raises(RuntimeError, match='scene_models_missing'):
        scene.reset(threading.Event())
    assert len(written) == 2
    scene.world = lambda: Obj(success=True, model_names=['sawyer', 'cube', 'table'])
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(RuntimeError, match='cancelled'):
        scene.reset(cancelled)
    assert len(written) == 2
    scene.set = lambda state: Obj(success=False, status_message='write failed')
    with pytest.raises(RuntimeError, match='write failed'):
        scene.reset(threading.Event())


def scene_core():
    backend = FakeBackend()
    backend.name = 'gazebo'
    backend.neutral_pose = dict(backend.neutral_pose, q=[0.]*7)
    backend.scene_reset_capability = {'available': True}
    core = BridgeCore(backend)
    core.connect()
    core.request(dict(type='hold', session=core.session))
    return core, backend


def reset_request(core, **changes):
    msg = dict(type='reset_scene', session=core.session, generation=core.generation,
               feedback_seq=core.latest['seq'])
    msg.update(changes)
    return core.request(msg)


@pytest.mark.parametrize('finish', ['success', 'failure', 'hold', 'disconnect', 'stale'])
def test_scene_job_keeps_feedback_live_blocks_rearm_and_reports_result(finish):
    core, backend = scene_core()
    started, release = threading.Event(), threading.Event()
    def restore(cancelled):
        started.set()
        assert release.wait(2)
        if finish == 'failure':
            raise RuntimeError('service failed')
        return ['cube']
    backend.reset_scene = restore
    job = reset_request(core)
    try:
        assert started.wait(1)
        _, state = core.tick()
        assert state['scene_reset'] == dict(job, state='running')
        with pytest.raises(ProtocolError, match='in_progress'):
            core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq']))
        with pytest.raises(ProtocolError, match='in_progress'):
            reset_request(core)
        if finish == 'hold':
            core.request(dict(type='hold', session=core.session))
        elif finish == 'disconnect':
            core.disconnect(core.session)
        elif finish == 'stale':
            backend.frozen = True
            backend.sample['captured_ns'] -= 200_000_000
            core.tick()
    finally:
        release.set()
    wait_until(lambda: core.scene_reset['state'] != 'running', 1)
    assert core.scene_reset['state'] == ('completed' if finish == 'success' else 'failed')
    assert not core.armed and core.pending is None


@pytest.mark.parametrize('fault', ['backend', 'unsupported', 'generation', 'moving', 'not_neutral', 'session'])
def test_scene_request_is_gated_before_services(fault):
    core, backend = scene_core()
    backend.reset_scene = lambda cancelled: pytest.fail('must not call service')
    changes = {}
    if fault == 'backend': backend.name = 'intera'
    if fault == 'unsupported': backend.scene_reset_capability = {'available': False}
    if fault == 'generation': changes['generation'] = core.generation - 1
    if fault == 'moving': core.armed = True
    if fault == 'not_neutral': backend.neutral_pose = dict(backend.neutral_pose, q=[1.]*7)
    if fault == 'session': changes['session'] = 'old'
    with pytest.raises((ProtocolError, ValueError)):
        reset_request(core, **changes)
    assert core.scene_reset is None


@pytest.fixture
def operator(tmp_path):
    robot = Robot()
    robot.hello.update(backend='gazebo', scene_reset={'available': True})
    robot.scene_calls = 0
    def reset():
        assert not robot.state['armed'] and robot.holds
        robot.scene_calls += 1
        return {'id': 'scene-1'}
    robot.reset_scene = reset
    op = Operator(robot, SyntheticQuest(), [], tmp_path)
    yield op
    op.executor.shutdown(wait=True, cancel_futures=True)


def reach_neutral(op):
    op.request('reset_arm')
    op.tick()
    assert not op.robot.scene_calls
    op.robot.state['sample'].update(q=op.robot.hello['neutral_pose']['q'], dq=[0.]*7)
    op.tick()
    op.resetting['settled_ns'] = ns()-110_000_000
    op.robot.state['sample']['seq'] += 1
    op.tick()


@pytest.mark.parametrize('finish', ['completed', 'failed', 'stop', 'timeout'])
def test_operator_scene_phase_and_no_teleop_until_release(operator, finish):
    op = operator
    op.quest.grip = 1
    reach_neutral(op)
    assert op.resetting['phase'] == 'scene' and op.robot.scene_calls == 1
    # B preserves the first hold cause when the arm is already held.
    original_hold = op.robot.hold
    def held_again(reason):
        return dict(original_hold(reason), reason='Arm is at neutral', pause_details={'holding': True})
    op.robot.hold = held_again
    if finish == 'stop': op.request('stop_reset')
    elif finish == 'timeout': op.resetting['started_ns'] -= 6_000_000_000
    else: op.robot.state['scene_reset'] = {'id':'scene-1', 'state':finish, 'error':'service failed'}
    op.tick()
    assert not op.resetting and not op.following and not op.release_seen
    assert ('scene restored' in op.view()['message']) == (finish == 'completed')
    if finish in ('failed', 'timeout'): assert op.view()['message_level'] == 'error'


def test_old_gazebo_bridge_refused_before_motion(operator):
    operator.robot.hello.pop('scene_reset')
    operator.request('reset_arm')
    operator.tick()
    assert not operator.robot.arms and not operator.robot.scene_calls
    assert not operator.view()['reset_available']
    assert 'Update B' in operator.view()['message']


def test_scene_waits_for_telemetry_after_reliable_hold_reply(operator):
    reach_neutral(operator)
    state = copy.deepcopy(operator.robot.state)
    state.update(generation=operator.robot.generation-1, armed=True)
    operator._reset_tick(state)
    assert operator.resetting['phase'] == 'scene'
    assert operator.robot.scene_calls == 1
