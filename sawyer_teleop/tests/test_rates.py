import io
import json
import socket
import time
from types import SimpleNamespace

from sawyer_teleop.rates import EventRate
from sawyer_teleop.quest import QuestInput, QuestValidator
from sawyer_teleop.transport import Connection, read_one
from test_quest import packet


def test_idle_rate_decays_without_new_events_and_total_is_retained():
    now = [10.0]
    rate = EventRate(clock=lambda: now[0])
    for i in range(90):
        now[0] = 10.0 + i / 90
        rate.mark()
    assert rate.snapshot() == {"hz": 90.0, "total": 90}
    assert rate.snapshot(active=False) == {"hz": 0.0, "total": 90}
    now[0] = 12.0
    assert rate.snapshot() == {"hz": 0.0, "total": 90}
    assert not rate.samples


def test_quest_rate_counts_new_packets_not_polls_or_tracking_validity(monkeypatch):
    monkeypatch.setattr("sawyer_teleop.quest.shutil.which", lambda _: "adb")
    monkeypatch.setattr("sawyer_teleop.quest.ns", lambda: 1_010_000_000)
    quest = QuestInput("test")
    lost = packet(2)
    lost["hands"]["r"]["tracking_ok"] = False
    stale = packet(3)
    stale["sample_ns"] = 1
    messages = [packet(1), packet(1), lost, stale, packet(4)]
    quest.validator = QuestValidator(0, 1_000_000)
    quest.process = SimpleNamespace(
        stdout=io.BytesIO(b"".join(json.dumps(p).encode() + b"\n" for p in messages)),
        poll=lambda: None)
    quest.sync_at = 1_010_000_000
    quest._read()
    for _ in range(100):
        quest.poll()
    assert quest.rates() == {"hz": 3.0, "total": 3}
    quest.process.poll = lambda: 1
    assert quest.rates() == {"hz": 0.0, "total": 3}


def test_cached_quest_sample_expires_from_capture_time_not_receipt(monkeypatch):
    now = [1_100_000_000]
    monkeypatch.setattr("sawyer_teleop.quest.shutil.which", lambda _: "adb")
    monkeypatch.setattr("sawyer_teleop.quest.ns", lambda: now[0])
    quest = QuestInput("test")
    quest.process = SimpleNamespace(poll=lambda: None)
    quest.validator = QuestValidator(0, 10_000_000)
    quest.sync_at = now[0]
    quest.latest = quest.validator.parse(packet(1))
    assert quest.poll()["valid"]
    # Received only 100 ms ago, but source age + clock uncertainty exceeds 200 ms.
    now[0] += 100_000_000
    assert not quest.poll()["valid"]
    assert quest.poll()["reason"] == "quest_stale"


def test_sent_rate_counts_wire_targets_not_enqueued_or_control_messages():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    left = socket.create_connection(listener.getsockname())
    right, _ = listener.accept()
    listener.close()
    connection = Connection(left, lambda _: None)
    try:
        # Overwritten targets and hold-cleared targets never reach sendall.
        connection.send({"type": "target", "seq": -1}, latest="target")
        connection.send({"type": "hold"}, clear_latest=True)
        for i in range(500):
            connection.send({"type": "target", "seq": i}, latest="target")
        assert connection.target_sent.snapshot()["total"] == 0
        connection.start()
        assert read_one(right)["type"] == "hold"
        assert read_one(right) == {"type": "target", "seq": 499}
        deadline = time.monotonic() + 1
        while connection.target_sent.snapshot()["total"] == 0 and time.monotonic() < deadline:
            time.sleep(.001)
        assert connection.target_sent.snapshot()["total"] == 1
    finally:
        connection.close()
        right.close()


def test_failed_send_is_not_counted():
    class FailedSocket:
        def setsockopt(self, *args): pass
        def settimeout(self, *args): pass
        def shutdown(self, *args): pass
        def close(self): pass
        def sendall(self, data): raise socket.timeout("simulated timeout")
    connection = Connection(FailedSocket(), lambda _: None)
    connection.send({"type": "target"}, latest="target")
    connection._write()
    assert connection.closed.is_set()
    assert connection.target_sent.snapshot()["total"] == 0
