"""Native execution contracts with SDK doubles only: never imports ROS or moves hardware."""
from concurrent.futures import Future
from copy import deepcopy
from types import SimpleNamespace as Obj
import threading

import pytest

from sawyer_teleop.intera_cartesian import InteraCartesian, cartesian_options, MODE, SEND_HZ_CHOICES
from sawyer_teleop.protocol import JOINTS, ProtocolError
from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from test_bridge import Clock, target


class Trajectory:
    def __init__(self):
        self.waypoints, self.sent, self.stops = [], [], 0
        self.state, self.result = 1, Obj(result=True, errorId='')
    def clear_waypoints(self): self.waypoints.clear()
    def append_waypoint(self, msg): self.waypoints.append(deepcopy(msg))
    def send_trajectory(self, *, wait_for_result):
        assert wait_for_result is False
        self.sent.append(deepcopy(self.waypoints))
        return True
    def stop_trajectory(self, *, wait_for_result):
        assert wait_for_result is False
        self.stops += 1
        self.state = 1
        return True
    def get_state(self): return self.state
    def wait_for_result(self, timeout):
        assert self.state == 3  # Never block on a running goal.
        return self.result


class Waypoint:
    def set_joint_angles(self, *, joint_angles, active_endpoint, perform_fk):
        assert perform_fk is True
        self.msg = Obj(joint_positions=list(joint_angles), active_endpoint=active_endpoint,
            pose=Obj(header=Obj(frame_id='base'), pose=Obj(
                position=Obj(x=.5, y=.1, z=.3), orientation=Obj(x=0., y=0., z=0., w=1.))))
    def to_msg(self): return deepcopy(self.msg)


def test_sdk_fk_cartesian_message_and_reuse_without_waypoint_accumulation():
    traj = Trajectory()
    executor = InteraCartesian(traj, Waypoint(), 'right_hand')
    for q in ([.1]*7, [.2]*7):
        msg = executor.prepare(q).result(1)
        assert not traj.sent or len(traj.sent) == 1
        executor.send(msg)
        assert len(traj.sent[-1]) == 1
        assert traj.sent[-1][0].joint_positions == q
    assert len(traj.sent) == 2 and not traj.stops
    assert executor.status() == 'running'
    traj.state = 3
    assert executor.status() == 'succeeded'
    executor.stop()
    assert executor.status() == 'running'
    traj.state = 3
    assert executor.status() == 'stopped'


@pytest.mark.parametrize('state', [2, 4, 5, 8, 9])
def test_action_failure_is_observed(state):
    traj = Trajectory()
    executor = InteraCartesian(traj, Waypoint(), 'right_hand')
    executor.send(executor.prepare([0]*7).result(1))
    traj.state = state
    with pytest.raises(RuntimeError, match='action_state'):
        executor.status()


def test_terminal_result_failure_is_not_success():
    traj = Trajectory()
    executor = InteraCartesian(traj, Waypoint(), 'right_hand')
    executor.command_sent = True
    traj.state, traj.result = 3, Obj(result=False, errorId='PLANNING_FAILED')
    with pytest.raises(RuntimeError, match='PLANNING_FAILED'):
        executor.status()


def test_fk_failure_and_zero_quaternion_never_send():
    class Bad(Waypoint):
        def to_msg(self):
            msg = super().to_msg()
            msg.pose.pose.orientation.w = 0.
            return msg
    traj = Trajectory()
    executor = InteraCartesian(traj, Bad(), 'right_hand')
    with pytest.raises(ValueError): executor.prepare([0]*7).result(1)
    assert not traj.sent


def test_only_one_fk_worker_and_worker_never_sends_or_stops():
    entered, release = threading.Event(), threading.Event()
    class Slow(Waypoint):
        def set_joint_angles(self, **kw):
            entered.set()
            assert release.wait(2)
            super().set_joint_angles(**kw)
    traj = Trajectory()
    executor = InteraCartesian(traj, Slow(), 'right_hand')
    future = executor.prepare([.2]*7)
    try:
        assert entered.wait(1) and executor.busy
        with pytest.raises(RuntimeError, match='busy'): executor.prepare([.3]*7)
        executor.stop()  # Does not wait for FK.
        assert traj.stops == 1
    finally:
        release.set()
    future.result(1)
    assert not traj.sent


