"""Neutral return uses named joints, bounded streaming, and explicit cancellation."""
import copy
import time

import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.client import RobotClient
from sawyer_teleop.demo import SyntheticQuest, wait_until
from sawyer_teleop.neutral import NEUTRAL_PARAM, NEUTRAL_TIMEOUT_S, read_neutral_pose, neutral_target
from sawyer_teleop.operator import Operator
from sawyer_teleop.protocol import JOINTS, ProtocolError, ns
from sawyer_teleop.server import BridgeServer


def test_official_pose_reordered_by_joint_name_without_changing_angles():
    angles = [0.55823046875, -0.38636328125, -1.474220703125, 1.7777822265625, 1.219943359375, 1.4652578125, 3.3439736328125]
    params = {NEUTRAL_PARAM: angles[::-1], '/named_poses/right/joint_names': JOINTS[::-1]}
    capability = read_neutral_pose(params.get)
    assert capability['source'] == NEUTRAL_PARAM
    assert neutral_target(capability) == angles
    assert angles[-1] > 3.14  # This bounded joint's named pose must not wrap to -pi.


@pytest.mark.parametrize('params', [
    {}, {NEUTRAL_PARAM: [0.]*6}, {NEUTRAL_PARAM: [float('nan')]*7},
    {NEUTRAL_PARAM: [True]*7},
    {NEUTRAL_PARAM: [0.]*7, '/named_poses/right/joint_names': ['right_j0']*7},
])
def test_missing_or_corrupt_official_pose_disables_reset_without_fallback(params):
    capability = read_neutral_pose(params.get)
    assert not capability['available'] and 'reason' in capability and 'q' not in capability
    with pytest.raises(ValueError):
        neutral_target(capability)


@pytest.mark.parametrize('capability', [None, {}, {'available':False},
    {'available':True, 'joint_names':JOINTS[::-1], 'q':[0.]*7},
    {'available':True, 'joint_names':JOINTS, 'q':[float('inf')]*7}])
def test_old_or_malformed_capability_cannot_authorize_motion(capability):
    with pytest.raises(ValueError):
        neutral_target(capability)


class Robot:
    """Deterministic control peer for cancellation and fault interleavings."""
    def __init__(self):
        self.hello = {'neutral_pose':copy.deepcopy(FakeBackend.neutral_pose), 'backend':'test'}
        self.generation = 1
        self.rtt_ms = 0
        self.state = {'generation':1, 'armed':False, 'age_s':0, 'sample':{
            'q':[0.]*7, 'dq':[0.]*7, 'seq':1}}
        self.targets, self.holds, self.arms = [], [], []
        self.stale = False

    def snapshot(self, require_fresh=True):
        if require_fresh and self.stale:
            raise RuntimeError('feedback_stale')
        return copy.deepcopy(self.state)

    def gripper_observation(self, trigger): pass
    def drain_receipts(self): return []

    def arm(self, **options):
        self.arms.append(options)
        self.generation += 1
        self.state.update(generation=self.generation, armed=True)

    def hold(self, reason):
        self.holds.append(reason)
        self.generation += 1
        self.state.update(generation=self.generation, armed=False)
        return {'holding':True, 'reason':reason}

    def target(self, q, feedback_seq, **kwargs):
        self.targets.append((list(q), feedback_seq, kwargs))


@pytest.fixture
def manual(tmp_path):
    op = Operator(Robot(), SyntheticQuest(), [], tmp_path, hz=120,
                  trajectory={'enabled':False, 'profile':'responsive',
                              'max_velocity_rad_s':[.2,.3,.4,.5,.6,.7,.8]})
    yield op
    op.executor.shutdown(wait=True, cancel_futures=True)


def test_reset_without_tracking_or_calibration_uses_coordinated_limits(manual, tmp_path):
    op = manual
    op.quest.valid = False
    op.request('reset_arm')
    op.tick()
    assert op.resetting and not op.following and op.mapping is None
    options = op.robot.arms[0]['trajectory']
    assert options['enabled'] and options['profile'] == 'cubic'
    assert options['max_velocity_rad_s'] == [v*.25 for v in op.trajectory_options['max_velocity_rad_s']]
    assert options['max_acceleration_rad_s2'] == [a*.25 for a in op.trajectory_options['max_acceleration_rad_s2']]
    assert op.robot.targets[0][0] == FakeBackend.neutral_pose['q']
    assert op.future is None and not list(tmp_path.iterdir())


def test_stop_preempts_full_queue_and_pending_start(manual):
    op = manual
    op.request('reset_arm')
    for _ in range(op.requests.maxlen-1):
        op.request('calibrate')
    op.request('stop_reset')
    op.tick()
    assert not op.robot.arms and not op.robot.targets
    assert not op.resetting and 'stopped' in op.view()['message']


