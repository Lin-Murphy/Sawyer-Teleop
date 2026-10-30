"""Read-only B dashboard; a single bounded snapshot, preferably on Linux tmpfs."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from .protocol import ns


def write_status(path, core, running=True, error=None):
    path = Path(path)
    state = core.diagnostics()
    state.update(pid=os.getpid(), written_ns=ns(), running=running, error=error)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".status-", delete=False) as stream:
            temp = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return state


def render(state, previous=None):
    age = (ns()-state["written_ns"])/1e9
    stale = age < 0 or age > 3
    running = state.get("running") and not stale
    def number(value):
        return "—" if value is None else f"{value:.2f}"
    def joints(value):
        return "—" if value is None else "  ".join(f"{v:+.4f}" for v in value)
    gripper = (state.get("gripper_observation") or {}).get("value") if running else None
    gripper_text = {-1: "-1 (open)", 1: "1 (closed)"}.get(gripper, "— (waiting / stale input)")
    target = state.get("last_target") or {}
    submission = state.get("last_submission") or {}
    sample = state.get("sample") or {}
    execution = sample.get("gripper") or {}
    gripper_status = ("Unsupported by this backend" if not execution.get("supported") else
                      "Feedback unavailable" if not running or not execution.get("valid") else
                      "Following" if execution.get("active") else
                      "Waiting for control" if execution.get("target") is None else "Holding")
    finger_positions = execution.get("position_m")
    measured = (" | Fingers: "+" / ".join(f"{v:.3f} m" for v in finger_positions)
                if running and execution.get("valid") and finger_positions else "")
    rate = None
    if running and previous and previous.get("pid") == state["pid"]:
        elapsed = (state["written_ns"]-previous["written_ns"])/1e9
        if elapsed > 0:
            rate = (state["accepted"]-previous["accepted"])/elapsed
    events = state.get("events", [])[-5:]
    return "\n".join([
        "Sawyer B Live Monitor (read-only; Ctrl+C exits monitor, B keeps running)",
        f"Backend: {state.get('backend')} | PID: {state['pid']} | Status: " +
        ("Running" if running else "Stopped / stale status; check service"),
        f"Snapshot age: {age:.2f} s | A connected: {state.get('connected')} "
        f"| Armed: {state.get('armed')} | Reason: {state.get('reason')}",
        "Last snapshot only; stale values do not represent the current state." if not running else "",
        f"Valid target receive rate: {number(rate)} Hz | Received: {state['accepted']} "
        f"| Submitted: {state['counts']['submitted']} | Rejected: {state['counts']['rejected']}",
        f"Last valid target seq: {target.get('seq', '—')} | Last submitted seq: {submission.get('seq', '—')}",
        "A→B gripper input: " + gripper_text,
        "Gripper: "+gripper_status+" | Last command: "
        +str(execution["target"] if execution.get("target") is not None else "—")+measured,
        "Joint order: right_j0 ... right_j6; units: rad",
        "A→B last valid target: " + joints(target.get("q")),
        "Last backend command:  " + joints(submission.get("q")),
        "Actual feedback:       " + joints(sample.get("q")),
        f"B feedback age: {number(state.get('feedback_age_ms'))} ms "
        f"| Feedback valid: {state.get('feedback_valid')}",
        f"Last target receive-to-submit time: {number(submission.get('receive_to_submit_ms'))} ms",
        "Excludes A→B network transit and robot motion completion time.",
        "See A for network RTT and feedback age; B does not estimate one-way network delay.",
        "Recent events: " + (" | ".join(str(e['seq'])+":"+e['reason'] for e in events) or "—"),
        "Service error: " + str(state.get("error") or "—"),
    ])


def watch(path, once=False):
    previous = None
    try:
        while True:
            try:
                state = json.loads(Path(path).read_text(encoding="utf-8"))
                output = render(state, previous)
                if previous is None or state["written_ns"] != previous["written_ns"]:
                    previous = state
            except (OSError, ValueError, KeyError) as exc:
                output = "Waiting for a B status snapshot. Start B with --status-file.\n" + str(exc)
                previous = None
            if sys.stdout.isatty() and not once:
                print("\033[2J\033[H", end="")
            print(output, flush=True)
            if once:
                return
            time.sleep(.5)
    except KeyboardInterrupt:
        return
