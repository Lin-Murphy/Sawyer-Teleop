"""Rotation must reach feedback and recordings, including without translation."""
import math
import time

import h5py
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from sawyer_teleop.backends import FakeBackend
from sawyer_teleop.client import RobotClient
from sawyer_teleop.demo import wait_until
from sawyer_teleop.operator import Operator
from sawyer_teleop.protocol import ns
from sawyer_teleop.quest import QuestValidator
from sawyer_teleop.server import BridgeServer


def assert_orientation(actual, expected):
    error = Rotation.from_quat(actual).inv()*Rotation.from_quat(expected)
    assert error.magnitude() < 1e-8


@pytest.mark.parametrize("angles", [
    [0, 0, 0], [.4, 0, 0], [0, -.6, 0], [0, 0, .8],
    [.3, -.5, .7], [1.1, 2.2, -2.4],
    [.3, math.pi/2, -.4], [.3, -math.pi/2, -.4],
])
def test_fake_rotation_solution_and_feedback_match_pose(angles):
    now = [1_000_000_000]
    backend = FakeBackend(clock=lambda: now[0])
    initial = backend.snapshot()
    quaternion = Rotation.from_euler("xyz", angles).as_quat()
    solution = backend.solve({"p": [.7, -.1, .6], "q": quaternion.tolist()}, [0]*7)
    assert solution[:4] == pytest.approx([.2, -.1, .3, 0])
    backend.publish(solution)
    now[0] += 10_000_000
    feedback = backend.snapshot()
    assert feedback["seq"] > initial["seq"]
    assert feedback["q"] == pytest.approx(solution)
    assert feedback["dq"] == pytest.approx(np.array(solution)/.01)
    assert feedback["pose"]["p"] == pytest.approx([.7, -.1, .6])
    assert_orientation(feedback["pose"]["q"], quaternion)
    assert backend.description()["test_only"]


@pytest.mark.parametrize("axis", range(3))
def test_fake_rotation_does_not_jump_at_pi(axis):
    backend = FakeBackend()
    seed = [0., 0., 0., .25, 0., 0., 0.]
    seed[4+axis] = math.pi-.085
    for angle in np.linspace(math.pi-.08, math.pi+.08, 33):
        quaternion = Rotation.from_rotvec(np.eye(3)[axis]*angle).as_quat()
        result = backend.solve({"p": [.5, 0, .3], "q": quaternion.tolist()}, seed)
        assert result[3] == .25  # Redundant test joint is not fabricated.
        assert result[4+axis] == pytest.approx(angle)
        assert np.linalg.norm(np.array(result[4:])-seed[4:]) < .006
        seed = result


@pytest.mark.parametrize("sign", [-1, 1])
def test_fake_rotation_continues_through_pitch_singularity(sign):
    backend = FakeBackend()
    seed = [0, 0, 0, .25, .3, sign*math.radians(88), -.4]
    for degrees in np.linspace(88, 92, 17):
        angles = [.3, sign*math.radians(degrees), -.4]
        quaternion = Rotation.from_euler("xyz", angles).as_quat()
        result = backend.solve({"p": [.5, 0, .3], "q": quaternion.tolist()}, seed)
        assert result[4:] == pytest.approx(angles)
        assert np.linalg.norm(np.array(result)-seed) < .01
        seed = result


def test_fake_arbitrary_orientation_and_quaternion_sign():
    backend = FakeBackend()
    rng = np.random.default_rng(17)
    for quaternion in Rotation.random(200, random_state=rng).as_quat():
        seed = rng.uniform(-3*math.pi, 3*math.pi, 7).tolist()
        target = {"p": [.5, 0, .3], "q": quaternion.tolist()}
        result = backend.solve(target, seed)
        negated = backend.solve(dict(target, q=(-quaternion).tolist()), seed)
        assert result == pytest.approx(negated)
        assert result[3] == seed[3]
        backend.publish(result)
        backend.sample = None
        assert_orientation(backend.snapshot()["pose"]["q"], quaternion)
        assert backend.solve(target, result) == pytest.approx(result)


