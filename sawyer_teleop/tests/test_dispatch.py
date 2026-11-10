"""Independent feedback leases and completion dispatch with stop priority."""
import threading
import time

import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.client import RobotClient
from sawyer_teleop.demo import wait_until
from sawyer_teleop.operator import Operator
from sawyer_teleop.pause import bridge_pause_message
from sawyer_teleop.protocol import ProtocolError, encode
from sawyer_teleop.server import BridgeServer
from test_bridge import Clock, target
from test_motion import SteppedQuest
from test_trajectory import make_core


def test_source_reference_has_120ms_but_current_feedback_still_has_100ms():
    clock, backend, core = make_core()
    reference = core.latest['seq']
    target(core, 1, [.5]*7)
    for _ in range(12):
        clock.advance(.01)
        receipt, _ = core.tick()
        assert receipt is not None and core.armed
    clock.value += 1
    receipt, state = core.tick()
    assert receipt is None and not core.armed
    assert state['reason'] == 'feedback_reference_expired'
    detail = state['pause_details']
    assert detail['reference_feedback_seq'] == reference
    assert detail['reference_feedback_age_ms'] == pytest.approx(120.000001)
    assert detail['latest_feedback_age_ms'] < .001
    assert detail['source_command_seq'] == 1
    assert detail['thresholds_ms'] == dict(latest_feedback=100., source_reference=120., command=200., control_gap=100.)
    assert detail['holding'] and core.events[-1]['details'] == detail
    count = backend.publish_count
    clock.advance(.05)
    core.tick()
    assert backend.publish_count == count


def test_latest_feedback_freeze_stops_at_100ms_inside_reference_grace():
    clock, backend, core = make_core()
    target(core, 1, [.5]*7)
    core.tick()
    backend.frozen = True
    for _ in range(10):
        clock.advance(.01)
        core.tick()
    assert core.armed
    count = backend.publish_count
    clock.value += 1
    receipt, state = core.tick()
    assert receipt is None and state['reason'] == 'feedback_latest_expired'
    assert not state['holding'] and backend.publish_count == count
    assert state['pause_details']['latest_feedback_age_ms'] == pytest.approx(100.000001)
    assert state['pause_details']['reference_feedback_age_ms'] < 120


def test_reference_timeout_checked_at_receive_and_malformed_reference_stops():
    clock, _, core = make_core()
    reference = core.latest['seq']
    clock.advance(.11)
    target(core, 1, feedback_seq=reference)
    assert core.pending and core.armed
    clock.advance(.011)
    with pytest.raises(ProtocolError, match='feedback_reference_expired'):
        target(core, 2, feedback_seq=reference)
    assert not core.armed and core.pause_details['source_command_seq'] == 2
    assert core.pause_details['reference_feedback_seq'] == reference
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq']))
    with pytest.raises(ProtocolError, match='invalid feedback reference'):
        target(core, 3, feedback_seq=[])
    assert not core.armed


@pytest.mark.parametrize('fault,reason', [('missing','feedback_reference_missing'),
    ('invalid','feedback_latest_invalid'), ('future','feedback_latest_clock_regressed')])
def test_diagnostics_disambiguate_feedback_faults(fault, reason):
    clock, backend, core = make_core()
    if fault == 'missing':
        with pytest.raises(ProtocolError, match=reason): target(core, feedback_seq=99999)
    else:
        clock.advance(.01)
        core._refresh()
        if fault == 'invalid': backend.sample.update(valid=False, fault='robot_collision')
        else: backend.sample['captured_ns'] = clock()+10_000_000
        core.tick()
    assert core.reason == reason
    if fault == 'invalid': assert core.pause_details['latest_feedback_fault'] == 'robot_collision'


def test_release_in_reference_grace_cancels_and_rearm_drops_old_motion():
    clock, backend, core = make_core()
    target(core, 1, [.5]*7)
    for _ in range(11):
        clock.advance(.01); core.tick()
    old = core.generation
    core.request(dict(type='hold', session=core.session, reason='released'))
    held, count = list(backend.target), core.submitted
    clock.advance(.01); core.tick()
    assert backend.target == held and core.submitted == count
    assert not target(core, 2, generation=old)['accepted']
    core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq'], trajectory={}))
    target(core, 3, held)
    receipt,_ = core.tick()
    assert receipt['q'] == held and receipt['trajectory']['dq'] == [0.]*7


def test_detailed_event_history_fits_protocol_frame():
    clock, _, core = make_core()
    for _ in range(80):
        core.request(dict(type='hold', session=core.session, reason='test'))
        core.request(dict(type='arm', session=core.session, feedback_seq=core.latest['seq']))
    assert len(core.events) == 64
    assert len(encode(core.telemetry())) < 65540