@pytest.mark.parametrize('options', [{'send_hz': 0}, {'send_hz': True}, {'send_hz': 101},
    {'max_linear_speed': float('nan')}, {'max_joint_speed_ratio': 2}, {'unknown': 1}, []])
def test_invalid_options_rejected(options):
    with pytest.raises(ValueError): cartesian_options(options)


class Native:
    """Manual FK timing and controller acknowledgement, no physical simulation."""
    period_ns = 100_000_000
    def __init__(self):
        self.jobs, self.sent, self.stops = [], [], 0
        self.state = 'idle'
        self.options = cartesian_options()
    @property
    def busy(self): return bool(self.jobs and not self.jobs[-1][0].done())
    def prepare(self, q):
        f = Future()
        self.jobs.append((f, list(q)))
        return f
    def finish(self):
        f, q = self.jobs[-1]
        f.set_result(list(q))
    def send(self, waypoint):
        self.sent.append(list(waypoint))
        self.state = 'running'
    def stop(self):
        self.stops += 1
        self.state = 'running'
    def status(self):
        if isinstance(self.state, Exception): raise self.state
        return self.state


def setup_native(send_hz=None):
    clock = Clock()
    backend = FakeBackend(clock)
    backend.name = 'intera'
    backend.cartesian = Native()
    backend.cartesian_capability = dict(available=True, schema=1, options=cartesian_options(),
                                        send_hz_choices=list(SEND_HZ_CHOICES))
    core = BridgeCore(backend, clock=clock)
    hello = core.connect()
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'], execution_mode=MODE,
                      **({'cartesian_send_hz': send_hz} if send_hz is not None else {})))
    return clock, backend, core


def send_one(clock, backend, core, seq=1, q=None):
    target(core, seq, q or [.1]*7)
    assert core.tick()[0] is None
    backend.cartesian.finish()
    clock.advance(.001)
    receipt, _ = core.tick()
    assert receipt is not None
    return receipt


def test_native_mode_bypasses_joint_planner_and_latest_target_only():
    clock, backend, core = setup_native()
    target(core, 1, [.1]*7)
    target(core, 2, [.2]*7)
    core.tick()
    assert backend.cartesian.jobs[-1][1] == [.2]*7
    backend.cartesian.finish()
    receipt, _ = core.tick()
    assert receipt['seq'] == 2 and receipt['q'] == [.2]*7
    assert receipt['native_cartesian']['command_kind'] == 'cartesian_goal'
    assert 'trajectory' not in receipt and 'execution_seq' not in receipt
    assert core.trajectory is None and backend.publish_count == 0


def test_rate_limit_keeps_only_latest_and_no_repeated_send_without_input():
    clock, backend, core = setup_native()
    send_one(clock, backend, core)
    for seq in range(2, 7):
        clock.advance(.019)
        core._refresh()
        target(core, seq, [seq/100]*7)
        core.tick()
    assert len(backend.cartesian.sent) == 1
    clock.advance(.005)
    core.tick()
    assert backend.cartesian.jobs[-1][1] == [.06]*7
    backend.cartesian.finish()
    core.tick()
    assert backend.cartesian.sent == [[.1]*7, [.06]*7]
    core.tick()
    assert len(backend.cartesian.sent) == 2


@pytest.mark.parametrize('cause', ['hold', 'disconnect', 'stale', 'timeout'])
def test_late_fk_after_stop_never_sends(cause):
    clock, backend, core = setup_native()
    target(core, q=[.3]*7)
    core.tick()
    if cause == 'hold': core.request(dict(type='hold', session=core.session))
    elif cause == 'disconnect': core.disconnect(core.session)
    elif cause == 'stale':
        backend.frozen = True
        clock.advance(.11)
        core.tick()
    else:
        clock.advance(.21)
        core.tick()
    assert backend.cartesian.stops == 1 and not core.armed
    backend.cartesian.finish()
    core.tick()
    assert not backend.cartesian.sent and backend.publish_count == 0


