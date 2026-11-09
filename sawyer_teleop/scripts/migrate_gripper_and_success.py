"""One-off migration of saved raw episodes to the 2026-11-09 recording schema.

* Removes the uncalibrated metric gripper placeholders ``states/gripper_position_m``
  and ``states/gripper_valid`` (only when every row is invalid, i.e. pure placeholder).
* Adds ``states/gripper_open_fraction`` = (ticks - closed_ticks) / (open_ticks - closed_ticks),
  clipped to [0, 1], NaN where ticks are invalid. Encoder scale only, not finger distance.
* Labels ``task_success = True`` for operator-ended saves without a recording error
  (lab convention: failed demonstrations are discarded with B). Anything else stays null.

Dry run by default; pass ``--apply`` to write. Each changed episode gets a ``migrations``
entry in manifest.json and in the HDF5 ``metadata`` attribute. HDF5 does not shrink after
deleting datasets; the file size stays the same.
"""
import argparse
import datetime
import hashlib
import json
import re
from pathlib import Path

import h5py
import numpy as np

NAME = "gripper_ticks_and_operator_success_v1"
ENDPOINTS = re.compile(r'"open_ticks": (\d+), "closed_ticks": (\d+)')


def endpoints(episode, manifest):
    gripper = (manifest.get("initial_state") or {}).get("gripper") or {}
    if gripper.get("open_ticks") is not None and gripper.get("closed_ticks") is not None:
        return int(gripper["open_ticks"]), int(gripper["closed_ticks"]), "manifest.initial_state"
    log = episode/"events.jsonl"
    if log.exists():
        found = set(ENDPOINTS.findall(log.read_text(encoding="utf-8")))
        if len(found) == 1:
            open_ticks, closed_ticks = map(int, found.pop())
            return open_ticks, closed_ticks, "events.jsonl"
    return None, None, None


def migrate(episode, apply, script_sha):
    manifest_path, raw_path = episode/"manifest.json", episode/"raw.hdf5"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if any(m.get("name") == NAME for m in manifest.get("migrations", [])):
        return {"episode": episode.name, "skipped": "already_migrated"}
    changes = {}
    with h5py.File(raw_path, "r+" if apply else "r") as data:
        metadata = json.loads(data.attrs["metadata"])
        states = data["states"] if "states" in data else None
        if states is not None and "gripper_valid" in states:
            if np.any(states["gripper_valid"][:]):
                changes["kept_metric_fields"] = "gripper_valid has true rows"
            else:
                changes["deleted"] = [k for k in ("gripper_position_m", "gripper_valid") if k in states]
        if states is not None and "gripper_open_fraction" not in states and "gripper_position_ticks" in states:
            open_ticks, closed_ticks, source = endpoints(episode, manifest)
            if open_ticks is not None and open_ticks != closed_ticks:
                ticks = states["gripper_position_ticks"][:].astype(np.float64)
                valid = states["gripper_ticks_valid"][:]
                fraction = np.where(valid, np.clip((ticks-closed_ticks)/(open_ticks-closed_ticks), 0., 1.), np.nan)
                changes["added_open_fraction"] = {"open_ticks": open_ticks, "closed_ticks": closed_ticks,
                                                  "endpoints_from": source}
            else:
                changes["no_open_fraction"] = "encoder endpoints unknown"
        stop = manifest.get("stop") or {}
        if manifest.get("task_success") is None:
            if (stop.get("reason") == "explicit_recording_end" and not stop.get("camera_lost")
                    and not manifest.get("recording_error") and bool(data.attrs.get("complete"))):
                changes["task_success"] = True
            else:
                changes["task_success_left_null"] = ("recording_error=%s complete=%s stop=%s" % (
                    manifest.get("recording_error"), bool(data.attrs.get("complete")), stop.get("reason")))
        record = {"name": NAME, "at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "script_sha256": script_sha, "changes": changes}
        if apply:
            for key in changes.get("deleted", []):
                del states[key]
            if "added_open_fraction" in changes:
                states.create_dataset("gripper_open_fraction", data=fraction, compression="lzf")
            for target in (metadata, manifest):
                if changes.get("task_success"):
                    target["task_success"] = True
                    target["task_success_source"] = "operator_saved (migrated; lab discards failures with B)"
                target.setdefault("migrations", []).append(record)
            data.attrs["metadata"] = json.dumps(metadata, ensure_ascii=False)
            data.flush()
    if apply:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"episode": episode.name, **changes}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="data root containing batch_*/episode_*/raw.hdf5")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    script_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for raw in sorted(args.root.glob("*/episode_*/raw.hdf5")):
        if (raw.parent/"manifest.json").exists():
            print(json.dumps(migrate(raw.parent, args.apply, script_sha), ensure_ascii=False))
    print("applied" if args.apply else "dry run; pass --apply to write", "script_sha256="+script_sha)


if __name__ == "__main__":
    main()
