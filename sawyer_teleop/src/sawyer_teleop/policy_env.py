"""State-only policy environment over the unchanged A/B protocol (Gazebo only)."""
import copy
import time

import numpy as np

from .client import RobotClient
from .protocol import JOINTS, ProtocolError, vector

ENV_NAME = "SawyerGazeboJointTarget"
ENV_TYPE = 1001
OBS_SHAPES = {"joint_pos": [7], "joint_vel": [7], "eef_pos": [3], "eef_quat": [4]}


def environment_metadata(normalization, reset_q, hz):
    return {"env_name": ENV_NAME, "type": ENV_TYPE, "env_version": "1",
            "env_kwargs": {"action_normalization": copy.deepcopy(normalization),
                           "reset_q": vector(reset_q, 7), "control_freq": hz,
                           "joint_names": list(JOINTS), "tip_frame": "right_hand"}}


class JointTargetEnvironment:
    def __init__(self, action_normalization, reset_q, control_freq=50, horizon=500,
                 host="127.0.0.1", control_port=8875, ik_port=8876,
                 joint_names=JOINTS, tip_frame="right_hand", reset_seconds=2.0):
        if list(joint_names) != JOINTS or tip_frame != "right_hand":
            raise ValueError("policy_joint_order_or_tip_mismatch")
        if not 10 <= control_freq <= 100 or type(horizon) is not int or horizon < 1:
            raise ValueError("invalid_policy_rate_or_horizon")
        if not np.isfinite(reset_seconds) or not .2 <= reset_seconds <= 30:
            raise ValueError("invalid_reset_duration")
        if action_normalization.get("kind") != "affine_dataset_encoding":
            raise ValueError("unsupported_action_encoding")
        self.center = np.asarray(vector(action_normalization.get("center"), 7))
        self.scale = np.asarray(vector(action_normalization.get("scale"), 7))
        if np.any(self.scale <= 0):
            raise ValueError("invalid_action_scale")
        self.reset_q = np.asarray(vector(reset_q, 7))
        self.kwargs = dict(action_normalization=copy.deepcopy(action_normalization),
                           reset_q=self.reset_q.tolist(), control_freq=control_freq,
                           horizon=horizon, host=host, control_port=control_port, ik_port=ik_port,
                           joint_names=list(JOINTS), tip_frame=tip_frame, reset_seconds=reset_seconds)
        self.period, self.horizon = 1/control_freq, horizon
        self.robot = None
        self.steps = 0
        self.done = True
        self.last_state = None
        self.last_receipt = None

    def _connect(self):
        reconnecting = self.robot is not None
        if self.robot:
            self.close()
        k = self.kwargs
        deadline = time.monotonic()+1
        while True:
            robot = RobotClient(k["host"], k["control_port"], k["ik_port"])
            try:
                robot.connect()
                break
            except (ProtocolError, OSError) as exc:
                robot.close()
                retryable = isinstance(exc, OSError) or "bridge_busy" in str(exc)
                if not reconnecting or not retryable or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)  # Wait only for our just-closed B session to release.
        try:
            hello = robot.hello
            if (hello["backend"] != "gazebo" or hello["tip_frame"] != k["tip_frame"]
                    or hello.get("description", {}).get("simulation") is not True):
                raise ValueError("policy_environment_requires_gazebo")
        except BaseException:
            robot.close()
            raise
        self.robot = robot

    def decode(self, action):
        action = np.asarray(vector(np.asarray(action).tolist(), 7), dtype=np.float64)
        if np.any(np.abs(action) > 1.000001):
            raise ValueError("policy_action_outside_dataset_encoding")
        return vector((action*self.scale+self.center).tolist(), 7)

    @staticmethod
    def observation(state):
        sample = state["sample"]
        return {"joint_pos": np.asarray(sample["q"], dtype=np.float32),
                "joint_vel": np.asarray(sample["dq"], dtype=np.float32),
                "eef_pos": np.asarray(sample["pose"]["p"], dtype=np.float32),
                "eef_quat": np.asarray(sample["pose"]["q"], dtype=np.float32)}

    def _submit(self, q):
        start = time.monotonic()
        state = self.robot.snapshot()
        if not state["armed"] or state["generation"] != self.robot.generation:
            raise RuntimeError("policy_disarmed_reset_required")
        seq = self.robot.target(q, state["sample"]["seq"])
        receipt = None
        while time.monotonic()-start < .15:
            for item in self.robot.drain_receipts():
                if item["seq"] == seq:
                    receipt = item
            state = self.robot.snapshot()
            if not state["armed"] or state["generation"] != self.robot.generation:
                raise RuntimeError("policy_disarmed_reset_required")
            if (receipt and state["sample"]["seq"] > receipt["before"]["seq"]
                    and state["sample"]["available_ns"] >= receipt["b_ns"]
                    and time.monotonic()-start >= self.period):
                self.last_state, self.last_receipt = state, receipt
                return state
            time.sleep(.001)
        raise TimeoutError("policy_submission_or_feedback_timeout")

    def reset(self):
        # New connection/session prevents reuse of old queued actions after failures.
        self._connect()
        self.done, self.steps = True, 0
        try:
            self.robot.hold("policy_reset")
            initial = np.asarray(self.robot.snapshot()["sample"]["q"])
            self.robot.arm()
            start = time.monotonic()
            duration = self.kwargs["reset_seconds"]
            while time.monotonic()-start < duration+4:
                fraction = min(1., (time.monotonic()-start)/duration)
                blend = fraction*fraction*(3-2*fraction)
                state = self._submit((initial+blend*(self.reset_q-initial)).tolist())
                if (fraction == 1 and np.max(np.abs(np.asarray(state["sample"]["q"])-self.reset_q)) < .02
                        and np.max(np.abs(state["sample"]["dq"])) < .1):
                    self.done = False
                    return self.observation(state)
            raise TimeoutError("policy_home_not_reached")
        except BaseException:
            self.close()
            raise

    def step(self, action):
        if self.done or self.robot is None:
            raise RuntimeError("policy_reset_required")
        try:
            q = self.decode(action)
            state = self._submit(q)
            self.steps += 1
            self.done = self.steps >= self.horizon
            info = {"submitted_seq": self.last_receipt["seq"], "joint_target_rad": q,
                    "feedback_seq": state["sample"]["seq"], "simulation": True,
                    "truncated": self.done, "task_success": None}
            if self.done:
                self.robot.hold("policy_horizon")
            return self.observation(state), 0., self.done, info
        except BaseException:
            self.close()
            raise

    def get_observation(self):
        if self.robot is None:
            raise RuntimeError("policy_reset_required")
        return self.observation(self.robot.snapshot())

    def close(self):
        self.done = True
        if self.robot:
            try:
                self.robot.hold("policy_stop")
            except Exception:
                pass
            finally:
                self.robot.close()
                self.robot = None

    def serialize(self):
        return {"env_name": ENV_NAME, "type": ENV_TYPE, "env_version": "1",
                "env_kwargs": copy.deepcopy(self.kwargs)}
