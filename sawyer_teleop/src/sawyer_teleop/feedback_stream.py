"""Lossless-when-possible copy of every accepted robot feedback message, for recording only.

Control keeps reading the backend's latest value. This side channel never blocks a ROS
callback or B's control loop: it is a bounded queue whose overflow is counted, not hidden.
"""
import collections
import math
import threading

SCHEMA = 1
# One message per B control tick normally carries 1-2 rows; the cap keeps the
# merged state frame well under protocol.MAX_FRAME (64 KiB) even after a stall.
MAX_ROWS_PER_MESSAGE = 96


def finite_vector(values, size):
    """Return a float list, or None when a field is absent or not finite."""
    try:
        result = [float(v) for v in values]
    except (TypeError, ValueError):
        return None
    if len(result) != size or not all(math.isfinite(v) for v in result):
        return None
    return result


def twist_from(msg):
    """EndpointState twist as [vx, vy, vz, wx, wy, wz] in the base frame, or None."""
    try:
        linear, angular = msg.twist.linear, msg.twist.angular
        return finite_vector([linear.x, linear.y, linear.z, angular.x, angular.y, angular.z], 6)
    except AttributeError:
        return None


class FeedbackStream:
    """Per-topic sequence numbers let A detect any row lost after this point."""

    def __init__(self, capacity=1024):
        self.lock = threading.Lock()
        self.rows = collections.deque()
        self.capacity = capacity
        self.dropped = 0
        self.sequences = collections.Counter()

    def append(self, kind, row):
        with self.lock:
            self.sequences[kind] += 1
            if len(self.rows) >= self.capacity:
                self.rows.popleft()
                self.dropped += 1
            self.rows.append(dict(row, kind=kind, stream_seq=self.sequences[kind]))

    def clear(self):
        with self.lock:
            self.rows.clear()
            self.dropped = 0

    def drain(self, limit=MAX_ROWS_PER_MESSAGE):
        """Return (rows, dropped). Rows beyond ``limit`` stay queued for the next tick."""
        with self.lock:
            rows = [self.rows.popleft() for _ in range(min(limit, len(self.rows)))]
            dropped, self.dropped = self.dropped, 0
            return rows, dropped


class FeedbackStreamSource:
    """Backend mixin: stage a payload inside an accepted update, commit it with its times.

    Uses instance-dict defaults so test doubles built with ``__new__`` keep working.
    """

    @property
    def feedback_stream(self):
        return self.__dict__.setdefault("_feedback_stream", FeedbackStream())

    def _stream_stage(self, name, payload):
        self.__dict__.setdefault("_stream_pending", {})[name] = payload

    def _stream_commit(self, name, stamp, received_ns, captured_ns):
        payload = self.__dict__.get("_stream_pending", {}).pop(name, None)
        if payload is not None:
            self.feedback_stream.append(name, dict(payload, source_stamp=stamp,
                                                   b_received_ns=received_ns, b_captured_ns=captured_ns))

    def drain_feedback_stream(self):
        return self.feedback_stream.drain()

    def clear_feedback_stream(self):
        self.feedback_stream.clear()


def fit_frame(message, field, encode):
    """Encode ``message``; if too large, drop the oldest ``field`` rows (counted as dropped).

    Recording rows are expendable but counted; the control link must never close because
    the side channel grew. Raises only if the frame is too large even without rows.
    """
    try:
        return encode(message)
    except ValueError:
        stream = message.get(field)
        if not stream or not stream.get("rows"):
            raise
    rows = list(stream["rows"])
    dropped = stream.get("dropped", 0)
    while rows:
        cut = max(1, len(rows)//2)
        rows, dropped = rows[cut:], dropped+cut
        try:
            return encode(dict(message, **{field: dict(stream, rows=rows, dropped=dropped)}))
        except ValueError:
            continue
    return encode(dict(message, **{field: dict(stream, rows=[], dropped=dropped)}))


def merge_stream(previous, current, limit=MAX_ROWS_PER_MESSAGE):
    """Carry an unsent message's rows into its replacement (oldest rows dropped first)."""
    if not previous:
        return current
    if not current:
        return previous
    rows = list(previous.get("rows", [])) + list(current.get("rows", []))
    dropped = previous.get("dropped", 0) + current.get("dropped", 0)
    if len(rows) > limit:
        dropped += len(rows)-limit
        rows = rows[-limit:]
    return dict(current, rows=rows, dropped=dropped)