def test_inflight_fk_can_complete_and_newest_pending_target_is_retained():
    clock, backend, core = setup_native()
    target(core, 1, [.1]*7)
    core.tick()
    target(core, 2, [.2]*7)
    target(core, 3, [.3]*7)
    backend.cartesian.finish()
    receipt, _ = core.tick()
    assert receipt['seq'] == 1 and core.pending['seq'] == 3
    clock.advance(.1)
    core.tick()
    assert backend.cartesian.jobs[-1][1] == [.3]*7
    backend.cartesian.finish()
    receipt, _ = core.tick()
    assert receipt['seq'] == 3
    assert backend.cartesian.sent == [[.1]*7, [.3]*7]


def test_stop_must_be_acknowledged_before_any_mode_can_arm():
    clock, backend, core = setup_native()
    send_one(clock, backend, core)
    reply = core.request(dict(type='hold', session=core.session))
    assert not reply['holding'] and core.native_stopping
    for mode in ('joint', MODE):
        with pytest.raises(ProtocolError, match='stop_unconfirmed'):
            core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'], execution_mode=mode))
    backend.cartesian.state = 'stopped'
    core.tick()
    assert core.telemetry()['holding'] and not core.native_stopping
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq']))
    assert core.execution_mode == 'joint'
    target(core, 2, [.2]*7)
    core.tick()
    assert backend.publish_count == 1


def test_stop_timeout_is_latched_and_never_falls_back_to_position():
    clock, backend, core = setup_native()
    core.request(dict(type='hold', session=core.session))
    clock.advance(2.01)
    core.tick()
    assert core.native_stop_failed and not core.telemetry()['holding']
    assert backend.publish_count == 0
    with pytest.raises(ProtocolError, match='stop_unconfirmed'):
        core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq']))


def test_original_goal_reference_expires_even_with_fresh_robot_feedback():
    clock, backend, core = setup_native()
    send_one(clock, backend, core)
    clock.advance(.07)
    core.tick()
    clock.advance(.051)
    core.tick()
    assert not core.armed and backend.cartesian.stops == 1
    assert core.pause_details['native_error'] == 'feedback_reference_expired'


def test_fk_timeout_does_not_occupy_control_thread():
    clock, backend, core = setup_native()
    target(core)
    core.tick()
    clock.advance(.06)
    core.tick()
    clock.advance(.045)
    core.tick()
    assert not core.armed and backend.cartesian.stops == 1
    assert core.pause_details['native_error'] == 'intera_cartesian_fk_timeout'


def test_action_failure_stops_before_sending_another_goal():
    clock, backend, core = setup_native()
    send_one(clock, backend, core)
    backend.cartesian.state = RuntimeError('planner_failed')
    target(core, 2)
    core.tick()
    assert not core.armed and len(backend.cartesian.sent) == 1
    assert backend.cartesian.stops == 1


@pytest.mark.parametrize('backend_name', ['gazebo', 'fake', 'intera'])
def test_missing_native_capability_cannot_be_forced_by_client(backend_name):
    clock = Clock()
    backend = FakeBackend(clock)
    backend.name = backend_name
    core = BridgeCore(backend, clock=clock)
    hello = core.connect()
    assert not hello['intera_cartesian']['available']
    with pytest.raises(ProtocolError, match='unavailable'):
        core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'], execution_mode=MODE))
    assert not core.armed and backend.publish_count == 0


def test_cannot_stack_native_and_existing_trajectory():
    _, _, core = setup_native()
    core.armed = False
    with pytest.raises(ProtocolError, match='cannot_combine'):
        core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'],
                          execution_mode=MODE, trajectory={'enabled': True}))


