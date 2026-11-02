"""Feedback compatibility and fail-closed checks without ROS or hardware."""
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from sawyer_teleop.intera_backend import InteraBackend


def stamp(value):
    return NS(to_sec=lambda: value)


class InteraFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('sawyer_teleop.intera_backend.ns', return_value=10_000_000_000)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        b = self.b = InteraBackend.__new__(InteraBackend)
        b.lock = threading.RLock()
        b.ros = NS(Time=NS(now=lambda: stamp(1000)), is_shutdown=lambda: False)
        b.state_timeout, b.status_timeout = .1, .5
        b.seq = 1
        b.q, b.dq = [0.] * 7, [0.] * 7
        b.endpoint = {'p': [0., 0., 0.], 'q': [0., 0., 0., 1.]}
        b.stamps = {'joint': 999.999, 'pose': 999.999}
        b.received = {name: 10_000_000_000 for name in ('joint', 'pose', 'robot', 'collision')}
        b.captured = dict(b.received)
        b.invalid = {}
        b.robot = dict(enabled=True, ready=True, homed=True, low_voltage=False,
                       stopped=False, error=False, estop=False)
        b.cuff, b.collision = None, False
        b.publisher = NS(get_num_connections=lambda: 1)
        self.cuff_connections = 1
        b.cuff_subscriber = NS(get_num_connections=lambda: self.cuff_connections)

    def cuff(self, value=0, timestamp=100, status='ready'):
        return NS(time=stamp(timestamp), signals=[NS(name='right_cuff',
                  data='[%s]' % value, status=NS(tag=status))])

    def endpoint(self, frame='', timestamp=1000, valid=True):
        return NS(valid=valid, header=NS(frame_id=frame, stamp=stamp(timestamp)),
                  pose=NS(position=NS(x=.5, y=.1, z=.2),
                          orientation=NS(x=0., y=0., z=0., w=1.)))

    def test_empty_and_explicit_base_frames(self):
        for frame in ('', 'base', '/base'):
            with self.subTest(frame=frame):
                self.b.stamps.pop('pose', None)
                self.b._endpoint(self.endpoint(frame))
                self.assertNotIn('pose', self.b.invalid)
                self.assertEqual(self.b.endpoint['p'], [.5, .1, .2])
                self.assertEqual(self.b.captured['pose'], 10_000_000_000)

    def test_unknown_frame_invalid_pose_and_old_pose_rejected(self):
        for frame in ('map', '/', 'right_hand'):
            self.b._endpoint(self.endpoint(frame))
            self.assertEqual(self.b.invalid['pose'], 'endpoint_invalid')
        self.b._endpoint(self.endpoint(valid=False))
        self.assertEqual(self.b.invalid['pose'], 'endpoint_invalid')
        self.b._endpoint(self.endpoint(timestamp=999))
        self.assertEqual(self.b.invalid['pose'], 'pose_source_time_invalid')

    def test_old_cached_cuff_does_not_expire(self):
        self.b._cuff(self.cuff())
        self.b.received['cuff'] = 1
        self.assertTrue(self.b.snapshot()['valid'])

    def test_missing_or_pressed_cuff_blocks_control(self):
        self.assertFalse(self.b.snapshot()['valid'])
        self.b._cuff(self.cuff(1))
        self.assertEqual(self.b.snapshot()['fault'], 'hand_guiding')
        self.b._cuff(self.cuff(0, timestamp=101))
        self.assertTrue(self.b.snapshot()['valid'])

    def test_disconnect_requires_new_cuff_message(self):
        self.b._cuff(self.cuff())
        self.cuff_connections = 0
        self.assertEqual(self.b.snapshot()['fault'], 'cuff_connection_lost')
        self.cuff_connections = 1
        self.assertFalse(self.b.snapshot()['valid'])
        self.b._cuff(self.cuff())  # A latched replay may have the same timestamp.
        self.assertTrue(self.b.snapshot()['valid'])

    def test_bad_cuff_time_status_and_conflicting_replay(self):
        for timestamp in (0, float('nan'), 1001):
            self.b._cuff(self.cuff(timestamp=timestamp))
            self.assertEqual(self.b.invalid['cuff'], 'cuff_source_time_invalid')
        self.b._cuff(self.cuff())
        self.b._cuff(self.cuff(timestamp=99))
        self.assertEqual(self.b.invalid['cuff'], 'cuff_source_time_regressed')
        self.b._cuff(self.cuff(1))
        self.assertEqual(self.b.invalid['cuff'], 'cuff_inconsistent')
        self.b._cuff(self.cuff(status='error'))
        self.assertEqual(self.b.invalid['cuff'], 'cuff_invalid')

    def test_continuous_feedback_timeouts_and_robot_interlocks_remain(self):
        self.b._cuff(self.cuff())
        for name, delay in (('joint', 100_000_001), ('pose', 100_000_001),
                            ('collision', 500_000_001)):
            with self.subTest(name=name):
                self.b.captured[name] -= delay
                self.assertEqual(self.b.snapshot()['fault'], name + '_stale')
                self.b.captured[name] += delay
        self.b.robot['estop'] = True
        self.assertEqual(self.b.snapshot()['fault'], 'robot_not_ready')

    def collision(self, timestamp, value=False):
        return NS(header=NS(stamp=stamp(timestamp)), collision_state=value)

    def test_collision_small_future_skew_is_bounded_and_capture_is_not_future(self):
        self.b._cuff(self.cuff())
        # Small lab skew and the demo's explicit 50 ms boundary.
        for offset in (.000056, .000127, .001, .031471, .05):
            with self.subTest(offset=offset):
                self.b._collision(self.collision(1000 + offset))
                self.assertTrue(self.b.snapshot()['valid'])
                self.assertEqual(self.b.stamps['collision'], 1000 + offset)
                self.assertEqual(self.b.captured['collision'], 10_000_000_000)
        # A tolerated future stamp must not extend the freshness window.
        for name in ('joint', 'pose', 'robot'):
            self.b.captured[name] = self.b.received[name] = 10_500_000_001
        with patch('sawyer_teleop.intera_backend.ns', return_value=10_500_000_001):
            self.assertEqual(self.b.snapshot()['fault'], 'collision_stale')

    def test_collision_bad_time_does_not_replace_or_refresh_valid_state(self):
        self.b._collision(self.collision(1000))
        original = (self.b.stamps['collision'], self.b.received['collision'],
                    self.b.captured['collision'])
        for timestamp in (0, float('nan'), float('inf'), 1000.050001, 999.499999):
            with self.subTest(timestamp=timestamp):
                self.b._collision(self.collision(timestamp, True))
                self.assertEqual(self.b.invalid['collision'], 'collision_source_time_invalid')
                self.assertFalse(self.b.collision)
                self.assertEqual(original, (self.b.stamps['collision'],
                                 self.b.received['collision'], self.b.captured['collision']))
        self.b._collision(self.collision(1000.0001))
        self.assertNotIn('collision', self.b.invalid)

    def test_collision_duplicate_regression_and_true_collision_still_block(self):
        self.b._cuff(self.cuff())
        self.b._collision(self.collision(1000.0001))
        with patch('sawyer_teleop.intera_backend.ns', return_value=10_100_000_000):
            self.b._collision(self.collision(1000.0001))
        self.assertEqual(self.b.received['collision'], 10_000_000_000)
        self.b._collision(self.collision(1000))
        self.assertEqual(self.b.snapshot()['fault'], 'collision_source_time_regressed')
        self.b._collision(self.collision(1000.0002, True))
        self.assertEqual(self.b.snapshot()['fault'], 'robot_collision')

    def test_future_skew_above_50ms_is_rejected_for_joint_pose_and_cuff(self):
        self.b._accept('joint', 1000.050001, lambda: self.fail('accepted future joint'))
        self.b._endpoint(self.endpoint(timestamp=1000.050001))
        self.b._cuff(self.cuff(timestamp=1000.050001))
        for name in ('joint', 'pose', 'cuff'):
            self.assertEqual(self.b.invalid[name], name + '_source_time_invalid')

    def test_demo_joint_pose_and_cuff_accept_50ms_but_keep_raw_stamps(self):
        for offset in (.0001, .031471, .05):
            with self.subTest(offset=offset):
                timestamp = 1000 + offset
                self.b._joint(NS(name=['right_j%d' % n for n in range(7)],
                                 position=[0.] * 7, velocity=[0.] * 7,
                                 header=NS(stamp=stamp(timestamp))))
                self.b._endpoint(self.endpoint(timestamp=timestamp))
                self.b._cuff(self.cuff(timestamp=timestamp))
                self.b._collision(self.collision(timestamp))
                sample = self.b.snapshot()
                self.assertTrue(sample['valid'], sample['fault'])
                self.assertEqual(sample['captured_ns'], 10_000_000_000)
                for name in ('joint', 'pose', 'cuff', 'collision'):
                    self.assertEqual(self.b.stamps[name], timestamp)
        # No extra 50 ms is added to the local freshness window.
        self.b.captured['pose'] = self.b.received['robot'] = 10_100_000_001
        with patch('sawyer_teleop.intera_backend.ns', return_value=10_100_000_001):
            self.assertEqual(self.b.snapshot()['fault'], 'joint_stale')

    def test_demo_does_not_accept_historical_85ms_skew_or_old_feedback(self):
        for name in ('joint', 'pose', 'collision'):
            for timestamp in (1000.085, 1000.116278,
                              999.899999 if name != 'collision' else 999.499999):
                with self.subTest(name=name, timestamp=timestamp):
                    self.b._accept(name, timestamp, lambda: self.fail('accepted bad time'))
                    self.assertEqual(self.b.invalid[name], name + '_source_time_invalid')

    def test_raw_estop_codes_and_all_invalid_fields_remain_in_diagnostics(self):
        self.b._cuff(self.cuff())
        self.b._robot(NS(enabled=False, ready=False, homed=True, lowVoltage=False,
                         stopped=True, error=True, estop_button=2, estop_source=3))
        self.b.invalid.update(pose='endpoint_invalid', collision='collision_source_time_invalid')
        sample = self.b.snapshot()
        self.assertEqual(sample['robot']['estop_button'], 2)
        self.assertEqual(sample['robot']['estop_source'], 3)
        self.assertEqual(sample['feedback_diagnostics']['invalid'], self.b.invalid)
        self.assertFalse(sample['valid'])


if __name__ == '__main__':
    unittest.main()
