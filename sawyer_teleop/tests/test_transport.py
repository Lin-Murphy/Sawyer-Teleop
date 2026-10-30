import socket
import time
import threading
import pytest
from sawyer_teleop.transport import Connection, read_one
from sawyer_teleop.protocol import Decoder, ProtocolError, encode


def tcp_pair():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    server, _ = listener.accept()
    listener.close()
    return client, server


def test_handshake_does_not_consume_following_frame():
    left, right = socket.socketpair()
    try:
        left.sendall(encode({"a": 1})+encode({"a": 2}))
        assert read_one(right) == {"a": 1}
        assert read_one(right) == {"a": 2}
    finally:
        left.close()
        right.close()


def test_read_deadline_includes_body():
    left, right = socket.socketpair()
    try:
        blob = encode({"a": 1})
        left.sendall(blob[:4])
        start = time.monotonic()
        with pytest.raises(ProtocolError):
            read_one(right, timeout=.04)
        assert time.monotonic()-start < .15
    finally:
        left.close()
        right.close()


def test_latest_slot_and_reliable_backpressure():
    left, right = tcp_pair()
    connection = Connection(left, lambda msg: None, capacity=2)
    try:
        for i in range(500):
            connection.send({"seq": i}, latest="target")
        assert len(connection.latest) == 1 and connection.latest["target"]["seq"] == 499
        connection.send({"type": "hold"}, clear_latest=True)
        assert not connection.latest
        connection.send({"type": "ping"})
        with pytest.raises(ConnectionError):
            connection.send({"type": "ping"})
        assert connection.closed.is_set()
    finally:
        connection.close()
        right.close()


def test_slow_partial_frame_does_not_occupy_connection_forever():
    left, right = tcp_pair()
    connection = Connection(right, lambda msg: None).start()
    try:
        left.sendall(b"\0\0\x01\0")
        for _ in range(15):
            time.sleep(.05)
            try:
                left.sendall(b" ")
            except OSError:
                break
        assert connection.closed.wait(.2)
        assert "partial_frame_timeout" in connection.error
    finally:
        connection.close()
        left.close()


class WriteSink:
    """A deterministic wire that can replenish backlog while each send completes."""
    def setsockopt(self, *args): pass
    def settimeout(self, *args): pass
    def shutdown(self, *args): pass
    def close(self): pass
    def sendall(self, data): self.sent(Decoder().feed(data)[0])


def test_continuous_receipts_cannot_starve_latest_state_or_lose_receipts():
    sink = WriteSink()
    connection = Connection(sink, lambda msg: None, capacity=3)
    receipts, states = [], []
    next_receipt = 3
    for seq in range(3):
        connection.send({'type':'submitted','seq':seq})
    def sent(msg):
        nonlocal next_receipt
        if msg['type'] == 'submitted':
            receipts.append(msg['seq'])
            if next_receipt < 20:
                connection.send({'type':'submitted','seq':next_receipt})
                next_receipt += 1
            connection.send({'type':'state','seq':msg['seq']}, latest='state')
        else:
            states.append(msg['seq'])
            if len(receipts) == 20:
                connection.close()
    sink.sent = sent
    connection._write()
    assert receipts == list(range(20))  # All reliable frames, in their original order.
    assert states == list(range(20))    # Feedback progresses even while backlog stays nonempty.


def test_client_reliable_hold_and_arm_still_preempt_latest_target():
    sink = WriteSink()
    connection = Connection(sink, lambda msg: None)
    connection.send({'type':'target','seq':1}, latest='target')
    connection.send({'type':'hold'}, clear_latest=True)
    connection.send({'type':'arm'})
    connection.send({'type':'target','seq':2}, latest='target')
    received = []
    def sent(msg):
        received.append(msg)
        if len(received) == 3:
            connection.close()
    sink.sent = sent
    connection._write()
    assert received == [{'type':'hold'},{'type':'arm'},{'type':'target','seq':2}]