def test_discovery_configures_cartesian_without_sending(monkeypatch):
    import sys
    from sawyer_teleop.intera_cartesian import discover_cartesian
    pubs, subs, types = [], [], []
    for suffix, kind in (('goal', 'intera_motion_msgs/MotionCommandActionGoal'),
                         ('cancel', 'actionlib_msgs/GoalID'),
                         ('status', 'intera_motion_msgs/MotionCommandActionStatus'),
                         ('feedback', 'intera_motion_msgs/MotionCommandActionFeedback'),
                         ('result', 'intera_motion_msgs/MotionCommandActionResult')):
        # ROS uses GoalStatusArray for status, not an Intera-specific type.
        if suffix == 'status': kind = 'actionlib_msgs/GoalStatusArray'
        topic = '/motion/motion_command/'+suffix
        (subs if suffix in ('goal', 'cancel') else pubs).append((topic, ['native']))
        types.append((topic, kind))
    master = Obj(getSystemState=lambda: (pubs, subs, []), getTopicTypes=lambda: types)
    monkeypatch.setitem(sys.modules, 'rosgraph', Obj(Master=lambda _: master))
    made = []
    traj = Trajectory()
    def make_trajectory(**kw):
        made.append(kw)
        return traj
    class Options:
        CARTESIAN = 'CARTESIAN'
    monkeypatch.setitem(sys.modules, 'intera_motion_msgs.msg', Obj(TrajectoryOptions=Options))
    monkeypatch.setitem(sys.modules, 'intera_motion_interface', Obj(MotionTrajectory=make_trajectory,
        MotionWaypoint=lambda **kw: Waypoint(), MotionWaypointOptions=lambda **kw: Obj(to_msg=lambda: kw)))
    monkeypatch.setitem(sys.modules, 'intera_interface', Obj(Limb=lambda: Obj(joint_names=lambda: JOINTS)))
    ros = Obj(get_name=lambda: 'test', get_param=lambda key, default: default, is_shutdown=lambda: False)
    executor, cap = discover_cartesian(ros, 'right_hand')
    assert cap['available'] and cap['schema'] == 1
    assert cap['send_hz_choices'] == list(SEND_HZ_CHOICES)
    assert made[0]['trajectory_options'].interpolation_type == 'CARTESIAN'
    assert made[0]['trajectory_options'].nso_start_offset_allowed is False
    assert not traj.sent and not traj.stops and executor is not None
    subs.clear()
    executor, cap = discover_cartesian(ros, 'right_hand')
    assert executor is None and not cap['available'] and len(made) == 1


def test_client_explicit_native_arm_has_no_joint_planner():
    from sawyer_teleop.client import RobotClient
    client = RobotClient.__new__(RobotClient)
    client.local_ik = None
    client.timeout = .1
    client.hello = dict(backend='intera', intera_cartesian=dict(available=True, schema=1))
    state = dict(sample=dict(seq=42, q=[0.]*7), generation=1, armed=False)
    client.snapshot = lambda: dict(state)
    requests = []
    def request(kind, **kw):
        requests.append((kind, kw))
        state.update(generation=2, armed=True)
        return dict(generation=2)
    client.request = request
    assert client.arm(execution_mode=MODE) == 2
    assert requests == [('arm', dict(feedback_seq=42, execution_mode=MODE))]
    with pytest.raises(ProtocolError): client.arm(execution_mode=MODE, trajectory={'enabled': False})
    client.hello['intera_cartesian']['available'] = False
    with pytest.raises(ProtocolError): client.arm(execution_mode=MODE)
    assert len(requests) == 1


@pytest.mark.parametrize('mode, hz', [(MODE, 10), ('intera_pose_50hz', 50)])
def test_gazebo_native_arm_accepts_local_ik_but_real_sawyer_does_not(mode, hz):
    # 2026-10-29: forcing remote IK in Gazebo added ~50 ms and expired every input.
    from sawyer_teleop.client import RobotClient
    client = RobotClient.__new__(RobotClient)
    client.local_ik = object()
    client._check_kinematic_feedback = lambda: None
    client.local_ik = type('IK', (), {'model': {'sha256': 'm'}})()
    client.timeout = .1
    capability = dict(available=True, schema=1, simulated=True, direct_pose_50hz=True, send_hz_choices=[10, 20, 50, 100])
    client.hello = dict(backend='gazebo', intera_cartesian=capability)
    state = dict(sample=dict(seq=7, q=[0.]*7), generation=1, armed=False)
    client.snapshot = lambda: dict(state)
    requests = []
    def request(kind, **kw):
        requests.append((kind, kw))
        state.update(generation=2, armed=True)
        return dict(generation=2)
    client.request = request
    assert client.arm(execution_mode=mode, cartesian_send_hz=hz) == 2
    assert requests[0][1]['execution_mode'] == mode and requests[0][1]['model_sha256'] == 'm'
    client.hello = dict(backend='intera', intera_cartesian=capability)
    with pytest.raises(ProtocolError, match='remote IK'):
        client.arm(execution_mode=mode, cartesian_send_hz=hz)
    assert len(requests) == 1


