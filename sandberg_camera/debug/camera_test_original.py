
import cv2
import time
import csv
from pathlib import Path

out = Path("robot_dataset")
(out / "images").mkdir(parents=True, exist_ok=True)

cap = cv2.VideoCapture("/dev/video4", cv2.CAP_V4L2)

cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(
    cv2.CAP_PROP_FOURCC,
    cv2.VideoWriter_fourcc(*"JPEG")
)

if not cap.isOpened():
    raise RuntimeError("Cannot open camera")

records = []
start = time.monotonic()

while time.monotonic() - start < 10:
    ok, frame = cap.read()
    if not ok:
        print("Frame read failed")
        break

    timestamp_ns = time.monotonic_ns()
    frame_id = len(records)
    filename = f"{frame_id:06d}.jpg"

    if cv2.imwrite(str(out / "images" / filename), frame):
        records.append([frame_id, timestamp_ns, filename])

cap.release()

with open(out / "timestamps.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["frame_id", "monotonic_ns", "filename"])
    writer.writerows(records)

print(f"Saved {len(records)} frames")
