"""Causal, fixed-rate export to robomimic. Raw data is always retained in radians."""
import json
import os
from pathlib import Path
import tempfile

import h5py
import numpy as np


def causal_indices(stamps, grid, max_age_ns):
    stamps = np.asarray(stamps, dtype=np.int64)
    if not len(stamps) or np.any(np.diff(stamps) <= 0):
        raise ValueError("nonmonotonic_or_empty_timestamps")
    indices = np.searchsorted(stamps, grid, side="right")-1
    valid = indices >= 0
    age = grid-stamps[np.maximum(indices, 0)]
    valid &= (age >= 0) & (age <= max_age_ns)
    return indices, valid


def runs(valid):
    edges = np.diff(np.r_[False, valid, False].astype(int))
    return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))


def export_dataset(root, output, hz=50, valid_fraction=.2, seed=42):
    if not 1 <= hz <= 100 or not 0 <= valid_fraction < 1:
        raise ValueError("invalid export parameters")
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    step = round(1e9/hz)
    candidates = sorted(Path(root).glob("episode_*/raw.hdf5"))
    episodes = []
    excluded = []
    for path in candidates:
        with h5py.File(path, "r") as source:
            if not source.attrs.get("complete", False):
                excluded.append({"path": str(path), "reason": "incomplete"})
                continue
            quality = json.loads(source.attrs.get("quality", "{}") or "{}")
            if quality.get("status") == "incomplete":
                # e.g. a camera was lost; kept on disk for inspection, never trained on.
                excluded.append({"path": str(path), "reason": "quality_incomplete",
                                 "details": quality.get("reasons", [])})
                continue
            if "ns" not in source["commands"] or len(source["commands/ns"]) < 2:
                excluded.append({"path": str(path), "reason": "insufficient_commands"})
                continue
            episodes.append(path)
    if not episodes:
        raise ValueError("No complete episodes to export")
    rng = np.random.default_rng(seed)
    shuffled = rng.permutation(len(episodes))
    nvalid = min(len(episodes)-1, max(1, round(len(episodes)*valid_fraction))) if valid_fraction else 0
    validation = set(shuffled[:nvalid].tolist())
    # Dataset-wide reversible range, including validation, guarantees [-1,1] without clipping.
    # This is an actuator-coordinate encoding, not an estimate of safe robot joint limits.
    extrema = []
    source_backends = set()
    for path in episodes:
        with h5py.File(path, "r") as source:
            metadata = json.loads(source.attrs["metadata"])
            source_backends.add(metadata.get("backend", "unknown"))
            q = source["commands/q"][:]
            extrema.extend([q.min(axis=0), q.max(axis=0)])
    low, high = np.min(extrema, axis=0), np.max(extrema, axis=0)
    center = (low+high)/2
    scale = np.maximum((high-low)/2, 1e-6)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Own a unique temporary file. A previous or concurrent export may have
    # recovery data beside this output; never remove somebody else's partial.
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name+'.',
                                     suffix='.partial', delete=False) as handle:
        temp = Path(handle.name)
    demos = []
    try:
        with h5py.File(temp, "w") as dest:
            data = dest.create_group("data")
            data.attrs["env_args"] = json.dumps({
                "env_name": "SawyerJointTargetOffline", "type": 1,
                "env_kwargs": {"offline_only": True, "control_freq": hz}})
            data.attrs["action_normalization"] = json.dumps({
                "kind": "affine_dataset_encoding", "center": center.tolist(),
                "scale": scale.tolist(), "inverse": "radians = actions * scale + center",
                "scope": "all selected complete episodes; not safety limits"})
            data.attrs["source_backends"] = json.dumps(sorted(source_backends))
            from .protocol import JOINTS
            data.attrs["joint_names"] = json.dumps(JOINTS)
            data.attrs["control_hz"] = hz
            data.attrs["action_semantics"] = "latest submitted absolute joint target, zero-order hold"
            data.attrs["resampling"] = "past-only obs/images, independent release segments, no interpolation"
            data.attrs["excluded"] = json.dumps(excluded)
            split_names = {"train": [], "valid": []}
            for episode_index, path in enumerate(episodes):
                split = "valid" if episode_index in validation else "train"
                with h5py.File(path, "r") as source:
                    states, commands, frames = source["states"], source["commands"], source["frames"]
                    if "ns" not in states:
                        continue
                    times = commands["ns"][:]
                    if np.any(np.diff(times) <= 0):
                        raise ValueError("command clock regressed: "+str(path))
                    segments = commands["segment"][:]
                    cuts = np.r_[0, np.flatnonzero((np.diff(segments) != 0) |
                                                  (np.diff(times) > 100_000_000))+1, len(times)]
                    for start, end in zip(cuts[:-1], cuts[1:]):
                        grid = np.arange(times[start], times[end-1], step, dtype=np.int64)
                        if len(grid) < 2:
                            continue
                        # Conservative source timestamp upper bound prevents future observation use.
                        state_times = states["ns"][:]+states["uncertainty_ns"][:]
                        oi, valid = causal_indices(state_times, grid, 100_000_000)
                        ni, next_valid = causal_indices(state_times, grid+step, 100_000_000)
                        ai, action_valid = causal_indices(times[start:end], grid, 100_000_000)
                        valid &= next_valid & action_valid
                        images = {}
                        for camera, group in frames.items():
                            fi, fv = causal_indices(group["ns"][:], grid, 250_000_000)
                            fn, fnv = causal_indices(group["ns"][:], grid+step, 250_000_000)
                            images[camera] = (fi, fn)
                            valid &= fv & fnv
                        required = json.loads(source.attrs["metadata"])["cameras"]
                        if set(required) != set(images):
                            continue
                        for left, right in runs(valid):
                            if right-left < 2:
                                continue
                            sl = slice(left, right)
                            name = "demo_"+str(len(demos))
                            demo = data.create_group(name)
                            q = commands["q"][:][ai[sl]+start]
                            demo.create_dataset("actions", data=((q-center)/scale).astype("float32"))
                            demo.create_dataset("actions_raw", data=q)
                            demo.create_dataset("timestamps_ns", data=grid[sl])
                            demo.create_dataset("command_seq", data=commands["seq"][:][ai[sl]+start])
                            for groupname, indices in (("obs", oi[sl]), ("next_obs", ni[sl])):
                                group = demo.create_group(groupname)
                                for key, raw in (("joint_pos", "q"), ("joint_vel", "dq"),
                                                 ("eef_pos", "eef_pos"), ("eef_quat", "eef_quat")):
                                    group.create_dataset(key, data=states[raw][:][indices].astype("float32"))
                                for camera, (fi, fn) in images.items():
                                    selection = (fi if groupname == "obs" else fn)[sl]
                                    out = group.create_dataset(camera+"_image",
                                        shape=(len(selection),)+frames[camera]["rgb"].shape[1:],
                                        dtype="uint8", compression="lzf", chunks=True)
                                    # Bounded memory even for long episodes.
                                    for row, index in enumerate(selection):
                                        out[row] = frames[camera]["rgb"][index]
                            count = int(right-left)
                            demo.create_dataset("rewards", data=np.zeros(count, dtype="float32"))
                            done = np.zeros(count, dtype="int64")
                            done[-1] = 1  # Dataset boundary, not success.
                            demo.create_dataset("dones", data=done)
                            demo.attrs["num_samples"] = count
                            demo.attrs["source_episode"] = path.parent.name
                            demo.attrs["task_success"] = "unknown"
                            demo.attrs["truncated"] = True
                            split_names[split].append(name.encode())
                            demos.append((name, count))
            if not demos or not split_names["train"]:
                raise ValueError("Not enough recent observations to generate training segments")
            mask = dest.create_group("mask")
            for split, names in split_names.items():
                mask.create_dataset(split, data=np.asarray(names, dtype="S64"))
            data.attrs["total"] = sum(n for _, n in demos)
            dest.flush()
        # Windows FlushFileBuffers needs a writable handle.
        with temp.open("r+b") as file:
            os.fsync(file.fileno())
        # Same-directory hard link publishes the complete file atomically and
        # fails if another exporter created output after the initial check.
        os.link(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return {"output": str(output), "demos": len(demos), "samples": sum(n for _, n in demos),
            "episodes": len(episodes), "excluded": excluded}
