"""Read-only motion diagnostics for old and new episode recordings."""
import json
from pathlib import Path

import h5py
import numpy as np
from scipy.spatial.transform import Rotation


def summary(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return {"count": 0, "median": None, "p95": None, "max": None}
    return {"count": len(values), "median": float(np.median(values)),
            "p95": float(np.percentile(values, 95)), "max": float(np.max(values))}


def analyze_episode(directory):
    directory = Path(directory)
    with h5py.File(directory/"raw.hdf5", "r") as data:
        metadata = json.loads(data.attrs.get("metadata", "{}"))
        complete = bool(data.attrs.get("complete", False))
        times = data["commands/ns"][:] if "commands/ns" in data else np.array([], dtype=np.int64)
        joints = data["commands/q"][:] if "commands/q" in data else np.empty((0, 7))
        segments = data["commands/segment"][:] if "commands/segment" in data else np.zeros(len(times))
        feedback = data["states/ns"][:] if "states/ns" in data else np.array([], dtype=np.int64)
    if np.any(np.diff(times) <= 0) or np.any(np.diff(feedback) <= 0):
        raise ValueError("Analysis requires increasing, non-duplicate timestamps: "+str(directory))
    dt = np.diff(times)/1e9
    same = np.diff(segments) == 0
    continuous = same & (dt <= .1)
    steps = np.abs(np.diff(joints, axis=0))[continuous]
    timing = {name: [] for name in ("ik_queue", "ik_call", "ik_result_wait", "input_receive_to_submit_estimate",
              "input_sample_to_submit_estimate", "clock_uncertainty", "feedback_age", "network_rtt")}
    position_error, rotation_error = [], []
    reference_velocity, reference_acceleration, goal_error = [], [], []
    source_commands = set()
    path = directory/"events.jsonl"
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if event.get("reason") != "submitted":
                    continue
                trajectory = event.get("trajectory")
                if trajectory:
                    reference_velocity.append(np.abs(trajectory["dq"]))
                    reference_acceleration.append(np.abs(trajectory["ddq"]))
                    goal_error.append(np.max(np.abs(
                        np.asarray(trajectory["goal_q"]) - event["submitted_q"])))
                    source_commands.add(event["source_command_seq"])
                stamps = event.get("timing", {})
                for label, finish, start in (
                    ("ik_queue", "ik_started_ns", "ik_queued_ns"),
                    ("ik_call", "ik_finished_ns", "ik_started_ns"),
                    ("ik_result_wait", "target_sent_ns", "ik_finished_ns"),
                    ("input_receive_to_submit_estimate", "submitted_a_ns", "input_received_ns"),
                    ("input_sample_to_submit_estimate", "submitted_a_ns", "input_mapped_ns"),
                ):
                    if stamps.get(start) is not None and stamps.get(finish) is not None:
                        timing[label].append((stamps[finish]-stamps[start])/1e6)
                if stamps.get("input_uncertainty_ns") is not None and stamps.get("submit_clock_uncertainty_ns") is not None:
                    timing["clock_uncertainty"].append(
                        (stamps["input_uncertainty_ns"]+stamps["submit_clock_uncertainty_ns"])/1e6)
                for label in ("feedback_age", "network_rtt"):
                    if stamps.get(label+"_ms") is not None:
                        timing[label].append(stamps[label+"_ms"])
                raw, filtered = event.get("raw_target_pose"), event.get("target_pose")
                if raw is not None and filtered is not None:
                    position_error.append(float(np.linalg.norm(np.asarray(raw["p"])-filtered["p"])*1000))
                    rotation_error.append(float(np.rad2deg((Rotation.from_quat(raw["q"]).inv()
                                                          *Rotation.from_quat(filtered["q"])).magnitude())))
    model = metadata.get("robot", {}).get("description", {})
    return {
        "episode": str(directory.resolve()), "complete": complete,
        "backend": metadata.get("backend"), "test_only": model.get("test_only", metadata.get("backend") == "fake"),
        "motion": metadata.get("motion", {"enabled": False, "legacy": True}),
        "trajectory": metadata.get("trajectory", {"enabled": False, "legacy": True}),
        "interpolated_source_targets": len(source_commands),
        "planned_velocity_abs_rad_s": [
            summary([row[i] for row in reference_velocity]) for i in range(7)],
        "planned_acceleration_abs_rad_s2": [
            summary([row[i] for row in reference_acceleration]) for i in range(7)],
        "ik_goal_to_submitted_max_joint_error_rad": summary(goal_error),
        "submitted_commands": len(times), "feedback_samples": len(feedback),
        "command_interval_ms": summary(dt[same]*1000),
        "command_gaps_over_100ms": int(np.sum(same & (dt > .1))),
        "feedback_interval_ms": summary(np.diff(feedback)/1e6),
        "joint_target_step_abs_rad": [summary(steps[:, i]) for i in range(7)],
        "joint_target_slope_abs_rad_s": [
            summary(steps[:, i]/dt[continuous]) for i in range(7)],
        "timing_ms": {name: summary(values) for name, values in timing.items()},
        "raw_to_filtered_position_mm": summary(position_error),
        "raw_to_filtered_orientation_deg": summary(rotation_error),
    }


def analyze_motion(source):
    source = Path(source)
    if source.name == "raw.hdf5" and source.is_file():
        directories = [source.parent]
    elif (source/"raw.hdf5").is_file():
        directories = [source]
    else:
        directories = sorted(p.parent for p in source.rglob("raw.hdf5")
                             if not any(part.startswith(".pending_") for part in p.parts))
    if not directories:
        raise ValueError("No episode/raw.hdf5 found: "+str(source))
    return {"schema_version": 1, "episodes": [analyze_episode(path) for path in directories],
            "notes": [
                "Target changes do not measure real robot jitter; fake results do not validate physical motion.",
                "Command intervals and changes are grouped by grip segment; gaps over 100 ms are excluded from target slopes.",
                "Cross-device latency uses clock alignment estimates; check clock_uncertainty. Submission does not mean the robot has reached its target.",
                "Planned velocity/acceleration describe the continuous reference generated by B; use states feedback for actual joint motion.",
                "Phase 2 timing fields missing from older data are null, not zero."]}
