"""Human-readable B pause details shared by the operator and tests."""

LABELS = {
    "joint_jump": "Joint protection rejected a discontinuous target",
    "feedback_latest_expired": "Latest robot feedback expired",
    "feedback_latest_missing": "Latest robot feedback is missing",
    "feedback_latest_invalid": "Latest robot feedback is invalid",
    "feedback_latest_clock_regressed": "Latest robot feedback timestamp is in the future",
    "feedback_reference_expired": "Source feedback reference expired",
    "feedback_reference_missing": "Source feedback reference is no longer available",
    "feedback_reference_invalid": "Source feedback reference is invalid",
    "feedback_reference_clock_regressed": "Source feedback reference timestamp is in the future",
    "feedback_stale": "Feedback expired (older B did not identify the reference)",
    "command_timeout": "No new operator target within the command timeout",
    "trajectory_control_gap": "B motion update interval exceeded its limit",
    "trajectory_following_error": "Joint following error exceeded its limit",
    "trajectory_unfeasible": "No trajectory satisfies the configured motion limits",
    "joint_model_limit_exceeded": "Joint target or trajectory reference exceeds the robot model limits",
}


def bridge_pause_message(state):
    reason = state.get("reason") or "unknown"
    details = state.get("pause_details") or {}
    label = LABELS.get(reason, reason)
    fields = {
        "feedback_latest_expired": ("latest_feedback_age_ms", "latest_feedback"),
        "feedback_reference_expired": ("reference_feedback_age_ms", "source_reference"),
        "command_timeout": ("command_age_ms", "command"),
        "trajectory_control_gap": ("control_gap_ms", "control_gap"),
    }
    if reason == "joint_jump" and details.get("delta_rad") is not None:
        label += " (%s, %s: %.3f rad > %.3f rad)" % (details["stage"], details["joint"],
                  details["delta_rad"], details["max_step_rad"])
    elif reason in fields:
        age_key, limit_key = fields[reason]
        age, limit = details.get(age_key), details.get("thresholds_ms", {}).get(limit_key)
        if age is not None and limit is not None:
            label += " (%.3f ms > %.3f ms)" % (age, limit)
    elif reason == "trajectory_following_error" and details.get("following_error_rad") is not None:
        label += " (%.3f rad > %.3f rad)" % (details["following_error_rad"], details["max_following_error_rad"])
    fault = details.get("latest_feedback_fault")
    if reason.startswith("feedback_latest") and fault:
        label += "; " + str(fault)
    return "B paused: " + label + ". Release grip, then squeeze again."