@pytest.mark.parametrize('field', ['position', 'orientation'])
def test_sdk_zero_sum_pose_cannot_trigger_blocking_fk_inside_send(field):
    class ZeroSum(Waypoint):
        def to_msg(self):
            msg = super().to_msg()
            if field == 'position':
                msg.pose.pose.position = Obj(x=.5, y=-.5, z=0.)
            else:
                msg.pose.pose.orientation = Obj(x=.5, y=-.5, z=.5, w=-.5)
            return msg
    traj = Trajectory()
    executor = InteraCartesian(traj, ZeroSum(), 'right_hand')
    with pytest.raises(ValueError, match='sdk_zero_sum_pose'):
        executor.prepare([.1]*7).result(1)
    assert not traj.sent


@pytest.mark.parametrize('send_hz', SEND_HZ_CHOICES)
def test_loopback_client_bridge_sdk_contract_without_ros_or_motion(send_hz):
    import time
    from sawyer_teleop.client import RobotClient
    from sawyer_teleop.server import BridgeServer
    class AckStop(Trajectory):
        def stop_trajectory(self, **kw):
            super().stop_trajectory(**kw)
            self.state = 3
            return True
    backend = FakeBackend()
    backend.name = 'intera'
    traj = AckStop()
    backend.cartesian = InteraCartesian(traj, Waypoint(), 'right_hand')
    backend.cartesian_capability = dict(available=True, schema=1, options=cartesian_options(),
                                        send_hz_choices=list(SEND_HZ_CHOICES))
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, server.ik_port, expected_backend='intera')
    try:
        client.connect()
        client.arm(execution_mode=MODE, cartesian_send_hz=send_hz)
        assert client.snapshot()["native_send_hz"] == send_hz
        sample = client.snapshot()['sample']
        seq = client.target([.2]*7, sample['seq'])
        receipts = []
        deadline = time.monotonic()+1
        while time.monotonic() < deadline and not receipts:
            receipts.extend(client.drain_receipts())
            time.sleep(.002)
        assert len(receipts) == 1 and receipts[0]['seq'] == seq
        assert receipts[0]['native_cartesian']['command_kind'] == 'cartesian_goal'
        assert receipts[0]['native_cartesian']['send_hz'] == send_hz
        assert receipts[0]['native_cartesian']['submit_interval_ms'] is None
        assert traj.sent[0][0].joint_positions == [.2]*7
        assert not client.hold()['holding']
        deadline = time.monotonic()+1
        while time.monotonic() < deadline and not client.snapshot()['holding']:
            time.sleep(.002)
        assert client.snapshot()['holding']
        assert traj.stops == 1 and backend.publish_count == 0
        client.arm()  # A confirmed stop allows a deliberate switch to Standard.
        assert server.core.execution_mode == 'joint'
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize('send_hz', SEND_HZ_CHOICES)
def test_continuous_120hz_input_cannot_starve_native_submissions(send_hz):
    clock, backend, core = setup_native(send_hz)
    sequence = 0
    for tick in range(60):
        clock.advance(.01)
        core._refresh()
        # Six input packets per five 100 Hz control ticks: 120 Hz ingress.
        for _ in range(2 if tick % 5 == 0 else 1):
            sequence += 1
            target(core, sequence, [sequence/1000.]*7)
        core.tick()
        if backend.cartesian.busy:
            backend.cartesian.finish()
    assert core.armed
    assert int(.6*send_hz)-1 <= len(backend.cartesian.sent) <= int(.6*send_hz)
    sent = [row[0] for row in backend.cartesian.sent]
    assert sent == sorted(set(sent))
    assert backend.publish_count == 0


