"""Offline checks of the SN9C20x user-space reader (no USB device needed)."""
import numpy as np
import pytest

from sawyer_teleop import sn9c20x as m
from sawyer_teleop.cameras import Camera


HEADER = m.FRAME_HEADER + bytes(58)


def test_header_packet_ends_frame_and_starts_next():
    frames = []
    a = m.FrameAssembler(frames.append)
    a.packet(b"\x01" * 100)                    # before first header: not synced, dropped
    a.packet(HEADER)
    for _ in range(m.FRAME_BYTES // 3072):
        a.packet(b"\x02" * 3072)
    a.packet(HEADER + b"\x03" * 10)            # completes frame, next frame starts with 10 bytes
    assert len(frames) == 1 and len(frames[0]) == m.FRAME_BYTES and set(frames[0]) == {2}
    assert a.size == 10


def test_short_or_corrupt_frames_are_dropped():
    frames = []
    a = m.FrameAssembler(frames.append)
    a.packet(HEADER)
    a.packet(b"\x02" * 1000)
    a.packet(HEADER)                           # too short (bridge FIFO overflow)
    for _ in range(m.FRAME_BYTES // 3072):
        a.packet(b"\x02" * 3072)
    a.corrupt()                                # lost packet invalidates the frame
    a.packet(HEADER)
    assert frames == []


def test_controls_match_linux_formulas():
    cm = m.color_matrix(127, 127, 127, 0)
    assert len(cm) == 21 and cm[2] == 127 * 0x25 // 0x100 + 0x26
    assert cm[18] == (127 - 0x80) & 0xff
    gamma = m.gamma_table(16)
    assert len(gamma) == 17 and gamma[0] == 0x0a and gamma[16] == 0xf5
    with pytest.raises(ValueError):
        m.color_matrix(127, 127, 127, 10)


def test_bayer_conversion_shape():
    bgr = m.bayer_to_bgr(bytes(m.FRAME_BYTES))
    assert bgr.shape == (480, 640, 3) and bgr.dtype == np.uint8


def test_invalid_controls_rejected_before_usb_access():
    with pytest.raises(ValueError):
        m.SN9C20xCapture({"exposre": 300})
    with pytest.raises(ValueError):
        m.SN9C20xCapture({"gain_automatic": 1})


class FailedTransfer:
    """WinUSB/libusb leave USBD {Offset, Length, Status} in failed iso descriptors."""
    def getStatus(self):
        import usb1
        return usb1.TRANSFER_STALL

    def getBuffer(self):
        return bytes(range(16))

    def getISOSetupList(self):
        return [dict(length=0, actual_length=4, status=0),
                dict(length=8, actual_length=4, status=-1073610752),   # 0xC0020000
                dict(length=12, actual_length=8, status=0)]            # out of range


def test_failed_transfer_keeps_good_packets():
    reader = m.SN9C20xCapture.__new__(m.SN9C20xCapture)
    packets = list(reader._packets(FailedTransfer()))
    assert packets[0] == (0, bytes(range(4)))
    assert packets[1][0] == m.USBD_STATUS_ISO_NOT_ACCESSED_BY_HW and packets[2] == (-1, b"")


def test_frames_must_be_exact_size_and_late_polls_do_not_drop():
    import collections
    frames, stats = [], collections.Counter()
    a = m.FrameAssembler(frames.append, stats)
    a.packet(HEADER)
    for _ in range(m.FRAME_BYTES // 3072 + 1):        # one packet too many
        a.packet(b"\x02" * 3072)
    a.packet(HEADER)
    assert frames == [] and stats["dropped_long"] == 1

    reader = m.SN9C20xCapture.__new__(m.SN9C20xCapture)
    reader.running, reader.stats = True, collections.Counter()
    reader.assembler = m.FrameAssembler(frames.append, reader.stats)
    reader._submit = lambda transfer: None
    class LatePoll(FailedTransfer):
        def getISOSetupList(self):
            return [dict(length=0, actual_length=0, status=-1073610752)]
    reader.assembler.packet(HEADER)
    reader._transferred(LatePoll())
    assert reader.assembler.synced and reader.stats["packets_not_polled"] == 1


def test_camera_uses_sn9c20x_backend(monkeypatch):
    seen = {}
    class Fake:
        def __init__(self, controls):
            seen.update(controls)
    monkeypatch.setattr(m, "SN9C20xProcessCapture", Fake)
    cam = Camera("wrist", "usb:0c45:627b", backend="sn9c20x", fps=20, controls={"exposure": 300})
    assert isinstance(cam._open(), Fake) and seen == {"exposure": 300, "frame_rate": 20}
    with pytest.raises(ValueError):
        Camera("wrist", backend="sn9c20x", width=320, height=240)._open()


def test_frame_rate_uses_dummy_lines_without_shortening_exposure():
    assert m.dummy_lines(0, 300) == 0 and m.dummy_lines(30, 300) == 0
    lines = m.dummy_lines(20, 300)
    frame_s = 1 / m.NATIVE_FPS + lines * m.LINE_PERIOD_S
    assert abs(1 / frame_s - 20) < 0.1
    assert m.dummy_lines(20, 2000) == 2000 - 547      # long exposures still get their lines


def test_pacer_thins_30_to_20_evenly_and_restarts_after_stall():
    from sawyer_teleop.cameras import FramePacer
    pacer = FramePacer(20)
    stamps = [round(i * 1e9 / 30) for i in range(90)]
    kept = [t for t in stamps if pacer.keep(t)]
    assert len(kept) == 60
    gaps = {round((b - a) / 1e6) for a, b in zip(kept, kept[1:])}
    assert gaps <= {33, 67}
    late = stamps[-1] + 2_000_000_000
    assert pacer.keep(late) and not pacer.keep(late + 10_000_000)
    native = FramePacer(20)
    assert all(native.keep(i * 50_000_000) for i in range(20))


def test_exposure_metrics_flags_over_and_under_exposure():
    from sawyer_teleop.cameras import exposure_metrics
    assert exposure_metrics(np.full((480, 640, 3), 255, np.uint8))["state"] == "over"
    assert exposure_metrics(np.full((480, 640, 3), 5, np.uint8))["state"] == "under"
    ramp = np.tile(np.linspace(30, 220, 640, dtype=np.uint8), (480, 1))
    ok = exposure_metrics(np.dstack([ramp] * 3))
    assert ok["state"] == "ok" and ok["channel_clip_pct"] == 0


def test_process_capture_reports_child_errors_and_stats(monkeypatch):
    """The child process reports USB failures instead of hanging the caller."""
    class Boom(m.SN9C20xCapture):
        def __init__(self, *a, **k):
            raise RuntimeError("sn9c20x_not_found: test")
    monkeypatch.setattr(m, "SN9C20xCapture", Boom)
    # Exercise the child entry point in-process with real multiprocessing primitives.
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    q, stop = ctx.Queue(), ctx.Event()
    m._usb_process({}, {}, q, stop)
    assert q.get(timeout=5) == ("error", "sn9c20x_not_found: test")
    with pytest.raises(ValueError):
        m.SN9C20xProcessCapture({"gain_automatic": 1})


def test_color_correction_is_an_rgb_affine_with_saturation():
    cam = Camera("wrist", color_correction={"matrix": [[2, 0, 0], [0, 1, 0], [0, 0, .5]],
                                            "offset": [0, 10, 0], "source": "test"})
    rgb = np.array([[[100, 100, 100], [200, 250, 40]]], dtype=np.uint8)
    out = cam.correct(rgb)
    assert out.dtype == np.uint8
    np.testing.assert_array_equal(out, [[[200, 110, 50], [255, 255, 20]]])
    assert Camera("wrist").correct(rgb) is rgb   # no correction: untouched
    with pytest.raises(ValueError):
        Camera("wrist", color_correction={"matrix": [[1, 0], [0, 1]]})


def test_live_exposure_and_gain_write_sensor_registers_without_reopen():
    cap = m.SN9C20xCapture.__new__(m.SN9C20xCapture)
    cap.controls = m.validate_controls({"exposure": 300, "gain": 4, "frame_rate": 20})
    writes = []
    cap.i2c_w = writes.append
    cap.set_controls({"exposure": 150, "gain": 0})
    assert cap.controls["exposure"] == 150 and cap.controls["gain"] == 0
    regs = {row[2]: row[3] for row in writes}
    assert regs[0x00] == m.OV_GAIN[0] and regs[0x10] == 150 >> 2 and regs[0x04] == 150 & 3
    with pytest.raises(ValueError):
        cap.set_controls({"red_balance": 1})      # only exposure/gain are live
    with pytest.raises(ValueError):
        cap.set_controls({"gain": len(m.OV_GAIN)})


def test_camera_queues_controls_for_the_capture_thread():
    applied = []
    class Cap:
        def set_controls(self, changes):
            applied.append(changes)
    cam = Camera("wrist", backend="sn9c20x", controls={"exposure": 300, "gain": 4})
    spec = cam.set_controls({"exposure": 120})
    assert spec["exposure"]["value"] == 120 and "auto_exposure" not in spec
    cam._apply_pending(Cap()); cam._apply_pending(Cap())
    assert applied == [{"exposure": 120}]           # applied once, on the open device
    for bad in ({"exposure": 0}, {"gain": 1.5}, {"auto_exposure": True}, {}):
        with pytest.raises(ValueError):
            cam.set_controls(bad)


def test_opencv_camera_controls_and_auto_exposure():
    calls = []
    class Cap:
        def set(self, prop, value):
            calls.append((prop, value))
    cam = Camera("realsense", device=0, backend="dshow")
    assert cam.control_spec()["auto_exposure"] is True    # nothing configured: device auto
    cam.set_controls({"exposure": -6})
    cam._apply_pending(Cap())
    assert (m.cv2.CAP_PROP_AUTO_EXPOSURE, 0.25) in calls and (m.cv2.CAP_PROP_EXPOSURE, -6) in calls
    calls.clear()
    cam.set_controls({"auto_exposure": True})
    cam._apply_pending(Cap())
    assert calls == [(m.cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)]
    assert Camera("rs", backend="dshow", control_ranges={"gain": [0, 64]}).control_spec()["gain"]["max"] == 64
