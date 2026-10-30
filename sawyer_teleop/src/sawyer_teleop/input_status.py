"""Presentation-only input diagnostics; never changes validation or control."""
import math

from .a_timing import INPUT_MAX_AGE_S
from .protocol import ns

def _mapping(value):
    return value if isinstance(value, dict) else {}

def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None

def _flags(value):
    return value if type(value) is int and value >= 0 else 0



def describe_input(value):
    packet = _mapping(value.get("packet"))
    age = (ns()-value.get("received_ns", 0))/1e9
    fresh = 0 <= age <= INPUT_MAX_AGE_S and value.get("reason") not in ("quest_stale", "quest_waiting")
    result = {"received_ns": value.get("received_ns", 0), "fresh": fresh, "age_ms": round(age*1000, 1) if value.get("received_ns") else None,
              "valid": bool(value.get("valid")), "reason": value.get("reason", "—"),
              "focused": packet.get("focused") if fresh else None,
              "mounted": packet.get("mounted") if fresh else None,
              "head_tracked": (_flags(packet.get("head_status")) & 15) == 15 if fresh and "head_status" in packet else None,
              "hands": {}}
    for side in ("l", "r"):
        hand = _mapping(_mapping(packet.get("hands")).get(side))
        active = fresh and hand.get("input_ok") is True
        tracked = fresh and hand.get("tracking_ok") is True and (_flags(hand.get("status")) & 15) == 15
        position = _mapping(hand.get("pose")).get("p")
        if not isinstance(position, list) or len(position) != 3 or any(_number(v) is None for v in position):
            position = None
        result["hands"][side] = {
            "input": active, "tracked": tracked,
            "buttons": hand.get("buttons") if active and type(hand.get("buttons")) is int else None,
            "grip": _number(hand.get("grip")) if active else None,
            "trigger": _number(hand.get("trigger")) if active else None,
            "position": position if tracked else None,
        }
    return result


def expire_status(state):
    """Never leave a disconnected or stalled GUI showing live pressed buttons."""
    age = (ns()-state.get("received_ns", 0))/1e9
    if 0 <= age <= INPUT_MAX_AGE_S:
        return state
    result = dict(state, fresh=False, valid=False, focused=None, mounted=None, head_tracked=None)
    result["hands"] = {side: {"input": False, "tracked": False, "buttons": None,
                              "grip": None, "trigger": None, "position": None} for side in ("l", "r")}
    return result
