"""Independent reference bounds, reversals, compatibility and release authority."""
import math
import random
import time

import numpy as np
import pytest

from sawyer_teleop.trajectory import ResponsiveJointTrajectory, make_trajectory, trajectory_options
from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.server import BridgeServer
from sawyer_teleop.client import RobotClient
from sawyer_teleop.operator import Operator
from sawyer_teleop.demo import wait_until
from test_trajectory import make_core
from test_bridge import target
from test_motion import SteppedQuest


def test_responsive_options_are_explicit_and_old_default_is_preserved():
    assert make_trajectory({}).algorithm == 'online_cubic_hermite_v2'
    assert make_trajectory({'profile': 'responsive'}).algorithm == 'online_bounded_acceleration_v1'
    assert 'min_duration_ms' not in trajectory_options({'profile': 'responsive'})
    with pytest.raises(ValueError, match='only to the cubic'):
        trajectory_options({'profile': 'responsive', 'min_duration_ms': 20})
    with pytest.raises(ValueError, match='Unknown'):
        trajectory_options({'profile': 'unknown'})


def test_random_phase_endpoints_and_retarget_continuity():
    rng = random.Random(12910)
    options = {'max_velocity_rad_s': [.2+i*.1 for i in range(7)],
               'max_acceleration_rad_s2': [.5+i*.2 for i in range(7)]}
    stream = ResponsiveJointTrajectory(options)
    stamp = 1_000_000_000
    stream.reset([0.]*7, stamp)
    for _ in range(600):
        stamp += rng.randint(1_000_000, 80_000_000)
        before = stream.sample(stamp)
        stream.retarget([rng.uniform(-2, 2) for _ in range(7)], stamp)
        after = stream.sample(stamp)
        assert after[0] == pytest.approx(before[0], abs=1e-10)
        assert after[1] == pytest.approx(before[1], abs=1e-10)
        for i, phases in enumerate(stream.plans):
            q, v = stream.origin[i], stream.initial_velocity[i]
            for duration, a in phases:
                assert duration >= 0 and abs(a) <= stream.acceleration[i]
                for fraction in np.linspace(0, 1, 17):
                    assert abs(v+a*duration*fraction) <= stream.velocity[i]+1e-10
                q += v*duration + .5*a*duration**2
                v += a*duration
            assert q == pytest.approx(stream.goal[i], abs=1e-9)
            assert v == pytest.approx(stream.end_velocity[i], abs=1e-9)


def test_small_joint_finishes_without_waiting_for_large_wrist_move():
    stream = ResponsiveJointTrajectory()
    stream.reset([0.]*7, 0)
    stream.retarget([.015, 0, 0, 0, 0, 0, 2.], 0)
    q, dq, _ = stream.sample(220_000_000)
    assert q[0] == pytest.approx(.015)
    assert dq[0] == 0 and 0 < q[6] < .1
    # The stationary endpoint is held, without further overshoot or creep.
    q, dq, _ = stream.sample(5_000_000_000)
    assert q == pytest.approx([.015, 0, 0, 0, 0, 0, 2.])
    assert dq == [0.]*7


def test_reversal_brakes_promptly_within_original_acceleration_limit():
    stream = ResponsiveJointTrajectory()
    stream.reset([0.]*7, 0)
    stream.retarget([2.]*7, 0)
    stream.sample(600_000_000)
    stream.retarget([-2.]*7, 600_000_000)
    before = stream.sample(600_000_000)
    assert before[1] == pytest.approx([.6]*7)
    assert before[2] == [-1.5]*7
    at_stop = stream.sample(1_000_000_000)
    assert at_stop[1] == pytest.approx([0.]*7, abs=1e-12)
    assert np.max(np.array(at_stop[0])-before[0]) <= .6**2/(2*1.5)+1e-10
    assert max(stream.sample(1_020_000_000)[1]) < 0


