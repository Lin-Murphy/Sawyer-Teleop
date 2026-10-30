import copy
import json
import time

import h5py
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.client import RobotClient
from sawyer_teleop.demo import SyntheticQuest, wait_until
from sawyer_teleop.motion import PoseSmoother
from sawyer_teleop.motion_report import analyze_motion
from sawyer_teleop.operator import Operator
from sawyer_teleop.server import BridgeServer


ORIGIN = {"p": [0., 0., 0.], "q": [0., 0., 0., 1.]}


@pytest.mark.parametrize("options", [
    {"enabled": "yes"}, {"time_constant_ms": float("nan")},
    {"time_constant_ms": 0}, {"time_constant_ms": 151}, {"typo": 40},
])
def test_bad_motion_settings_fail_explicitly(options):
    with pytest.raises(ValueError):
        PoseSmoother(options)


def test_disabled_smoothing_preserves_full_precision_and_does_not_mutate():
    target = {"p": [.123456789123, -.1241, 12.43], "q": [0., 0., 0., 1.]}
    before = copy.deepcopy(target)
    assert PoseSmoother().update(target, 123) == before
    assert target == before


def test_filter_uses_input_interval_and_no_extra_steps_for_duplicate_samples():
    target = dict(ORIGIN, p=[1., 0., 0.])
    single, divided = [PoseSmoother({"enabled": True}) for _ in range(2)]
    for smoother in (single, divided):
        smoother.reset(ORIGIN, 0)
    expected = single.update(target, 40_000_000)
    divided.update(target, 10_000_000)
    assert divided.update(target, 40_000_000)["p"] == pytest.approx(expected["p"])
    assert divided.update(target, 40_000_000) == expected
    assert 0 < expected["p"][0] < 1
    assert expected["p"][0] == pytest.approx(1-np.exp(-1))
    with pytest.raises(RuntimeError, match="time_regressed"):
        divided.update(target, 39_000_000)
    with pytest.raises(RuntimeError, match="input_gap"):  # gap limit 200 ms since 2026-10-29
        divided.update(target, 250_000_000)
    divided.reset()
    with pytest.raises(RuntimeError, match="reengage"):
        divided.update(target, 251_000_000)


def test_rotation_smoothing_uses_short_arc_across_180_degrees_and_quaternion_sign():
    first = Rotation.from_euler("z", 179, degrees=True)
    last = Rotation.from_euler("z", -179, degrees=True)
    results = []
    for sign in (1, -1):
        smoother = PoseSmoother({"enabled": True})
        smoother.reset(dict(ORIGIN, q=first.as_quat().tolist()), 0)
        result = smoother.update(dict(ORIGIN, q=(sign*last.as_quat()).tolist()), 20_000_000)
        rotation = Rotation.from_quat(result["q"])
        assert np.rad2deg((first.inv()*rotation).magnitude()) < 2
        assert np.rad2deg((last.inv()*rotation).magnitude()) < 2
        assert np.linalg.norm(result["q"]) == pytest.approx(1.)
        results.append(rotation.as_matrix())
    assert np.allclose(*results)


class SteppedQuest(SyntheticQuest):
    def __init__(self):
        super().__init__()
        self.position = np.zeros(3)
        self.rotation = Rotation.identity()

    def poll(self):
        value = super().poll()
        value["transform"][:3, 3] = self.position
        value["transform"][:3, :3] = self.rotation.as_matrix()
        value["mapped_ns"], value["uncertainty_ns"] = value["received_ns"], 0
        for hand in value["packet"]["hands"].values():
            hand["pose"] = {"p": self.position.tolist(), "q": self.rotation.as_quat().tolist()}
        return value


@pytest.fixture(params=[20.0, 40.0, 150.0], ids=['light', 'heavy', 'droid_like'])
def smooth_link(tmp_path, request):
    backend = FakeBackend()
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    quest = SteppedQuest()
    operator = Operator(RobotClient("127.0.0.1", server.control_port, server.ik_port),
                        quest, [], tmp_path, motion={"enabled": True, "time_constant_ms": request.param}).start()
    try:
        wait_until(lambda: operator.epoch is not None)
        operator.request("calibrate")
        wait_until(lambda: operator.mapping is not None and operator.release_seen)
        quest.grip = 1
        wait_until(lambda: operator.following)
        yield backend, server, quest, operator
    finally:
        operator.close()
        server.close()


