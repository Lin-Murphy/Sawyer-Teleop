#!/usr/bin/env python3
"""RealSense D435 RGB-only preview using Tkinter/Pillow. Q / Esc exits."""

import argparse
import sys
import time

import numpy as np
import pyrealsense2 as rs


def read_rgb(frames):
    frame = frames.get_color_frame()
    if not frame:
        return None
    # 独立内存、真正的 RGB 顺序，可在这里接入后续图像处理。
    return np.asanyarray(frame.get_data()).copy()  # (480, 640, 3), uint8


def preview(pipeline, frame_limit):
    import tkinter as tk
    from PIL import Image, ImageTk

    root = tk.Tk()
    root.title("RealSense D435 | RGB | Q / Esc to quit")
    root.configure(bg="#15191f")
    root.minsize(640, 520)
    status = tk.Label(root, text="Waiting for RGB...", bg="#15191f", fg="white",
                      font=("DejaVu Sans", 12), padx=12, pady=10)
    status.pack(fill="x")
    view = tk.Label(root, bg="#15191f")
    view.pack(expand=True)
    root.protocol("WM_DELETE_WINDOW", root.quit)
    for key in ("<Escape>", "<q>", "<Q>"):
        root.bind(key, lambda event: root.quit())
    count = 0
    first_received = None
    last_received = time.monotonic()
    error = None

    def update():
        nonlocal count, first_received, last_received, error
        try:
            # 不等待深度帧，也不阻塞 GUI。
            frames = pipeline.poll_for_frames()
            rgb = read_rgb(frames) if frames else None
            now = time.monotonic()
            if rgb is None:
                if now - last_received > 5:
                    raise RuntimeError("No RGB frame received within 5000 ms")
                root.after(5, update)
                return
            last_received = now
            if first_received is None:
                first_received = now
            count += 1
            fps = (count - 1) / max(now - first_received, 0.001)
            photo = ImageTk.PhotoImage(Image.fromarray(rgb), master=root)
            view.configure(image=photo)
            view.image = photo
            status.configure(text="RGB 640 x 480 | {:.1f} FPS | Frames: {} | Q / Esc: exit".format(
                fps, count))
            if count == 1 or count % 30 == 0:
                print("RGB frame {} | {} | {:.1f} FPS".format(count, rgb.shape, fps), flush=True)
            if frame_limit and count >= frame_limit:
                root.quit()
                return
            root.after(5, update)
        except Exception as exc:
            error = exc
            root.quit()

    try:
        root.after(0, update)
        root.mainloop()
    finally:
        root.destroy()
    if error is not None:
        raise error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", help="Select a camera by serial number")
    parser.add_argument("--headless", action="store_true", help="Read without a GUI")
    parser.add_argument("--frames", type=int, default=0, help="Exit after N RGB frames; 0 means unlimited")
    args = parser.parse_args()
    if args.frames < 0:
        parser.error("--frames must be >= 0")

    pipeline = rs.pipeline()
    config = rs.config()
    if args.serial:
        config.enable_device(args.serial)
    # 只启用彩色流；不配置、不读取、不等待 depth。
    config.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 30)
    started = False
    try:
        profile = pipeline.start(config)
        started = True
        print("Camera: {} | RGB only | 640 x 480 @ 30 FPS".format(
            profile.get_device().get_info(rs.camera_info.name)), flush=True)
        if not args.headless:
            preview(pipeline, args.frames)
            return
        count = 0
        first_received = None
        while args.frames == 0 or count < args.frames:
            rgb = read_rgb(pipeline.wait_for_frames(timeout_ms=5000))
            if rgb is None:
                continue
            now = time.monotonic()
            if first_received is None:
                first_received = now
            count += 1
            if count == 1 or count % 30 == 0:
                fps = (count - 1) / max(now - first_received, 0.001)
                print("RGB frame {} | {} | {:.1f} FPS".format(count, rgb.shape, fps), flush=True)
    finally:
        if started:
            pipeline.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print("RealSense RGB error: {}".format(exc), file=sys.stderr)
        sys.exit(1)