@pytest.mark.parametrize('send_hz', [10, 20, 50, 100])
def test_scheduler_does_not_add_fk_poll_to_every_send_period(send_hz):
    clock, backend, core = setup_native(send_hz)
    stamps = []
    for tick in range(100):
        clock.advance(.01)
        core._refresh()
        target(core, tick+1, [(tick+1)/1000.]*7)
        receipt, _ = core.tick()
        if receipt: stamps.append(receipt['b_ns'])
        if backend.cartesian.busy: backend.cartesian.finish()
    assert core.armed
    assert len(stamps) >= send_hz-1
    assert all(b-a == int(1e9/send_hz) for a, b in zip(stamps, stamps[1:]))


@pytest.mark.parametrize('bad', [True, False, 0, 15, 101, '50', None, float('nan'), float('inf')])
def test_b_rejects_invalid_rate_without_arming_or_changing_defaults(bad):
    _, backend, core = setup_native()
    core.armed = False
    generation = core.generation
    with pytest.raises(ProtocolError, match='invalid_send_hz'):
        core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'],
                          execution_mode=MODE, cartesian_send_hz=bad))
    assert not core.armed and core.generation == generation
    assert backend.cartesian.options['send_hz'] == 10 and not backend.cartesian.sent


def test_rate_is_per_engagement_and_joint_or_unsupported_rate_is_not_ignored():
    clock, backend, core = setup_native(100)
    assert core.native_period_ns == 10_000_000
    core.request(dict(type='hold', session=core.session))
    backend.cartesian.state = 'stopped'
    core.tick()
    arm = dict(type='arm', session=core.session, feedback_seq=core.latest['seq'])
    with pytest.raises(ProtocolError, match='requires_native_mode'):
        core.request(dict(arm, cartesian_send_hz=50))
    backend.cartesian_capability['send_hz_choices'] = [10]
    with pytest.raises(ProtocolError, match='unsupported'):
        core.request(dict(arm, execution_mode=MODE, cartesian_send_hz=50))
    # An old A with no per-engagement rate still uses B's configured default.
    core.request(dict(arm, execution_mode=MODE))
    assert core.native_send_hz == backend.cartesian.options['send_hz'] == 10
    assert core.native_period_ns == 100_000_000 and core.last_native_submit_ns is None


def test_client_rate_selection_refuses_old_b_without_sending_arm():
    from sawyer_teleop.client import RobotClient
    client = RobotClient.__new__(RobotClient)
    client.local_ik = None
    client.hello = dict(backend='intera', intera_cartesian=dict(available=True, schema=1))
    client.snapshot = lambda: dict(sample=dict(seq=1))
    calls = []
    client.request = lambda *a, **kw: calls.append((a, kw))
    with pytest.raises(ProtocolError, match='Update B'):
        client.arm(execution_mode=MODE, cartesian_send_hz=100)
    with pytest.raises(ProtocolError, match='requires_native_mode'):
        client.arm(cartesian_send_hz=100)
    assert calls == []


@pytest.mark.parametrize('cause', ['hold', 'disconnect', 'expired'])
def test_ready_early_fk_waits_for_deadline_but_cannot_cross_stop_or_expiry(cause):
    clock, backend, core = setup_native(20)
    send_one(clock, backend, core)
    clock.advance(.04)
    core._refresh()
    target(core, 2, [.2]*7)
    core.tick()
    backend.cartesian.finish()
    assert core.tick()[0] is None  # Prepared, but next submission is not due.
    assert len(backend.cartesian.sent) == 1
    if cause == 'hold': core.request(dict(type='hold', session=core.session))
    elif cause == 'disconnect': core.disconnect(core.session)
    else:
        # Age the original reference without a control-loop gap or frozen latest.
        core.samples[core.native_job[1]['feedback_seq']]['captured_ns'] -= 125_000_000
    clock.advance(.01)
    core.tick()
    assert not core.armed and backend.cartesian.stops == 1
    assert len(backend.cartesian.sent) == 1


