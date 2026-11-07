"""Live wrist-camera alignment. Only explicit snapshots are written to disk."""

import argparse
from collections import deque
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk

import cv2
from PIL import Image, ImageTk

from camera_pipeline import (
    DEFAULT_PROFILE, load_profile, open_camera, preprocess_bgr, validate_frame,
)


def claim_preview(device):
    """Use one preview per physical device, including /dev/videoN aliases."""
    key = hashlib.sha256(str(Path(device).resolve()).encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / "wrist-preview-{}-{}.lock".format(os.getuid(), key)
    owner = path.open("a+")
    try:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner.seek(0)
        try:
            pid = int(owner.read().strip())
            if pid <= 1:
                raise ValueError("Invalid preview PID")
            os.kill(pid, signal.SIGUSR1)
            print("相机预览已在运行，已唤起已有窗口（PID {}）。".format(pid), flush=True)
        except (ValueError, OSError):
            print("该相机的预览已在运行或正在启动，请使用已有窗口。", flush=True)
        owner.close()
        return None
    owner.seek(0)
    owner.truncate()
    owner.write(str(os.getpid()))
    owner.flush()
    return owner


def zoom_region(frame, point, side=160):
    """Keep a square detail view inside the image, including at the edges."""
    height, width = frame.shape[:2]
    side = min(side, width, height)
    x = max(0, min(width - side, round(point[0] - side / 2)))
    y = max(0, min(height - side, round(point[1] - side / 2)))
    return frame[y:y + side, x:x + side], (x, y)


def draw_guides(frame, profile, target=None, tips=(), grid=True):
    image = frame.copy()
    height, width = image.shape[:2]
    cx, cy = width // 2, height // 2
    if grid:
        for x in (width // 3, width * 2 // 3):
            cv2.line(image, (x, 0), (x, height - 1), (90, 90, 90), 1)
        for y in (height // 3, height * 2 // 3):
            cv2.line(image, (0, y), (width - 1, y), (90, 90, 90), 1)
    x, y, w, h = profile["preprocessing"]["crop_xywh"]
    cv2.rectangle(image, (x, y), (x + w - 1, y + h - 1), (98, 220, 75), 1)
    # Fixed image centre: moving a marker never moves this reference.
    cv2.line(image, (cx, 0), (cx, height - 1), (255, 215, 65), 1)
    cv2.line(image, (0, cy), (width - 1, cy), (255, 215, 65), 1)
    cv2.circle(image, (cx, cy), 18, (255, 215, 65), 1, cv2.LINE_AA)
    for point in tips:
        cv2.circle(image, tuple(point), 6, (0, 205, 255), 2, cv2.LINE_AA)
    if len(tips) == 2:
        cv2.line(image, tuple(tips[0]), tuple(tips[1]), (0, 205, 255), 1)
    if target is not None:
        point = tuple(round(v) for v in target)
        cv2.line(image, (cx, cy), point, (0, 205, 255), 2, cv2.LINE_AA)
        cv2.drawMarker(image, point, (0, 205, 255), cv2.MARKER_CROSS, 22, 2)
    return image


class CameraFeed:
    """One camera owner and one latest frame; UI delays cannot build a queue."""

    def __init__(self, profile, device=None):
        self.profile = copy.deepcopy(profile)
        if device:
            self.profile["device"] = device
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.frame = None
        self.stamp = 0.0
        self.sequence = 0
        self.fps = 0.0
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        cap = None
        times = deque(maxlen=60)
        try:
            cap, _ = open_camera(self.profile)
            print("相机已连接：{} · {}×{}。按 Q / Esc 退出。".format(
                self.profile["device"], self.profile["capture"]["width"],
                self.profile["capture"]["height"]), flush=True)
            while not self.stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError("相机读取失败，请检查 USB 连接，然后重新启动预览。")
                validate_frame(frame, self.profile)
                now = time.monotonic()
                times.append(now)
                with self.lock:
                    self.frame, self.stamp = frame, now
                    self.sequence += 1
                    self.fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            print("相机错误：{}；请检查设备连接，以及是否有其他采集程序占用相机。".format(exc),
                  file=sys.stderr, flush=True)
        finally:
            if cap is not None:
                cap.release()

    def latest(self):
        with self.lock:
            return (self.frame, self.stamp, self.sequence, self.fps, self.error,
                    copy.deepcopy(self.profile))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)


class Preview:
    def __init__(self, root, feed, output):
        self.root, self.feed, self.output = root, feed, output
        self.target, self.tips = None, []
        self.frame = self.annotated = None
        self.profile = copy.deepcopy(feed.profile)
        self.last_sequence = -1
        self.last_stamp = 0
        self.view_geometry = None
        self.closed = False
        root.title("Wrist Camera · 夹爪视角调整")
        root.geometry("1360x930")
        root.minsize(1080, 780)
        root.configure(bg="#111722")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#111722")
        style.configure("TLabel", background="#111722", foreground="#e3e9f1", font=("sans-serif", 11))
        style.configure("Title.TLabel", font=("sans-serif", 19, "bold"))
        style.configure("TButton", font=("sans-serif", 10), padding=(10, 7))
        style.configure("TCheckbutton", background="#111722", foreground="#e3e9f1", font=("sans-serif", 10))
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="WRIST CAMERA  /  LIVE PREVIEW", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="全屏 F", command=self.fullscreen).pack(side="right")
        self.status = tk.StringVar(value="正在打开相机并预热…")
        ttk.Label(outer, textvariable=self.status, wraplength=1250).pack(anchor="w", pady=(8, 12))
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        ttk.Label(body, text="完整视野 · 蓝色十字 = 图像中心 · 绿色框 = 策略裁剪").grid(row=0, column=0, sticky="w", pady=(0, 7))
        self.canvas = tk.Canvas(body, bg="#080d14", highlightthickness=0, cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=(0, 18))
        self.canvas.bind("<Button-1>", self.mark)
        self.canvas.bind("<Configure>", lambda event: self.render())
        aside = ttk.Frame(body, width=270)
        aside.grid(row=0, column=1, rowspan=2, sticky="ns")
        ttk.Label(aside, text="局部放大 · 标记附近 160×160").pack(anchor="w")
        self.blank_detail = tk.PhotoImage(width=256, height=256)
        self.detail = tk.Label(aside, bg="#080d14", image=self.blank_detail)
        self.detail.pack(pady=(8, 16))
        ttk.Label(aside, text="实际策略输入 · 224×224").pack(anchor="w")
        self.blank_policy = tk.PhotoImage(width=224, height=224)
        self.policy = tk.Label(aside, bg="#080d14", image=self.blank_policy)
        self.policy.pack(pady=(8, 8))
        ttk.Label(aside, text="放大图跟随标记；未标记时显示中心。", wraplength=265).pack(anchor="w")
        self.offset = tk.StringVar(value="点击夹爪中心，查看它与画面中心的偏移。")
        ttk.Label(outer, textvariable=self.offset, font=("sans-serif", 12, "bold"), wraplength=1250).pack(anchor="w", pady=(12, 8))
        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        self.tip_mode = tk.BooleanVar(value=False)
        self.grid = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="依次点两个指尖，计算中点", variable=self.tip_mode, command=self.reset).pack(side="left")
        ttk.Checkbutton(actions, text="三分网格", variable=self.grid, command=self.render).pack(side="left", padx=12)
        ttk.Button(actions, text="清除标记 R", command=self.reset).pack(side="left")
        ttk.Button(actions, text="保存快照 S", command=self.snapshot).pack(side="right")
        self.notice = tk.StringVar(value="已自动使用 camera_test 的固定成像配置，可直接查看实时画面。")
        ttk.Label(outer, textvariable=self.notice, wraplength=1250).pack(anchor="w", pady=(4, 0))
        ttk.Label(outer, text="持续预览，不自动录像  ·  S 快照  ·  R 清除标记  ·  F 全屏  ·  Q / Esc 退出", foreground="#9aa9bc").pack(anchor="w", pady=(8, 0))
        root.bind("<KeyPress>", self.key)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.feed.start()
        self.root.after(30, self.tick)

    def tick(self):
        if self.closed:
            return
        frame, stamp, seq, fps, error, profile = self.feed.latest()
        self.profile = profile
        age = time.monotonic() - stamp if stamp else 0
        if error:
            self.status.set("相机错误：" + error)
        elif stamp and age > .5:
            self.status.set("画面已停滞 · {:.1f} 秒没有新帧，请检查相机连接".format(age))
        elif stamp:
            self.status.set("实时  ·  {:.1f} FPS  ·  {}×{}  ·  {}".format(
                fps, frame.shape[1], frame.shape[0], profile["device"]))
        stale = bool(error or (stamp and age > .5))
        if seq != self.last_sequence or stale:
            self.frame, self.last_stamp, self.last_sequence = frame, stamp, seq
            self.render(stale=stale)
        self.root.after(30, self.tick)

    @staticmethod
    def photo(bgr, size):
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return ImageTk.PhotoImage(Image.fromarray(rgb).resize(size, Image.BILINEAR))

    def render(self, stale=False):
        if self.frame is None:
            return
        height, width = self.frame.shape[:2]
        self.annotated = draw_guides(self.frame, self.profile, self.target, self.tips, self.grid.get())
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        scale = min(cw / width, ch / height)
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        left, top = (cw - size[0]) // 2, (ch - size[1]) // 2
        self.view_geometry = (left, top, size[0], size[1], width, height)
        self.source_photo = self.photo(self.annotated, size)
        self.canvas.delete("all")
        self.canvas.create_image(left, top, anchor="nw", image=self.source_photo)
        if stale:
            self.canvas.create_rectangle(0, 0, cw, 50, fill="#9b2638", outline="")
            self.canvas.create_text(cw // 2, 25, text="画面已停止更新 · 当前为最后一帧", fill="white", font=("sans-serif", 15, "bold"))
        point = self.target or (width // 2, height // 2)
        detail, origin = zoom_region(self.frame, point)
        detail = detail.copy()
        cv2.drawMarker(detail, (round(point[0] - origin[0]), round(point[1] - origin[1])),
                       (0, 205, 255), cv2.MARKER_CROSS, 15, 1)
        self.detail_photo = self.photo(detail, (256, 256))
        self.detail.configure(image=self.detail_photo, width=256, height=256)
        self.policy_photo = self.photo(preprocess_bgr(self.frame, self.profile), (224, 224))
        self.policy.configure(image=self.policy_photo, width=224, height=224)

    def mark(self, event):
        if not self.view_geometry:
            return
        left, top, vw, vh, width, height = self.view_geometry
        if not (left <= event.x < left + vw and top <= event.y < top + vh):
            return
        point = (min(width - 1, int((event.x - left) * width / vw)),
                 min(height - 1, int((event.y - top) * height / vh)))
        if self.tip_mode.get():
            if len(self.tips) == 2:
                self.tips = []
            self.tips.append(point)
            self.target = tuple((a + b) / 2 for a, b in zip(*self.tips)) if len(self.tips) == 2 else None
        else:
            self.tips = []
            self.target = point
        if self.target is None:
            self.offset.set("已标记第一个指尖，请点击另一个指尖。")
        else:
            dx, dy = self.target[0] - width // 2, self.target[1] - height // 2
            self.offset.set("手动标记 ({:.1f}, {:.1f})  ·  偏移 dx={:+.1f}px  dy={:+.1f}px  ·  右 / 下为正；移动相机后重新标记".format(*self.target, dx, dy))
        self.render()

    def reset(self):
        self.target, self.tips = None, []
        self.offset.set("点击夹爪中心，查看它与画面中心的偏移。")
        self.render()

    def snapshot(self):
        if self.frame is None or time.monotonic() - self.last_stamp > .5:
            self.notice.set("当前没有新鲜画面，未保存快照。")
            return
        try:
            destination = self.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            destination.mkdir(parents=True)
            for name, image in (("source.png", self.frame), ("alignment.png", self.annotated),
                                ("policy_224.png", preprocess_bgr(self.frame, self.profile))):
                if not cv2.imwrite(str(destination / name), image):
                    raise RuntimeError("无法保存 " + name)
            (destination / "alignment.json").write_text(json.dumps({
                "profile": self.profile, "manual_target_xy": self.target, "manual_fingertips_xy": self.tips,
                "frame_read_monotonic": self.last_stamp,
                "marker_semantics": "Manual fixed image coordinates, not live tracking or camera calibration",
            }, indent=2))
            (destination / "profile.json").write_text(json.dumps(self.profile, indent=2) + "\n")
            self.notice.set("已保存原图、标记图、224 图及当前参数：" + str(destination))
        except Exception as exc:
            self.notice.set("快照失败：" + str(exc))

    def fullscreen(self):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def key(self, event):
        key = event.keysym.lower()
        if key in ("q", "escape"):
            self.close()
        elif key == "f":
            self.fullscreen()
        elif key == "r":
            self.reset()
        elif key == "s":
            self.snapshot()

    def close(self):
        if not self.closed:
            self.closed = True
            self.feed.close()
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--device", help="Override profile device, e.g. /dev/video4")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "robot_dataset" / "wrist_alignment")
    args = parser.parse_args()
    profile = load_profile(args.profile)
    focus_requested = threading.Event()
    # Register before publishing the PID: a second launch may arrive immediately.
    signal.signal(signal.SIGUSR1, lambda *_: focus_requested.set())
    owner = claim_preview(args.device or profile["device"])
    if owner is None:
        return
    with owner:
        root = tk.Tk()
        preview = Preview(root, CameraFeed(profile, args.device), args.output)

        def check_focus():
            if preview.closed:
                return
            if focus_requested.is_set():
                focus_requested.clear()
                root.deiconify()
                root.lift()
                root.attributes("-topmost", True)
                root.after(150, lambda: root.attributes("-topmost", False))
                root.focus_force()
            root.after(100, check_focus)

        root.after(100, check_focus)
        try:
            root.mainloop()
        finally:
            if not preview.closed:
                preview.close()


if __name__ == "__main__":
    main()
