"""B-only measurement of the robot's wall clock via its NTP service (unprivileged SNTP client).

After a robot reboot its clock can differ from B's by tens of milliseconds, and
systemd-timesyncd only slews such offsets away over minutes. Robot ROS stamps are
in the robot clock, so B validates them against a *measured* robot clock instead of
waiting for the OS to converge. This is an explicit measurement with a reported
bound (half the round trip), refreshed continuously; it never widens any feedback
limit and falls back to B's own clock (the previous behaviour) when unmeasured.
"""
import socket
import struct
import threading
import time

NTP_EPOCH_OFFSET = 2208988800  # Seconds from 1900-01-01 to 1970-01-01.
QUERY_INTERVAL_S = 2.0
MAX_DELAY_S = .05       # Reject round trips that would make the bound useless.
MAX_SAMPLE_AGE_S = 10.  # A robot clock jump is picked up within one interval.


def _to_ntp(seconds):
    seconds += NTP_EPOCH_OFFSET
    whole = int(seconds)
    return struct.pack('!II', whole, int((seconds-whole) * 2**32) & 0xffffffff)


def _from_ntp(data):
    whole, fraction = struct.unpack('!II', data)
    return whole - NTP_EPOCH_OFFSET + fraction / 2**32


def sntp_query(host, timeout=.5, clock=time.time, port=123):
    """Return (offset_s, delay_s): robot_clock ~= clock() + offset, +/- delay/2."""
    request = bytearray(48)
    request[0] = 0x23  # LI=0, VN=4, mode=3 (client).
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
        t1 = clock()
        transmit = _to_ntp(t1)
        request[40:48] = transmit
        sock.sendto(bytes(request), address)
        while True:
            data, sender = sock.recvfrom(512)
            t4 = clock()
            if sender[0] == address[0] and len(data) >= 48 and data[24:32] == transmit:
                break  # Ignore stray or replayed datagrams.
    mode, stratum = data[0] & 7, data[1]
    if mode != 4 or not 1 <= stratum <= 16:
        raise ValueError('robot NTP reply rejected (mode %d, stratum %d)' % (mode, stratum))
    t2, t3 = _from_ntp(data[32:40]), _from_ntp(data[40:48])
    delay = (t4-t1) - (t3-t2)
    if not 0 <= delay <= MAX_DELAY_S:
        raise ValueError('robot NTP round trip %.1f ms is too long' % (delay*1000))
    return ((t2-t1) + (t3-t4)) / 2, delay


class RobotClock:
    """Background SNTP sampler; estimate() is lock-protected and never blocks on the network."""

    def __init__(self, host, query=sntp_query, interval=QUERY_INTERVAL_S, monotonic=time.monotonic):
        self.host, self.query, self.interval, self.monotonic = host, query, interval, monotonic
        self.lock = threading.Lock()
        self.sample = None  # (offset_s, delay_s, measured_monotonic)
        self.error = 'not measured yet'
        self.stop_event = threading.Event()
        self.thread = None

    def measure_once(self):
        try:
            offset, delay = self.query(self.host)
        except Exception as exc:  # Network, DNS or protocol failure: keep last sample until it expires.
            with self.lock:
                self.error = '%s: %s' % (type(exc).__name__, exc)
            return False
        with self.lock:
            self.sample = (offset, delay, self.monotonic())
            self.error = None
        return True

    def start(self):
        self.thread = threading.Thread(target=self._run, name='robot-clock', daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop_event.wait(self.interval):
            self.measure_once()

    def close(self):
        self.stop_event.set()

    def estimate(self):
        """(offset_s, uncertainty_s) of the robot clock, or None if no fresh measurement."""
        with self.lock:
            if self.sample is None:
                return None
            offset, delay, measured = self.sample
            if self.monotonic()-measured > MAX_SAMPLE_AGE_S:
                return None
            return offset, delay/2

    def describe(self):
        estimate = self.estimate()
        with self.lock:
            error = self.error
        return {'host': self.host, 'measured': estimate is not None,
                'offset_ms': None if estimate is None else estimate[0]*1000,
                'uncertainty_ms': None if estimate is None else estimate[1]*1000,
                'error': error}
