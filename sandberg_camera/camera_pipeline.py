"""Shared capture/preprocessing for recording and future VLA inference.

OpenCV reads BGR. PNG encoders take BGR; policy arrays use preprocess_rgb.
Exposure values are driver units, not milliseconds.
"""

import json
import subprocess
from pathlib import Path

import cv2

DEFAULT_PROFILE = Path(__file__).with_name("camera_profile.json")


def load_profile(path=DEFAULT_PROFILE):
    profile = json.loads(Path(path).read_text())
    capture = profile["capture"]
    prep = profile["preprocessing"]
    x, y, width, height = prep["crop_xywh"]
    if (min(x, y) < 0 or min(width, height) <= 0
            or x + width > capture["width"] or y + height > capture["height"]):
        raise ValueError("Crop must fit inside the source frame")
    if width != height or prep["output_size"] != [224, 224]:
        raise ValueError("This VLA profile requires a square crop and 224x224 output")
    if prep["interpolation"] != "INTER_AREA" or prep["model_color_order"] != "RGB":
        raise ValueError("Unexpected preprocessing convention")
    return profile


def v4l2(device, *args):
    return subprocess.check_output(
        ["v4l2-ctl", "-d", device] + list(args), text=True,
        stderr=subprocess.STDOUT,
    )


def apply_controls(device, controls):
    # Disable automatic exposure before writing manual exposure.
    if "gain_automatic" in controls:
        v4l2(device, "--set-ctrl", "gain_automatic={}".format(controls["gain_automatic"]))
    settings = ",".join("{}={}".format(k, v) for k, v in controls.items()
                        if k != "gain_automatic")
    if settings:
        v4l2(device, "--set-ctrl", settings)
    actual = v4l2(device, "--get-ctrl", ",".join(controls))
    values = {}
    for line in actual.splitlines():
        key, value = line.split(":", 1)
        values[key.strip()] = int(value.strip())
    if any(values.get(k) != v for k, v in controls.items()):
        raise RuntimeError("Camera controls did not match profile: " + actual)
    return actual


def open_camera(profile, device=None):
    device = device or profile["device"]
    info = v4l2(device, "--info")
    if profile["expected_driver"] not in info:
        raise RuntimeError("This profile is for gspca_sn9c20x; check --device")
    settings = profile["capture"]
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError("Cannot open camera " + device)
    try:
        for prop, value in (
            (cv2.CAP_PROP_FRAME_WIDTH, settings["width"]),
            (cv2.CAP_PROP_FRAME_HEIGHT, settings["height"]),
            (cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*settings["fourcc"])),
            (cv2.CAP_PROP_CONVERT_RGB, 1),
        ):
            if not cap.set(prop, value):
                raise RuntimeError("Camera rejected property {}={}".format(prop, value))
        accepted = cap.set(cv2.CAP_PROP_BUFFERSIZE, settings["buffer_count"])
        actual_format = int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode("ascii")
        if actual_format != settings["fourcc"]:
            raise RuntimeError("Unexpected camera format: " + actual_format)
        applied = apply_controls(device, profile["controls"])
        for _ in range(settings["warmup_frames"]):
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError("Camera failed during warmup")
            validate_frame(frame, profile)
        return cap, {
            "device": device,
            "driver_info": info,
            "format": actual_format,
            "buffer_request_accepted": bool(accepted),
            "reported_buffer_count": cap.get(cv2.CAP_PROP_BUFFERSIZE),
            "applied_controls": applied,
            "format_info": v4l2(device, "--get-fmt-video"),
        }
    except BaseException:
        cap.release()
        raise


def validate_frame(frame, profile):
    settings = profile["capture"]
    expected = (settings["height"], settings["width"], 3)
    if frame is None or frame.shape != expected or str(frame.dtype) != "uint8":
        raise RuntimeError("Unexpected frame shape/type; expected {} uint8".format(expected))


def preprocess_bgr(frame_bgr, profile):
    """Return 224x224 BGR pixels for cv2.imwrite. No adaptive enhancement."""
    validate_frame(frame_bgr, profile)
    prep = profile["preprocessing"]
    x, y, width, height = prep["crop_xywh"]
    return cv2.resize(frame_bgr[y:y + height, x:x + width],
                      tuple(prep["output_size"]), interpolation=cv2.INTER_AREA)


def preprocess_rgb(frame_bgr, profile):
    """Return contiguous 224x224 RGB uint8 for a VLA policy processor."""
    return cv2.cvtColor(preprocess_bgr(frame_bgr, profile), cv2.COLOR_BGR2RGB)
