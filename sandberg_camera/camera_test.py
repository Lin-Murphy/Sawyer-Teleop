"""Record a reproducible camera-only VLA test without overwriting older runs."""

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

import cv2
import numpy as np

from camera_pipeline import DEFAULT_PROFILE, load_profile, open_camera, preprocess_bgr


def save_frame(out, frame_id, source, image, before, after, writer, rows, brightness):
    write_started = time.monotonic_ns()
    name = "{:06d}.png".format(frame_id)
    for directory, frame in (("source_images", source), ("images", image)):
        if not cv2.imwrite(str(out / directory / name), frame,
                           [cv2.IMWRITE_PNG_COMPRESSION, 1]):
            raise RuntimeError("Image write failed: " + str(out / directory / name))
    row = [frame_id, after, "images/" + name, "source_images/" + name,
           before, after, write_started, time.monotonic_ns()]
    writer.writerow(row)
    rows.append(row)
    brightness.append(float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).mean()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--device", help="Override profile device, e.g. /dev/video4")
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "robot_dataset" / "runs")
    parser.add_argument("--preview", action="store_true", help="Show crop boundary and VLA image; Q stops")
    args = parser.parse_args()
    if args.duration <= 0 or not np.isfinite(args.duration):
        parser.error("--duration must be finite and positive")
    profile = load_profile(args.profile)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.output / (stamp + "_" + uuid.uuid4().hex[:8])
    (out / "source_images").mkdir(parents=True)
    (out / "images").mkdir()
    (out / "profile.json").write_text(json.dumps(profile, indent=2))
    metadata = {
        "profile_sha256": hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(),
        "profile": profile,
        "opencv_version": cv2.__version__,
        "timestamp_semantics": "Host monotonic time immediately after cap.read; NOT sensor exposure time",
        "image_semantics": "images: 224x224 RGB PNG; source_images: 640x480 RGB PNG; cv2 encoding receives BGR",
        "dataset_scope": "Camera-only diagnostic; robot state/actions are not recorded or synchronized",
        "status": "starting",
    }
    rows, brightness = [], []
    started = None
    capture_stopped = None
    cap = None
    failure = None
    interrupted = False
    submitted = 0
    max_pending = 0
    try:
        cap, metadata["camera"] = open_camera(profile, args.device)
        print("Camera ready; format {}. Output: {}".format(metadata["camera"]["format"], out), flush=True)
        started = time.monotonic()
        last_report = started
        with (out / "timestamps.csv").open("w", newline="") as f, ThreadPoolExecutor(max_workers=1) as pool:
            writer = csv.writer(f)
            writer.writerow(["frame_id", "monotonic_ns", "filename", "source_filename",
                             "read_started_monotonic_ns", "read_completed_monotonic_ns",
                             "write_started_monotonic_ns", "write_completed_monotonic_ns"])
            pending = deque()
            try:
                while time.monotonic() - started < args.duration:
                    while pending and pending[0].done():
                        pending.popleft().result()
                    # Stop explicitly on sustained disk overload; never silently drop frames.
                    if len(pending) >= 90:
                        raise RuntimeError("Writer backlog reached 90 frames; recording stopped")
                    before = time.monotonic_ns()
                    ok, source = cap.read()
                    after = time.monotonic_ns()
                    if not ok:
                        raise RuntimeError("Frame read failed; recording stopped")
                    image = preprocess_bgr(source, profile)
                    pending.append(pool.submit(save_frame, out, submitted, source.copy(),
                                               image, before, after, writer, rows, brightness))
                    submitted += 1
                    max_pending = max(max_pending, len(pending))
                    if args.preview:
                        display = source.copy()
                        x, y, w, h = profile["preprocessing"]["crop_xywh"]
                        cv2.rectangle(display, (x, y), (x + w - 1, y + h - 1), (0, 255, 0), 1)
                        cv2.imshow("Source 640x480: green = VLA crop", display)
                        cv2.imshow("VLA 224x224", image)
                        if cv2.waitKey(1) & 0xff in (ord("q"), 27):
                            break
                    now = time.monotonic()
                    if now - last_report >= 2:
                        print("{} captured; {} saved; {:.1f} capture FPS".format(
                            submitted, len(rows), submitted / (now - started)), flush=True)
                        last_report = now
            finally:
                capture_stopped = time.monotonic()
                cap.release()
                for future in pending:
                    future.result()
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        failure = str(exc)
        raise
    finally:
        elapsed = (capture_stopped or time.monotonic()) - started if started is not None else 0
        if cap is not None:
            cap.release()
        if args.preview:
            cv2.destroyAllWindows()
        intervals = np.diff([r[1] for r in rows]) / 1e6
        metadata.update({
            "status": "failed" if failure else ("interrupted" if interrupted else "complete"),
            "error": failure,
            "frames": len(rows),
            "elapsed_seconds": round(elapsed, 3),
            "total_seconds_including_flush": round(time.monotonic() - started, 3) if started else 0,
            "saved_fps": round(len(rows) / elapsed, 2) if elapsed else 0,
            "submitted_frames": submitted,
            "max_pending_writes": max_pending,
            "interval_ms_p50_p95_max": np.percentile(intervals, [50, 95, 100]).round(3).tolist() if len(intervals) else [],
            "intervals_over_50ms": int((intervals > 50).sum()),
            "image_mean_brightness_p05_p50_p95": np.percentile(brightness, [5, 50, 95]).round(2).tolist() if brightness else [],
        })
        (out / "metadata.json").write_text(json.dumps(metadata, indent=2))
        print("Saved {} frames at {} FPS: {}".format(len(rows), metadata["saved_fps"], out), flush=True)


if __name__ == "__main__":
    main()
