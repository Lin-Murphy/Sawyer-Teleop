"""User-space reader for the Sonix SN9C201 + OV7660 wrist camera (USB 0c45:627b).

Windows has no signed driver for this camera. With the inbox Microsoft WinUSB driver
bound to the device, this module performs the same bridge/sensor setup as the Linux
gspca ``sn9c20x`` driver and reads raw BGGR Bayer frames over isochronous USB.

Register tables, I2C protocol, colour-matrix/gamma formulas and the frame header are
ported from linux/drivers/media/usb/gspca/sn9c20x.c (GPL-2.0-or-later;
Copyright Brian Johnson, the microdia project and Jean-Francois Moine).
Only raw 640x480 mode with manual exposure/gain is implemented.
"""
import collections
import threading
import time

import cv2
import numpy as np

VID, PID = 0x0C45, 0x627B
WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT
FRAME_HEADER = bytes((0xFF, 0xFF, 0x00, 0xC4, 0xC4, 0x96))
ENDPOINT = 0x81
INTERFACE = 0
ALT_SETTING = 8          # highest bandwidth; Linux requests full bandwidth for this bridge
I2C_INTF, I2C_ADDR = 0x80, 0x21   # 100 kb/s bus, OV7660 address
USBD_STATUS_ISO_NOT_ACCESSED_BY_HW = 0xC0020000
_ABANDONED_TRANSFERS = []   # see SN9C20xCapture._abandon_stuck_transfers

BRIDGE_INIT = (
    (0x1000, 0x78), (0x1001, 0x40), (0x1002, 0x1c), (0x1020, 0x80), (0x1061, 0x01),
    (0x1067, 0x40), (0x1068, 0x30), (0x1069, 0x20), (0x106a, 0x10), (0x106b, 0x08),
    (0x1188, 0x87), (0x11a1, 0x00), (0x11a2, 0x00), (0x11a3, 0x6a), (0x11a4, 0x50),
    (0x11ab, 0x00), (0x11ac, 0x00), (0x11ad, 0x50), (0x11ae, 0x3c), (0x118a, 0x04),
    (0x0395, 0x04), (0x11b8, 0x3a), (0x118b, 0x0e), (0x10f7, 0x05), (0x10f8, 0x14),
    (0x10fa, 0xff), (0x10f9, 0x00), (0x11ba, 0x0a), (0x11a5, 0x2d), (0x11a6, 0x2d),
    (0x11a7, 0x3a), (0x11a8, 0x05), (0x11a9, 0x04), (0x11aa, 0x3f), (0x11af, 0x28),
    (0x11b0, 0xd8), (0x11b1, 0x14), (0x11b2, 0xec), (0x11b3, 0x32), (0x11b4, 0xdd),
    (0x11b5, 0x32), (0x11b6, 0xdd), (0x10e0, 0x2c), (0x11bc, 0x40), (0x11bd, 0x01),
    (0x11be, 0xf0), (0x11bf, 0x00), (0x118c, 0x1f), (0x118d, 0x1f), (0x118e, 0x1f),
    (0x118f, 0x1f), (0x1180, 0x01), (0x1181, 0x00), (0x1182, 0x01), (0x1183, 0x00),
    (0x1184, 0x50), (0x1185, 0x80), (0x1007, 0x00),
)
OV7660_INIT = (
    (0x0e, 0x80), (0x0d, 0x08), (0x0f, 0xc3), (0x04, 0xc3), (0x10, 0x40), (0x11, 0x40),
    (0x12, 0x05), (0x13, 0xba), (0x14, 0x2a), (0x17, 0x10), (0x18, 0x61), (0x37, 0x0f),
    (0x38, 0x02), (0x39, 0x43), (0x3a, 0x00), (0x69, 0x90), (0x2d, 0x00), (0x2e, 0x00),
    (0x01, 0x78), (0x02, 0x50),
)
# Gain = (bit[3:0] / 16 + 1) * (bit[4] + 1) * (bit[5] + 1) * (bit[6] + 1); V4L2 gain is an index.
OV_GAIN = (0x00, 0x04, 0x08, 0x0c, 0x10, 0x12, 0x14, 0x16, 0x18, 0x1a, 0x1c, 0x1e, 0x30, 0x31,
           0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x3b, 0x3c, 0x3d, 0x3e, 0x3f, 0x70)
