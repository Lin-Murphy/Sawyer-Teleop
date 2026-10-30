"""Nonblocking control for the two-finger gripper in the ROG Gazebo scene."""
import math

from .protocol import ns


def trigger_command(trigger):
    if isinstance(trigger, bool) or not isinstance(trigger, (int, float)):
        raise ValueError("invalid_trigger")
    if not math.isfinite(trigger) or not 0 <= trigger <= 1:
        raise ValueError("invalid_trigger")
    return -1 if trigger < .3 else 1


class SimulationGripper:
    names = ("generic_left_finger_joint", "generic_right_finger_joint")

    def __init__(self, publishers, message, timeout=.1, clock=ns):
        self.publishers, self.message = publishers, message
        self.timeout_ns, self.clock = int(timeout*1e9), clock
        self.positions = None
        self.captured_ns = None
        self.target = None
        self.active = False
        self.submitted = 0

    def observe(self, positions, captured_ns):
        values = [positions.get(name) for name in self.names]
        if all(v is not None and math.isfinite(v) for v in values):
            self.positions, self.captured_ns = values, captured_ns
        else:
            self.positions = None

    def snapshot(self):
        connected = all(p.get_num_connections() for p in self.publishers)
        fresh = (self.positions is not None and self.captured_ns is not None
                 and 0 <= self.clock()-self.captured_ns <= self.timeout_ns)
        fault = ("gripper_controller_missing" if not connected else
                 "gripper_feedback_stale" if not fresh else None)
        return {"supported": True, "valid": fault is None, "fault": fault,
                "position_m": self.positions, "target": self.target,
                "active": self.active, "submitted": self.submitted}

    def _publish(self, positions):
        for publisher, position in zip(self.publishers, positions):
            publisher.publish(self.message(data=position))

    def command(self, value):
        if type(value) is not int or value not in (-1, 1):
            raise ValueError("invalid_gripper_command")
        state = self.snapshot()
        if not state["valid"]:
            raise RuntimeError(state["fault"])
        if not self.active or self.target != value:
            self._publish([.04 if value == -1 else 0.] * 2)
            self.submitted += 1
            self.target, self.active = value, True

    def hold(self):
        # Never open automatically on grip release; stop at fresh measured positions.
        active, self.active = self.active, False
        if active and self.snapshot()["valid"]:
            self._publish([min(.04, max(0., p)) for p in self.positions])

    def close(self):
        for publisher in self.publishers:
            publisher.unregister()
