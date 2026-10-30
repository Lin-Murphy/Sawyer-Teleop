"""Versioned, length-prefixed JSON. Shared by A and B; standard library only."""
import json
import math
import socket
import struct
import time

VERSION = 1
MAX_FRAME = 65536
JOINTS = ["right_j%d" % i for i in range(7)]


class ProtocolError(ValueError):
    pass


def integer(value, name="integer", minimum=0):
    if type(value) is not int or value < minimum:
        raise ProtocolError("invalid " + name)
    return value


def vector(value, size, name="vector"):
    if (not isinstance(value, (list, tuple)) or len(value) != size
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
        raise ProtocolError("invalid " + name)
    return [float(v) for v in value]


def pose(value):
    if not isinstance(value, dict):
        raise ProtocolError("invalid pose")
    p, q = vector(value.get("p"), 3), vector(value.get("q"), 4)
    if abs(sum(v*v for v in q) - 1) > 0.01:
        raise ProtocolError("invalid quaternion")
    return {"p": p, "q": q}


def _constant(value):
    raise ProtocolError("non-finite JSON: " + value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def encode(message):
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    data = json.dumps(message, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(data) <= MAX_FRAME:
        raise ProtocolError("frame too large")
    return struct.pack("!I", len(data)) + data


class Decoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= 4:
            size = struct.unpack_from("!I", self.buffer)[0]
            if not 0 < size <= MAX_FRAME:
                raise ProtocolError("invalid frame length")
            if len(self.buffer) < size + 4:
                break
            raw = bytes(self.buffer[4:size+4])
            del self.buffer[:size+4]
            try:
                msg = json.loads(raw, parse_constant=_constant, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ProtocolError(str(exc)) from exc
            if not isinstance(msg, dict):
                raise ProtocolError("message must be an object")
            result.append(msg)
        if len(self.buffer) > MAX_FRAME + 4:
            raise ProtocolError("oversized partial frame")
        return result


def configure_socket(sock, timeout=0.1):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.settimeout(timeout)


def close_socket(sock):
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def ns():
    # Python 3.11 monotonic_ns uses the coarse GetTickCount64 on Windows.
    # perf_counter_ns uses monotonic QueryPerformanceCounter at sub-ms resolution.
    return time.perf_counter_ns()
