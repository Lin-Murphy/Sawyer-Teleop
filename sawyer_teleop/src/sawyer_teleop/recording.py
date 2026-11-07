"""A-only episode storage. One writer owns HDF5, videos and episode diagnostics."""
import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import uuid

import cv2
import h5py
import numpy as np

from .protocol import ns

SCHEMA_VERSION = 2
# Commands carry an integer kind; h5py cannot store numpy unicode arrays.
COMMAND_KINDS = {"joint_target": 0, "cartesian_goal": 1}
# Camera quality (DROID-style: never duplicate or synthesize frames; flag, then let
# offline alignment drop timesteps without a fresh frame).
CAMERA_GAP_FACTOR = 1.5          # an interval above 1.5 periods is logged as a gap
CAMERA_GAP_DEGRADED_MS = 250.    # any single gap above this marks the episode degraded
CAMERA_DROP_DEGRADED = .05       # missing-frame fraction above this marks it degraded
CAMERA_LOST_S = 1.               # no frame for this long: camera lost, episode incomplete
# Feedback stream quality: any lost row is visible as a per-topic sequence gap.
FEEDBACK_DROP_DEGRADED = .01
FEEDBACK_GAP_DEGRADED_MS = 100.


def camera_quality(stamps, fps, start_ns, end_ns):
    """Summarize one camera's frame times (A clock, ns) over the recording window."""
    stamps = np.asarray(stamps, dtype=np.int64)
    duration = max(0., (end_ns-start_ns)/1e9)
    expected = int(round(duration*fps)) if fps else 0
    period_ms = 1000./fps if fps else None
    edges = np.r_[start_ns, stamps, end_ns] if len(stamps) else np.array([start_ns, end_ns])
    intervals = np.diff(edges)/1e6
    gaps = intervals[intervals > CAMERA_GAP_FACTOR*period_ms] if period_ms else intervals[:0]
    received = int(len(stamps))
    drop = max(0., 1-received/expected) if expected else 0.
    max_gap = float(intervals.max()) if len(intervals) else 0.
    lost = received == 0 or max_gap >= CAMERA_LOST_S*1000
    reasons = []
    if lost:
        reasons.append("camera_lost")
    if max_gap > CAMERA_GAP_DEGRADED_MS:
        reasons.append("gap_over_%dms" % CAMERA_GAP_DEGRADED_MS)
    if drop > CAMERA_DROP_DEGRADED:
        reasons.append("drop_over_%d%%" % round(CAMERA_DROP_DEGRADED*100))
    return {"fps_target": fps, "expected_frames": expected, "received_frames": received,
            "drop_fraction": round(drop, 4), "max_gap_ms": round(max_gap, 1),
            "gaps_over_1_5_period": int(len(gaps)),
            "measured_fps": round(received/duration, 2) if duration else None,
            "status": "incomplete" if lost else "degraded" if reasons else "ok", "reasons": reasons}


def stream_quality(seq, stamps):
    """Rows lost after B accepted them appear as gaps in the per-topic sequence."""
    seq = np.asarray(seq, dtype=np.int64)
    stamps = np.asarray(stamps, dtype=np.int64)
    if len(seq) == 0:
        return {"rows": 0, "missing_rows": 0, "drop_fraction": 0., "max_gap_ms": None,
                "rate_hz": None, "status": "missing", "reasons": ["no_rows"]}
    steps = np.diff(seq)
    missing = int(np.clip(steps-1, 0, None).sum())
    intervals = np.diff(stamps)/1e6
    max_gap = float(intervals.max()) if len(intervals) else 0.
    span = (stamps[-1]-stamps[0])/1e9
    drop = missing/(len(seq)+missing)
    reasons = []
    if drop > FEEDBACK_DROP_DEGRADED:
        reasons.append("drop_over_%d%%" % round(FEEDBACK_DROP_DEGRADED*100))
    if max_gap > FEEDBACK_GAP_DEGRADED_MS:
        reasons.append("gap_over_%dms" % FEEDBACK_GAP_DEGRADED_MS)
    if np.any(steps <= 0):
        reasons.append("sequence_not_increasing")
    return {"rows": int(len(seq)), "missing_rows": missing, "drop_fraction": round(drop, 5),
            "max_gap_ms": round(max_gap, 2), "median_interval_ms":
                round(float(np.median(intervals)), 3) if len(intervals) else None,
            "rate_hz": round((len(seq)-1)/span, 2) if span > 0 else None,
            "status": "degraded" if reasons else "ok", "reasons": reasons}