# hsv_{red,green,blue}_{x,y}[180], i.e. hue = 0 (the only hue supported here).
HSV_AT_HUE0 = (-41, -82, 124, 100, -112, 11)

# Same names/units/defaults as the Linux V4L2 controls used by sandberg_camera/camera_profile.json.
DEFAULT_CONTROLS = dict(gain_automatic=0, exposure=0x33, gain=0, red_balance=0x28,
                        blue_balance=0x28, brightness=127, contrast=127, saturation=127,
                        hue=0, gamma=0x10, horizontal_flip=0, vertical_flip=0,
                        frame_rate=0)   # not a V4L2 control: 0 = native ~30 Hz

# OV7660 timing at 640x480 on this bridge: ~30 Hz with no dummy lines. Lowering the
# frame rate inserts vertical dummy lines (ADVFL/ADVFH, the registers Linux uses for
# exposures > 547), which lengthens the frame but leaves the exposure time unchanged.
NATIVE_FPS = 30.0
# Measured on the wrist camera (2026-11-07): 274 dummy lines -> 19.50 Hz from 30.0 Hz.
LINE_PERIOD_S = (1 / 19.50 - 1 / NATIVE_FPS) / 274


# Live-adjustable ranges exposed to the Web sliders. Exposure is in OV7660 line units;
# above 547 the sensor needs extra dummy lines, so the ceiling keeps the 20 Hz frame
# (547 + 274 dummy lines at 20 Hz). Gain is an index into OV_GAIN (1x .. 16x).
LIVE_RANGES = {"exposure": (1, 800), "gain": (0, len(OV_GAIN) - 1)}


def validate_controls(controls):
    merged = dict(DEFAULT_CONTROLS, **(controls or {}))
    unknown = set(merged) - set(DEFAULT_CONTROLS)
    if unknown:
        raise ValueError("Unknown SN9C20x controls: " + ", ".join(sorted(unknown)))
    if merged["gain_automatic"]:
        raise ValueError("Automatic gain is not implemented; set gain_automatic=0")
    if type(merged["gain"]) is not int or not 0 <= merged["gain"] < len(OV_GAIN):
        raise ValueError("SN9C20x gain must be an integer index 0..%d" % (len(OV_GAIN) - 1))
    if type(merged["exposure"]) is not int or not 0 <= merged["exposure"] <= 0x1780:
        raise ValueError("SN9C20x exposure must be an integer 0..6016")
    return merged


def bayer_to_bgr(raw):
    """640x480 BGGR (V4L2 SBGGR8 / BA81) bytes to an OpenCV BGR image.

    Edge-aware demosaicing avoids the colour fringes ("zipper" artefacts) of plain
    bilinear interpolation on edges such as gripper fingers and object outlines.
    """
    return cv2.cvtColor(np.frombuffer(raw, np.uint8, FRAME_BYTES).reshape(HEIGHT, WIDTH),
                        cv2.COLOR_BayerBGGR2BGR_EA)


def dummy_lines(frame_rate, exposure):
    """Vertical dummy lines for the requested rate; never fewer than the exposure needs."""
    needed_for_exposure = max(0, exposure - 547)
    if not frame_rate or frame_rate >= NATIVE_FPS:
        return needed_for_exposure
    extra = round((1 / frame_rate - 1 / NATIVE_FPS) / LINE_PERIOD_S)
    return max(needed_for_exposure, min(extra, 0xFFFF))


