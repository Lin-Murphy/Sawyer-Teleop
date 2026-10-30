"""Whole-curve bounds, live replacement, source leases, and receipt semantics."""
import math
import random
import time

import numpy as np
import pytest

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.bridge import BridgeCore
from sawyer_teleop.client import RobotClient
from sawyer_teleop.demo import wait_until
from sawyer_teleop.operator import Operator
from sawyer_teleop.server import BridgeServer
from sawyer_teleop.trajectory import JointTrajectory, trajectory_options
from test_bridge import Clock, target
from test_motion import SteppedQuest


@pytest.mark.parametrize("options", [
    {"enabled": 1}, {"unknown": 2}, {"max_velocity_rad_s": [0]*7},
    {"max_acceleration_rad_s2": [1]*6}, {"min_duration_ms": float("nan")},
    {"max_velocity_rad_s": [float("inf")]*7},
    {"max_following_error_rad": 0},
])
def test_bad_limits_rejected(options):
    with pytest.raises(ValueError):
        trajectory_options(options)


def test_random_retargets_preserve_position_velocity_and_whole_curve_bounds():
    rng = random.Random(804)
    limits = {
        "max_velocity_rad_s": [.2 + i*.1 for i in range(7)],
        "max_acceleration_rad_s2": [.5 + i*.2 for i in range(7)],
    }
    stream = JointTrajectory(limits)
    stamp = 1_000_000_000
    stream.reset([0.0]*7, stamp)
    for _ in range(200):
        stamp += rng.randint(5_000_000, 70_000_000)
        before, velocity, _ = stream.sample(stamp)
        stream.retarget([rng.uniform(-1, 1) for _ in range(7)], stamp)
        after, next_velocity, _ = stream.sample(stamp)
        assert after == pytest.approx(before, abs=1e-12)
        assert next_velocity == pytest.approx(velocity, abs=1e-12)
        # Independent dense evaluation, including endpoints and every velocity extremum.
        fractions = list(np.linspace(0, 1, 121))
        fractions += [-c2/(3*c3) for _, c2, c3 in stream.coefficients
                      if c3 and 0 < -c2/(3*c3) < 1]
        for u in fractions:
            for i, (c1, c2, c3) in enumerate(stream.coefficients):
                v = (c1 + 2*c2*u + 3*c3*u*u)/stream.duration
                a = (2*c2 + 6*c3*u)/stream.duration**2
                assert abs(v) <= limits["max_velocity_rad_s"][i] + 1e-10
                assert abs(a) <= limits["max_acceleration_rad_s2"][i] + 1e-10


def test_duplicate_goal_finishes_and_reset_drops_old_momentum():
    stream = JointTrajectory()
    stamp = 1_000_000_000
    stream.reset([0]*7, stamp)
    stream.retarget([.3]*7, stamp)
    started = stream.start_ns
    for i in range(1, 201):
        stream.retarget([.3]*7, stamp+i*10_000_000)
        assert stream.start_ns == started
    assert stream.sample(stamp+2_000_000_000) == ([.3]*7, [0.]*7, [0.]*7)
    stream.reset()
    with pytest.raises(RuntimeError, match="reengage"):
        stream.sample(stamp+2_000_000_001)
    stream.reset([.12]*7, stamp+3_000_000_000)
    assert stream.sample(stamp+3_010_000_000) == ([.12]*7, [0.]*7, [0.]*7)
    with pytest.raises(RuntimeError, match="clock_regressed"):
        stream.sample(stamp)


def test_moving_endpoint_brakes_continuously_when_next_goal_is_missing():
    stream = JointTrajectory()
    stamp = 1_000_000_000
    stream.reset([0]*7, stamp)
    stream.retarget([.002]*7, stamp)
    stream.retarget([.004]*7, stamp+20_000_000)
    assert stream.end_velocity == pytest.approx([.1]*7)
    end = stream.start_ns + round(stream.duration*1e9)
    before = stream.sample(end-1_000)
    after = stream.sample(end+1_000)
    assert before[0] == pytest.approx(after[0], abs=3e-7)
    assert before[1] == pytest.approx(after[1], abs=1e-5)
    points = [stream.sample(end+i*1_000_000) for i in range(2, 151)]
    assert all(max(map(abs, p[1])) <= .1+1e-9 for p in points)
    assert all(max(map(abs, p[2])) <= 1.5+1e-9 for p in points)
    expected = .004 + .1**2/(2*1.5)
    assert points[-1][0] == pytest.approx([expected]*7)
    assert points[-1][1:] == ([0.]*7, [0.]*7)


