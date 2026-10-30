"""Reproducible desktop-only acceptance flow. Never imports or connects to ROS."""
import math
from pathlib import Path
import time

import numpy as np

from .backends import FakeBackend
from .cameras import SyntheticCamera
from .client import RobotClient
from .dataset import export_dataset
from .operator import Operator
from .protocol import ns
from .server import BridgeServer
from .rates import EventRate


class SyntheticQuest:
    def __init__(self):
        self.grip = 0
        self.trigger = 0.
        self.events = []
        self.valid = True
        self.input_rate = EventRate()

    def start(self):
        return self

    def poll(self):
        self.input_rate.mark()
        now = time.monotonic()
        transform = np.eye(4)
        transform[:3, 3] = [.05*math.sin(now), .03*math.cos(now), .02*math.sin(2*now)]
        events, self.events = self.events, []
        return {"valid": self.valid, "reason": "synthetic_tracking_loss",
                "grip": self.grip, "events": events, "transform": transform,
                "head_forward": np.array([0., 0., -1.]), "epoch": ("synthetic", 0, 0, 2),
                "received_ns": ns(), "packet": {
                    "test_only": True, "focused": True, "mounted": True, "head_status": 15,
                    "hands": {side: {"input_ok": True, "tracking_ok": self.valid,
                                     "status": 15 if self.valid else 0,
                                     "buttons": 1 if "save" in events and side == "r" else (2 if "discard" in events and side == "r" else 0),
                                     "grip": self.grip if side == "r" else 0.,
                                     "trigger": self.trigger if side == "r" else 0.,
                                     "pose": {"p": transform[:3, 3].tolist(), "q": [0., 0., 0., 1.]}}
                              for side in ("l", "r")}}}

    def rates(self):
        return self.input_rate.snapshot()

    def close(self):
        pass


def wait_until(predicate, timeout=5):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise TimeoutError("demo wait timed out")


def run_demo(output, seconds=3):
    root = Path(output).resolve()
    bridge = BridgeServer(FakeBackend(), control_port=0, ik_port=0).start()
    quest = SyntheticQuest()
    operator = Operator(
        RobotClient("127.0.0.1", bridge.control_port, bridge.ik_port),
        quest, [SyntheticCamera(width=64, height=48, fps=30)], root).start()
    try:
        wait_until(lambda: operator.view()["mode"] == "paused")
        operator.request("calibrate")
        wait_until(lambda: operator.mapping is not None)
        for discard in (False, False, True):
            quest.grip = 0
            time.sleep(.1)
            quest.grip = 1
            wait_until(lambda: operator.following)
            operator.request("start_recording", name="demo", count=3, seconds=120, pause=1)
            wait_until(lambda: operator.episode is not None)
            time.sleep(seconds)
            if not operator.following:
                raise RuntimeError(str(operator.view()))
            if discard:
                quest.events.append("discard")
                wait_until(lambda: operator.view().get("recording_phase") == "countdown")
                operator.request("stop_batch")
            else:
                operator.request("end_recording")
            wait_until(lambda: operator.view().get("recording_phase") == "idle")
            result = operator.view().get("last_result")
            if (discard and result != "discarded") or (
                    not discard and (not result or not Path(result).is_dir())):
                raise RuntimeError("episode finalization failed: " + str(operator.view()))
            quest.grip = 0
            time.sleep(.1)
        result = export_dataset(root/"demo", root/"dataset.hdf5")
        result["discarded_pending_dirs"] = len(list(root.rglob(".pending_*")))
        return result
    finally:
        operator.close()
        bridge.close()