def test_moving_goal_missing_update_brakes_and_duplicate_finishes():
    stream = ResponsiveJointTrajectory()
    stream.reset([0.]*7, 0)
    stream.retarget([.002]*7, 0, 0)
    stream.retarget([.004]*7, 20_000_000, 20_000_000)
    assert stream.end_velocity == pytest.approx([.1]*7)
    end = stream.start_ns+math.ceil(stream.duration*1e9)
    before = stream.sample(end-100)
    after = stream.sample(end+100)
    assert before[0] == pytest.approx(after[0], abs=1e-7)
    assert before[1] == pytest.approx(after[1], abs=1e-6)
    q, dq, _ = stream.sample(end+200_000_000)
    assert q == pytest.approx([.004+.1**2/(2*1.5)]*7)
    assert dq == [0.]*7
    stream.retarget([.004]*7, end+200_000_000)
    start = stream.start_ns
    stream.retarget([.004]*7, end+220_000_000)
    assert stream.start_ns == start
    assert stream.sample(end+2_000_000_000)[0] == pytest.approx([.004]*7)
    with pytest.raises(RuntimeError, match='clock_regressed'):
        stream.sample(0)
    stream.reset()
    with pytest.raises(RuntimeError, match='reengage'):
        stream.sample(0)


@pytest.mark.parametrize('fault', ['hold', 'disconnect', 'feedback', 'lease', 'loop_gap', 'collision'])
def test_responsive_stop_cancels_all_phases_and_never_replays(fault):
    clock, backend, core = make_core({'profile': 'responsive'})
    target(core, 1, [.8]*7)
    for _ in range(4):
        clock.advance(.01); core.tick()
    assert backend.target[0] > 0
    generation = core.generation
    if fault == 'hold':
        core.request(dict(type='hold', session=core.session, reason='released'))
    elif fault == 'disconnect': core.disconnect(core.session)
    elif fault == 'feedback':
        backend.frozen = True; clock.advance(.11); core.tick()
    elif fault == 'lease':
        for _ in range(10): clock.advance(.01); core.tick()
        assert core.reason == 'feedback_reference_expired'
    elif fault == 'loop_gap':
        clock.advance(.11); core._refresh(); target(core, 2, [.8]*7); core.tick()
        assert core.reason == 'trajectory_control_gap'
    else:
        backend.fault = 'robot_collision'; clock.advance(.01); core.tick()
    assert not core.armed and core.trajectory is None and core.pending is None and core.active_target is None
    count, held = backend.publish_count, list(backend.target)
    for _ in range(30): clock.advance(.01); core.tick()
    assert backend.publish_count == count and backend.target == held
    if core.session:
        assert not target(core, 99, [.9]*7, generation=generation)['accepted']


def test_responsive_following_error_still_stops():
    clock, backend, core = make_core({'profile': 'responsive', 'max_following_error_rad': .01})
    snapshot = backend.snapshot
    backend.snapshot = lambda: dict(snapshot(), q=[0.]*7)
    for seq in range(40):
        clock.advance(.01); core._refresh(); target(core, seq, [.5]*7); core.tick()
        if not core.armed: break
    assert core.reason == 'trajectory_following_error' and backend.target == [0.]*7


@pytest.mark.parametrize("profile", ["responsive", "coordinated"])
def test_old_b_rejects_responsive_profile_before_arm(profile):
    backend = FakeBackend()
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient('127.0.0.1', server.control_port, server.ik_port).connect()
    try:
        client.hello['joint_trajectory'].pop('profiles')
        with pytest.raises(ValueError, match='profile'):
            client.arm(trajectory={'profile': profile})
        assert not server.core.armed
        client.arm(trajectory={})
        assert server.core.trajectory.algorithm == 'online_cubic_hermite_v2'
    finally:
        client.close(); server.close()