def test_stop_arriving_during_arm_reply_prevents_first_target(manual):
    op = manual
    original = op.robot.arm
    def arm(**options):
        original(**options)
        op.request('stop_reset')
    op.robot.arm = arm
    op.request('reset_arm')
    op.tick()
    assert not op.robot.targets and not op.resetting and not op.robot.state['armed']
    assert len(op.robot.holds) == 1


@pytest.mark.parametrize('error', [ProtocolError('arm_cancelled'), TimeoutError('arm_feedback_timeout')])
def test_failed_arm_cancels_reset_even_after_peer_has_armed(manual, error):
    op = manual
    original = op.robot.arm
    def arm(**options):
        original(**options)
        raise error
    op.robot.arm = arm
    op.request('reset_arm')
    op.tick()
    assert not op.resetting and not op.robot.targets and not op.robot.state['armed']
    assert op.view()['message_level'] == 'error'
    assert str(error) in op.view()['message']


@pytest.mark.parametrize('busy', ['episode','review','finishing','auto_recording'])
def test_recording_and_pending_save_block_reset_before_any_motion(manual, busy):
    setattr(manual, busy, True)
    with pytest.raises(ValueError, match='recording batch'):
        manual._start_reset()
    assert not manual.robot.arms and not manual.robot.holds


def test_recording_cannot_start_during_reset(manual, tmp_path):
    manual._start_reset()
    with pytest.raises(RuntimeError, match='Stop arm reset'):
        manual._start_recording({})
    assert manual.resetting and not list(tmp_path.iterdir())


@pytest.mark.parametrize('fault', ['feedback_stale','generation','timeout'])
def test_fault_aborts_remaining_motion_and_does_not_resume_with_held_grip(manual, fault):
    op = manual
    op.quest.grip = 1
    op.request('reset_arm')
    op.tick()
    if fault == 'feedback_stale':
        op.robot.stale = True
    elif fault == 'generation':
        op.robot.state.update(generation=op.robot.generation+1, armed=False, reason='trajectory_following_error')
    else:
        op.resetting['started_ns'] -= int((NEUTRAL_TIMEOUT_S+1)*1e9)
    op.tick()
    assert op.resetting is None and not op.following
    if fault == 'generation':
        assert not op.robot.holds  # Preserve B's first stop cause.
        assert 'Joint following error exceeded its limit' in op.view()['message']
    else:
        assert op.robot.holds
    count = len(op.robot.targets)
    op.robot.stale = False
    op.tick()
    assert not op.following and len(op.robot.targets) == count


def test_completion_requires_new_stationary_feedback_and_release_before_reengage(manual):
    op = manual
    op.epoch = op.quest.poll()['epoch']
    op.quest.grip = 1
    op.request('reset_arm')
    op.tick()
    op.robot.state['sample'].update(q=neutral_target(op.robot.hello['neutral_pose']), dq=[.1]*7)
    op.tick()
    assert op.resetting['settled_ns'] is None
    op.robot.state['sample']['dq'] = [0.]*7
    op.tick()
    op.resetting['settled_ns'] = ns()-110_000_000
    op.tick()
    assert op.resetting  # Repeated copy of the same observation is insufficient.
    op.robot.state['sample']['seq'] += 1
    op.tick()
    assert op.resetting is None and not op.release_seen
    assert 'at neutral' in op.view()['message']
    count = len(op.robot.targets)
    op.tick()
    assert not op.following and not op.release_seen and len(op.robot.targets) == count
    op.quest.grip = 0
    op.tick()
    assert op.release_seen


@pytest.fixture
def live(tmp_path, request):
    backend = FakeBackend()
    backend.neutral_pose = dict(backend.neutral_pose, q=[.07,-.05,.08,-.06,.04,-.03,.09])
    bridge = BridgeServer(backend, control_port=0, ik_port=0).start()
    quest = SyntheticQuest()
    op = Operator(RobotClient('127.0.0.1',bridge.control_port,bridge.ik_port), quest, [], tmp_path,
                  hz=getattr(request, 'param', 120), trajectory={'profile':'responsive','enabled':False})
    receipts = []
    drain = op.robot.drain_receipts
    def collect():
        items = drain()
        receipts.extend(items)
        return items
    op.robot.drain_receipts = collect
    op.start()
    wait_until(lambda:op.view().get('reset_available'))
    yield backend, bridge, quest, op, receipts
    op.close()
    bridge.close()


