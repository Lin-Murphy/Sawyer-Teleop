import threading
import time
import pytest
from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.client import RobotClient
from sawyer_teleop.server import BridgeServer
from sawyer_teleop.protocol import ProtocolError


@pytest.fixture
def link():
    backend = FakeBackend()
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient("127.0.0.1", server.control_port, server.ik_port).connect()
    yield backend, server, client
    client.close()
    server.close()


def test_control_feedback_and_ik(link):
    backend, server, client = link
    client.arm()
    state = client.snapshot()
    q = client.solve({"p": [.7, .2, .4], "q": [0, 0, 0, 1]}, [0]*7, state["sample"]["seq"])
    seq = client.target(q, state["sample"]["seq"])
    # Submission and measured feedback are separate messages. Wait for both;
    # a receipt can arrive before the next backend sample, especially on Windows.
    expected = pytest.approx([.2, .2, .1, 0, 0, 0, 0])
    receipts = []
    observed = state
    deadline = time.monotonic()+.15
    while time.monotonic() < deadline:
        receipts.extend(client.drain_receipts())
        observed = client.snapshot()
        if (receipts and receipts[-1]["seq"] == seq
                and observed["sample"]["seq"] > state["sample"]["seq"]
                and observed["sample"]["q"] == expected):
            break
        time.sleep(.005)
    assert receipts
    assert receipts[-1]["seq"] == seq
    assert observed["sample"]["seq"] > state["sample"]["seq"]
    assert observed["sample"]["q"] == expected
    assert client.hold()["holding"]


def test_slow_ik_does_not_block_feedback_and_late_result_dropped(link):
    backend, server, client = link
    client.arm()
    backend.ik_delay = .15
    state = client.snapshot()
    errors = []
    def solve():
        try:
            client.solve(state["sample"]["pose"], [0]*7, state["sample"]["seq"], timeout=.04)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=solve)
    thread.start()
    time.sleep(.06)
    assert client.snapshot()["sample"]["seq"] > state["sample"]["seq"]
    client.hold()
    thread.join(1)
    assert errors and not server.core.armed
    time.sleep(.12)
    assert backend.publish_count == 1  # Only hold, no IK motion.


def test_frozen_feedback_and_reconnect(link):
    backend, server, client = link
    client.arm()
    backend.frozen = True
    time.sleep(.14)
    with pytest.raises(ConnectionError):
        client.snapshot()
    assert not server.core.armed
    old = client.session
    client.close()
    backend.frozen = False
    time.sleep(.12)
    new = RobotClient("127.0.0.1", server.control_port, server.ik_port).connect()
    try:
        assert new.session != old and not new.snapshot()["armed"]
    finally:
        new.close()


def test_second_controller_rejected(link):
    _, server, first = link
    second = RobotClient("127.0.0.1", server.control_port, server.ik_port)
    # Windows can reset a busy socket before its rejection frame is read.
    with pytest.raises((ProtocolError, ConnectionError)):
        second.connect()
    assert first.snapshot()["sample"]["valid"]
    assert second.connection is None or second.connection.closed.is_set()


def test_successful_control_retry_is_idempotent(link):
    backend, server, client = link
    state = client.snapshot()
    client.request("arm", feedback_seq=state["sample"]["seq"])
    ident = client.ident
    generation = server.core.generation
    client.connection.send({"type": "arm", "id": ident, "session": client.session,
                            "feedback_seq": state["sample"]["seq"]})
    time.sleep(.03)
    assert server.core.generation == generation
    assert server.core.armed


def test_connect_disconnect_without_arming_never_publishes(link):
    backend, server, client = link
    client.close()
    time.sleep(.05)
    assert backend.publish_count == 0


def test_arm_waits_for_matching_telemetry_after_reply(link):
    backend, _, client = link
    previous = client.generation
    receive = client.connection.on_message
    acknowledged = threading.Event()
    release = threading.Event()
    def delayed(msg):
        if msg.get("type") == "state" and msg["generation"] > previous and not release.is_set():
            return
        receive(msg)
        if msg.get("type") == "reply" and msg.get("result", {}).get("generation") == previous + 1:
            acknowledged.set()
    client.connection.on_message = delayed
    outcome = []
    def arm():
        try:
            outcome.append(client.arm())
        except Exception as exc:
            outcome.append(exc)
    worker = threading.Thread(target=arm)
    worker.start()
    try:
        assert acknowledged.wait(.3)
        worker.join(.02)
        assert worker.is_alive(), "arm returned before its telemetry arrived"
        release.set()
        worker.join(.3)
        assert outcome == [previous + 1]
        assert client.snapshot()["generation"] == previous + 1
        assert client.snapshot()["armed"]
        assert backend.publish_count == 0
    finally:
        release.set()
        worker.join(.3)


def test_arm_without_new_telemetry_fails_within_feedback_deadline(link):
    backend, _, client = link
    receive = client.connection.on_message
    client.connection.on_message = lambda msg: receive(msg) if msg.get("type") != "state" else None
    started = time.monotonic()
    with pytest.raises((TimeoutError, ConnectionError)):
        client.arm()
    assert time.monotonic() - started < .3
    assert backend.publish_count == 0
    client.hold("test_cleanup")


@pytest.mark.parametrize('selected,reported', [('gazebo','fake'),('intera','fake'),('gazebo',None)])
def test_wrong_backend_is_rejected_before_ik_or_control(selected,reported):
    backend = FakeBackend()
    backend.name = reported
    server = BridgeServer(backend,control_port=0,ik_port=0).start()
    client = RobotClient('127.0.0.1',server.control_port,server.ik_port,
                         expected_backend=selected)
    try:
        with pytest.raises(ProtocolError,match='Backend mismatch'):
            client.connect()
        assert client.connection is None and client.ik_socket is None
        assert not server.core.armed and backend.publish_count == 0
        deadline = time.monotonic()+1
        while server.owner_lock.locked() and time.monotonic()<deadline:
            time.sleep(.005)
        assert server.core.session is None and not server.owner_lock.locked()
        # A rejected identity must release its socket and allow a later valid connection.
        backend.name = 'fake'
        matching = RobotClient('127.0.0.1',server.control_port,server.ik_port,
                               expected_backend='fake')
        try:
            matching.connect()
            assert matching.hello['backend'] == 'fake'
            assert backend.publish_count == 0
        finally:
            matching.close()
    finally:
        client.close()
        server.close()
