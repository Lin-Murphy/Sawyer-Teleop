"""A-only episode storage: one writer thread appends plain raw files (no HDF5 while recording).

See ``episode_store`` for the folder layout. Disk writes release the GIL, so recording does not
compete with control, IK or camera threads; previews (MP4) are encoded after save in another process.
"""
import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import uuid

import numpy as np

from .protocol import ns
from .episode_store import TABLES, jsonable, start_video_process

SCHEMA_VERSION = 3
STORAGE_FORMAT = "folder_raw_v1"
# Commands carry an integer kind (kept from schema 2 so readers need no string columns).
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
# About 3 seconds at the lab's ~680 messages/s. Bound queued images by bytes
# as well, including when no telemetry is being recorded.
RECORDING_QUEUE_ITEMS = 2048
RECORDING_QUEUE_IMAGE_BYTES = 128 * 1024 * 1024
FLUSH_INTERVAL_S = .1


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


def open_file(path, mode):
    """Single place files are opened (tests patch it to inject disk failures)."""
    return open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"}))


class _Lines:
    """Buffered JSON-lines table; flushed in batches by the writer thread only."""

    def __init__(self, path):
        self.file = open_file(path, "w")
        self.lines = []

    def append(self, row):
        self.lines.append(json.dumps(jsonable(row), ensure_ascii=False, allow_nan=False))

    def flush(self):
        if self.lines:
            self.file.write("\n".join(self.lines)+"\n")
            self.lines.clear()
        self.file.flush()

    def close(self):
        self.flush()
        self.file.close()


class _Camera:
    """frames.rgb (raw bytes, appended) + frames.jsonl (per-frame times) + info.json."""

    def __init__(self, folder, shape, dtype):
        folder.mkdir(parents=True, exist_ok=True)
        (folder/"info.json").write_text(json.dumps({"shape": list(shape), "dtype": str(dtype),
            "layout": "C-order frames back to back; frame i is bytes [i*N, (i+1)*N)"}), encoding="utf-8")
        self.rgb = open_file(folder/"frames.rgb", "wb")
        self.meta = _Lines(folder/"frames.jsonl")
        self.shape = shape

    def append(self, frame, rgb):
        self.rgb.write(memoryview(np.ascontiguousarray(rgb)).cast("B"))
        self.meta.append({"ns": frame["ns"], "read_start_ns": frame["read_start_ns"], "seq": frame["seq"],
                          "source_seq": int(frame.get("source_seq", -1)),
                          "open_count": int(frame.get("open_count", -1))})

    def flush(self):
        self.rgb.flush()
        self.meta.flush()

    def close(self):
        self.meta.close()
        self.rgb.close()


class _Stats:
    """Writer health: queue high-water marks and time spent per item kind."""

    def __init__(self):
        self.max_items = self.max_image_bytes = 0
        self.kinds = {}
        self.queue_full = None

    def add(self, kind, seconds):
        item = self.kinds.setdefault(kind, {"count": 0, "total_s": 0., "max_ms": 0.})
        item["count"] += 1
        item["total_s"] += seconds
        item["max_ms"] = max(item["max_ms"], seconds*1000)

    def summary(self, started_ns, ended_ns):
        duration = max(1e-9, (ended_ns-started_ns)/1e9)
        busy = sum(k["total_s"] for k in self.kinds.values())
        return {"queue_capacity_items": RECORDING_QUEUE_ITEMS,
                "queue_capacity_image_bytes": RECORDING_QUEUE_IMAGE_BYTES,
                "max_queued_items": self.max_items, "max_queued_image_bytes": self.max_image_bytes,
                "busy_fraction": round(busy/duration, 4), "queue_full": self.queue_full,
                "kinds": {k: dict(v, total_s=round(v["total_s"], 4), max_ms=round(v["max_ms"], 3))
                          for k, v in self.kinds.items()}}


