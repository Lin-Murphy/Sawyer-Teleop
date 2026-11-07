"""Independent capture threads; timestamp is host arrival, not sensor exposure time."""
import collections
import ctypes
import os
import threading
import time
import uuid

import cv2
import numpy as np

from .protocol import ns


def windows_camera_names():
    """DirectShow video input names in the same order OpenCV's CAP_DSHOW indexes them."""
    if os.name != "nt":
        return []
    ole32, oleaut32 = ctypes.windll.ole32, ctypes.windll.oleaut32
    def guid(text):
        return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)
    def method(obj, index, *argtypes):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])
    def release(obj):
        if obj:
            method(obj, 2)(obj)
    initialized = ole32.CoInitializeEx(None, 0) in (0, 1)  # S_OK / S_FALSE
    names, devenum, monikers = [], ctypes.c_void_p(), ctypes.c_void_p()
    try:
        if ole32.CoCreateInstance(guid("62BE5D10-60EB-11d0-BD3B-00A0C911CE86"), None, 1,
                                  guid("29840822-5B84-11D0-BD3B-00A0C911CE86"), ctypes.byref(devenum)):
            return names
        create = method(devenum, 3, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
        if create(devenum, guid("860BB310-5D01-11d0-BD3B-00A0C911CE86"), ctypes.byref(monikers), 0) != 0:
            return names  # S_FALSE: no video input devices
        bag_iid = guid("55272A00-42CB-11CE-8135-00AA004BB851")
        variant = (ctypes.c_byte * 24)()
        while True:
            moniker, fetched = ctypes.c_void_p(), ctypes.c_ulong()
            if method(monikers, 3, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)(
                    monikers, 1, ctypes.byref(moniker), ctypes.byref(fetched)) != 0 or not fetched.value:
                break
            bag, name = ctypes.c_void_p(), ""
            try:
                if method(moniker, 9, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
                        moniker, None, None, bag_iid, ctypes.byref(bag)) == 0:
                    ctypes.memset(variant, 0, 24)
                    if method(bag, 3, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p)(
                            bag, "FriendlyName", variant, None) == 0:
                        if ctypes.c_ushort.from_buffer(variant).value == 8:  # VT_BSTR
                            name = ctypes.wstring_at(ctypes.c_void_p.from_buffer(variant, 8).value)
                        oleaut32.VariantClear(variant)
            finally:
                release(bag)
                release(moniker)
            names.append(name)
        return names
    finally:
        release(monikers)
        release(devenum)
        if initialized:
            ole32.CoUninitialize()


def resolve_device(device, backend):
    """Return (OpenCV device, backend). Windows names select a DirectShow index."""
    if not isinstance(device, str) or device.isdigit() or os.name != "nt":
        return (int(device) if isinstance(device, str) and device.isdigit() else device), backend
    names = windows_camera_names()
    for match in (lambda n: n == device, lambda n: device.casefold() in n.casefold()):
        hits = [i for i, n in enumerate(names) if match(n)]
        if hits:
            return hits[0], "dshow"
    raise RuntimeError("camera_not_found:" + device + " (Windows has no working video device with this name; "
                       "check the USB cable and Device Manager driver)")


class FramePacer:
    """Thin a faster stream to ``fps`` on an even time grid (no bursts, no drift)."""
    def __init__(self, fps):
        self.period = 1e9 / fps if fps else 0
        self.due = None

    def keep(self, stamp_ns):
        if not self.period:
            return True
        if self.due is not None and stamp_ns < self.due - self.period / 4:
            return False
        self.due = (self.due or stamp_ns) + self.period
        if self.due < stamp_ns:          # after a stall, restart the grid instead of bursting
            self.due = stamp_ns + self.period
        return True


def exposure_metrics(rgb):
    """Brightness/clipping summary used to flag over- or under-exposure (on a 1/4 image)."""
    small = rgb[::4, ::4]
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    p05, p50, p95 = (float(x) for x in np.percentile(gray, (5, 50, 95)))
    dark = float((gray <= 20).mean() * 100)
    bright = float((gray >= 245).mean() * 100)
    clip = float((small >= 254).any(axis=2).mean() * 100)
    r, g, b = (float(x) for x in small.reshape(-1, 3).mean(axis=0))
    state = "over" if bright > 5 or clip > 5 or p50 > 200 else "under" if dark > 30 or p50 < 35 else "ok"
    return dict(mean=float(gray.mean()), p05=p05, p50=p50, p95=p95, dark_pct=dark,
                bright_pct=bright, channel_clip_pct=clip, rgb_mean=[r, g, b], state=state)


class Camera:
    def __init__(self, name="front", device=0, width=640, height=480, fps=30, backend="auto",
                 label=None, reconnect=False, controls=None, source_fps=None):
        if not name.isidentifier():
            raise ValueError("camera name must be a Python identifier")
        self.name, self.device, self.label = name, device, label or name
        self.width, self.height, self.fps, self.backend = width, height, fps, backend
        # Mode requested from the device; frames are then evenly thinned to ``fps``
        # (e.g. a D435 RGB has no 20 Hz mode, so it runs at 30 and keeps 2 of 3).
        self.source_fps = source_fps or fps
        self.controls = dict(controls or {})   # sn9c20x backend: V4L2-named imaging controls
        self.reconnect = reconnect
        self.lock = threading.Lock()
        self.frames = collections.deque(maxlen=4)
        self.stop = threading.Event()
        self.error = None
        self.seq = 0
        self.source_seq = 0   # every successful device read, before FramePacer thinning
        self.open_count = 0   # increments on each (re)open; a change marks a reconnect
        self.measured_fps = None
        self.driver_stats = None   # counters from user-space drivers (sn9c20x)
        self.thread = None
        self.sink = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="camera-"+self.name, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.is_set():
            try:
                self._capture()
            except Exception as exc:
                self.error = str(exc)
            if not self.reconnect:
                break
            self.stop.wait(2.0)

    def _open(self):
        if self.backend == "sn9c20x":
            # Sonix SN9C201 wrist camera through WinUSB; no Windows vendor driver exists.
            # USB is received in a child process so this process's GIL load cannot
            # make the camera FIFO overflow (see sn9c20x.SN9C20xProcessCapture).
            from .sn9c20x import SN9C20xProcessCapture
            if (self.width, self.height) != (640, 480):
                raise ValueError("sn9c20x backend supports 640x480 only")
            # The sensor itself runs at ``fps`` (dummy lines), so no thinning is needed.
            return SN9C20xProcessCapture(dict(self.controls, frame_rate=self.source_fps))
        modes = {"auto": cv2.CAP_ANY, "dshow": cv2.CAP_DSHOW,
                 "msmf": cv2.CAP_MSMF, "v4l2": cv2.CAP_V4L2}
        device, backend = resolve_device(self.device, self.backend)
        cap = cv2.VideoCapture(device, modes[backend])
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_FPS, self.source_fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _capture(self):
        cap = self._open()
        self.driver_stats = getattr(cap, "stats", None)
        try:
            if not cap.isOpened():
                raise RuntimeError("camera_open_failed:" + self.name)
            self.open_count += 1
            window_start, window_seq = time.monotonic(), self.seq
            pacer = FramePacer(self.fps)
            while not self.stop.is_set():
                start = ns()
                ok, bgr = cap.read()
                end = ns()
                if not ok:
                    raise RuntimeError("camera_read_failed:" + (getattr(cap, "error", None) or self.name))
                # Every successful device read, before thinning: an expected stride
                # (e.g. 30->20 Hz keeps 2 of 3) is normal; time gaps reveal real drops.
                self.source_seq += 1
                if not pacer.keep(end):
                    continue
                frame = {"seq": self.seq, "ns": end, "read_start_ns": start,
                         "source_seq": self.source_seq, "open_count": self.open_count,
                         "rgb": cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)}
                self.seq += 1
                self.error = None
                elapsed = time.monotonic() - window_start
                if elapsed >= 1.0:
                    self.measured_fps = (self.seq - window_seq) / elapsed
                    window_start, window_seq = time.monotonic(), self.seq
                with self.lock:
                    self.frames.append(frame)
                    sink = self.sink
                if sink:
                    sink(self.name, frame)
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
            self.source_seq += 1
            frame = {"seq": self.seq, "ns": stamp, "read_start_ns": stamp,
                     "source_seq": self.source_seq, "open_count": 1, "rgb": rgb}
            self.seq += 1
            self.measured_fps = self.fps
            with self.lock:
                self.frames.append(frame)
                sink = self.sink
            if sink:
                sink(self.name, frame)
            self.stop.wait(1/self.fps)