@pytest.mark.parametrize("profile", ["responsive", "coordinated"])
def test_tcp_release_discards_late_ik_and_regrip_starts_at_current_pose(tmp_path, profile):
    backend = FakeBackend()
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    quest = SteppedQuest()
    operator = Operator(RobotClient('127.0.0.1', server.control_port, server.ik_port),
                        quest, [], tmp_path, motion={'enabled': True, 'time_constant_ms': 20},
                        trajectory={'profile': profile}).start()
    try:
        wait_until(lambda: operator.epoch is not None)
        operator.request('calibrate')
        wait_until(lambda: operator.mapping is not None and operator.release_seen)
        quest.grip = 1; wait_until(lambda: operator.following)
        quest.position = np.array([.2, 0., 0.])
        wait_until(lambda: abs(backend.target[1]) > .01)
        backend.ik_delay = .06
        wait_until(lambda: operator.future is not None and not operator.future.done())
        quest.grip = 0
        wait_until(lambda: not operator.following and not server.core.armed)
        count, held = backend.publish_count, list(backend.target)
        time.sleep(.15)
        assert backend.publish_count == count and server.core.pending is None
        quest.position = np.array([.5, -.3, .2]); backend.ik_delay = .005
        time.sleep(.05); quest.grip = 1
        wait_until(lambda: operator.following)
        time.sleep(.1)
        assert backend.target == pytest.approx(held, abs=1e-6)
        assert list(tmp_path.iterdir()) == []
    finally:
        operator.close(); server.close()


def test_input_intervals_ignore_feedback_cadence_and_duplicates_keep_momentum():
    clock, backend, core = make_core({'profile': 'responsive'})
    target(core, 1, [.001]*7, source_sample_ns=1_000_000_000)
    clock.advance(.01); core.tick()
    target(core, 2, [.003]*7, source_sample_ns=1_020_000_000)
    clock.advance(.01); receipt, _ = core.tick()
    assert core.trajectory.end_velocity == pytest.approx([.1]*7)
    assert receipt['trajectory']['source_sample_ns'] == 1_020_000_000
    start = core.trajectory.start_ns
    target(core, 3, [.003]*7, source_sample_ns=1_020_000_000)
    clock.advance(.01); core.tick()
    assert core.trajectory.start_ns == start
    assert core.trajectory.end_velocity == pytest.approx([.1]*7)


def test_new_input_timestamps_never_extend_old_reference_lease():
    clock, backend, core = make_core({'profile': 'responsive'})
    reference = core.latest['seq']
    for seq in range(1, 13):
        clock.advance(.01)
        target(core, seq, [.2]*7, feedback_seq=reference, source_sample_ns=seq*100_000_000)
        core.tick()
    clock.advance(.01); core.tick()
    assert not core.armed and core.reason == 'feedback_reference_expired'


@pytest.mark.parametrize('stamp', [-1, True, 1.2, '123'])
def test_malformed_input_timestamp_stops_before_motion(stamp):
    clock, backend, core = make_core({'profile': 'responsive'})
    count = backend.publish_count
    with pytest.raises(ValueError, match='source_sample_ns'):
        target(core, 1, [.2]*7, source_sample_ns=stamp)
    assert not core.armed and core.trajectory is None
    # The only possible extra publish is the immediate measured-position hold.
    assert backend.publish_count <= count+1 and backend.target == [0.]*7


def test_irregular_latest_goals_keep_discrete_reference_bounds():
    clock, backend, core = make_core({'profile': 'responsive'})
    rng = random.Random(1310)
    rows, next_goal, seq = [], 0, 0
    for tick in range(600):
        clock.advance(.01); core._refresh()
        if tick >= next_goal:
            seq += 1
            target(core, seq, [.3*math.sin(tick*.017+j*.2) for j in range(7)],
                   source_sample_ns=tick*10_000_000)
            next_goal = tick+rng.choice([1, 2, 3, 5, 7])
        receipt, _ = core.tick()
        assert core.armed, core.reason
        assert receipt['trajectory']['algorithm'] == 'online_bounded_acceleration_v1'
        rows.append(receipt['q'])
    v = np.diff(rows, axis=0)/.01
    a = np.diff(v, axis=0)/.01
    assert np.max(abs(v)) <= .6+1e-9
    assert np.max(abs(a)) <= 1.5+1e-8
