"""Bounded, thread-safe one-second activity counters for diagnostics only."""
from collections import deque
import threading
import time


class EventRate:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        # Quest and control streams are well below 4096 packets/second.
        self.samples = deque(maxlen=4096)
        self.total = 0

    def mark(self):
        with self.lock:
            self.samples.append(self.clock())
            self.total += 1

    def snapshot(self, active=True):
        with self.lock:
            cutoff = self.clock() - 1.0
            while self.samples and self.samples[0] <= cutoff:
                self.samples.popleft()
            return {"hz": float(len(self.samples)) if active else 0.0,
                    "total": self.total}