@pytest.mark.parametrize('action', ['send','release','tracking','shutdown'])
def test_completion_wakes_owner_before_next_period_and_checks_stop(tmp_path, action):
    # A 100 ms period makes an accidental wait for the next tick observable.
    # Only the first IK is tested, so the normal lease policy is unchanged.
    entered, finish, sent = threading.Event(), threading.Event(), threading.Event()
    backend = FakeBackend()
    original_solve = backend.solve
    def solve(*args):
        entered.set()
        assert finish.wait(1)
        return original_solve(*args)
    backend.solve = solve
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, server.ik_port)
    original_target = client.target
    send_threads = []
    def send(*args, **kwargs):
        send_threads.append(threading.get_ident())
        result = original_target(*args, **kwargs)
        sent.set()
        return result
    client.target = send
    quest = SteppedQuest()
    operator = Operator(client, quest, [], tmp_path, hz=10).start()
    try:
        wait_until(lambda: operator.epoch is not None)
        operator.request('calibrate')
        wait_until(lambda: operator.mapping is not None and operator.release_seen)
        quest.grip = 1
        assert entered.wait(1)
        if action == 'release': quest.grip = 0
        elif action == 'tracking': quest.valid = False
        elif action == 'shutdown': operator.stop.set(); operator.wake.set()
        finish.set()
        if action == 'send':
            assert sent.wait(.06), 'Completed IK waited for the next 100 ms control tick'
            assert send_threads == [operator.thread.ident]
        else:
            wait_until(lambda: not server.core.armed)
            assert not sent.is_set()
    finally:
        finish.set(); operator.close(); server.close()


def test_a_preserves_b_reason_without_second_hold_and_records_details(tmp_path):
    backend=FakeBackend()
    server=BridgeServer(backend, control_port=0, ik_port=0).start()
    quest=SteppedQuest()
    operator=Operator(RobotClient('127.0.0.1',server.control_port,server.ik_port),quest,[],tmp_path).start()
    try:
        wait_until(lambda: operator.epoch is not None)
        operator.request('calibrate')
        wait_until(lambda: operator.mapping is not None and operator.release_seen)
        quest.grip=1
        wait_until(lambda: server.core.submitted > 3)
        with server.core.lock:
            server.core._hold('feedback_reference_expired',details={'reference_feedback_age_ms':121.5})
            count=server.core.event_seq
            generation=server.core.generation
        wait_until(lambda: not operator.following)
        assert server.core.event_seq == count and server.core.generation == generation
        assert 'Source feedback reference expired' in operator.view()['message']
        assert '121.500 ms > 120.000 ms' in operator.view()['message']
        assert server.core.reason == 'feedback_reference_expired'
    finally:
        operator.close();server.close()


def test_solved_old_input_cannot_borrow_current_input_freshness(tmp_path):
    server=BridgeServer(FakeBackend(),control_port=0,ik_port=0).start()
    client=RobotClient('127.0.0.1',server.control_port,server.ik_port).connect()
    quest=SteppedQuest();quest.start()
    operator=Operator(client,quest,[],tmp_path)
    try:
        operator.tick();operator.request('calibrate');operator.tick()
        quest.grip=1;operator.tick()
        wait_until(lambda: operator.future.done())
        operator.future_info['timing']['input_mapped_ns'] -= 200_000_000
        operator.tick(queue_ik=False)
        assert not operator.following and client.command_seq == 0
        assert operator.view()['message'] == 'ik_input_expired'
    finally:
        operator.executor.shutdown(wait=True);client.close();quest.close();server.close()


def test_older_bridge_reason_is_still_understandable():
    assert 'older B' in bridge_pause_message({'reason':'feedback_stale'})


def test_late_hold_after_b_fault_keeps_first_cause_generation_and_context():
    clock, backend, core = make_core()
    backend.fault = 'simulation_source_time_invalid'
    clock.advance(.01);core.tick()
    assert core.reason == 'feedback_latest_invalid'
    first=core.events[-1];generation=core.generation;count=backend.publish_count
    result=core.request(dict(type='hold', session=core.session, reason='generation_expired'))
    assert result['reason']=='feedback_latest_invalid'
    assert result['pause_details']['latest_feedback_fault']=='simulation_source_time_invalid'
    assert core.generation==generation and core.events[-1]==first
    assert backend.publish_count==count


def test_a_uses_first_b_cause_returned_by_hold_even_with_stale_telemetry(tmp_path):
    class Robot:
        def hold(self, reason):
            return dict(holding=False,reason='feedback_latest_invalid',generation=4,
                        pause_details={'latest_feedback_fault':'simulation_source_time_invalid'})
    operator=Operator(Robot(),None,[],tmp_path)
    try:
        operator.following=True
        operator._pause('generation_expired')
        assert 'simulation_source_time_invalid' in operator.view()['message']
        assert 'Latest robot feedback is invalid' in operator.view()['message']
    finally:operator.executor.shutdown(wait=True)


def test_tick_frame_carries_recent_events_only_and_fits_with_real_sized_details():
    from sawyer_teleop.bridge import STATE_EVENT_LIMIT
    clock, _, core = make_core()
    for i in range(64):
        core._event('pause_%d' % i, {'diagnostics': 'x' * 1600})  # real-robot detail size
    _, state = core.tick()
    assert [e['reason'] for e in state['events']] == ['pause_%d' % i for i in range(64-STATE_EVENT_LIMIT, 64)]
    assert len(encode(state)) < 20000
    assert len(core.diagnostics()['events']) == 64  # monitor / journal keep full history
