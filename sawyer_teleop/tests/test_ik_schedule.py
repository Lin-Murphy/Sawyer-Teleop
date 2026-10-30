"""Deterministic A scheduling and cancellation checks; no physical robot."""
import concurrent.futures

import pytest

import sawyer_teleop.operator as module
from sawyer_teleop.a_timing import INPUT_MAX_AGE_S, IK_TASK_TIMEOUT_NS
from sawyer_teleop.operator import Operator
from test_motion import SteppedQuest


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        future = concurrent.futures.Future()
        self.jobs.append((future, function, args))
        return future


class Robot:
    hello = {"backend": "fake"}
    rtt_ms = 0.
    generation = 0

    def __init__(self):
        self.targets = []
        self.holds = []
        self.armed = False

    def snapshot(self, require_fresh=True):
        return {"generation": self.generation, "armed": self.armed, "age_s": 0.,
                "clock_offset_ns": 0, "clock_uncertainty_ns": 0,
                "reason": "test_pause", "sample": {"seq": 1, "q": [0.]*7,
                "pose": {"p": [.5, 0., .3], "q": [0., 0., 0., 1.]}}}

    def arm(self, **kwargs):
        self.generation += 1
        self.armed = True

    def hold(self, reason):
        self.generation += 1
        self.armed = False
        self.holds.append(reason)
        return {"holding": True, "reason": reason}

    def target(self, q, feedback_seq, **kwargs):
        self.targets.append((q, feedback_seq, kwargs))
        return len(self.targets)

    def gripper_observation(self, value):
        pass

    def drain_receipts(self):
        return []


@pytest.fixture(params=["responsive", "coordinated"])
def rig(tmp_path, monkeypatch, request):
    clock = [1_000_000_000_000]
    monkeypatch.setattr(module, "ns", lambda: clock[0])

    class Quest(SteppedQuest):
        stamp = clock[0]

        def poll(self):
            value = super().poll()
            value.update(received_ns=clock[0], mapped_ns=clock[0])
            value["packet"]["sample_ns"] = self.stamp
            return value

    def create(hz=120):
        robot, quest = Robot(), Quest()
        operator = Operator(robot, quest, [], tmp_path, hz=hz,
                            motion={"enabled": True, "time_constant_ms": 20},
                            trajectory={"enabled": True, "profile": request.param})
        operator.executor.shutdown(wait=True)
        operator.executor = ManualExecutor()
        operator.tick()
        operator.request("calibrate")
        operator.tick()
        quest.grip = 1
        operator.tick()
        assert operator.following and len(operator.executor.jobs) == 1
        return clock, robot, quest, operator

    return create


def finish(operator, clock):
    operator.future.set_result(([0.]*7, operator.future_info["started"], clock[0]))


@pytest.mark.parametrize("hz", [50, 90, 120])
def test_fast_result_sends_now_but_next_solve_obeys_actual_queue_interval(rig, hz):
    clock, robot, quest, operator = rig(hz)
    due = operator.future_info["started"] + operator.ik_period_ns
    clock[0] += 1_000_000
    finish(operator, clock)
    quest.stamp += 1
    operator.tick()
    assert len(robot.targets) == 1 and operator.future is None
    assert operator.commands[1]["timing"]["target_sent_ns"] == clock[0]
    assert robot.targets[0][2]["source_sample_ns"] == quest.stamp-1
    clock[0] = due-1
    operator.tick()
    assert len(operator.executor.jobs) == 1
    clock[0] = due
    operator.tick()
    assert len(operator.executor.jobs) == 2
    assert operator.future_info["source_sample_ns"] == quest.stamp


def test_slow_solve_starts_latest_immediately_without_queued_intermediate_samples(rig):
    clock, robot, quest, operator = rig()
    for _ in range(3):
        clock[0] += 10_000_000
        quest.stamp += 10_000_000
        operator.tick()
        assert len(operator.executor.jobs) == 1
    finish(operator, clock)
    operator.tick()
    assert len(robot.targets) == 1 and len(operator.executor.jobs) == 2
    assert operator.future_info["source_sample_ns"] == quest.stamp
    assert operator.future_info["started"] == clock[0]
    assert operator.commands[1]["source_sample_ns"] == quest.stamp-30_000_000
    # A long solve cannot create a catch-up burst of queued work.
    finish(operator, clock)
    quest.stamp += 1
    operator.tick()
    assert len(operator.executor.jobs) == 2


def test_duplicate_sample_is_not_resolved_and_does_not_prevent_release(rig):
    clock, robot, quest, operator = rig()
    clock[0] += 20_000_000
    finish(operator, clock)
    operator.tick()
    for _ in range(3):
        clock[0] += 10_000_000
        operator.tick()
    assert len(robot.targets) == 1 and len(operator.executor.jobs) == 1
    quest.grip = 0
    operator.tick()
    assert not operator.following and not robot.armed
    assert robot.holds[-1].startswith("Grip released")
    # Re-engagement resets deduplication and never replays the old result.
    quest.grip = 1
    operator.tick()
    assert operator.following and len(operator.executor.jobs) == 2
    assert len(robot.targets) == 1


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("fault", ["release", "tracking", "generation", "shutdown"])
def test_120hz_stop_checks_precede_dispatch_and_new_solve(rig, fault, completed):
    clock, robot, quest, operator = rig()
    clock[0] += 20_000_000
    if completed:
        finish(operator, clock)
    if fault == "release":
        quest.grip = 0
    elif fault == "tracking":
        quest.valid = False
    elif fault == "generation":
        robot.generation += 1
        # Real client keeps its armed generation separate from telemetry.
        original = robot.snapshot
        robot.snapshot = lambda **kw: dict(original(**kw), generation=robot.generation+1,
                                            armed=False)
    else:
        operator.stop.set()
    operator.tick()
    assert not robot.targets and len(operator.executor.jobs) == 1
    if not completed:
        finish(operator, clock)
        operator.tick()
        assert not robot.targets and len(operator.executor.jobs) == 1


def test_120hz_does_not_extend_original_input_or_ik_lifetime(rig):
    clock, robot, quest, operator = rig()
    # Just past the input limit (200 ms since 2026-10-29) after 10 ms more.
    operator.future_info["timing"]["input_mapped_ns"] -= int(INPUT_MAX_AGE_S*1e9)-5_000_000
    clock[0] += 10_000_000
    quest.stamp += 10_000_000
    finish(operator, clock)
    operator.tick()
    assert not robot.targets and not operator.following
    assert operator.view()["message"] == "ik_input_expired"


def test_120hz_rejects_expired_slow_ik_without_queueing_more(rig):
    clock, robot, quest, operator = rig()
    clock[0] += IK_TASK_TIMEOUT_NS+1
    quest.stamp += IK_TASK_TIMEOUT_NS+1
    operator.tick()
    assert not operator.following and not robot.targets
    assert operator.view()["message"] == "ik_deadline"
    assert len(operator.executor.jobs) == 1


@pytest.mark.parametrize("hz", [0, 120.01, float("inf"), float("nan")])
def test_rate_above_120_or_invalid_is_rejected(tmp_path, hz):
    with pytest.raises(ValueError, match="invalid operator parameters"):
        Operator(None, None, [], tmp_path, hz=hz)