class FrameAssembler:
    """Linux sd_pkt_scan: a 64-byte header packet ends a frame; the rest starts the next."""
    def __init__(self, emit, stats=None):
        self.emit, self.parts, self.size, self.synced = emit, [], 0, False
        self.stats = stats if stats is not None else collections.Counter()

    def corrupt(self):
        """Data was lost: drop the partial frame and wait for the next header."""
        if self.synced:
            self.stats["dropped_lost_data"] += 1
        self.parts, self.size, self.synced = [], 0, False

    def packet(self, data):
        if len(data) >= 64 and data[:6] == FRAME_HEADER:
            if self.synced:
                # A raw frame is exactly width*height bytes; anything else lost data
                # (e.g. the bridge FIFO overflowed, flagged in header byte 6 bit 3).
                if self.size == FRAME_BYTES:
                    self.emit(b"".join(self.parts))
                else:
                    self.stats["dropped_short" if self.size < FRAME_BYTES else "dropped_long"] += 1
            if data[6] & 0x08:
                self.stats["bridge_fifo_full"] += 1
            self.parts, self.size, self.synced = [], 0, True
            data = data[64:]
        if data and self.synced:
            self.parts.append(bytes(data))
            self.size += len(data)
            if self.size > 2 * FRAME_BYTES:      # lost header: resynchronise
                self.parts, self.size, self.synced = [], 0, False


def color_matrix(brightness, contrast, saturation, hue):
    if hue != 0:
        raise ValueError("Only hue=0 is supported for the SN9C20x wrist camera")
    m = [0] * 21
    m[2] = contrast * 0x25 // 0x100 + 0x26
    m[0] = 0x13 + (m[2] - 0x26) * 0x13 // 0x25
    m[4] = 0x07 + (m[2] - 0x26) * 0x07 // 0x25
    m[18] = (brightness - 0x80) & 0xff
    for i, h in enumerate(HSV_AT_HUE0):
        coord = (h * saturation) >> 8
        m[6 + 2*i], m[7 + 2*i] = coord & 0xff, (coord >> 8) & 0x0f
    return bytes(m)


