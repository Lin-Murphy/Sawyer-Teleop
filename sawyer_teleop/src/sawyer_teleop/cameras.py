"""Independent capture threads; timestamp is host arrival, not sensor exposure time."""
import collections
import threading

import cv2

from .protocol import ns


class Camera:
    def __init__(self, name="front", device=0, width=640, height=480, fps=30, backend="auto"):
        if not name.isidentifier():
            raise ValueError("camera name must be a Python identifier")
        self.name, self.device = name, device
        self.width, self.height, self.fps, self.backend = width, height, fps, backend
        self.lock = threading.Lock()
        self.frames = collections.deque(maxlen=4)
        self.stop = threading.Event()
        self.error = None
        self.seq = 0
        self.thread = None
        self.sink = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        modes = {"auto": cv2.CAP_ANY, "dshow": cv2.CAP_DSHOW,
                 "msmf": cv2.CAP_MSMF, "v4l2": cv2.CAP_V4L2}
        cap = cv2.VideoCapture(self.device, modes[self.backend])
        try:
            if not cap.isOpened():
                raise RuntimeError("camera_open_failed:" + self.name)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            while not self.stop.is_set():
                start = ns()
                ok, bgr = cap.read()
                end = ns()
                if not ok:
                    raise RuntimeError("camera_read_failed:" + self.name)
                frame = {"seq": self.seq, "ns": end, "read_start_ns": start,
                         "rgb": cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)}
                self.seq += 1
                with self.lock:
                    self.frames.append(frame)
                    sink = self.sink
                if sink:
                    sink(self.name, frame)
        except Exception as exc:
            self.error = str(exc)
        finally:
            cap.release()

    def latest(self, max_age=.25):
        with self.lock:
            if not self.frames or ns()-self.frames[-1]["ns"] > max_age*1e9:
                raise RuntimeError(self.error or "camera_stale:" + self.name)
            return self.frames[-1]

    def set_sink(self, sink):
        with self.lock:
            self.sink = sink

    def close(self):
        self.stop.set()
        self.set_sink(None)
        if self.thread:
            self.thread.join(timeout=2)


class SyntheticCamera(Camera):
    """Explicit test source, never selected by the real-robot command."""
    def _run(self):
        import numpy as np
        while not self.stop.is_set():
            stamp = ns()
            rgb = np.full((self.height, self.width, 3), self.seq % 256, dtype=np.uint8)
            frame = {"seq": self.seq, "ns": stamp, "read_start_ns": stamp, "rgb": rgb}
            self.seq += 1
            with self.lock:
                self.frames.append(frame)
                sink = self.sink
            if sink:
                sink(self.name, frame)
            self.stop.wait(1/self.fps)
