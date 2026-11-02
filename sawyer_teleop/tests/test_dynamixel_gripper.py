"""Serial substitutions only. No SDK, ROS, USB or physical movement."""
import threading
import time
from types import SimpleNamespace as Obj

import pytest

from sawyer_teleop.dynamixel_gripper import DynamixelGripper, DynamixelTransport, OPEN, CLOSED
from test_bridge import setup_core, target


class Transport:
    def __init__(self, device='fake'):
        self.position = OPEN
        self.goals = []
        self.closed = False
        self.failure = False
        self.threads = []

    def start(self):
        self.threads.append(threading.get_ident())

    def read(self):
        self.threads.append(threading.get_ident())
        if self.failure:
            raise OSError('USB disconnected')
        return self.position

    def goal(self, value):
        self.threads.append(threading.get_ident())
        self.goals.append(value)

    def close(self):
        self.closed = True
        self.threads.append(threading.get_ident())


def wait(predicate):
    deadline = time.monotonic()+2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.005)


def test_worker_preempts_deduplicates_holds_and_never_disables_torque():
    transport = Transport()
    g = DynamixelGripper('fake', transport_factory=lambda _: transport)
    try:
        assert transport.goals == [OPEN]  # initial hold only
        g.command(1)
        wait(lambda: transport.goals[-1] == CLOSED)
        for _ in range(20):
            g.command(1)
        assert transport.goals.count(CLOSED) == 1
        transport.position = 2400
        g.command(-1)
        wait(lambda: transport.goals[-1] == OPEN)
        g.hold()
        wait(lambda: transport.goals[-1] == 2400)
        assert g.snapshot()['state'] == 'held'
        assert g.snapshot()['position_ticks'] == 2400
        assert 'position_m' not in g.snapshot()
    finally:
        g.close()
    assert transport.closed and transport.goals[-1] == 2400
    assert len(set(transport.threads)) == 1
    assert transport.threads[0] != threading.get_ident()


def test_worker_lease_expires_without_bridge_and_fault_is_latched():
    transport = Transport()
    g = DynamixelGripper('fake', command_timeout=.06, transport_factory=lambda _: transport)
    try:
        g.command(1)
        wait(lambda: transport.goals[-1] == CLOSED)
        transport.position = 2600
        wait(lambda: g.snapshot()['state'] == 'held')
        assert transport.goals[-1] == 2600
        transport.failure = True
        wait(lambda: g.snapshot()['fault'])
        with pytest.raises(RuntimeError, match='USB disconnected'):
            g.command(-1)
    finally:
        g.close()


def test_partial_start_failure_closes_on_worker_and_preserves_original_error():
    transport = Transport()
    def fail():
        raise RuntimeError('mode write failed')
    transport.start = fail
    transport.failure = True
    with pytest.raises(RuntimeError, match='mode write failed'):
        DynamixelGripper('fake', transport_factory=lambda _: transport)
    assert transport.closed


def engine():
    # Exercise the worker state machine at exact sample times without sleeps.
    g = DynamixelGripper.__new__(DynamixelGripper)
    g.clock = lambda: g.now
    g.now = 1.
    g.lock = threading.Lock()
    g.wake, g.stop = threading.Event(), threading.Event()
    g.intent, g.generation, g.lease = None, 0, 0.
    g.applied, g.active, g.submitted = -1, False, 0
    g.fault = None
    g.position = g.captured = g.target = None
    g.command_timeout = .25
    return g, Transport()


@pytest.mark.parametrize('moved', [False, True])
def test_stall_holds_and_distinguishes_no_movement_from_possible_contact(moved):
    g, t = engine()
    g.command(1)
    g._step(t)
    t.position = 2400 if moved else OPEN
    failure = None
    for _ in range(14):
        g.now += .1
        g.command(1)
        try:
            g._step(t)
        except RuntimeError as exc:
            failure = exc
            break
    assert t.goals[-1] == t.position
    if moved:
        assert g.state == 'contact_candidate' and not g.active and failure is None
    else:
        assert 'without_closing_movement' in str(failure)
    assert t.goals.count(CLOSED) == 1


@pytest.mark.parametrize('command,position', [(1, CLOSED), (-1, OPEN)])
def test_empty_close_and_open_reach_without_restarting(command, position):
    g, t = engine()
    g.command(command)
    g._step(t)
    t.position = position
    for _ in range(4):
        g.now += .1
        g.command(command)
        g._step(t)
    assert g.state == 'reached' and not g.active and g.submitted == 1


def test_open_obstruction_times_out_but_slow_close_is_not_contact():
    for command in [-1, 1]:
        g, t = engine()
        t.position = 2400
        g.command(command)
        g._step(t)
        with pytest.raises(RuntimeError, match='motion_timeout'):
            for _ in range(103):
                g.now += .1
                if command == 1:
                    t.position -= 2  # >5 ticks per .6 seconds
                g.command(command)
                g._step(t)
        assert t.goals[-1] == t.position


