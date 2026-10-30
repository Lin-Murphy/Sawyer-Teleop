"""Shared A/B discontinuity checks; bounded joints are never angle-wrapped."""
from .protocol import JOINTS, ProtocolError, vector
from .neutral import neutral_target

MAX_STEP_RAD = 0.35
CAPABILITY = {"schema": 1, "max_step_rad": MAX_STEP_RAD, "neutral_reset": True}


def protection_options(enabled=True):
    if type(enabled) is not bool:
        raise ProtocolError("invalid_joint_protection")
    return {"enabled": enabled, "max_step_rad": MAX_STEP_RAD}


def parse_options(value):
    # Unnegotiated legacy clients keep their existing playback semantics.
    if value is None:
        return protection_options(False)
    if (not isinstance(value, dict) or set(value) != {"enabled", "max_step_rad"}
            or type(value["max_step_rad"]) not in (int, float)
            or value["max_step_rad"] != MAX_STEP_RAD):
        raise ProtocolError("invalid_joint_protection")
    return protection_options(value["enabled"])


class JointJumpError(ProtocolError):
    def __init__(self, stage, index, q, reference):
        self.details = dict(joint=JOINTS[index], stage=stage, target_rad=q[index],
                            reference_rad=reference[index],
                            delta_rad=abs(q[index]-reference[index]),
                            max_step_rad=MAX_STEP_RAD)
        super().__init__("joint_jump: %s %s %.6f rad > %.2f rad" %
                         (stage, JOINTS[index], self.details["delta_rad"], MAX_STEP_RAD))


def check_jump(q, reference, options, stage):
    q = vector(q, 7, "joint target")
    if not options["enabled"]:
        return
    reference = vector(reference, 7, "joint reference")
    index = max(range(7), key=lambda i: abs(q[i]-reference[i]))
    if abs(q[index]-reference[index]) > MAX_STEP_RAD + 1e-12:
        raise JointJumpError(stage, index, q, reference)


def reset_destination(purpose, trajectory, capability):
    if purpose == "teleop":
        return None
    if (purpose != "neutral_reset" or not trajectory or not trajectory.get("enabled")
            or trajectory.get("profile", "cubic") != "cubic"):
        raise ProtocolError("invalid_motion_purpose")
    return neutral_target(capability)


def check_destination(q, reference, options, stage, neutral=None):
    if neutral is not None:
        # This narrow exception permits only the named destination through the
        # bounded cubic planner. Every execution sample is still checked.
        q = vector(q, 7, "neutral target")
        if any(abs(a-b) > 1e-12 for a, b in zip(q, neutral)):
            raise ProtocolError("neutral_reset_target_mismatch")
    else:
        check_jump(q, reference, options, stage)
