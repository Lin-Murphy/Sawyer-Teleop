"""A-only pose smoothing. No command queue, joint limits, or speed clipping."""
import math

import numpy as np
from scipy.spatial.transform import Rotation

from .a_timing import INPUT_MAX_AGE_S
from .protocol import pose


class PoseSmoother:
    def __init__(self, options=None):
        options = {} if options is None else dict(options)
        if set(options)-{"enabled", "time_constant_ms"}:
            raise ValueError("Unknown smoothing option")
        enabled = options.get("enabled", False)
        tau = options.get("time_constant_ms", 40.)
        if (type(enabled) is not bool or type(tau) not in (int, float)
                or not math.isfinite(tau) or not 1 <= tau <= 150):
            raise ValueError("Smoothing enabled must be a boolean and the time constant must be 1–150 ms")
        self.options = {"enabled": enabled, "time_constant_ms": float(tau)}
        self.enabled, self.tau = enabled, tau/1000
        self.reset()

    def description(self):
        return dict(self.options, algorithm="cartesian_exponential_v1",
                    ik_seed="previous_target" if self.enabled else "feedback")

    def reset(self, origin=None, stamp_ns=None):
        self.value = pose(origin) if origin is not None else None
        self.stamp_ns = stamp_ns

    def update(self, target, stamp_ns):
        target = pose(target)
        if not self.enabled:
            return target
        if self.value is None or self.stamp_ns is None:
            raise RuntimeError("motion_requires_reengage")
        if stamp_ns < self.stamp_ns:
            raise RuntimeError("motion_input_time_regressed")
        if stamp_ns == self.stamp_ns:
            return pose(self.value)  # One input sample must not be filtered twice.
        dt = (stamp_ns-self.stamp_ns)/1e9
        if dt > INPUT_MAX_AGE_S:  # Was a fixed 150 ms; follows the input lifetime.
            raise RuntimeError("motion_input_gap")
        alpha = -math.expm1(-dt/self.tau)
        position = np.asarray(self.value["p"])
        position = position + alpha*(np.asarray(target["p"])-position)
        current, desired = Rotation.from_quat(self.value["q"]), Rotation.from_quat(target["q"])
        # The principal rotation vector gives the shortest quaternion arc.
        rotation = current*Rotation.from_rotvec(alpha*(current.inv()*desired).as_rotvec())
        self.value = {"p": position.tolist(), "q": rotation.as_quat().tolist()}
        self.stamp_ns = stamp_ns
        return pose(self.value)


def input_stamp(value):
    """Use the Quest sample clock for intervals; no host clock-offset jitter."""
    stamp = value.get("packet", {}).get("sample_ns")
    return stamp if type(stamp) is int else value["received_ns"]