def test_smoothed_capture_keeps_raw_input_and_actual_submitted_actions(smooth_link):
    _, _, quest, operator = smooth_link
    operator.request("start_recording", name="smooth", count=1, seconds=60)
    wait_until(lambda: operator.episode is not None)
    episode = operator.episode
    quest.position = np.array([.123456789123, 0, 0])
    quest.rotation = Rotation.from_euler("y", .4)
    time.sleep(.3)
    quest.grip = 0
    wait_until(lambda: not operator.following)
    operator.request("end_recording")
    wait_until(lambda: episode.result is not None and operator.finishing is None)
    events = [json.loads(line) for line in (episode.result/"events.jsonl").read_text(encoding="utf-8").splitlines()]
    submitted = [e for e in events if e["reason"] == "submitted"]
    assert any(e["quest"]["hands"]["r"]["pose"]["p"][0] == .123456789123 for e in submitted)
    assert any(np.linalg.norm(np.asarray(e["raw_target_pose"]["p"])-e["target_pose"]["p"]) > .001
               for e in submitted)
    with h5py.File(episode.result/"raw.hdf5") as data:
        assert json.loads(data.attrs["metadata"])["motion"]["enabled"]
        # The Cartesian IK goal is not the intermediate command actually submitted.
        targets = {e["command_seq"]: e["submitted_q"] for e in submitted}
        for seq, q in zip(data["commands/seq"][:], data["commands/q"][:]):
            assert q == pytest.approx(targets[seq])
        assert len(set(data["commands/seq"][:])) == len(data["commands/seq"])
        assert any(np.linalg.norm(np.asarray(e["submitted_q"]) - e["trajectory"]["goal_q"]) > .001
                   for e in submitted)
    report = analyze_motion(episode.result)["episodes"][0]
    assert report["complete"] and report["test_only"]
    assert report["timing_ms"]["ik_call"]["count"] > 0
    assert report["timing_ms"]["input_sample_to_submit_estimate"]["p95"] >= 0
    assert report["raw_to_filtered_position_mm"]["max"] > 1
    from sawyer_teleop.dataset import export_dataset
    exported = export_dataset(episode.result.parent, episode.result.parent/"dataset.hdf5")
    assert exported["episodes"] == 1


@pytest.mark.parametrize("fault", ["release", "tracking", "feedback", "slow_ik"])
def test_pausing_clears_smoothing_and_cannot_send_a_delayed_target(smooth_link, fault):
    backend, server, quest, operator = smooth_link
    quest.position = np.array([.1, 0, 0])
    wait_until(lambda: abs(backend.snapshot()["q"][1]) > .01)
    if fault == "release":
        quest.grip = 0
    elif fault == "tracking":
        quest.valid = False
    elif fault == "feedback":
        backend.frozen = True
    else:
        backend.ik_delay = .15
    wait_until(lambda: not operator.following)
    count = server.core.submitted
    time.sleep(.18)
    assert server.core.submitted == count
    assert operator.smoother.value is None and operator.ik_seed is None


def test_reengage_starts_at_feedback_not_previous_filter_target(smooth_link):
    backend, server, quest, operator = smooth_link
    quest.position = np.array([.2, .1, 0])
    quest.rotation = Rotation.from_euler("xyz", [.2, -.3, .4])
    wait_until(lambda: abs(backend.snapshot()["q"][1]) > .04)
    quest.grip = 0
    wait_until(lambda: not operator.following)
    held = list(backend.snapshot()["q"])
    quest.position = np.array([-.8, .2, -.5])
    quest.rotation = Rotation.from_euler("xyz", [1.2, -.6, .9])
    time.sleep(.08)
    count = server.core.submitted
    quest.grip = 1
    wait_until(lambda: operator.following and server.core.submitted > count+2)
    assert backend.snapshot()["q"] == pytest.approx(held)


def test_ik_seed_continues_previous_solution_even_when_feedback_lags(tmp_path):
    class LaggingBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.solutions = []
            self.seeds = []
        def snapshot(self):
            sample = super().snapshot()
            sample.update(q=[0.]*7, pose={"p": [.5, 0., .3], "q": [0., 0., 0., 1.]})
            return sample
        def solve(self, target, seed):
            self.seeds.append(list(seed))
            answer = super().solve(target, seed)
            self.solutions.append(answer)
            return answer
    backend = LaggingBackend()
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
        quest.position = np.array([.1, 0, 0])
        wait_until(lambda: len(backend.solutions) > 6 or not operator.following)
        assert len(backend.solutions) > 6, operator.view()
        for seed, previous in zip(backend.seeds[1:6], backend.solutions[:5]):
            assert seed == pytest.approx(previous)
        assert any(abs(seed[1]) > .01 for seed in backend.seeds)
    finally:
        operator.close()
        server.close()