def test_tcp_reset_reaches_goal_with_bounded_coordinated_references_and_no_recording(live, tmp_path):
    backend, bridge, quest, op, receipts = live
    quest.grip = 1
    op.request('reset_arm')
    wait_until(lambda:op.resetting is not None)
    wait_until(lambda:op.resetting is None and not op.view().get("resetting"))
    assert 'at neutral' in op.view()['message']
    assert backend.q == pytest.approx(backend.neutral_pose['q'], abs=.008726646)
    assert not op.following and not bridge.core.armed and backend.gripper_publish_count == 0
    assert op.future is None and not list(tmp_path.iterdir())
    assert len(receipts) > 10
    for receipt in receipts:
        assert receipt['trajectory']['algorithm'] == 'online_cubic_hermite_v2'
        assert max(abs(v) for v in receipt['trajectory']['dq']) <= .6+1e-9
        assert max(abs(a) for a in receipt['trajectory']['ddq']) <= 1.5+1e-9
        progress = [a/b for a,b in zip(receipt['q'], backend.neutral_pose['q'])]
        assert max(progress)-min(progress) < 1e-8
    count = bridge.core.submitted
    time.sleep(.08)
    assert not op.following and bridge.core.submitted == count


@pytest.mark.parametrize('stop', ['button','disconnect','stale_feedback'])
def test_tcp_cancel_removes_trajectory_and_no_targets_follow(live, stop):
    backend, bridge, quest, op, _ = live
    quest.grip = 1
    op.request('reset_arm')
    wait_until(lambda:bridge.core.submitted > 3)
    if stop == 'button':
        op.request('stop_reset')
    elif stop == 'disconnect':
        op.close()
    else:
        backend.frozen = True
    wait_until(lambda:op.resetting is None and not bridge.core.armed)
    wait_until(lambda:bridge.core.trajectory is None)
    count = bridge.core.submitted
    time.sleep(.1)
    assert bridge.core.submitted == count and not op.following


def test_reset_from_active_teleop_discards_old_ik_and_gripper_control(live):
    backend, bridge, quest, op, _ = live
    op.request('calibrate')
    wait_until(lambda:op.mapping is not None and op.release_seen)
    quest.grip = 1
    quest.trigger = 1
    wait_until(lambda:op.following and backend.gripper_active)
    op.request('reset_arm')
    wait_until(lambda:op.resetting is not None)
    generation = op.robot.generation
    wait_until(lambda:op.resetting is None and not op.view().get("resetting"))
    assert 'at neutral' in op.view()['message']
    assert backend.q == pytest.approx(backend.neutral_pose['q'], abs=.008726646)
    assert not backend.gripper_active and not op.following and not op.release_seen
    assert op.robot.generation > generation
    quest.grip = 0
    wait_until(lambda:op.release_seen)
    quest.grip = 1
    wait_until(lambda:op.following)
    quest.grip = 0
    wait_until(lambda:not op.following and not bridge.core.armed)


@pytest.mark.parametrize('live', [1], indirect=True)
def test_low_ik_rate_does_not_starve_reset_command_lease(live):
    _, bridge, quest, op, _ = live
    quest.grip = 1
    op.request('reset_arm')
    wait_until(lambda:op.resetting is not None)
    wait_until(lambda:op.resetting is None and not op.view().get('resetting'))
    assert 'at neutral' in op.view()['message']
    assert bridge.core.submitted > 10 and not bridge.core.armed


def test_stop_remains_visible_during_reset_preparation_and_failure(manual):
    op = manual
    op.following = True
    hold = op.robot.hold
    def held(reason):
        assert op.view()['resetting']  # GUI must keep Stop during the hold RPC.
        if reason == 'Preparing arm reset':
            op.request('stop_reset')
        return hold(reason)
    op.robot.hold = held
    op.request('reset_arm')
    assert op.view()['resetting']
    op.tick()
    assert not op.robot.targets and not op.view()['resetting']
    op.robot.hello.pop('neutral_pose')
    op.request('reset_arm')
    op.tick()
    assert not op.view()['resetting'] and 'neutral pose' in op.view()['message']


def test_gazebo_neutral_outside_captured_model_is_unavailable_without_wrapping():
    from pathlib import Path
    from sawyer_teleop.neutral import validate_neutral_model
    urdf=(Path(__file__).parent/'fixtures/sideways_lift_1_2_11/robot.urdf').read_text()
    capability=read_neutral_pose({NEUTRAL_PARAM:[0.,-1.18,0.,2.18,0.,.57,3.3161]}.get)
    result=validate_neutral_model(capability,urdf)
    assert not result['available'] and 'right_j6=3.3161' in result['reason']
    assert '3.1400' in result['reason'] and 'q' not in result
    assert capability['q'][-1]==3.3161  # Official pose itself was not modified.
    compatible=dict(capability,q=[0.,-1.18,0.,2.18,0.,.57,0.])
    assert validate_neutral_model(compatible,urdf)==compatible
    assert not validate_neutral_model(capability,'')['available']
