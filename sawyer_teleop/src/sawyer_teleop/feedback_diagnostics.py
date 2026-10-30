"""A-only, read-only timing estimates. Never grants or extends a B reference."""

import math

EXPERIMENTAL_TIMEOUTS = {"gazebo_110ms": .11, "gazebo_150ms": .15, "gazebo_250ms": .25}
FEEDBACK_PROFILES = ("standard", *EXPERIMENTAL_TIMEOUTS)


def feedback_timeout(base_timeout, profile, expected_backend, hello):
    if profile not in FEEDBACK_PROFILES:
        raise ValueError("invalid_feedback_profile")
    if profile in EXPERIMENTAL_TIMEOUTS:
        # A UI selection alone cannot enable the experiment on a real/fake B.
        if expected_backend == "gazebo" and (hello or {}).get("backend") == "gazebo":
            if profile in ("gazebo_150ms", "gazebo_250ms"):
                reference = hello.get("reference_timeout_s")
                if (type(reference) not in (int, float) or not math.isfinite(reference)
                        or reference < .18):
                    raise ValueError(f"Gazebo {EXPERIMENTAL_TIMEOUTS[profile]*1000:.0f} ms "
                                     "requires B reference timeout >= 180 ms. "
                                     "Start the Gazebo 180 ms B, or select standard / 110 ms.")
            # A's observation allowance never extends B's independent reference budget.
            return EXPERIMENTAL_TIMEOUTS[profile]
        return min(base_timeout, .1)
    return base_timeout


def reference_from_snapshot(state):
    """Freeze the original reference's conservative age on A's monotonic clock."""
    return {"seq": state["sample"]["seq"], "observed_ns": state["a_received_ns"],
            "age_ns": round(state["age_s"] * 1e9)}


def reference_remaining_ms(reference, now, timeout_s):
    if reference is None or timeout_s is None:
        return None
    return timeout_s * 1000 - (reference["age_ns"] + now-reference["observed_ns"]) / 1e6


def feedback_fault(state, now, connected, offset, uncertainty, sync_ns, timeout):
    sample = (state or {}).get("sample")
    if not sample or offset is None or uncertainty is None:
        return "feedback_missing", None
    age = (now-sample["captured_ns"]-offset+uncertainty) / 1e9
    if not connected:
        return "feedback_connection_lost", age
    if now-sync_ns > 10_000_000_000:
        return "feedback_clock_sync_expired", age
    if age < 0:
        return "feedback_latest_clock_regressed", age
    if age > timeout:
        return "feedback_latest_expired", age
    if sample.get("valid") is not True:
        return sample.get("fault") or "feedback_latest_invalid", age
    return None, age


def diagnose_feedback(*, state, hello, now, connected, offset, uncertainty, sync_ns,
                      timeout, received_ns, arrival_gap_ns, reference=None):
    fault, age = feedback_fault(state, now, connected, offset, uncertainty, sync_ns, timeout)
    sample = (state or {}).get("sample") or {}
    b_ns = (state or {}).get("b_ns")
    result = {"connected": connected, "fault": fault, "fresh": fault is None,
              "age_ms": None if age is None else age * 1000,
              "a_limit_ms": timeout * 1000,
              "b_latest_limit_ms": hello.get("state_timeout_s", .1) * 1000,
              "b_reference_limit_ms": hello.get("reference_timeout_s", .12) * 1000,
              "b_limits_reported": all(k in hello for k in ("state_timeout_s", "reference_timeout_s")),
              "source_valid": sample.get("valid"), "source_fault": sample.get("fault"),
              "b_reason": (state or {}).get("reason"),
              "b_source_age_ms": None, "delivery_upper_ms": None,
              "a_residence_ms": None if received_ns is None else (now-received_ns) / 1e6,
              "arrival_gap_ms": None if arrival_gap_ns is None else arrival_gap_ns / 1e6,
              "clock_uncertainty_ms": None if uncertainty is None else uncertainty / 1e6,
              "clock_sync_age_ms": None if offset is None else (now-sync_ns) / 1e6}
    if b_ns is not None and sample.get("captured_ns") is not None:
        result["b_source_age_ms"] = (b_ns-sample["captured_ns"]) / 1e6
        if received_ns is not None and offset is not None and uncertainty is not None:
            # Includes B queue/write, transport and A receive; NOT network-only.
            result["delivery_upper_ms"] = (received_ns-b_ns-offset+uncertainty) / 1e6
    if reference is None and age is not None:
        reference = {"seq": sample["seq"], "observed_ns": now, "age_ns": round(age * 1e9)}
    result["reference_seq"] = None if reference is None else reference["seq"]
    result["reference_remaining_ms"] = reference_remaining_ms(
        reference, now, hello.get("reference_timeout_s", .12))
    result["budget_valid"] = bool(connected and offset is not None and uncertainty is not None
                                  and now-sync_ns <= 10_000_000_000 and age is not None and age >= 0)
    return result
