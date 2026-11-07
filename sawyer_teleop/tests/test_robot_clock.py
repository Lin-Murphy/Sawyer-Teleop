"""Measured robot clock: SNTP math, expiry/fallback, and feedback validation after a robot reboot."""
import socket
import struct
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from sawyer_teleop.intera_backend import InteraBackend
from sawyer_teleop.robot_clock import MAX_SAMPLE_AGE_S, RobotClock, _from_ntp, _to_ntp, sntp_query


def fake_ntp_server(skew_s, stratum=3, stray_first=False):
    """One-shot NTP server on loopback whose clock is B's clock + skew_s."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    sock.settimeout(5)
    def serve():
        data, sender = sock.recvfrom(512)
        received = time.time() + skew_s
        reply = bytearray(48)
        reply[0], reply[1] = 0x24, stratum  # VN=4, mode=4 (server)
        reply[24:32] = data[40:48]
        reply[32:40] = _to_ntp(received)
        reply[40:48] = _to_ntp(time.time() + skew_s)
        if stray_first:
            stray = bytearray(reply)
            stray[24:32] = b'\0' * 8
            stray[40:48] = _to_ntp(time.time() + 99)
            sock.sendto(bytes(stray), sender)
        sock.sendto(bytes(reply), sender)
        sock.close()
    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1]


def test_ntp_timestamp_round_trip_keeps_sub_microsecond_precision():
    value = 1_794_000_000.123456
    assert abs(_from_ntp(_to_ntp(value)) - value) < 1e-6


@pytest.mark.parametrize('skew', [.085, -.0315, 0.])
def test_sntp_measures_robot_minus_b_offset_over_udp(skew):
    port = fake_ntp_server(skew)
    offset, delay = sntp_query('127.0.0.1', port=port)
    assert offset == pytest.approx(skew, abs=.005) and 0 <= delay < .05


def test_sntp_ignores_stray_reply_and_rejects_kiss_of_death():
    offset, _ = sntp_query('127.0.0.1', port=fake_ntp_server(.04, stray_first=True))
    assert offset == pytest.approx(.04, abs=.005)
    with pytest.raises(ValueError, match='stratum 0'):
        sntp_query('127.0.0.1', port=fake_ntp_server(.04, stratum=0))


def test_robot_clock_expires_stale_measurement_and_keeps_last_error():
    now = [100.]
    results = [(.085, .002), OSError('no route')]
    def query(host):
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    clock = RobotClock('sawyer.local', query=query, monotonic=lambda: now[0])
    assert clock.estimate() is None and not clock.describe()['measured']
    assert clock.measure_once()
    assert clock.estimate() == (.085, .001)
    assert not clock.measure_once()  # A failure keeps the fresh sample...
    assert clock.estimate() == (.085, .001) and 'no route' in clock.describe()['error']
    now[0] += MAX_SAMPLE_AGE_S + .1
    assert clock.estimate() is None  # ...but never beyond its age limit.


def stamp(value):
    return NS(to_sec=lambda: value)


class MeasuredClockFeedbackTests(unittest.TestCase):
    """B clock reads 1000.000 s; the rebooted robot clock runs 85 ms ahead (2026-11-02 field case)."""

    def setUp(self):
        patcher = patch('sawyer_teleop.intera_backend.ns', return_value=10_000_000_000)
        patcher.start()
        self.addCleanup(patcher.stop)
        b = self.b = InteraBackend.__new__(InteraBackend)
        b.lock = threading.RLock()
        b.ros = NS(Time=NS(now=lambda: stamp(1000.)), is_shutdown=lambda: False)
        b.state_timeout, b.status_timeout = .1, .5
        b.seq, b.q, b.dq, b.endpoint = 0, None, None, None
        b.stamps, b.received, b.captured, b.invalid = {}, {}, {}, {}
        b.cuff, b.collision = None, False

    def measured(self, offset, uncertainty=.0005):
        self.b.robot_clock = NS(estimate=lambda: (offset, uncertainty))

    def collision(self, at):
        self.b._collision(NS(header=NS(stamp=stamp(at)), collision_state=False))

    def test_unmeasured_clock_keeps_previous_rejection(self):
        self.collision(1000.085)
        self.assertEqual(self.b.invalid['collision'], 'collision_source_time_invalid')

    def test_measured_robot_clock_accepts_fresh_feedback_after_reboot_skew(self):
        self.measured(.085)
        self.collision(1000.080)  # 5 ms old on the robot clock.
        self.assertNotIn('collision', self.b.invalid)
        # Age includes the measurement bound (conservative); float stamps round to ~1 ns.
        self.assertAlmostEqual(self.b.captured['collision'], 10_000_000_000 - 5_500_000, delta=10)

    def test_measured_clock_still_rejects_stale_and_future_feedback(self):
        self.measured(.085)
        self.b._accept('joint', 1000.085-.1-.001, lambda: None)  # >100 ms old on the robot clock.
        self.assertEqual(self.b.invalid['joint'], 'joint_source_time_invalid')
        self.collision(1000.085+.051)  # Beyond the unchanged 50 ms residual tolerance.
        self.assertEqual(self.b.invalid['collision'], 'collision_source_time_invalid')

    def test_cuff_uses_the_same_measured_clock(self):
        self.measured(.085)
        self.b.cuff_subscriber = NS(get_num_connections=lambda: 1)
        self.b._cuff(NS(time=stamp(1000.120), signals=[NS(name='right_cuff', data='[0]', status=NS(tag='ready'))]))
        self.assertNotIn('cuff', self.b.invalid)
        self.assertIs(self.b.cuff, False)