def test_slow_fk_does_not_catch_up_in_bursts_or_queue_old_targets():
    clock, backend, core = setup_native(100)
    receipts = []
    for tick in range(60):
        clock.advance(.01)
        core._refresh()
        target(core, tick+1, [(tick+1)/1000.]*7)
        if core.native_job and clock()-core.native_job[3] >= 30_000_000 and backend.cartesian.busy:
            backend.cartesian.finish()
        receipt, _ = core.tick()
        if receipt: receipts.append(receipt)
    assert core.armed and 18 <= len(receipts) <= 20
    assert len({r['seq'] for r in receipts}) == len(receipts)
    assert all(b['b_ns']-a['b_ns'] == 30_000_000 for a,b in zip(receipts, receipts[1:]))
    assert all(r['native_cartesian']['submit_interval_ms'] == 30 for r in receipts[1:])
    assert all(r['native_cartesian']['send_hz'] == 100 for r in receipts)
    assert all(r['native_cartesian']['status_before_send'] == 'running' for r in receipts[1:])
    assert all(r['native_cartesian']['reference_age_ms'] <= 120 for r in receipts)
    assert backend.publish_count == 0


def test_submitted_receipt_survives_next_pipeline_prepare_failure():
    clock, backend, core = setup_native(100)
    target(core, 1, [.1]*7)
    core.tick()
    backend.cartesian.finish()
    target(core, 2, [.2]*7)
    def fail(q): raise RuntimeError('prepare_failed')
    backend.cartesian.prepare = fail
    receipt, _ = core.tick()
    assert receipt['seq'] == 1 and backend.cartesian.sent == [[.1]*7]
    assert not core.armed and backend.cartesian.stops == 1


@pytest.mark.parametrize('send_hz', SEND_HZ_CHOICES)
def test_small_control_jitter_does_not_halve_requested_cadence(send_hz):
    clock, backend, core = setup_native(send_hz)
    origin = clock()
    receipts = []
    for tick in range(1, 201):
        # Non-uniform +/-0.2 ms wakeups around the 100 Hz schedule.
        clock.value = origin+tick*10_000_000+(200_000 if tick%2 else -200_000)
        core._refresh()
        target(core, tick, [tick/10000.]*7)
        receipt, _ = core.tick()
        if receipt: receipts.append(receipt)
        if backend.cartesian.busy: backend.cartesian.finish()
    assert core.armed
    assert 2*send_hz-1 <= len(receipts) <= 2*send_hz
    assert all(abs((b['b_ns']-a['b_ns'])/1e6-1000/send_hz) <= .401
               for a,b in zip(receipts, receipts[1:]))


def test_missed_native_slots_are_skipped_without_catchup():
    clock, backend, core = setup_native(50)
    send_one(clock, backend, core)
    clock.advance(.01)
    core._refresh()
    target(core, 2, [.2]*7)
    core.tick()
    # Delayed FK/control: several nominal slots pass while there is one job.
    clock.advance(.06)
    core._refresh()
    target(core, 3, [.3]*7)
    backend.cartesian.finish()
    receipt, _ = core.tick()
    assert receipt['seq'] == 2 and core.armed
    assert core.next_native_ns > clock()+5_000_000
    if backend.cartesian.busy: backend.cartesian.finish()
    assert core.tick()[0] is None
    clock.advance(.004)
    assert core.tick()[0] is None
    assert len(backend.cartesian.sent) == 2


def test_rate_capability_matches_b_control_frequency_and_is_checked_again_on_arm():
    clock, backend, _ = setup_native()
    core = BridgeCore(backend, clock=clock, control_hz=20)
    hello = core.connect()
    assert hello['intera_cartesian']['send_hz_choices'] == [10, 20]
    assert backend.cartesian_capability['send_hz_choices'] == [10, 20, 50, 100]
    with pytest.raises(ProtocolError, match='exceeds_control_hz'):
        core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'],
                          execution_mode=MODE, cartesian_send_hz=100))
    assert not core.armed
