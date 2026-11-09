"""Folder-based raw episode format (schema 3) and a reader shared with legacy raw.hdf5.

Recording never builds an HDF5 file: it only appends plain files, which costs little CPU
and releases the GIL during disk writes. Layout of ``episode_<id>/``::

    manifest.json                     metadata, initial_state, quality, writer_stats, complete
    events.jsonl                      one JSON event per line
    states.jsonl                      A-loop snapshots of B's latest feedback (deduplicated)
    commands.jsonl                    submitted commands
    feedback_joint.jsonl              every accepted /robot/joint_states message
    feedback_pose.jsonl               every accepted endpoint_state message
    cameras/<name>/frames.rgb         raw uint8 RGB frames appended back to back (lossless)
    cameras/<name>/frames.jsonl       one line per frame: ns, read_start_ns, seq, source_seq, open_count
    cameras/<name>/info.json          frame shape and dtype
    cameras/<name>/video.mp4          lossy preview, generated after save in a separate process

All ``*ns`` values are A ``perf_counter`` ns. JSON NaN is written as null.

``open_episode`` returns the same nested ``data["states/q"][:]`` / ``data.attrs`` view for
both this layout and legacy ``raw.hdf5`` episodes, so export and analysis code is shared.
"""
import json
import math
from pathlib import Path

import numpy as np

TABLES = {"states": "states.jsonl", "commands": "commands.jsonl",
          "feedback/joint": "feedback_joint.jsonl", "feedback/pose": "feedback_pose.jsonl"}


