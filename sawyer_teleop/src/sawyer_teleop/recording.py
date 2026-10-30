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
        self.metadata = dict(metadata, cameras=list(camera_names),
                             started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                             schema_version=1, action_semantics="absolute_joint_target_rad",
                             camera_timestamp="host_read_completion; exposure time unknown")
        self.fps = fps
        self.queue = queue.Queue(queue_size)
        self.error = None
        self.accepting = True
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

    def command(self, receipt, segment, offset_ns, uncertainty_ns):
        self.put("command", dict(receipt, segment=segment, offset_ns=offset_ns,
                                 uncertainty_ns=uncertainty_ns))

    def frame(self, name, frame):
        self.put("frame", (name, frame))

    def seal(self):
        """Stop accepting samples, including an in-flight camera callback."""
        with self.lock:
            self.accepting = False

    def finish(self, save, stop_result=None):
        with self.lock:
            if self.finish_request is None:
                self.accepting = False
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
            frames = data.create_group("frames")
            log = (self.path/"events.jsonl").open("w", encoding="utf-8")
            next_flush = time.monotonic()+.1
            while True:
                # Batch storage work without holding up control or changing its
                # sampling rate. Idle/low-rate captures also flush every 100 ms.
                if time.monotonic() >= next_flush:
                    states.flush()
                    commands.flush()
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
                               "gripper_position_m": fingers if fingers is not None else [0., 0.],
                               "gripper_valid": bool(gripper.get("valid") and fingers is not None)}
                        states.append(row)
                elif kind == "command":
                    commands.append({
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
                elif kind == "event":
                    log.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+"\n")
            # Finish only after every accepted row, including a short final
            # batch, has been written. A write failure keeps complete=False.
            states.flush()
            commands.flush()
            save, stop_result = self.finish_request
            self.metadata.update(ended_ns=ns(), stop=stop_result,
                                 task_success=None, recording_error=self.error)
            data.attrs["metadata"] = json.dumps(self.metadata, ensure_ascii=False)
            # Completion is a closed capture, not a statement that the robot is stopped.
            data.attrs["complete"] = bool(not self.error and (stop_result.get("holding") or stop_result.get("capture_closed")))
            data.flush()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._finish_files(data, log, videos)

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