def latest_index(source_ns, query_ns):
    """Index of the newest source row at or before each query time (-1 when none)."""
    return np.searchsorted(np.asarray(source_ns), np.asarray(query_ns), side="right").astype(np.int64)-1


def append(group, name, value, dtype=None):
    value = np.asarray(value, dtype=dtype)
    if name not in group:
        group.create_dataset(name, shape=(0,)+value.shape, maxshape=(None,)+value.shape,
                             dtype=value.dtype, chunks=(1,)+value.shape,
                             compression="lzf" if value.ndim else None)
    dataset = group[name]
    index = len(dataset)
    dataset.resize(index+1, axis=0)
    dataset[index] = value


class _RowBuffer:
    """Bounded telemetry batches; owned only by the episode's writer thread."""

    def __init__(self, group):
        self.group = group
        self.rows = []
        self.datasets = {}

    def append(self, row):
        self.rows.append(row)
        if len(self.rows) >= 32:
            self.flush()

    def flush(self):
        if not self.rows:
            return
        for name in self.rows[0]:
            values = np.asarray([row[name] for row in self.rows])
            if name not in self.datasets:
                shape = values.shape[1:]
                self.datasets[name] = self.group.create_dataset(
                    name, shape=(0,)+shape, maxshape=(None,)+shape,
                    dtype=values.dtype, chunks=(128,)+shape,
                    compression="lzf" if shape else None)
            dataset = self.datasets[name]
            start = len(dataset)
            dataset.resize(start+len(values), axis=0)
            dataset[start:] = values
        self.rows.clear()


