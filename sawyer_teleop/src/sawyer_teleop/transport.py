"""TCP transport with independent readers/writers and bounded latest-value slots."""
import collections
import socket
import struct
import threading
import time

from .protocol import Decoder, ProtocolError, close_socket, configure_socket, encode
from .rates import EventRate


class Connection:
    def __init__(self, sock, on_message, on_close=lambda: None, capacity=128):
        self.sock = sock
        configure_socket(sock)
        self.on_message, self.on_close = on_message, on_close
        self.capacity = capacity
        self.reliable = collections.deque()
        self.latest = {}
        self.condition = threading.Condition()
        self.closed = threading.Event()
        self.error = None
        self.threads = []
        self.target_sent = EventRate()

    def start(self):
        for action in (self._read, self._write):
            thread = threading.Thread(target=action, daemon=True)
            self.threads.append(thread)
            thread.start()
        return self

    def send(self, msg, latest=None, clear_latest=False):
        with self.condition:
            if self.closed.is_set():
                raise ConnectionError(self.error or "connection closed")
            if clear_latest:
                self.latest.clear()
            if latest is not None:
                self.latest[latest] = msg
            elif len(self.reliable) < self.capacity:
                self.reliable.append(msg)
            else:
                self.close("outbound_queue_full")
                raise ConnectionError("outbound_queue_full")
            self.condition.notify()

    def clear_targets(self):
        with self.condition:
            self.latest.pop("target", None)

    def _write(self):
        offer_state = False
        try:
            while not self.closed.is_set():
                with self.condition:
                    self.condition.wait_for(
                        lambda: self.closed.is_set() or self.reliable or self.latest, timeout=.1)
                    if self.closed.is_set():
                        break
                    # B's receipts must stay lossless, but a continuous receipt
                    # stream must not starve its latest feedback. A's target
                    # slot still yields to every reliable hold/control request.
                    if self.reliable and not (offer_state and "state" in self.latest):
                        msg = self.reliable.popleft()
                        offer_state = True
                    elif self.latest:
                        key = "state" if "state" in self.latest else next(iter(self.latest))
                        msg = self.latest.pop(key)
                        offer_state = False
                    else:
                        continue
                # sendall has a finite deadline; buffered old bytes cannot be revoked.
                self.sock.sendall(encode(msg))
                if msg.get("type") in ("target", "pose_target"):
                    self.target_sent.mark()
        except (OSError, ValueError) as exc:
            self.close("send_failed:" + str(exc))

    def _read(self):
        decoder = Decoder()
        partial_since = None
        try:
            while not self.closed.is_set():
                try:
                    data = self.sock.recv(16384)
                except socket.timeout:
                    if partial_since and time.monotonic() - partial_since > .5:
                        raise ProtocolError("partial_frame_timeout")
                    continue
                if not data:
                    break
                if partial_since and time.monotonic()-partial_since > .5:
                    raise ProtocolError("partial_frame_timeout")
                for msg in decoder.feed(data):
                    self.on_message(msg)
                if decoder.buffer:
                    partial_since = partial_since or time.monotonic()
                else:
                    partial_since = None
        except Exception as exc:
            self.error = "receive_failed:" + str(exc)
        finally:
            self.close(self.error or "peer_closed")

    def close(self, error=None):
        with self.condition:
            if self.closed.is_set():
                return
            self.error = error or self.error
            self.closed.set()
            self.reliable.clear()
            self.latest.clear()
            self.condition.notify_all()
        close_socket(self.sock)
        self.on_close()


def read_one(sock, timeout=2):
    deadline = time.monotonic() + timeout
    consumed = False
    original_timeout = sock.gettimeout()

    def exact(length):
        nonlocal consumed
        result = bytearray()
        while len(result) < length:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                if consumed:
                    raise ProtocolError("partial_frame_timeout")
                raise TimeoutError("read_timeout")
            try:
                sock.settimeout(min(.1, remaining))
                data = sock.recv(length-len(result))
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("peer_closed")
            consumed = True
            result.extend(data)
        return bytes(result)

    try:
        header = exact(4)
        size = struct.unpack("!I", header)[0]
        from .protocol import MAX_FRAME
        if not 0 < size <= MAX_FRAME:
            raise ProtocolError("invalid frame length")
        return Decoder().feed(header + exact(size))[0]
    finally:
        try:
            sock.settimeout(original_timeout)
        except OSError:
            pass