def jsonable(value):
    """Plain JSON types; NaN/inf become null (strict JSON)."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return value if math.isfinite(value) else None
    return value


def is_episode(path):
    path = Path(path)
    return (path/"manifest.json").is_file() and (path/"states.jsonl").is_file() or (path/"raw.hdf5").is_file()


def find_episodes(root, pattern="episode_*"):
    return sorted(p for p in Path(root).glob(pattern) if p.is_dir() and is_episode(p))


def _column(values):
    """Typed numpy column: integers as int64 (Windows numpy defaults to int32)."""
    flat = [v for v in values if v is not None]
    probe = flat[0] if flat else None
    while isinstance(probe, list) and probe:
        probe = probe[0]
    if isinstance(probe, bool):
        return np.asarray(values, dtype=np.bool_)
    if isinstance(probe, int) and all(_all_int(v) for v in values):
        return np.asarray(values, dtype=np.int64)
    return np.asarray([np.nan if v is None else v for v in values], dtype=np.float64)


def _all_int(value):
    if isinstance(value, list):
        return all(_all_int(v) for v in value)
    return isinstance(value, int) and not isinstance(value, bool)


def read_table(path):
    rows = []
    if Path(path).is_file():
        with open(path, encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    if not rows:
        return {}
    return {name: _column([row.get(name) for row in rows]) for name in rows[0]}


class Group(dict):
    """Minimal h5py.Group stand-in: nested lookup with "a/b" paths."""

    def __getitem__(self, key):
        node = self
        for part in str(key).strip("/").split("/"):
            node = dict.__getitem__(node, part)
        return node

    def __contains__(self, key):
        try:
            self[key]
            return True
        except (KeyError, TypeError):
            return False


class FolderEpisode(Group):
    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        manifest = json.loads((self.path/"manifest.json").read_text(encoding="utf-8"))
        self.attrs = {"metadata": json.dumps(manifest, ensure_ascii=False),
                      "complete": bool(manifest.get("complete", False))}
        if manifest.get("quality") is not None:
            self.attrs["quality"] = json.dumps(manifest["quality"])
        for name, file in TABLES.items():
            parent = self
            parts = name.split("/")
            for part in parts[:-1]:
                parent = dict.setdefault(parent, part, Group())
            dict.__setitem__(parent, parts[-1], Group(read_table(self.path/file)))
        frames = Group()
        for name in manifest.get("cameras", []):
            folder = self.path/"cameras"/name
            if not (folder/"info.json").is_file():
                continue
            group = Group(read_table(folder/"frames.jsonl"))
            info = json.loads((folder/"info.json").read_text(encoding="utf-8"))
            count = len(group.get("ns", []))
            shape = (count,)+tuple(info["shape"])
            group["rgb"] = (np.memmap(folder/"frames.rgb", dtype=info.get("dtype", "uint8"), mode="r", shape=shape)
                            if count else np.zeros(shape, dtype=np.uint8))
            frames[name] = group
        dict.__setitem__(self, "frames", frames)
        dict.__setitem__(self, "alignment", self._alignment())

    def _alignment(self):
        """Per frame: newest row at or before the frame time (-1 none). Computed, not stored."""
        result = Group()
        sources = {"state": self["states"].get("ns"), "joint": self["feedback/joint"].get("ns"),
                   "pose": self["feedback/pose"].get("ns")}
        for camera, group in self["frames"].items():
            frame_ns = group.get("ns", np.zeros(0, np.int64))
            aligned = Group()
            for label, source in sources.items():
                source = np.asarray(source if source is not None else [], dtype=np.int64)
                index = np.searchsorted(source, frame_ns, side="right").astype(np.int64)-1
                age = (np.where(index >= 0, frame_ns-source[np.clip(index, 0, None)], -1)
                       if len(source) else np.full(len(frame_ns), -1, np.int64))
                aligned[label+"_index"], aligned[label+"_age_ns"] = index, age.astype(np.int64)
            result[camera] = aligned
        return result

    def close(self):
        for group in self["frames"].values():
            rgb = group.get("rgb")
            if isinstance(rgb, np.memmap) and rgb._mmap is not None:
                rgb._mmap.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_episode(path):
    """Open an episode directory (folder schema or legacy raw.hdf5) read-only."""
    path = Path(path)
    if path.name == "raw.hdf5":
        path = path.parent
    if (path/"raw.hdf5").is_file():
        import h5py
        return h5py.File(path/"raw.hdf5", "r")
    return FolderEpisode(path)


def make_videos(episode, fps=None):
    """Write cameras/<name>/video.mp4 from frames.rgb. Safe to re-run; never touches raw files."""
    import cv2
    episode = Path(episode)
    manifest = json.loads((episode/"manifest.json").read_text(encoding="utf-8"))
    rates = {c["name"]: c.get("fps") for c in manifest.get("camera_config", [])}
    results = {}
    with FolderEpisode(episode) as data:
        for name, group in data["frames"].items():
            frames = group["rgb"]
            target = episode/"cameras"/name/"video.mp4"
            partial = target.with_name("video.partial.mp4")  # OpenCV picks the container by extension
            try:
                if not len(frames):
                    raise RuntimeError("no_frames")
                h, w = frames.shape[1:3]
                writer = cv2.VideoWriter(str(partial), cv2.VideoWriter_fourcc(*"mp4v"),
                                         float((fps or {}).get(name) or rates.get(name) or 20), (w, h))
                if not writer.isOpened():
                    raise RuntimeError("video_writer_failed")
                try:
                    for frame in frames:
                        writer.write(cv2.cvtColor(np.asarray(frame), cv2.COLOR_RGB2BGR))
                finally:
                    writer.release()
                partial.replace(target)
                results[name] = {"ok": True, "frames": int(len(frames))}
            except Exception as exc:
                partial.unlink(missing_ok=True)
                results[name] = {"ok": False, "error": str(exc)}
    (episode/"videos.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def _video_worker(episode):
    try:
        make_videos(episode)
    except Exception as exc:  # Raw data is already safe; record why preview failed.
        Path(episode, "videos.json").write_text(json.dumps({"error": str(exc)}), encoding="utf-8")


def start_video_process(episode):
    """Encode previews outside the A process so recording/control never wait for it."""
    import multiprocessing
    process = multiprocessing.get_context("spawn").Process(
        target=_video_worker, args=(str(episode),), name="episode-video", daemon=False)
    process.start()
    return process
