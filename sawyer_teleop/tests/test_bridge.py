import pytest
from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.protocol import ProtocolError


class Clock:
    value = 1_000_000_000
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += int(seconds*1e9)


def setup_core():
    clock = Clock()
    backend = FakeBackend(clock)
    core = BridgeCore(backend, clock=clock)
    hello = core.connect()
    core.request(dict(type="arm", session=hello["session"], feedback_seq=core.latest["seq"]))
    return clock, backend, core


def target(core, seq=1, q=None, **overrides):
    msg = dict(type="target", session=core.session, generation=core.generation,
               seq=seq, feedback_seq=core.latest["seq"], q=q or [0]*7)
    msg.update(overrides)
    return core.request(msg)


def test_joint_values_unchanged_and_latest_only():
    clock, backend, core = setup_core()
    target(core, 1, [100.]*7)
    target(core, 2, [-200.]*7)
    receipt, _ = core.tick()
    assert backend.target == [-200.]*7
    assert receipt["seq"] == 2
    assert backend.publish_count == 1
    assert target(core, 1)["accepted"] is False


def test_expired_reference_checked_at_submission():
    clock, backend, core = setup_core()
    target(core, 1)
    clock.advance(.121)
    receipt, _ = core.tick()
    assert receipt is None and not core.armed
    assert core.reason == "feedback_reference_expired"


def test_frozen_feedback_never_refreshed_and_not_used_to_hold():
    clock, backend, core = setup_core()
    backend.frozen = True
    clock.advance(.11)
    _, state = core.tick()
    assert not state["armed"] and not state["holding"]
    assert backend.publish_count == 0


def test_command_timeout_uses_local_current_pose_and_requires_rearm():
    clock, backend, core = setup_core()
    backend.target = [0.8]*7
    clock.advance(.21)
    core.tick()
    assert not core.armed
    assert backend.target == [0.8]*7
    assert target(core)["accepted"] is False


def test_session_and_generation_replay():
    _, _, core = setup_core()
    old = core.generation
    core.request(dict(type="hold", session=core.session))
    assert not target(core, generation=old)["accepted"]
    with pytest.raises(ProtocolError):
        target(core, session="old")


def test_disconnect_clears_pending():
    _, backend, core = setup_core()
    target(core, q=[1]*7)
    core.disconnect(core.session)
    assert core.pending is None and core.session is None
    assert backend.target == [0.]*7


def test_transport_close_cause_is_recorded_as_a_b_event(capfd):
    import time
    from sawyer_teleop.client import RobotClient
    from sawyer_teleop.server import BridgeServer
    server = BridgeServer(FakeBackend(), control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, server.ik_port, expected_backend='fake')
    try:
        client.connect()
        server.owner.close('send_failed:timed out')
        deadline = time.monotonic() + 2
        while server.core.session is not None and time.monotonic() < deadline:
            time.sleep(.01)
        reasons = [e['reason'] for e in server.core.events]
        assert 'connection_closed: send_failed:timed out' in reasons
        assert reasons.index('connection_closed: send_failed:timed out') < reasons.index('disconnected')
        assert 'A connection closed: send_failed:timed out' in capfd.readouterr().err
    finally:
        client.close()
        server.close()


def test_b_control_connection_uses_200ms_send_deadline():
    from sawyer_teleop.client import RobotClient
    from sawyer_teleop.server import BridgeServer
    server = BridgeServer(FakeBackend(), control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, server.ik_port, expected_backend='fake')
    try:
        client.connect()
        assert server.owner.sock.gettimeout() == .2
        assert client.connection.sock.gettimeout() == .1
    finally:
        client.close()
        server.close()