class RotatingQuest:
    """Real protocol parser with a stationary controller position and test rotation."""
    def __init__(self):
        self.grip = 0
        self.rotation = Rotation.identity()
        self.sequence = 0
        self.validator = QuestValidator(0, 0)

    def start(self):
        return self

    def close(self):
        pass

    def poll(self):
        self.sequence += 1
        stamp = ns()
        hand = {"input_ok": True, "tracking_ok": True, "status": 15,
                "grip": self.grip, "trigger": 0, "buttons": 0, "recenter": 0, "device": 2,
                "pose": {"p": [.1, 1.2, -.2], "q": self.rotation.as_quat().tolist()}}
        packet = {"protocol": 3, "session": "rotation-test", "seq": self.sequence,
                  "sample_ns": stamp, "focused": True, "mounted": True, "space": 1,
                  "head_status": 15, "head": {"p": [0, 1.6, 0], "q": [0, 0, 0, 1]},
                  "recenter": 0, "hands": {"r": hand, "l": dict(hand, grip=0)}}
        return self.validator.parse(packet, stamp)


def test_stationary_hand_rotation_reaches_b_ui_and_hdf5_without_clutch_jump(tmp_path):
    backend = FakeBackend()
    bridge = BridgeServer(backend, control_port=0, ik_port=0).start()
    quest = RotatingQuest()
    operator = Operator(RobotClient("127.0.0.1", bridge.control_port, bridge.ik_port),
                        quest, [], tmp_path)
    try:
        operator.start()
        wait_until(lambda: operator.epoch is not None)
        operator.request("calibrate")
        wait_until(lambda: operator.mapping is not None and operator.release_seen)
        quest.grip = 1
        wait_until(lambda: operator.following)
        operator.request("start_recording", name="rotation", count=1, seconds=60, pause=1)
        wait_until(lambda: operator.episode is not None)
        episode = operator.episode
        # Calibration maps Quest X/Y/Z to robot -Y/+Z/-X.
        for axis, expected in [("x", [0, -.35, 0]), ("y", [0, 0, .35]),
                               ("z", [-.35, 0, 0])]:
            quest.rotation = Rotation.from_euler(axis, .35)
            wait_until(lambda: np.allclose(operator.view()["joints"][4:], expected))
            # The displayed joints use a looser tolerance than assert_orientation.
            # Wait for the filtered pose to settle to the same strict tolerance.
            wanted = Rotation.from_euler("xyz", expected)
            wait_until(lambda: (Rotation.from_quat(
                operator.robot.snapshot()["sample"]["pose"]["q"]).inv()*wanted).magnitude() < 1e-8)
            sample = operator.robot.snapshot()["sample"]
            assert sample["q"][:4] == pytest.approx([0]*4)
            assert_orientation(sample["pose"]["q"],
                               Rotation.from_euler("xyz", expected).as_quat())
        # Release, reposition the hand, and re-engage without changing the robot pose.
        quest.grip = 0
        wait_until(lambda: not operator.following)
        held = operator.robot.snapshot()["sample"]
        quest.rotation = Rotation.from_euler("xyz", [.5, -.2, .8])
        time.sleep(.06)
        assert operator.robot.snapshot()["sample"]["q"] == pytest.approx(held["q"])
        count = bridge.core.submitted
        quest.grip = 1
        wait_until(lambda: operator.following and bridge.core.submitted > count+2)
        assert operator.robot.snapshot()["sample"]["q"] == pytest.approx(held["q"])
        quest.grip = 0
        wait_until(lambda: not operator.following)
        operator.request("end_recording")
        wait_until(lambda: operator.finishing is None and episode.result is not None)
        with h5py.File(episode.result/"raw.hdf5") as data:
            assert data.attrs["complete"]
            for name in ("states/q", "commands/q"):
                assert np.all(np.max(np.abs(data[name][:, 4:7]), axis=0) > .3)
            assert np.max(np.abs(data["states/eef_quat"][:, :3])) > .1
    finally:
        operator.close()
        bridge.close()