class Episode:
    def __init__(self, root, metadata, camera_names=(), fps=30, queue_size=256):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.ident = stamp+"_"+uuid.uuid4().hex[:12]
        self.path = self.root/(".pending_"+self.ident)
        self.path.mkdir()
        self.started_ns = ns()
        self.metadata = dict(metadata, cameras=list(camera_names),
                             started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                             started_ns=self.started_ns,
                             schema_version=SCHEMA_VERSION, action_semantics="absolute_joint_target_rad",
                             camera_timestamp="host_read_completion; exposure time unknown",
                             command_kind_codes=COMMAND_KINDS,
                             layout={
                                 "states": "A-loop snapshot of B's latest feedback, deduplicated (~operator rate)",
                                 "feedback/joint": "every accepted /robot/joint_states message (native rate) when B supports feedback_stream",
                                 "feedback/pose": "every accepted endpoint_state message (native rate) when B supports feedback_stream",
                                 "frames/<camera>": "every recorded frame, lossless RGB; <camera>.mp4 holds the same frames in order (lossy preview)",
                                 "alignment/<camera>": "per frame: newest row index at or before the frame time in states / feedback (-1 none)",
                                 "time": "all *ns fields are A perf_counter ns; source_stamp is the ROS header stamp (s)"})
        self.fps = fps
        self.queue = queue.Queue(queue_size)
        self.error = None
        self.accepting = True
        self.sealed_ns = None
        self.lock = threading.Lock()
        self.finish_request = None
        self.done = threading.Event()
        self.result = None
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def put(self, kind, data):
        with self.lock:
            if not self.accepting or self.error:
                return False
            try:
                self.queue.put_nowait((kind, data))
                return True
            except queue.Full:
                self.error = "recording_queue_full"
                return False

    def event(self, reason, **details):
        self.put("event", dict(details, reason=reason, a_ns=ns()))

    def state(self, state):
        self.put("state", state)

    def command(self, receipt, segment, offset_ns, uncertainty_ns, target=None):
        self.put("command", dict(receipt, segment=segment, offset_ns=offset_ns,
                                 uncertainty_ns=uncertainty_ns, target_pose=target))

    def stream(self, rows):
        """Rows from RobotClient.drain_feedback_stream (one per accepted B message)."""
        if rows:
            self.put("stream", rows)

    def frame(self, name, frame):
        self.put("frame", (name, frame))

    def seal(self):
        """Stop accepting samples, including an in-flight camera callback."""
        with self.lock:
            self.accepting = False
            self.sealed_ns = self.sealed_ns or ns()

    def finish(self, save, stop_result=None):
        with self.lock:
            if self.finish_request is None:
                self.accepting = False
                self.sealed_ns = self.sealed_ns or ns()
                self.finish_request = (save, stop_result or {"holding": False})
        return self.done

    def _write(self):
        videos = {}
        data = None
        log = None
        last_state_seq = None
        frame_sequences = {}
        try:
            data = h5py.File(self.path/"raw.hdf5", "w")
            data.attrs["metadata"] = json.dumps(self.metadata, ensure_ascii=False)
            data.attrs["complete"] = False
            states = _RowBuffer(data.create_group("states"))
            commands = _RowBuffer(data.create_group("commands"))
            feedback = data.create_group("feedback")
            streams = {"joint": _RowBuffer(feedback.create_group("joint")),
                       "pose": _RowBuffer(feedback.create_group("pose"))}
            stream_session = None
            stream_skipped = {"no_clock_sync": 0, "other_session": 0}
            frames = data.create_group("frames")
            last_frame_ns = {}
            log = (self.path/"events.jsonl").open("w", encoding="utf-8")
            buffers = [states, commands, *streams.values()]
            next_flush = time.monotonic()+.1
            while True:
                # Batch storage work without holding up control or changing its
                # sampling rate. Idle/low-rate captures also flush every 100 ms.
                if time.monotonic() >= next_flush:
                    for buffer in buffers:
                        buffer.flush()
                    next_flush = time.monotonic()+.1
                try:
                    kind, value = self.queue.get(timeout=max(0., next_flush-time.monotonic()))
                except queue.Empty:
                    with self.lock:
                        if self.finish_request is not None:
                            break
                    continue
                if kind == "state":
                    sample = value["sample"]
                    key = (value["session"], sample["seq"])
                    if sample["valid"] and key != last_state_seq:
                        last_state_seq = key
                        gripper = sample.get("gripper") or {}
                        fingers = gripper.get("position_m")
                        row = {"ns": sample.get("available_ns", sample["captured_ns"])+value["clock_offset_ns"],
                               "captured_ns": sample["captured_ns"]+value["clock_offset_ns"],
                               "source_stamp": sample.get("source_stamp", 0.),
                               "pose_source_stamp": sample.get("pose_source_stamp", 0.),
                               "uncertainty_ns": value["clock_uncertainty_ns"],
                               "source_seq": sample["seq"], "q": sample["q"], "dq": sample["dq"],
                               "eef_pos": sample["pose"]["p"], "eef_quat": sample["pose"]["q"],
                               "eef_twist": sample.get("twist") or [0.]*6,
                               "eef_twist_valid": sample.get("twist") is not None,
                               "gripper_position_m": fingers if fingers is not None else [0., 0.],
                               "gripper_valid": bool(gripper.get("valid") and fingers is not None),
                               "gripper_position_ticks": gripper.get("position_ticks") or 0,
                               "gripper_ticks_valid": bool(gripper.get("valid") and gripper.get("position_ticks") is not None),
                               "gripper_target_ticks": gripper.get("target_ticks") or 0,
                               "gripper_feedback_age_ms": gripper.get("age_ms") or 0.}
                        states.append(row)
                elif kind == "command":
                    target = value.get("target_pose") or {}
                    target_valid = (isinstance(target.get("p"), (list, tuple)) and len(target["p"]) == 3
                                    and isinstance(target.get("q"), (list, tuple)) and len(target["q"]) == 4)
                    commands.append({
                        "command_kind": COMMAND_KINDS.get(value.get("command_kind") or "joint_target", -1),
                        "target_pos": list(target["p"]) if target_valid else [0.]*3,
                        "target_quat": list(target["q"]) if target_valid else [0., 0., 0., 1.],
                        "target_pose_valid": bool(target_valid),
                        "ns": value["b_ns"]+value["offset_ns"],
                        "uncertainty_ns": value["uncertainty_ns"], "q": value["q"],
                        "seq": value["seq"], "feedback_seq": value["feedback_seq"],
                        "generation": value["generation"], "segment": value["segment"],
                        "gripper": value.get("gripper") if value.get("gripper") is not None else 0,
                        "gripper_valid": value.get("gripper") in (-1, 1),
                    })
                elif kind == "frame":
                    name, frame = value
                    if frame["seq"] <= frame_sequences.get(name, -1):
                        continue
                    frame_sequences[name] = frame["seq"]
                    group = frames.require_group(name)
                    for key in ("ns", "read_start_ns", "seq", "rgb"):
                        append(group, key, frame[key])
                    append(group, "source_seq", frame.get("source_seq", -1), "int64")
                    append(group, "open_count", frame.get("open_count", -1), "int64")
                    fps = self.fps.get(name, 30) if isinstance(self.fps, dict) else self.fps
                    previous = last_frame_ns.get(name)
                    if previous is not None and fps and frame["ns"]-previous > CAMERA_GAP_FACTOR*1e9/fps:
                        # Recorded, never filled: offline alignment drops timesteps without a frame.
                        log.write(json.dumps({"reason": "camera_frame_gap", "a_ns": frame["ns"],
                            "camera": name, "gap_ms": (frame["ns"]-previous)/1e6,
                            "missing_estimate": int(round((frame["ns"]-previous)*fps/1e9))-1},
                            allow_nan=False)+"\n")
                    last_frame_ns[name] = frame["ns"]
                    rgb = frame["rgb"]
                    if name not in videos:
                        h, w = rgb.shape[:2]
                        writer = cv2.VideoWriter(str(self.path/(name+".mp4")),
                                                 cv2.VideoWriter_fourcc(*"mp4v"),
                                                 self.fps.get(name, 30) if isinstance(self.fps, dict) else self.fps,
                                                 (w, h))
                        if not writer.isOpened():
                            raise RuntimeError("video_writer_failed:" + name)
                        videos[name] = (writer, rgb.shape)
                    writer, shape = videos[name]
                    if rgb.shape != shape:
                        raise RuntimeError("camera_resolution_changed")
                    writer.write(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                elif kind == "stream":
                    for row in value:
                        offset = row.get("clock_offset_ns")
                        if offset is None:
                            stream_skipped["no_clock_sync"] += 1
                            continue
                        stream_session = stream_session or row.get("session")
                        if row.get("session") != stream_session:
                            stream_skipped["other_session"] += 1
                            continue
                        common = {"ns": row["b_received_ns"]+offset,
                                  "captured_ns": row["b_captured_ns"]+offset,
                                  "a_received_ns": row["a_received_ns"],
                                  "source_stamp": row["source_stamp"],
                                  "uncertainty_ns": row.get("clock_uncertainty_ns") or 0,
                                  "stream_seq": row["stream_seq"]}
                        if row["kind"] == "joint":
                            head = row.get("head_pan")
                            streams["joint"].append(dict(common, q=row["q"], dq=row["dq"],
                                head_pan=head if head is not None else 0., head_pan_valid=head is not None))
                        else:
                            twist = row.get("twist")
                            streams["pose"].append(dict(common, eef_pos=row["p"], eef_quat=row["q"],
                                eef_twist=twist or [0.]*6, eef_twist_valid=twist is not None))
                elif kind == "event":
                    log.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+"\n")
            # Finish only after every accepted row, including a short final
            # batch, has been written. A write failure keeps complete=False.
            for buffer in buffers:
                buffer.flush()
            save, stop_result = self.finish_request
            ended_ns = ns()
            quality = None
            if not self.error:
                quality = self._quality_and_alignment(data, ended_ns, stop_result, stream_skipped)
            self.metadata.update(ended_ns=ended_ns, stop=stop_result, quality=quality,
                                 task_success=None, recording_error=self.error)
            data.attrs["metadata"] = json.dumps(self.metadata, ensure_ascii=False)
            # Completion is a closed capture, not a statement that the robot is stopped.
            data.attrs["complete"] = bool(not self.error and (stop_result.get("holding") or stop_result.get("capture_closed")))
            data.flush()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._finish_files(data, log, videos)

    def _quality_and_alignment(self, data, ended_ns, stop_result, stream_skipped):
        """Per-episode quality summary plus a frame->robot-row index. Never raises."""
        try:
            end = self.sealed_ns or ended_ns
            cameras = {}
            for name in self.metadata.get("cameras", []):
                fps = self.fps.get(name, 30) if isinstance(self.fps, dict) else self.fps
                stamps = data["frames"][name]["ns"][:] if name in data["frames"] else []
                cameras[name] = camera_quality(stamps, fps, self.started_ns, end)
            feedback = {}
            for topic in ("joint", "pose"):
                group = data["feedback"][topic]
                feedback[topic] = (stream_quality(group["stream_seq"][:], group["ns"][:])
                                   if "ns" in group else stream_quality([], []))
            states = data["states"]
            state_ns = states["ns"][:] if "ns" in states else np.zeros(0, np.int64)
            state_intervals = np.diff(state_ns)/1e6
            reasons = []
            for name, item in cameras.items():
                reasons += [name+":"+r for r in item["reasons"]]
            if stop_result.get("camera_lost"):
                reasons.append("camera_lost_during_recording:"+str(stop_result["camera_lost"]))
            for topic, item in feedback.items():
                reasons += ["feedback_"+topic+":"+r for r in item["reasons"]]
            status = ("incomplete" if any(c["status"] == "incomplete" for c in cameras.values())
                      or stop_result.get("camera_lost") else "degraded" if reasons else "ok")
            alignment = data.require_group("alignment")
            for name in cameras:
                if name not in data["frames"]:
                    continue
                frame_ns = data["frames"][name]["ns"][:]
                group = alignment.require_group(name)
                for label, source in (("state", state_ns),
                                      ("joint", data["feedback/joint/ns"][:] if "ns" in data["feedback/joint"] else []),
                                      ("pose", data["feedback/pose/ns"][:] if "ns" in data["feedback/pose"] else [])):
                    index = latest_index(source, frame_ns)
                    source = np.asarray(source, dtype=np.int64)
                    age = np.where(index >= 0, frame_ns-source[np.clip(index, 0, None)] if len(source) else 0, -1)
                    group.create_dataset(label+"_index", data=index)
                    group.create_dataset(label+"_age_ns", data=age.astype(np.int64))
            quality = {"status": status, "reasons": reasons,
                       "window_ns": [self.started_ns, end],
                       "thresholds": {"camera_gap_factor": CAMERA_GAP_FACTOR,
                                      "camera_gap_degraded_ms": CAMERA_GAP_DEGRADED_MS,
                                      "camera_drop_degraded": CAMERA_DROP_DEGRADED,
                                      "camera_lost_s": CAMERA_LOST_S,
                                      "feedback_drop_degraded": FEEDBACK_DROP_DEGRADED,
                                      "feedback_gap_degraded_ms": FEEDBACK_GAP_DEGRADED_MS},
                       "cameras": cameras, "feedback": feedback,
                       "feedback_rows_skipped": dict(stream_skipped),
                       "states": {"rows": int(len(state_ns)),
                                  "max_gap_ms": round(float(state_intervals.max()), 2) if len(state_intervals) else None,
                                  "note": "latest-value snapshots; use feedback/* for every source message"}}
            data.attrs["quality"] = json.dumps(quality)
            return quality
        except Exception as exc:  # Quality is diagnostic; never lose the capture over it.
            return {"status": "unknown", "reasons": ["quality_failed: "+str(exc)]}

    def _finish_files(self, data, log, videos):
        close_failed = False
        closers = ([data.close] if data is not None else [])
        closers += [log.close] if log is not None else []
        closers += [writer.release for writer, _ in videos.values()]
        for close in closers:
            try:
                close()
            except Exception as exc:
                close_failed = True
                self.error = self.error or 'recording_close_failed: '+str(exc)
        # On writer failure, wait for the operator to choose save or discard.
        while self.finish_request is None:
            time.sleep(.05)
        save, _ = self.finish_request
        try:
            if save:
                (self.path/"manifest.json").write_text(
                    json.dumps(dict(self.metadata, recording_error=self.error),
                               ensure_ascii=False, indent=2), encoding="utf-8")
                if close_failed:
                    # Closing did not confirm a valid file. Retain the pending
                    # directory for recovery; it must not become export input.
                    self.result = self.path
                else:
                    for file in self.path.iterdir():
                        # Windows FlushFileBuffers needs a writable handle.
                        with file.open("r+b") as handle:
                            os.fsync(handle.fileno())
                    final = self.root/("episode_"+self.ident)
                    self.path.rename(final)
                    self.result = final
            else:
                shutil.rmtree(self.path)
                self.result = "discarded"
        except Exception as exc:
            self.error = self.error or str(exc)
        finally:
            self.done.set()