def gamma_table(value):
    g = value * 0xb8 // 0x100
    ends = ((0x13, 0xcb), (0x25, 0xee), (0x37, 0xfa), (0x45, 0xfc), (0x55, 0xfb), (0x65, 0xfc),
            (0x74, 0xfd), (0x83, 0xfe), (0x92, 0xfc), (0xa1, 0xfc), (0xb0, 0xfc), (0xbf, 0xfb),
            (0xce, 0xfb), (0xdf, 0xfd), (0xea, 0xf9))
    return bytes([0x0a] + [(a + g * (b - a) // 0xb8) & 0xff for a, b in ends] + [0xf5])


class SN9C20xCapture:
    """cv2.VideoCapture-like reader: isOpened(), read() -> (ok, bgr), release()."""
    # 32 x 64 microframes keeps ~256 ms of reads queued, so a Python/GIL pause in the
    # process (Web, JPEG, operator threads) does not leave the bus unpolled.
    def __init__(self, controls=None, transfers=32, packets=64, timeout=3.0, alt_setting=ALT_SETTING):
        self.controls = validate_controls(controls)
        self.transfers_n, self.packets, self.timeout = transfers, packets, timeout
        self.alt_setting = alt_setting
        self.transfer_length = 0
        self.stats = collections.Counter()
        self.ctx = self.handle = None
        self.transfers = []
        self.cond = threading.Condition()
        self.latest, self.seq, self.read_seq = None, 0, 0
        self.running = False
        self.error = None
        self.thread = None
        self.assembler = FrameAssembler(self._frame, self.stats)
        self._open()

    # --- USB register / I2C access (vendor requests on interface 0) ---
    def reg_w(self, reg, data):
        data = bytes(data)
        if self.handle.controlWrite(0x41, 0x08, reg, 0, data, 500) != len(data):
            raise RuntimeError("sn9c20x_reg_write_failed:%04x" % reg)

    def reg_r(self, reg, length=1):
        return bytes(self.handle.controlRead(0xc1, 0x00, reg, 0, length, 500))

    def i2c_w(self, row):
        self.reg_w(0x10c0, row)
        for _ in range(5):
            status = self.reg_r(0x10c0)[0]
            if status & 0x04:
                if status & 0x08:
                    raise RuntimeError("sn9c20x_i2c_error:%02x" % row[2])
                return
            time.sleep(.01)
        # Linux logs and continues here ("fixme: may occur").

    def i2c_w1(self, reg, value):
        self.i2c_w((I2C_INTF | (2 << 4), I2C_ADDR, reg, value, 0, 0, 0, 0x10))

    # --- setup ---
    def _open(self):
        import usb1
        self.ctx = usb1.USBContext().open()
        try:
            self.handle = self.ctx.openByVendorIDAndProductID(VID, PID)
            if self.handle is None:
                raise RuntimeError("sn9c20x_not_found: wrist camera 0c45:627b not visible to WinUSB "
                                   "(check the USB cable and that 'WinUsb Device' is its driver)")
            self.handle.claimInterface(INTERFACE)
            self.handle.setInterfaceAltSetting(INTERFACE, 0)
            self._init_bridge_and_sensor()
            self._start()
        except BaseException:
            self.release()
            raise

    def _init_bridge_and_sensor(self):
        for reg, value in BRIDGE_INIT:                       # sd_init
            self.reg_w(reg, (value,))
        self.reg_w(0x1006, (0x20,))                          # LED on (no LED_REVERSE flag)
        self.reg_w(0x10c0, (0x80, I2C_ADDR, 0, 0, 0, 0, 0, 0, 0x03))
        self.i2c_w1(0x12, 0x80)                              # OV7660 soft reset
        time.sleep(.2)
        for reg, value in OV7660_INIT:
            self.i2c_w1(reg, value)

    def _apply_controls(self):
        c = self.controls
        self.reg_w(0x10e1, color_matrix(c["brightness"], c["contrast"], c["saturation"], c["hue"]))
        self.reg_w(0x1190, gamma_table(c["gamma"]))
        self.reg_w(0x118c, (c["red_balance"],))
        self.reg_w(0x118f, (c["blue_balance"],))
        self._apply_exposure_gain()
        flip = 0x01 | (0x20 if c["horizontal_flip"] else 0) | (0x10 if c["vertical_flip"] else 0)
        self.reg_w(0x1182, (2 if c["vertical_flip"] else 3,))
        self.i2c_w1(0x1e, flip)

    def _apply_exposure_gain(self):
        c = self.controls
        self.i2c_w((I2C_INTF | (2 << 4), I2C_ADDR, 0x00, OV_GAIN[c["gain"]], 0, 0, 0, 0x10))
        expo = c["exposure"]
        expo2 = min(expo, 547)
        self.i2c_w((I2C_INTF | (2 << 4), I2C_ADDR, 0x10, expo2 >> 2, 0, 0, 0, 0x10))   # AECH
        self.i2c_w((I2C_INTF | (2 << 4), I2C_ADDR, 0x04, expo2 & 3, 0, 0, 0, 0x10))    # COM1
        rest = dummy_lines(c["frame_rate"], expo)                                  # ADVFL/H
        self.i2c_w((I2C_INTF | (3 << 4), I2C_ADDR, 0x2d, rest & 0xff, rest >> 8 & 0xff, 0, 0, 0x1e))

    def set_controls(self, changes):
        """Change exposure/gain while streaming (sensor I2C writes, no USB reopen)."""
        if set(changes) - set(LIVE_RANGES):
            raise ValueError("Only exposure and gain can change while streaming")
        self.controls = validate_controls(dict(self.controls, **changes))
        self._apply_exposure_gain()

    def _start(self):
        self.handle.setInterfaceAltSetting(INTERFACE, self.alt_setting)
        hstart = vstart = 3
        self.reg_w(0x10fb, (0, WIDTH >> 2, 0, HEIGHT >> 1, ((WIDTH >> 10) & 1) | ((HEIGHT >> 8) & 6)))
        self.reg_w(0x1180, (hstart, 0, vstart, 0, 640 >> 4, 480 >> 3))
        self.reg_w(0x1189, (0x80,))      # 640x480 scale
        self.reg_w(0x10e0, (0x2d,))      # raw Bayer output
        self._apply_controls()
        self.reg_w(0x1007, (0x20,))
        self.reg_w(0x1061, (0x03,))      # start streaming
        raw = self._max_packet()
        packet = (raw & 0x7ff) * (1 + ((raw >> 11) & 3))
        if packet == 0:
            raise RuntimeError("sn9c20x_no_iso_bandwidth: alt setting %d" % self.alt_setting)
        self.transfer_length = packet * self.packets
        self.running = True
        for _ in range(self.transfers_n):
            transfer = self.handle.getTransfer(iso_packets=self.packets)
            self._submit(transfer)
            self.transfers.append(transfer)
        self.thread = threading.Thread(target=self._events, name="sn9c20x-usb", daemon=True)
        self.thread.start()

    def _max_packet(self):
        setting = next(s for s in self.handle.getDevice()[0][INTERFACE]
                       if s.getAlternateSetting() == self.alt_setting)
        return next(e for e in setting if e.getAddress() == ENDPOINT).getMaxPacketSize()

    # --- streaming ---
    def _events(self):
        try:
            while self.running:
                self.ctx.handleEventsTimeout(.1)
        except Exception as exc:
            self.error = str(exc)
            with self.cond:
                self.cond.notify_all()

    def _submit(self, transfer):
        # Re-arm every time: after a failed completion libusb leaves descriptors in WinUSB layout.
        transfer.setIsochronous(ENDPOINT, self.transfer_length, callback=self._transferred)
        transfer.submit()

    def _packets(self, transfer):
        """Yield (usbd_status, data) per packet, also for transfers WinUSB reports as failed.

        When any packet errors, WinUSB fails the whole read (ERROR_GEN_FAILURE) and
        libusb 1.0.29 skips its descriptor conversion, so the descriptors still hold
        USBD_ISO_PACKET_DESCRIPTOR {Offset, Length, Status}; the good packets are valid.
        """
        import usb1
        if transfer.getStatus() == usb1.TRANSFER_COMPLETED:
            for status, data in transfer.iterISO():
                yield (0 if status == usb1.TRANSFER_COMPLETED else -1), data
            return
        buffer = transfer.getBuffer()
        for desc in transfer.getISOSetupList():
            offset, length, status = desc["length"], desc["actual_length"], desc["status"]
            if 0 <= offset and 0 <= length and offset + length <= len(buffer):
                yield status & 0xFFFFFFFF, buffer[offset:offset + length]
            else:
                yield -1, b""

    def _transferred(self, transfer):
        import usb1
        status = transfer.getStatus()
        self.stats["transfer_%d" % status] += 1
        if status == usb1.TRANSFER_CANCELLED or not self.running:
            return
        for packet_status, data in self._packets(transfer):
            if packet_status == 0:
                if data:
                    self.assembler.packet(data)
            elif packet_status == USBD_STATUS_ISO_NOT_ACCESSED_BY_HW and not data:
                # The host polled late (e.g. Python busy); an isochronous IN device keeps
                # the data in its FIFO and sends it at the next poll, so nothing is lost.
                # A real loss shows up as a wrong frame size at the next header.
                self.stats["packets_not_polled"] += 1
            else:
                self.stats["packet_errors"] += 1
                self.stats["last_packet_status"] = packet_status if packet_status >= 0 else 0
                self.assembler.corrupt()
        try:
            self._submit(transfer)
        except Exception as exc:          # e.g. device unplugged
            self.error = "sn9c20x_resubmit_failed:%s" % exc
            with self.cond:
                self.cond.notify_all()

    def _frame(self, raw):
        self.stats["frames"] += 1
        with self.cond:
            self.latest, self.seq = raw, self.seq + 1
            self.cond.notify_all()

    def _abandon_stuck_transfers(self):
        """Forget transfers WinUSB never completes after the stream stops (seen: a few
        of 32), otherwise python-libusb1's close() waits for them forever. Their buffers
        are kept referenced, never freed, while the driver may still own them; the device
        handle close below cancels the I/O in the driver."""
        import ctypes
        import usb1
        submitted = usb1.USBTransfer._USBTransfer__submitted_dict
        for transfer in self.transfers:
            if transfer.isSubmitted():
                submitted.pop(ctypes.addressof(transfer._USBTransfer__transfer.contents), None)
                _ABANDONED_TRANSFERS.append(transfer)
                self.stats["transfers_abandoned"] += 1

    def isOpened(self):
        return self.handle is not None and self.running

    def read_raw(self, timeout=None):
        """Next unread raw BGGR frame as bytes: (ok, raw)."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self.cond:
            while self.seq == self.read_seq and self.error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, None
                self.cond.wait(remaining)
            if self.seq == self.read_seq:
                return False, None
            raw, self.read_seq = self.latest, self.seq
        return True, raw

    def read(self):
        ok, raw = self.read_raw()
        return (True, bayer_to_bgr(raw)) if ok else (False, None)

    def release(self, timeout=3.0):
        """Stop streaming and close. Never blocks longer than ``timeout``.

        If the camera vanished mid-transfer, WinUSB may never complete the cancelled
        transfers and libusb's close would wait forever; the close then stays in a
        daemon thread and its handles are abandoned (a reconnect opens new ones).
        """
        self.running = False
        with self.cond:
            self.cond.notify_all()
        closer = threading.Thread(target=self._close, name="sn9c20x-close", daemon=True)
        closer.start()
        closer.join(timeout)
        if closer.is_alive():
            self.stats["close_abandoned"] += 1

    def _close(self):
        # Do not cancel: libusb 1.0.29's WinUSB isoch fallback silently resubmits a
        # cancelled transfer, which then never completes. With running=False the
        # callback stops resubmitting, so queued transfers drain on their own.
        if self.thread:
            self.thread.join(timeout=2)
        if self.ctx is not None:
            end = time.monotonic() + 1
            while time.monotonic() < end and any(t.isSubmitted() for t in self.transfers):
                try:
                    self.ctx.handleEventsTimeout(.05)
                except Exception:
                    break
            self._abandon_stuck_transfers()
        if self.handle is not None:
            try:
                self.reg_w(0x1007, (0x00,))
                self.reg_w(0x1061, (0x01,))
            except Exception:
                pass
        handle, ctx = self.handle, self.ctx
        self.handle = self.ctx = None
        if handle is not None:
            for transfer in self.transfers:     # drop python-libusb1's in-flight bookkeeping
                try:
                    handle._USBDeviceHandle__inflight_remove(transfer)
                except (AttributeError, KeyError):
                    pass
        if handle is not None:
            try:
                handle.setInterfaceAltSetting(INTERFACE, 0)
                handle.releaseInterface(INTERFACE)
            except Exception:
                pass
            handle.close()
        self.transfers = []
        if ctx is not None:
            ctx.close()


# --- isolated USB process -------------------------------------------------------
# Receiving isochronous USB in Python is latency-sensitive: in a busy process (Web,
# JPEG, Quest, operator threads) the receive thread waits for the GIL, the camera
# bridge FIFO overflows and frames are lost. The child process below owns the USB
# device and only assembles frames; it has its own interpreter and GIL.

def _usb_process(controls, options, queue, stop, commands=None):
    import multiprocessing
    import queue as queue_module
    parent = multiprocessing.parent_process()
    cap = None
    try:
        cap = SN9C20xCapture(controls, **options)
        queue.put(("ready", None))
        dropped, last_stats = 0, 0.0
        while not stop.is_set() and (parent is None or parent.is_alive()):
            while commands is not None:
                try:
                    changes = commands.get_nowait()
                except queue_module.Empty:
                    break
                cap.set_controls(changes)       # I2C failure raises: reported, parent reconnects
            ok, raw = cap.read_raw(timeout=0.5)
            if ok:
                try:
                    queue.put_nowait(("frame", (time.monotonic_ns(), raw)))
                except queue_module.Full:
                    dropped += 1            # consumer behind; newest frames win next time
            elif cap.error:
                raise RuntimeError(cap.error)
            if time.monotonic() - last_stats >= .5:
                last_stats = time.monotonic()
                stats = dict(cap.stats, ipc_dropped=dropped)
                try:
                    queue.put_nowait(("stats", stats))
                except queue_module.Full:
                    pass
    except BaseException as exc:          # report, then exit; the parent reconnects
        try:
            queue.put(("error", str(exc)), timeout=1)
        except Exception:
            pass
    finally:
        if cap is not None:
            cap.release()


class SN9C20xProcessCapture:
    """Same interface as SN9C20xCapture, with USB handled in a child process."""
    def __init__(self, controls=None, timeout=3.0, start_timeout=20.0, **options):
        import multiprocessing
        self.controls = validate_controls(controls)    # fail fast in the caller
        ctx = multiprocessing.get_context("spawn")
        self.timeout = timeout
        self.stats = collections.Counter()
        self.error = None
        self.queue = ctx.Queue(maxsize=3)
        self.stop = ctx.Event()
        self.commands = ctx.Queue(maxsize=16)
        self.process = ctx.Process(target=_usb_process, name="sn9c20x-usb", daemon=True,
                                   args=(dict(controls or {}), options, self.queue, self.stop, self.commands))
        self.process.start()
        kind, value = self._get(start_timeout)
        if kind != "ready":
            self.release()
            raise RuntimeError(value if kind == "error" else "sn9c20x_process_start_timeout")

    def _get(self, timeout):
        import queue as queue_module
        try:
            return self.queue.get(timeout=timeout)
        except queue_module.Empty:
            if not self.process.is_alive():
                return "error", self.error or "sn9c20x_process_exited:%s" % self.process.exitcode
            return "timeout", None

    def isOpened(self):
        return self.error is None and self.process.is_alive()

    def set_controls(self, changes):
        """Validate here, apply in the USB child on its next loop (<= 0.5 s)."""
        if set(changes) - set(LIVE_RANGES):
            raise ValueError("Only exposure and gain can change while streaming")
        self.controls = validate_controls(dict(self.controls, **changes))
        self.commands.put_nowait(dict(changes))

    def read(self):
        deadline = time.monotonic() + self.timeout
        while self.error is None:
            kind, value = self._get(max(0.0, deadline - time.monotonic()))
            if kind == "frame":
                return True, bayer_to_bgr(value[1])
            if kind == "stats":
                for key, count in value.items():   # keys only grow; safe for readers
                    self.stats[key] = count
            elif kind == "error":
                self.error = value
            elif time.monotonic() >= deadline:
                return False, None
        return False, None

    def release(self, timeout=5.0):
        self.stop.set()
        self.process.join(timeout)
        if self.process.is_alive():
            self.process.terminate()             # OS closes the device handles
            self.process.join(2)
            self.stats["process_terminated"] += 1
        for q in (self.queue, self.commands):
            q.cancel_join_thread()
            q.close()