class SharedCamera:
    """Operator view of a hub-owned camera: recording may attach a sink, never stop capture."""
    def __init__(self, camera):
        self.camera = camera

    def __getattr__(self, key):
        return getattr(self.camera, key)

    def start(self):
        return self

    def latest(self, max_age=.25):
        return self.camera.latest(max_age)

    def set_sink(self, sink):
        self.camera.set_sink(sink)

    def close(self):
        self.camera.set_sink(None)


class CameraHub:
    """Long-lived A-side cameras for the Web live view and, when enabled, recording."""
    def __init__(self, configs, factory=Camera):
        self.cameras = [factory(**dict(c, reconnect=True)) for c in configs]
        self.lock = threading.Lock()
        self.encoded = {}
        self.exposure = {}   # name -> (monotonic time, metrics); refreshed at most 1 Hz

    def start(self):
        for camera in self.cameras:
            camera.start()
        return self

    def views(self):
        return [SharedCamera(c) for c in self.cameras]

    def status(self):
        result = []
        for c in self.cameras:
            exposure = None
            try:
                frame = c.latest(1.0)
                live, age, error = True, (ns()-frame["ns"])/1e6, None
                with self.lock:
                    cached = self.exposure.get(c.name)
                    if cached is None or time.monotonic() - cached[0] >= 1.0:
                        cached = self.exposure[c.name] = (time.monotonic(), exposure_metrics(frame["rgb"]))
                exposure = cached[1]
            except RuntimeError as exc:
                live, age, error = False, None, str(exc)
            result.append(dict(name=c.name, label=c.label, live=live, age_ms=age, error=error,
                               fps=c.measured_fps if live else None, width=c.width, height=c.height,
                               target_fps=c.fps, exposure=exposure,
                               diagnostics=dict(c.driver_stats) if c.driver_stats is not None else None))
        return result

    def jpeg(self, name, quality=80):
        camera = next((c for c in self.cameras if c.name == name), None)
        if camera is None:
            raise KeyError(name)
        frame = camera.latest(1.0)
        with self.lock:
            cached = self.encoded.get(name)
            if cached and cached[0] == frame["seq"]:
                return cached[1]
        ok, data = cv2.imencode(".jpg", cv2.cvtColor(frame["rgb"], cv2.COLOR_RGB2BGR),
                                [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("camera_encode_failed:" + name)
        data = data.tobytes()
        with self.lock:
            self.encoded[name] = (frame["seq"], data)
        return data

    def close(self):
        for camera in self.cameras:
            camera.close()