def test_hold_arriving_during_serial_write_is_next_and_old_close_never_replays():
    g, t = engine()
    original = t.goal
    def interrupted(value):
        original(value)
        if value == CLOSED:
            g.hold()
    t.goal = interrupted
    g.command(1)
    g._step(t)
    t.position = 2800
    g._step(t)
    for _ in range(5):
        g._step(t)
    assert t.goals == [CLOSED, 2800]
    assert g.intent is None and g.state == 'held'


def test_async_fault_in_cached_intera_feedback_disarms_bridge():
    from test_intera_feedback import InteraFeedbackTests
    test = InteraFeedbackTests()
    test.setUp()
    try:
        b = test.b
        b._cuff(test.cuff())
        fault = [None]
        b.gripper = Obj(snapshot=lambda: dict(supported=True, valid=fault[0] is None, fault=fault[0]))
        _, backend, core = setup_core()
        target(core, gripper=1)
        core.tick()
        assert backend.gripper_active
        fault[0] = 'gripper_fault: USB disconnected'
        sample = b.snapshot()
        assert not sample['valid'] and sample['fault'] == fault[0]
        original = backend.snapshot
        backend.snapshot = lambda: dict(original(), valid=sample['valid'], fault=sample['fault'])
        core.tick()
        assert not core.armed and not backend.gripper_active
    finally:
        test.doCleanups()


def test_sdk_initialization_holds_before_torque_and_signed_ticks(monkeypatch):
    import sys
    writes = []
    port = Obj(openPort=lambda: True, setBaudRate=lambda _: True,
               setPacketTimeout=lambda _: None, packet_timeout=200,
               setPacketTimeoutMillis=lambda _: None, ser=Obj(), closePort=lambda: None)
    packet = Obj(read4ByteTxRx=lambda *_: (0xFFFFFFFE, 0, 0))
    for size in [1, 2, 4]:
        setattr(packet, 'write%dByteTxRx' % size,
                lambda p, ident, address, value: (writes.append((address, value)) or (0, 0)))
    monkeypatch.setitem(sys.modules, 'dynamixel_sdk', Obj(PortHandler=lambda _: port, PacketHandler=lambda _: packet))
    t = DynamixelTransport('fake')
    t.start()
    assert writes == [(64, 0), (11, 4), (36, 300), (112, 80), (116, 0xFFFFFFFE), (64, 1)]
    assert t.read() == -2 and port.ser.write_timeout == .05


def test_blocked_serial_does_not_block_hold_and_feedback_becomes_invalid():
    t = Transport()
    g = DynamixelGripper('fake', transport_factory=lambda _: t)
    entered, release = threading.Event(), threading.Event()
    original = t.read
    def blocked():
        entered.set()
        assert release.wait(2)
        return original()
    try:
        t.read = blocked
        assert entered.wait(1)
        started = time.monotonic()
        g.command(1)
        g.hold()
        assert time.monotonic()-started < .1
        time.sleep(.16)
        assert g.snapshot()['fault'] == 'gripper_feedback_stale'
        release.set()
        wait(lambda: g.snapshot()['valid'])
        assert CLOSED not in t.goals
    finally:
        release.set()
        g.close()


@pytest.mark.parametrize('mode', ['standard', 'native10', 'native20', 'native50', 'native100', 'direct'])
def test_all_motion_submission_paths_share_gripper_and_release_hold(mode):
    if mode == 'direct':
        from test_direct_pose_50hz import setup, target as submit
        _, backend, core = setup()
    elif mode.startswith('native'):
        from test_intera_cartesian import setup_native
        _, backend, core = setup_native(int(mode[6:]))
        submit = target
    else:
        _, backend, core = setup_core()
        submit = target
    g, t = engine()
    backend.publish_gripper, backend.hold_gripper = g.command, g.hold
    submit(core, gripper=1)
    core.tick()
    if mode.startswith('native'):
        backend.cartesian.finish()
        core.tick()
    g._step(t)
    assert t.goals[-1] == CLOSED
    t.position = 2300
    core.request(dict(type='hold', session=core.session))
    g._step(t)
    assert g.state == 'held' and t.goals[-1] == 2300
    core.disconnect(core.session)
    core.connect()
    core.tick()
    g._step(t)
    assert t.goals.count(CLOSED) == 1


def test_shutdown_stops_native_arm_before_waiting_for_gripper_worker():
    from sawyer_teleop.intera_backend import InteraBackend
    b = InteraBackend.__new__(InteraBackend)
    order = []
    b.cartesian = Obj(command_sent=True, stop=lambda: order.append('arm_stop'))
    b.gripper = Obj(close=lambda: order.append('gripper_join'), snapshot=lambda: {'fault': None})
    b.ik, b.publisher, b.subscribers = None, None, []
    b.close()
    assert order == ['arm_stop', 'gripper_join']
