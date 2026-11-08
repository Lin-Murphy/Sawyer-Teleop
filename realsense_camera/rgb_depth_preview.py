#!/usr/bin/env python3
"""RealSense D435 RGB + aligned depth preview. Q / Esc exits; no files are saved."""

import argparse
import sys
import time

import cv2
import numpy as np
import pyrealsense2 as rs


def frame_arrays(frames, align, depth_scale):
    """返回独立内存的 RGB、对齐后的原始深度和米制深度。"""
    frames = align.process(frames)
    color_frame = frames.get_color_frame()
    depth_frame = frames.get_depth_frame()
    if not color_frame or not depth_frame:
        return None
    rgb = np.asanyarray(color_frame.get_data()).copy()  # H×W×3, uint8, RGB
    depth_raw = np.asanyarray(depth_frame.get_data()).copy()  # H×W, uint16
    depth_m = depth_raw.astype(np.float32) * depth_scale  # H×W, 单位：米
    # depth_m[y, x] 对应 rgb[y, x]；0 表示无有效深度。
    return rgb, depth_raw, depth_m


def report_frame(count, rgb, depth_raw, depth_m):
    height, width = depth_m.shape
    center_m = float(depth_m[height // 2, width // 2])
    center_text = "{:.3f} m".format(center_m) if center_m > 0 else "N/A"
    if count == 1 or count % 30 == 0:
        print("Frame {} | RGB {} | depth {} | center {}".format(
            count, rgb.shape, depth_raw.shape, center_text), flush=True)
    return center_text


def preview_tk(pipeline, align, depth_scale, frame_limit):
    # Tkinter/Pillow 显示，避免 arcap 中 OpenCV Qt 的线程/字体冲突。
    import tkinter as tk
    from PIL import Image, ImageTk

    root = tk.Tk()
    root.title("RealSense D435 | RGB + aligned depth | Q / Esc to quit")
    root.configure(bg="#15191f")
    status = tk.Label(root, text="Waiting for RGB + depth...", bg="#15191f",
                      fg="white", font=("DejaVu Sans", 12), padx=12, pady=10)
    status.pack(fill="x")
    view = tk.Label(root, bg="#15191f")
    view.pack()
    root.bind("<Escape>", lambda event: root.quit())
    root.bind("<q>", lambda event: root.quit())
    root.bind("<Q>", lambda event: root.quit())
    root.protocol("WM_DELETE_WINDOW", root.quit)
    count = 0
    last_received = time.monotonic()
    error = None

    def update():
        nonlocal count, last_received, error
        try:
            # 非阻塞读取，等待图像时窗口也能正常响应退出操作。
            frames = pipeline.poll_for_frames()
            arrays = frame_arrays(frames, align, depth_scale) if frames else None
            if arrays is None:
                if time.monotonic() - last_received > 5:
                    raise RuntimeError("No RGB/depth pair received within 5000 ms")
                root.after(5, update)
                return
            last_received = time.monotonic()
            rgb, depth_raw, depth_m = arrays
            count += 1
            center_text = report_frame(count, rgb, depth_raw, depth_m)

            # 伪彩图只用于显示，3 米显示上限不裁剪 depth_m。
            depth_u8 = (np.clip(depth_m / 3.0, 0, 1) * 255).astype(np.uint8)
            depth_vis = cv2.applyColorMap(depth_u8, cv2.COLORMAP_JET)
            depth_vis = cv2.cvtColor(depth_vis, cv2.COLOR_BGR2RGB)
            depth_vis[depth_raw == 0] = 0
            preview = np.hstack((rgb, depth_vis))
            height, width = depth_m.shape
            for cx in (width // 2, width + width // 2):
                cv2.drawMarker(preview, (cx, height // 2), (255, 255, 255),
                               cv2.MARKER_CROSS, 18, 1)
            photo = ImageTk.PhotoImage(Image.fromarray(preview), master=root)
            view.configure(image=photo)
            view.image = photo
            status.configure(text="RGB (left) | Depth 0-3 m (right, invalid: black)"
                             " | Center: {} | Frames: {}".format(center_text, count))
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
    parser.add_argument("--serial", help="Select a camera by its serial number")
    parser.add_argument("--headless", action="store_true", help="Read without a GUI")
    parser.add_argument("--frames", type=int, default=0,
                        help="Exit after N valid RGB/depth pairs; 0 means unlimited")
    args = parser.parse_args()
    if args.frames < 0:
        parser.error("--frames must be >= 0")

    pipeline = rs.pipeline()
    config = rs.config()
    if args.serial:
        config.enable_device(args.serial)
    config.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 30)
    config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)

    # 两个传感器视角不同，将 depth 投影到 RGB 的像素坐标。
    align = rs.align(rs.stream.color)
    started = False
    count = 0

    try:
        profile = pipeline.start(config)
        started = True
        device = profile.get_device()
        depth_scale = device.first_depth_sensor().get_depth_scale()
        print("Camera: {} | depth scale: {} m/unit".format(
            device.get_info(rs.camera_info.name), depth_scale), flush=True)

        if not args.headless:
            preview_tk(pipeline, align, depth_scale, args.frames)
            return

        while args.frames == 0 or count < args.frames:
            frames = pipeline.wait_for_frames(timeout_ms=5000)
            arrays = frame_arrays(frames, align, depth_scale)
            if arrays is None:
                continue
            rgb, depth_raw, depth_m = arrays
            # 在这里接入自己的处理/推理代码。
            count += 1
            report_frame(count, rgb, depth_raw, depth_m)
    finally:
        if started:
            pipeline.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print("RealSense error: {}".format(exc), file=sys.stderr)
        print("Check USB connection / permissions and close other camera viewers.",
              file=sys.stderr)
        sys.exit(1)