def test_endpoint_velocity_uses_source_time_not_jittery_arrival_time():
    stream = JointTrajectory()
    stream.reset([0]*7, 1_000_000_000)
    stream.retarget([.01]*7, 1_000_000_000, 900_000_000)
    stream.retarget([.012]*7, 1_070_000_000, 920_000_000)
    assert stream.end_velocity == pytest.approx([.1]*7)
    # Reusing an old source timestamp must not create an infinite derivative.
    stream.retarget([.014]*7, 1_080_000_000, 920_000_000)
    assert stream.end_velocity == [0.]*7


def make_core(options=None):
    clock = Clock()
    backend = FakeBackend(clock)
    core = BridgeCore(backend, clock=clock)
    hello = core.connect()
    assert hello["joint_trajectory"]["per_sample_receipts"]
    core.request(dict(type="arm", session=core.session, feedback_seq=core.latest["seq"],
                      trajectory=options or {}))
    return clock, backend, core


def test_interpolation_uses_b_clock_with_irregular_latest_only_goals():
    clock, backend, core = make_core()
    rows = []
    rng = random.Random(71)
    next_goal, seq = 0, 0
    for tick in range(500):
        clock.advance(.01)
        core._refresh()
        if tick >= next_goal:
            seq += 1
            goal = [0.3*math.sin(tick*.017 + j*.2) for j in range(7)]
            target(core, seq, goal)
            next_goal = tick + rng.choice([1, 2, 3, 5, 7])
        receipt, _ = core.tick()
        assert core.armed, core.reason
        rows.append(receipt)
    assert len(rows) > seq*2
    assert len({r["execution_seq"] for r in rows}) == len(rows)
    assert any(a["seq"] == b["seq"] and a["q"] != b["q"] for a, b in zip(rows, rows[1:]))
    points = np.asarray([r["q"] for r in rows])
    velocities = np.diff(points, axis=0)/.01
    accelerations = np.diff(velocities, axis=0)/.01
    assert np.max(np.abs(velocities)) <= .6 + 1e-9
    assert np.max(np.abs(accelerations)) <= 1.5 + 1e-8
    for row in rows:
        assert max(map(abs, row["trajectory"]["dq"])) <= .6+1e-10
        assert max(map(abs, row["trajectory"]["ddq"])) <= 1.5+1e-10


@pytest.mark.parametrize("fault", ["hold", "disconnect", "feedback", "lease", "loop_gap", "collision"])
def test_stop_cancels_curve_without_finishing_or_replaying(fault):
    clock, backend, core = make_core()
    target(core, 1, [.5]*7)
    for _ in range(4):
        clock.advance(.01)
        core.tick()
    assert 0 < backend.target[0] < .5
    generation = core.generation
    if fault == "hold":
        core.request(dict(type="hold", session=core.session, reason="released"))
    elif fault == "disconnect":
        core.disconnect(core.session)
    elif fault == "feedback":
        backend.frozen = True
        clock.advance(.11)
        core.tick()
    elif fault == "lease":
        for _ in range(9):
            clock.advance(.01)
            core.tick()
        assert core.reason == "feedback_reference_expired"
    elif fault == "loop_gap":
        clock.advance(.11)
        core._refresh()
        target(core, 2, [.5]*7)
        core.tick()
        assert core.reason == "trajectory_control_gap"
    else:
        backend.fault = "robot_collision"
        clock.advance(.01)
        core.tick()
    assert not core.armed
    assert core.trajectory is None and core.pending is None and core.active_target is None
    count, held = backend.publish_count, list(backend.target)
    for _ in range(25):
        clock.advance(.01)
        core.tick()
    assert backend.publish_count == count and backend.target == held
    if core.session:
        assert target(core, 99, [.9]*7, generation=generation)["accepted"] is False


def test_latest_goal_wins_and_rearm_starts_from_current_feedback():
    clock, backend, core = make_core()
    target(core, 1, [.8]*7)
    target(core, 2, [-.2]*7)
    clock.advance(.01)
    receipt, _ = core.tick()
    assert receipt["seq"] == 2 and receipt["trajectory"]["goal_q"] == [-.2]*7
    clock.advance(.01)
    core.tick()
    core.request(dict(type="hold", session=core.session))
    held = list(backend.target)
    core.request(dict(type="arm", session=core.session, feedback_seq=core.latest["seq"],
                      trajectory={}))
    target(core, 3, held)
    clock.advance(.01)
    receipt, _ = core.tick()
    assert receipt["q"] == held
    assert receipt["trajectory"]["dq"] == [0.]*7


def test_repeated_goal_cannot_renew_an_old_feedback_reference():
    clock, backend, core = make_core()
    reference = core.latest["seq"]
    for seq in range(1, 13):
        clock.advance(.01)
        core.request(dict(type="target", session=core.session, seq=seq,
                          generation=core.generation, feedback_seq=reference, q=[.5]*7))
        core.tick()
    clock.advance(.01)
    core.tick()
    assert not core.armed and core.reason == "feedback_reference_expired"
    assert core.trajectory is None


