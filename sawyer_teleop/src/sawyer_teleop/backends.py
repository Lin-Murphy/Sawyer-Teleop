"""Hardware-independent test backend. The fake backend is not a robot simulator."""
import math
import threading
import time
from .protocol import JOINTS, ns, pose, vector


def _quaternion_from_rpy(angles):
    """Test-model orientation: Rz(yaw) @ Ry(pitch) @ Rx(roll), xyzw."""
    cr, cp, cy = (math.cos(a/2) for a in angles)
    sr, sp, sy = (math.sin(a/2) for a in angles)
    return [sr*cp*cy-cr*sp*sy, cr*sp*cy+sr*cp*sy,
            cr*cp*sy-sr*sp*cy, cr*cp*cy+sr*sp*sy]


def _nearest_angle(angle, reference):
    return angle + 2*math.pi*round((reference-angle)/(2*math.pi))


def _rpy_near(quaternion, seed):
    """Choose the equivalent test-model angles closest to current feedback.

    Both Euler branches and whole turns are considered. At a pitch singularity,
    roll and yaw share the correction instead of resetting either angle to zero.
    This is only a reversible six-axis test mapping, not Sawyer kinematics.
    """
    norm = math.sqrt(sum(a*a for a in quaternion))
    x, y, z, w = (a/norm for a in quaternion)
    r00, r10 = 1-2*(y*y+z*z), 2*(x*y+w*z)
    r20 = 2*(x*z-w*y)
    cos_pitch = math.hypot(r00, r10)
    pitch = math.atan2(-r20, cos_pitch)
    if cos_pitch < 1e-10:
        sign = 1 if -r20 >= 0 else -1
        r01, r11 = 2*(x*y-w*z), 1-2*(x*x+z*z)
        # At pitch +/- pi/2 only yaw - sign*roll is determined.
        reference = seed[2]-sign*seed[0]
        correction = _nearest_angle(math.atan2(-r01, r11), reference)-reference
        return [seed[0]-sign*correction/2, _nearest_angle(pitch, seed[1]),
                seed[2]+correction/2]
    roll = math.atan2(2*(y*z+w*x), 1-2*(x*x+y*y))
    yaw = math.atan2(r10, r00)
    candidates = [
        [roll, pitch, yaw],
        [roll+math.pi, math.pi-pitch, yaw+math.pi],
    ]
    candidates = [[_nearest_angle(a, b) for a, b in zip(angles, seed)]
                  for angles in candidates]
    return min(candidates, key=lambda angles: sum((a-b)**2 for a, b in zip(angles, seed)))


class FakeBackend:
    name = "fake"
    tip_frame = "right_hand"
    # Software-test fixture only. Real/Gazebo adapters read the ROS named pose.
    neutral_pose = {"available": True, "joint_names": JOINTS,
                    "q": [0.55823046875, -0.38636328125, -1.474220703125, 1.7777822265625, 1.219943359375, 1.4652578125, 3.3439736328125],
                    "source": "synthetic fixture: Sawyer named_poses.yaml", "test_only": True}

    def __init__(self, clock=ns):
        self.clock = clock
        self.lock = threading.RLock()
        self.q = [0.0] * 7
        self.target = list(self.q)
        self.dq = [0.0] * 7
        self.seq = 0
        self.last = clock()
        self.frozen = False
        self.fault = None
        self.ik_delay = 0
        self.publish_count = 0
        self.sample = None
        self.gripper_target = None
        self.gripper_active = False
        self.gripper_publish_count = 0

    def description(self):
        return {"test_only": True, "model": "fake-cartesian-rpy", "gripper": True,
                "mapping": "J0-J2: position offsets; J3: seed; J4-J6: roll/pitch/yaw"}

    def snapshot(self):
        with self.lock:
            now = self.clock()
            if self.sample is None or (not self.frozen and now - self.last >= 5_000_000):
                dt = max((now-self.last)/1e9, .001)
                previous = self.q
                self.q = list(self.target)
                self.dq = [(a-b)/dt for a, b in zip(self.q, previous)]
                self.seq += 1
                self.last = now
                self.sample = {
                    "seq": self.seq, "captured_ns": now, "available_ns": now, "source_stamp": now/1e9,
                    "q": list(self.q), "dq": list(self.dq),
                    "pose": {"p": [0.5+self.q[0], self.q[1], 0.3+self.q[2]],
                             "q": _quaternion_from_rpy(self.q[4:7])},
                    "valid": self.fault is None, "fault": self.fault,
                    "robot": {"enabled": True, "estop": False},
                }
            return dict(self.sample, gripper={
                "supported": True, "valid": self.fault is None and not self.frozen,
                "target": self.gripper_target, "active": self.gripper_active,
                "submitted": self.gripper_publish_count})

    def publish_gripper(self, value):
        with self.lock:
            if type(value) is not int or value not in (-1, 1):
                raise ValueError("invalid_gripper_command")
            if not self.gripper_active or self.gripper_target != value:
                self.gripper_publish_count += 1
            self.gripper_target, self.gripper_active = value, True

    def hold_gripper(self):
        with self.lock:
            self.gripper_active = False

    def publish(self, q):
        with self.lock:
            self.target = vector(q, 7)
            self.publish_count += 1

    def solve(self, target, seed):
        target, seed = pose(target), vector(seed, 7)
        if self.ik_delay:
            time.sleep(self.ik_delay)
        return ([target["p"][0]-.5, target["p"][1], target["p"][2]-.3, seed[3]]
                + _rpy_near(target["q"], seed[4:7]))

    def close(self):
        pass
