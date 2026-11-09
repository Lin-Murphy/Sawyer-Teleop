"""Injected clocks and frames; no device, network, or physical simulation claims."""
import copy
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from sawyer_teleop.client import RobotClient
from sawyer_teleop.feedback_diagnostics import reference_from_snapshot
from sawyer_teleop.operator import Operator
from sawyer_teleop.protocol import JOINTS, VERSION, ProtocolError
from test_ik_schedule import rig, finish
from test_motion import SteppedQuest


@pytest.fixture
def link(monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr('sawyer_teleop.client.ns', lambda: clock[0])
    client = RobotClient('offline', expected_backend='gazebo')
    client.hello = dict(backend='gazebo', state_timeout_s=.1, reference_timeout_s=.12)
    client.connection = SimpleNamespace(closed=threading.Event())
    client.rates = lambda: {'hz': 0., 'total': 0, 'submitted': 0}
    client.offset_ns = 0
    client.uncertainty_ns = 2_000_000
    client.sync_ns = clock[0]
    client.session = 'test'
    frame = dict(type='state', session='test', b_ns=clock[0]-20_000_000, armed=True, generation=0,
                 sample=dict(seq=1, captured_ns=clock[0]-30_000_000, valid=True, q=[0.]*7))
    client._receive(frame)
    return clock, client


def test_age_decomposes_without_calling_transport_network_latency(link):
    clock, client = link
    clock[0] += 5_000_000
    diag = client.feedback_diagnostics()
    assert diag['b_source_age_ms'] == 10
    assert diag['delivery_upper_ms'] == 22
    assert diag['a_residence_ms'] == 5
    assert diag['age_ms'] == 37
    assert diag['reference_remaining_ms'] == 83
    assert client.snapshot()['a_frame_received_ns'] == clock[0]-5_000_000
    assert client.snapshot()['a_received_ns'] == clock[0]


@pytest.mark.parametrize('profile,limit', [('standard', 100), ('gazebo_110ms', 110),
                                        ('gazebo_150ms', 150), ('gazebo_250ms', 250)])
def test_exact_feedback_deadline_includes_uncertainty(link, profile, limit):
    clock, client = link
    client.feedback_profile = profile
    if limit >= 150: client.hello['reference_timeout_s'] = .18
    clock[0] += (limit-32)*1_000_000
    client.snapshot()
    clock[0] += 1
    with pytest.raises(ConnectionError, match='feedback_latest_expired'):
        client.snapshot()
    diag = client.feedback_diagnostics()
    assert diag['connected'] and not diag['fresh']
    assert diag['age_ms'] == pytest.approx(limit+.000001)


@pytest.mark.parametrize('selected,reported', [
    ('intera','intera'), ('fake','fake'), ('gazebo','intera'), ('gazebo','fake'),
    ('intera','gazebo'), (None,'gazebo'), ('gazebo',None)])
@pytest.mark.parametrize('profile', ['gazebo_110ms', 'gazebo_150ms', 'gazebo_250ms'])
def test_experiment_requires_both_gazebo_selection_and_handshake(selected, reported, profile):
    client = RobotClient('offline', expected_backend=selected, feedback_profile=profile)
    client.hello = {'backend': reported, 'reference_timeout_s': .18}
    assert client.feedback_timeout == .1


@pytest.mark.parametrize('reference', [None, .12, .179999, True, '0.18', float('nan'), float('inf')])
@pytest.mark.parametrize('profile', ['gazebo_150ms', 'gazebo_250ms'])
def test_extended_profile_rejects_inadequate_b_handshake_before_starting_connection(monkeypatch, reference, profile):
    class Socket:
        closed = False
        def sendall(self, data): pass
        def shutdown(self, how): pass
        def close(self): self.closed = True
    sock = Socket()
    client = RobotClient('offline', expected_backend='gazebo', feedback_profile=profile)
    assert client.feedback_timeout == .1  # No relaxation before handshake.
    monkeypatch.setattr(client, '_socket', lambda port: sock)
    hello = dict(type='hello', version=VERSION, joints=JOINTS, units='rad', backend='gazebo')
    if reference is not None: hello['reference_timeout_s'] = reference
    monkeypatch.setattr('sawyer_teleop.client.read_one', lambda socket: hello)
    with pytest.raises(ProtocolError, match='requires B reference timeout >= 180 ms'):
        client.connect()
    assert sock.closed and client.connection is None and client.hello is None


@pytest.mark.parametrize('profile,reference_ms', [('gazebo_110ms', 120),
                                               ('gazebo_150ms', 180), ('gazebo_250ms', 180)])
def test_new_sample_and_clock_sync_cannot_renew_old_ik_reference(link, profile, reference_ms):
    clock, client = link
    client.feedback_profile = profile
    client.hello['reference_timeout_s'] = reference_ms / 1000
    reference = reference_from_snapshot(client.snapshot())
    clock[0] += 80_000_000
    newer = copy.deepcopy(client.state)
    newer['sample'].update(seq=2, captured_ns=clock[0])
    newer['b_ns'] = clock[0]
    client._receive(newer)
    assert client.snapshot()['sample']['seq'] == 2
    client.uncertainty_ns = 0
    diag = client.feedback_diagnostics(reference)
    assert diag['reference_seq'] == 1 and diag['reference_remaining_ms'] == reference_ms-112
    assert client.feedback_diagnostics()['reference_remaining_ms'] == reference_ms
    assert diag['arrival_gap_ms'] == 80


@pytest.mark.parametrize('fault', ['disconnected', 'sync', 'source', 'missing', 'future'])
@pytest.mark.parametrize('profile', ['gazebo_110ms', 'gazebo_150ms', 'gazebo_250ms'])
def test_other_gates_are_not_relaxed(link, fault, profile):
    clock, client = link
    client.feedback_profile = profile
    client.hello['reference_timeout_s'] = .18
    if fault == 'disconnected': client.connection.closed.set()
    if fault == 'sync': client.sync_ns -= 10_000_000_001
    if fault == 'source': client.state['sample'].update(valid=False, fault='source_test_fault')
    if fault == 'missing': client.state = None
    if fault == 'future': client.state['sample']['captured_ns'] = clock[0]+10_000_000
    with pytest.raises(ConnectionError): client.snapshot()
    assert not client.feedback_diagnostics()['fresh']


def test_operator_view_preserves_connected_stale_age_and_pause_context(link, tmp_path):
    clock, client = link
    client.hold = lambda reason: {'holding': True, 'reason': reason}
    operator = Operator(client, SteppedQuest(), [], tmp_path)
    try:
        operator.following = True
        clock[0] += 80_000_000
        operator.tick()
        status = operator.view()
        assert not operator.following and not operator.release_seen
        assert status['robot_connected'] and not status['feedback_valid']
        assert status['feedback_age_ms'] == 112
        assert status['joints'] is None and not status['reset_available']
        assert status['last_pause_diagnostics']['age_ms'] == 112
        assert not list(tmp_path.iterdir())
        client.connection.closed.set()
        assert not operator.view()['robot_connected']
    finally:
        operator.executor.shutdown(wait=True)


def test_diagnostics_before_handshake_do_not_break_connection_startup(tmp_path):
    operator = Operator(RobotClient('offline'), SteppedQuest(), [], tmp_path)
    try:
        state = operator.view()
        assert not state['robot_connected']
        assert state['feedback_diagnostics']['fault'] == 'feedback_missing'
        assert not state['feedback_diagnostics']['budget_valid']
    finally:
        operator.executor.shutdown(wait=True)


@pytest.mark.parametrize('release', [False, True])
def test_original_reference_expiry_never_sends_even_after_new_feedback(rig, release):
    clock, robot, quest, operator = rig()
    operator.future_info['reference'] = dict(seq=1, observed_ns=clock[0], age_ns=75_000_000)
    clock[0] += 50_000_000
    quest.stamp += 50_000_000
    finish(operator, clock)
    if release: quest.grip = 0
    operator.tick()
    assert not robot.targets and not operator.following
    assert len(operator.executor.jobs) == 1
    assert operator.view()['message'].startswith('Grip released' if release else 'ik_reference_expired')


def test_reproducible_delay_matrix():
    path = Path(__file__).resolve().parents[1]/'scripts/validate_feedback_delay.py'
    spec = importlib.util.spec_from_file_location('feedback_delay_validation', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = module.run_matrix()
    assert len(first) == 16
    assert first == module.run_matrix()
    expanded = module.run_a150_matrix()
    assert len(expanded) == 14
    assert expanded == module.run_a150_matrix()
    final = module.run_a250_matrix()
    assert len(final) == 16
    assert final == module.run_a250_matrix()


def test_slow_ping_does_not_drop_b_connection(link):
    _, client = link
    sent, closed = [], []

    class Silent:
        closed = threading.Event()
        error = None
        def send(self, msg, clear_latest=False): sent.append(msg)
        def close(self, error=None): closed.append(error)

    client.connection = Silent()
    with pytest.raises(TimeoutError):
        client.sync_clock(2, timeout=.01)
    assert len(sent) == 2 and not closed and client.ping_timeouts == 2
    assert client.feedback_diagnostics()['connected']
    # Other requests keep the fail-closed timeout.
    with pytest.raises(TimeoutError):
        client.request('hold', timeout=.01)
    assert closed == ['request_timeout:hold']


def test_disconnect_reason_reaches_operator_view(link, tmp_path):
    _, client = link
    client.connection.error = 'send_failed:timed out'
    operator = Operator(client, SteppedQuest(), [], tmp_path)
    try:
        assert operator.view()['robot_disconnect_reason'] is None
        client.connection.closed.set()
        status = operator.view()
        assert not status['robot_connected']
        assert status['robot_disconnect_reason'] == 'send_failed:timed out'
    finally:
        operator.executor.shutdown(wait=True)
