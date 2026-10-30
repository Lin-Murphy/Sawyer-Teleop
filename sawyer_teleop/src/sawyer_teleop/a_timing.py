"""A-only input and IK timing policy; never changes B's advertised deadlines.

Sample lifetime, computation budget, and task lifetime have different origins.
They are concurrent limits, not durations to add together. Keep validation at
each use boundary: receiving a sample cannot grant it a fresh lifetime later.
"""

# 2026-10-29 user decision: an occasional lag up to 200 ms is acceptable and
# preferred over a pause that requires re-gripping (was 150 ms input / 100 ms task).
INPUT_MAX_AGE_S = .2
INPUT_FUTURE_TOLERANCE_NS = 10_000_000
LOCAL_IK_BUDGET_S = .02
REMOTE_IK_TIMEOUT_S = .08
IK_TASK_TIMEOUT_NS = 150_000_000


def input_age_expired(age_ns, uncertainty_ns, max_age_ns):
    """Validate the original sample age with the existing uncertainty bounds."""
    return (age_ns+uncertainty_ns > max_age_ns
            or age_ns < -uncertainty_ns-INPUT_FUTURE_TOLERANCE_NS)
