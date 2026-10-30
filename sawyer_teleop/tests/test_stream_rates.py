"""Rate semantics: count successful submissions on B and state arrivals on A."""
import threading
from types import SimpleNamespace as Obj
import pytest
from sawyer_teleop.rates import EventRate
from sawyer_teleop.client import RobotClient
from test_bridge import setup_core, target


def test_b_counts_submissions_not_control_ticks_or_received_targets():
    clock, backend, core = setup_core()
    target(core, 1)
    target(core, 2)
    assert core.telemetry()['submission_rate']['hz'] == 0
    core.tick()
    assert core.telemetry()['submission_rate']['hz'] == 1
    core.tick()
    assert core.telemetry()['submission_rate']['hz'] == 1
    clock.advance(1.01)
    assert core.telemetry()['submission_rate']['hz'] == 0


def test_failed_backend_publish_does_not_increment_rate():
    clock, backend, core = setup_core()
    def fail(q): raise RuntimeError('failed publish')
    backend.publish=fail
    target(core)
    with pytest.raises(RuntimeError): core.tick()
    assert core.submission_rate.snapshot()['total'] == 0


def test_cartesian_counts_successful_goals_not_fk_preparations():
    from test_intera_cartesian import setup_native, send_one
    clock, backend, core=setup_native(50)
    send_one(clock, backend, core)
    assert core.telemetry()['submission_rate']['hz']==1
    assert len(backend.cartesian.sent)==1


def test_direct_pose_submission_is_counted_on_b():
    from test_direct_pose_50hz import setup, target as pose_target
    clock, backend, core=setup()
    pose_target(core)
    core.tick()
    assert core.telemetry()['submission_rate']['hz']==1


def test_a_counts_state_arrivals_separately_from_receipts_and_b_rate(monkeypatch):
    now=[1_000_000_000]
    monkeypatch.setattr('sawyer_teleop.client.ns',lambda:now[0])
    client=RobotClient('unused')
    client.session='s'
    client.feedback_received=EventRate(clock=lambda:now[0]/1e9)
    client.connection=Obj(closed=threading.Event(),target_sent=EventRate(clock=lambda:now[0]/1e9))
    client._receive(dict(type='state',session='s',submission_rate={'hz':50.}))
    client._receive(dict(type='state',session='s',submission_rate={'hz':50.}))
    client._receive(dict(type='submitted',session='s'))
    r=client.rates()
    assert r['feedback_hz']==2 and r['submission_hz']==50 and r['hz']==0
    now[0]+=101_000_000
    assert client.rates()['submission_hz'] is None
    now[0]+=1_000_000_000
    assert client.rates()['feedback_hz']==0
    client._receive(dict(type='state',session='s'))  # Old B never fabricates 0 Hz.
    assert client.rates()['submission_hz'] is None
    client.connection.closed.set()
    assert client.rates()['feedback_hz']==0 and client.rates()['submission_hz'] is None