def test_tracking_error_pauses_instead_of_accumulating_command_distance():
    clock, backend, core = make_core({"max_following_error_rad": .01})
    real_snapshot = backend.snapshot
    def stationary_feedback():
        sample = real_snapshot()
        return dict(sample, q=[0.]*7)
    backend.snapshot = stationary_feedback
    for seq in range(1, 100):
        clock.advance(.01)
        core._refresh()
        target(core, seq, [.5]*7)
        core.tick()
        if not core.armed:
            break
    assert core.reason == "trajectory_following_error"
    assert backend.target == [0.]*7


@pytest.mark.parametrize("fault", ["release", "slow_ik", "orientation", "epoch"])
def test_real_tcp_trajectory_stop_and_no_delayed_ik_replay(tmp_path, fault):
    backend = FakeBackend()
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    quest = SteppedQuest()
    operator = Operator(RobotClient("127.0.0.1", server.control_port, server.ik_port),
                        quest, [], tmp_path, motion={"enabled": True}).start()
    try:
        wait_until(lambda: operator.epoch is not None)
        operator.request("calibrate")
        wait_until(lambda: operator.mapping is not None and operator.release_seen)
        quest.grip = 1
        wait_until(lambda: operator.following)
        quest.position = np.array([.2, 0., 0.])
        wait_until(lambda: abs(backend.target[1]) > .01)
        backend.ik_delay = .06
        wait_until(lambda: operator.future is not None and not operator.future.done())
        if fault == "release":
            quest.grip = 0
        elif fault == "slow_ik":
            backend.ik_delay = .2
        elif fault == "orientation":
            operator.request("reset_orientation")
        else:
            poll = quest.poll
            quest.poll = lambda: dict(poll(), epoch=("new-reference", 1, 0, 2))
        wait_until(lambda: not operator.following)
        wait_until(lambda: not server.core.armed)
        if fault == "epoch":
            assert operator.mapping is None
        elif fault == "orientation":
            assert "Direction reset" in operator.view()["message"]
        held, count = list(backend.target), server.core.submitted
        time.sleep(.25)
        assert server.core.submitted == count and backend.target == held
        assert server.core.trajectory is None
        assert operator.smoother.value is None
        assert not tmp_path.exists() or not list(tmp_path.iterdir())
    finally:
        operator.close()
        server.close()


def test_older_b_requires_explicit_upgrade_for_trajectory_but_direct_client_still_works():
    backend = FakeBackend()
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    client = RobotClient("127.0.0.1", server.control_port, server.ik_port).connect()
    try:
        client.hello.pop("joint_trajectory")
        with pytest.raises(ValueError, match="Update B"):
            client.arm(trajectory={})
        assert not server.core.armed
        client.arm()
        state = client.snapshot()
        sequence = client.target([.1]*7, state["sample"]["seq"])
        wait_until(lambda: server.core.submitted_seq == sequence)
        assert backend.target == [.1]*7
        assert server.core.trajectory is None
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("speed", [.05, .1, .2, .4, .55])
def test_below_limit_motion_does_not_accumulate_search_slack(speed):
    stream = JointTrajectory()
    stream.reset([0.]*7, 0)
    gaps = []
    for tick in range(1201):
        stamp = tick*10_000_000
        if tick % 2 == 0:
            stream.retarget([speed*tick*.01]*7, stamp)
        q, dq, ddq = stream.sample(stamp)
        assert max(map(abs, dq)) <= .6+1e-10
        assert max(map(abs, ddq)) <= 1.5+1e-10
        if tick >= 800:
            gaps.append((speed*tick*.01-q[0])/speed)
    # No frame-by-frame rest target and no accumulating lag below the speed cap.
    # This is reference tracking only; device/network/physical delays are absent.
    assert max(gaps) < .06
    assert abs(dq[0]-speed) < .005
    assert gaps[-1] <= gaps[0]+.001


def test_saturated_motion_still_respects_limit_and_reset_cancels_it():
    stream = JointTrajectory()
    stream.reset([0.]*7, 0)
    for tick in range(601):
        stamp = tick*10_000_000
        if tick % 2 == 0:
            stream.retarget([.8*tick*.01]*7, stamp)
        q, dq, ddq = stream.sample(stamp)
        assert max(map(abs, dq)) <= .6+1e-10
        assert max(map(abs, ddq)) <= 1.5+1e-10
    assert .8*6-q[0] > 1.2  # Real speed saturation is intentionally not bypassed.
    stream.reset([.12]*7, stamp)
    assert stream.sample(stamp+10_000_000) == ([.12]*7, [0.]*7, [0.]*7)