class Episode:
    def __init__(self, root, metadata, camera_names=(), fps=30, queue_size=RECORDING_QUEUE_ITEMS,
                 videos=False):
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
                             schema_version=SCHEMA_VERSION, storage_format=STORAGE_FORMAT,
                             action_semantics="absolute_joint_target_rad",
                             camera_timestamp="host_read_completion; exposure time unknown",
                             command_kind_codes=COMMAND_KINDS,
                             layout={
                                 "states.jsonl": "A-loop snapshot of B's latest feedback, deduplicated (~operator rate)",
                                 "feedback_joint.jsonl": "every accepted /robot/joint_states message (native rate) when B supports feedback_stream",
                                 "feedback_pose.jsonl": "every accepted endpoint_state message (native rate) when B supports feedback_stream",
                                 "commands.jsonl": "submitted commands",
                                 "cameras/<name>/frames.rgb": "every recorded frame, raw uint8 RGB back to back (shape in info.json); frames.jsonl has one line per frame",
                                 "cameras/<name>/video.mp4": "same frames in order, lossy preview made after save (videos.json reports status)",
                                 "alignment": "not stored; episode_store.open_episode computes frame->row indices from the ns columns",
                                 "time": "all *ns fields are A perf_counter ns; source_stamp is the ROS header stamp (s); null = NaN"})
        self.fps = fps
        self.videos = videos
        self.video_process = None
        self.queue = queue.Queue(queue_size)
        self.queue_size = queue_size
        self._queued_image_bytes = 0
        self.stats = _Stats()
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
            image_bytes = data[1]["rgb"].nbytes if kind == "frame" else 0
            full = self._queued_image_bytes + image_bytes > RECORDING_QUEUE_IMAGE_BYTES
            if not full:
                try:
                    self.queue.put_nowait((kind, data, image_bytes))
                except queue.Full:
                    full = True
            if full:
                self.error = "recording_queue_full"
                self.stats.queue_full = {"at_s": round((ns()-self.started_ns)/1e9, 3), "kind": kind,
                                         "queued_items": self.queue.qsize(),
                                         "queued_image_bytes": self._queued_image_bytes}
                return False
            self._queued_image_bytes += image_bytes
            self.stats.max_items = max(self.stats.max_items, self.queue.qsize())
            self.stats.max_image_bytes = max(self.stats.max_image_bytes, self._queued_image_bytes)
            return True

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

    def _fps(self, name):
        return self.fps.get(name, 30) if isinstance(self.fps, dict) else self.fps

    def _write(self):
        files = []
        self.complete = False
        try:
            tables = {name: _Lines(self.path/file) for name, file in TABLES.items()}
            log = _Lines(self.path/"events.jsonl")
            files += [*tables.values(), log]
            cameras = {}
            # Kept in memory for the quality summary (cheap integers only).
            seen = {"state": None, "frames": {}, "frame_ns": {}, "stream": {"joint": ([], []), "pose": ([], [])},
                    "state_ns": [], "stream_session": None,
                    "stream_skipped": {"no_clock_sync": 0, "other_session": 0}}
            next_flush = time.monotonic()+FLUSH_INTERVAL_S
            while True:
                # Batch file writes without holding up control or changing its
                # sampling rate. Idle/low-rate captures also flush every 100 ms.
                if time.monotonic() >= next_flush:
                    began = time.perf_counter()
                    for item in files:
                        item.flush()
                    self.stats.add("flush", time.perf_counter()-began)
                    next_flush = time.monotonic()+FLUSH_INTERVAL_S
                try:
                    kind, value, image_bytes = self.queue.get(timeout=max(0., next_flush-time.monotonic()))
                except queue.Empty:
                    with self.lock:
                        if self.finish_request is not None:
                            break
                    continue
                if image_bytes:
                    with self.lock:
                        self._queued_image_bytes -= image_bytes
                began = time.perf_counter()
                if kind == "state":
                    self._state(value, tables["states"], seen)
                elif kind == "command":
                    self._command(value, tables["commands"])
                elif kind == "frame":
                    self._frame(value, cameras, files, log, seen)
                elif kind == "stream":
                    self._stream(value, tables, seen)
                elif kind == "event":
                    log.append(value)
                self.stats.add(kind, time.perf_counter()-began)
            for item in files:
                item.flush()
            save, stop_result = self.finish_request
            ended_ns = ns()
            quality = None if self.error else self._quality(seen, ended_ns, stop_result)
            # Lab convention: the operator discards failed demonstrations, so an
            # explicit operator save is a success label. Time-limit, camera-loss,
            # shutdown and failed writes were not judged by the operator: unknown.
            task_success = True if (save and stop_result.get("operator_decision")
                                    and not self.error) else None
            self.metadata.update(ended_ns=ended_ns, stop=stop_result, quality=quality,
                                 task_success=task_success,
                                 task_success_source="operator_saved" if task_success else None)
            # Completion is a closed capture, not a statement that the robot is stopped.
            self.complete = bool(not self.error and (stop_result.get("holding") or stop_result.get("capture_closed")))
        except Exception as exc:
            self.error = str(exc)
            self.complete = False
        finally:
            self._finish_files(files)

    def _state(self, value, table, seen):
        sample = value["sample"]
        key = (value["session"], sample["seq"])
        if not sample["valid"] or key == seen["state"]:
            return
        seen["state"] = key
        gripper = sample.get("gripper") or {}
        ticks = gripper.get("position_ticks")
        ticks_valid = bool(gripper.get("valid") and ticks is not None)
        open_ticks, closed_ticks = gripper.get("open_ticks"), gripper.get("closed_ticks")
        # Encoder scale only (0 closed endpoint, 1 open endpoint);
        # finger distance is uncalibrated and deliberately not recorded.
        open_fraction = (float(np.clip((ticks-closed_ticks)/(open_ticks-closed_ticks), 0., 1.))
                         if ticks_valid and open_ticks is not None and closed_ticks is not None
                         and open_ticks != closed_ticks else None)
        row = {"ns": sample.get("available_ns", sample["captured_ns"])+value["clock_offset_ns"],
               "captured_ns": sample["captured_ns"]+value["clock_offset_ns"],
               "source_stamp": float(sample.get("source_stamp", 0.)),
               "pose_source_stamp": float(sample.get("pose_source_stamp", 0.)),
               "uncertainty_ns": value["clock_uncertainty_ns"],
               "source_seq": sample["seq"], "q": sample["q"], "dq": sample["dq"],
               "eef_pos": sample["pose"]["p"], "eef_quat": sample["pose"]["q"],
               "eef_twist": sample.get("twist") or [0.]*6,
               "eef_twist_valid": sample.get("twist") is not None,
               "gripper_position_ticks": ticks if ticks is not None else 0,
               "gripper_ticks_valid": ticks_valid,
               "gripper_open_fraction": open_fraction,
               "gripper_target_ticks": gripper.get("target_ticks") or 0,
               "gripper_feedback_age_ms": float(gripper.get("age_ms") or 0.)}
        seen["state_ns"].append(row["ns"])
        table.append(row)

    def _command(self, value, table):
        target = value.get("target_pose") or {}
        target_valid = (isinstance(target.get("p"), (list, tuple)) and len(target["p"]) == 3
                        and isinstance(target.get("q"), (list, tuple)) and len(target["q"]) == 4)
        table.append({
            "command_kind": COMMAND_KINDS.get(value.get("command_kind") or "joint_target", -1),
            "target_pos": [float(v) for v in target["p"]] if target_valid else [0.]*3,
            "target_quat": [float(v) for v in target["q"]] if target_valid else [0., 0., 0., 1.],
            "target_pose_valid": bool(target_valid),
            "ns": value["b_ns"]+value["offset_ns"],
            "uncertainty_ns": value["uncertainty_ns"], "q": value["q"],
            "seq": value["seq"], "feedback_seq": value["feedback_seq"],
            "generation": value["generation"], "segment": value["segment"],
            "gripper": value.get("gripper") if value.get("gripper") is not None else 0,
            "gripper_valid": value.get("gripper") in (-1, 1),
        })

    def _frame(self, value, cameras, files, log, seen):
        name, frame = value
        if frame["seq"] <= seen["frames"].get(name, -1):
            return
        seen["frames"][name] = frame["seq"]
        rgb = frame["rgb"]
        if name not in cameras:
            cameras[name] = _Camera(self.path/"cameras"/name, rgb.shape, rgb.dtype)
            files.append(cameras[name])
            seen["frame_ns"][name] = []
        camera = cameras[name]
        if rgb.shape != camera.shape:
            raise RuntimeError("camera_resolution_changed")
        fps = self._fps(name)
        stamps = seen["frame_ns"][name]
        if stamps and fps and frame["ns"]-stamps[-1] > CAMERA_GAP_FACTOR*1e9/fps:
            # Recorded, never filled: offline alignment drops timesteps without a frame.
            log.append({"reason": "camera_frame_gap", "a_ns": frame["ns"], "camera": name,
                        "gap_ms": (frame["ns"]-stamps[-1])/1e6,
                        "missing_estimate": int(round((frame["ns"]-stamps[-1])*fps/1e9))-1})
        stamps.append(frame["ns"])
        camera.append(frame, rgb)

    def _stream(self, rows, tables, seen):
        for row in rows:
            offset = row.get("clock_offset_ns")
            if offset is None:
                seen["stream_skipped"]["no_clock_sync"] += 1
                continue
            seen["stream_session"] = seen["stream_session"] or row.get("session")
            if row.get("session") != seen["stream_session"]:
                seen["stream_skipped"]["other_session"] += 1
                continue
            common = {"ns": row["b_received_ns"]+offset, "captured_ns": row["b_captured_ns"]+offset,
                      "a_received_ns": row["a_received_ns"], "source_stamp": float(row["source_stamp"]),
                      "uncertainty_ns": row.get("clock_uncertainty_ns") or 0,
                      "stream_seq": row["stream_seq"]}
            topic = "joint" if row["kind"] == "joint" else "pose"
            seq, stamps = seen["stream"][topic]
            seq.append(row["stream_seq"])
            stamps.append(common["ns"])
            if topic == "joint":
                head = row.get("head_pan")
                tables["feedback/joint"].append(dict(common, q=row["q"], dq=row["dq"],
                    head_pan=float(head) if head is not None else 0., head_pan_valid=head is not None))
            else:
                twist = row.get("twist")
                tables["feedback/pose"].append(dict(common, eef_pos=row["p"], eef_quat=row["q"],
                    eef_twist=twist or [0.]*6, eef_twist_valid=twist is not None))

    def _quality(self, seen, ended_ns, stop_result):
        """Per-episode quality summary from recorded times. Never raises."""
        try:
            end = self.sealed_ns or ended_ns
            cameras = {name: camera_quality(seen["frame_ns"].get(name, []), self._fps(name), self.started_ns, end)
                       for name in self.metadata.get("cameras", [])}
            feedback = {topic: stream_quality(*seen["stream"][topic]) for topic in ("joint", "pose")}
            state_intervals = np.diff(np.asarray(seen["state_ns"], dtype=np.int64))/1e6
            reasons = []
            for name, item in cameras.items():
                reasons += [name+":"+r for r in item["reasons"]]
            if stop_result.get("camera_lost"):
                reasons.append("camera_lost_during_recording:"+str(stop_result["camera_lost"]))
            for topic, item in feedback.items():
                reasons += ["feedback_"+topic+":"+r for r in item["reasons"]]
            status = ("incomplete" if any(c["status"] == "incomplete" for c in cameras.values())
                      or stop_result.get("camera_lost") else "degraded" if reasons else "ok")
            return {"status": status, "reasons": reasons,
                    "window_ns": [self.started_ns, end],
                    "thresholds": {"camera_gap_factor": CAMERA_GAP_FACTOR,
                                   "camera_gap_degraded_ms": CAMERA_GAP_DEGRADED_MS,
                                   "camera_drop_degraded": CAMERA_DROP_DEGRADED,
                                   "camera_lost_s": CAMERA_LOST_S,
                                   "feedback_drop_degraded": FEEDBACK_DROP_DEGRADED,
                                   "feedback_gap_degraded_ms": FEEDBACK_GAP_DEGRADED_MS},
                    "cameras": cameras, "feedback": feedback,
                    "feedback_rows_skipped": dict(seen["stream_skipped"]),
                    "states": {"rows": len(seen["state_ns"]),
                               "max_gap_ms": round(float(state_intervals.max()), 2) if len(state_intervals) else None,
                               "note": "latest-value snapshots; use feedback_* for every source message"}}
        except Exception as exc:  # Quality is diagnostic; never lose the capture over it.
            return {"status": "unknown", "reasons": ["quality_failed: "+str(exc)]}

    def _finish_files(self, files):
        close_failed = False
        for item in files:
            try:
                item.close()
            except Exception as exc:
                close_failed = True
                self.error = self.error or 'recording_close_failed: '+str(exc)
        if close_failed:
            self.complete = False
        # On writer failure, wait for the operator to choose save or discard.
        while self.finish_request is None:
            time.sleep(.05)
        save, _ = self.finish_request
        try:
            if save:
                self.metadata.update(recording_error=self.error, complete=bool(self.complete),
                                     writer_stats=self.stats.summary(self.started_ns, self.sealed_ns or ns()))
                (self.path/"manifest.json").write_text(
                    json.dumps(jsonable(self.metadata), ensure_ascii=False, indent=2), encoding="utf-8")
                if close_failed:
                    # Closing did not confirm valid files. Retain the pending
                    # directory for recovery; it must not become export input.
                    self.result = self.path
                else:
                    for file in self.path.rglob("*"):
                        if file.is_file():
                            # Windows FlushFileBuffers needs a writable handle.
                            with file.open("r+b") as handle:
                                os.fsync(handle.fileno())
                    final = self.root/("episode_"+self.ident)
                    self.path.rename(final)
                    self.result = final
                    if self.videos and self.metadata.get("cameras"):
                        try:
                            self.video_process = start_video_process(final)
                        except Exception as exc:  # Raw files are safe; previews can be re-made.
                            (final/"videos.json").write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
            else:
                shutil.rmtree(self.path)
                self.result = "discarded"
        except Exception as exc:
            self.error = self.error or str(exc)
        finally:
            self.done.set()
