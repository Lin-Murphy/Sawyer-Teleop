"""Quest input: owned ADB child process, portable reader threads, bounded mailbox."""
import collections
import json
import math
import queue
import shutil
import subprocess
import threading
import time

import numpy as np
from scipy.spatial.transform import Rotation

from .protocol import ProtocolError, integer, ns, pose
from .locking import ProcessLock
from .rates import EventRate
from .a_timing import INPUT_MAX_AGE_S, input_age_expired

PACKAGE = "org.sawyer.questinput"


def input_evidence(value):
    """JSON-safe input identity and original timing; never renew a cached sample."""
    return {key: value[key] for key in (
        "packet", "valid", "reason", "packet_fresh", "events", "epoch",
        "received_ns", "mapped_ns", "uncertainty_ns", "read_completed_ns",
        "ingress_seq", "clock_revision", "validation_offset_ns",
        "validation_uncertainty_ns") if key in value}


def matrix(value):
    value = pose(value)
    result = np.eye(4)
    result[:3, :3] = Rotation.from_quat(value["q"]).as_matrix()
    result[:3, 3] = value["p"]
    return result


def heading_mapping(forward):
    forward = np.asarray(forward, dtype=float).copy()
    forward[1] = 0
    length = np.linalg.norm(forward)
    if length < .3:
        raise ValueError("Look level along the robot base +X direction, then calibrate")
    forward /= length
    up = np.array([0., 1., 0.])
    return np.stack([forward, np.cross(up, forward), up])


class QuestValidator:
    def __init__(self, offset_ns, uncertainty_ns, timeout=INPUT_MAX_AGE_S):
        self.offset, self.uncertainty = offset_ns, uncertainty_ns
        self.timeout_ns = int(timeout*1e9)
        self.session = None
        self.seq = self.sample_ns = -1
        self.buttons = 0

    def parse(self, packet, now=None):
        now = ns() if now is None else now
        result = {"valid": False, "reason": "invalid_packet", "events": [],
                  "received_ns": now, "packet": packet, "packet_fresh": False}
        try:
            if packet.get("protocol") != 3:
                raise ProtocolError("Install the phase 1 protocol v3 APK")
            session = packet["session"]
            if not isinstance(session, str) or not session or len(session) > 128:
                raise ProtocolError("invalid_session")
            seq = integer(packet["seq"], "seq", 1)
            stamp = integer(packet["sample_ns"], "sample_ns", 1)
            age = now-stamp-self.offset
            result.update(mapped_ns=stamp+self.offset, uncertainty_ns=self.uncertainty)
            if input_age_expired(age, self.uncertainty, self.timeout_ns):
                raise ProtocolError("quest_stale")
            if session != self.session:
                self.session, self.seq, self.sample_ns, self.buttons = session, -1, -1, 0
            if seq <= self.seq or stamp <= self.sample_ns:
                raise ProtocolError("quest_replayed")
            self.seq, self.sample_ns = seq, stamp
            # Count fresh protocol packets even if the controller is untracked.
            result.update(packet_fresh=True, mapped_ns=stamp+self.offset,
                          uncertainty_ns=self.uncertainty)
            hand = packet["hands"]["r"]
            # Fresh button input remains usable even when pose tracking is lost.
            if hand.get("input_ok") is True:
                buttons = integer(hand["buttons"], "buttons")
                rising = buttons & ~self.buttons
                self.buttons = buttons
                result["events"] = (["save"] if rising & 1 else
                                    ["discard"] if rising & 2 else [])
                if rising & 4:
                    result["events"].append("reset_orientation")
            if not packet["focused"] or not packet["mounted"] or packet["space"] != 1:
                raise ProtocolError("quest_not_focused")
            if packet["head_status"] & 15 != 15:
                raise ProtocolError("head_tracking_lost")
            if not hand.get("input_ok") or not hand.get("tracking_ok") or hand["status"] & 15 != 15:
                raise ProtocolError("controller_tracking_lost")
            transform, head = matrix(hand["pose"]), matrix(packet["head"])
            grip = hand["grip"]
            if type(grip) not in (int, float) or not math.isfinite(grip) or not 0 <= grip <= 1:
                raise ProtocolError("grip_invalid")
            result.update(valid=True, reason="ready", grip=grip, transform=transform,
                          head_forward=-head[:3, 2],
                          epoch=(session, packet["recenter"], hand["recenter"], hand["device"]),
                          mapped_ns=stamp+self.offset, uncertainty_ns=self.uncertainty)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            result["reason"] = str(exc)
        return result


class UptimeShell:
    """One long-lived `adb shell` for clock sync samples.

    Starting adb.exe per sample cost 84-102 ms on Windows, which became most of
    the measured RTT and so of the input-age uncertainty (rtt/2 + 10 ms). A
    request/response on an open shell measured ~8 ms. Each sample is still timed
    around one uptime read, so the bound keeps its meaning.
    """

    def __init__(self, base, timeout=2.):
        self.base, self.timeout = base, timeout
        self.process = self.lines = None
        self.seq = 0

    def _start(self):
        self.process = subprocess.Popen(self.base+["shell"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.lines = lines = queue.Queue()
        stdout = self.process.stdout
        def pump():
            for line in iter(stdout.readline, b""):
                lines.put(line)
            lines.put(None)
        threading.Thread(target=pump, daemon=True).start()

    def read(self):
        if self.process is None or self.process.poll() is not None:
            self.close()
            self._start()
        while not self.lines.empty():
            self.lines.get_nowait()  # Never pair a reply with a later request.
        self.seq += 1
        tag = "sawyer-sync-%d" % self.seq
        try:
            self.process.stdin.write(("echo %s $(cat /proc/uptime)\n" % tag).encode())
            self.process.stdin.flush()
            deadline = time.monotonic()+self.timeout
            while True:
                line = self.lines.get(timeout=max(0., deadline-time.monotonic()))
                if line is None:
                    raise RuntimeError("adb_sync_shell_closed")
                text = line.decode("utf-8", "replace").strip()
                if text.startswith(tag+" "):
                    return text[len(tag)+1:]
        except (OSError, queue.Empty, RuntimeError):
            self.close()
            raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                pass
        self.process = None


class QuestInput:
    def __init__(self, serial=None, adb="adb", timeout=INPUT_MAX_AGE_S):
        self.adb = shutil.which(adb)
        if not self.adb:
            raise RuntimeError("ADB not found. Install Android Platform Tools and add it to PATH.")
        self.serial = serial
        self.timeout = timeout
        self.lock = threading.Lock()
        self.latest = None
        self.events = collections.deque(maxlen=64)
        self.error = None
        self.process = None
        self.stop = threading.Event()
        self.validator = None
        self.sync_at = 0
        self.threads = []
        self.process_lock = None
        self.input_rate = EventRate()
        self.ingress_seq = self.clock_revision = 0
        self.recording_sink = None
        self.sync_diagnostics = {}
        self.sync_error = None
        self.uptime_shell = None

    def _clock_evidence(self):
        return {"revision": self.clock_revision, "sync_at_ns": self.sync_at,
                "offset_ns": self.validator.offset if self.validator else None,
                "uncertainty_ns": self.validator.uncertainty if self.validator else None}

    def set_recording_sink(self, sink):
        """Attach Episode.event only during explicit recording; it only enqueues.

        The reader lock orders attach/detach with ingress, so recording end can
        seal the existing episode queue after the final input has been enqueued.
        """
        with self.lock:
            if self.recording_sink is not None:
                self.recording_sink("quest_capture_ended", ingress_seq=self.ingress_seq)
            self.recording_sink = sink
            if sink is not None:
                sink("quest_capture_started", schema=1, ingress_seq=self.ingress_seq,
                     clock=self._clock_evidence(), cached_input=input_evidence(self.latest or {}))

    def command(self, *args, timeout=10):
        base = [self.adb] + (["-s", self.serial] if self.serial else [])
        result = subprocess.run(base+list(args), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[:500])
        return result.stdout

    def uptime(self):
        """Quest /proc/uptime via the persistent shell; one spawned adb call if it fails."""
        if self.uptime_shell is None:
            self.uptime_shell = UptimeShell([self.adb] + (["-s", self.serial] if self.serial else []))
        try:
            return self.uptime_shell.read()
        except (OSError, ValueError, RuntimeError, queue.Empty):
            return self.command("shell", "cat", "/proc/uptime")

    def sync(self):
        samples = []
        for _ in range(5):
            start = ns()
            value = float(self.uptime().split()[0])
            finish = ns()
            samples.append((finish-start, (start+finish)//2-int(value*1e9)))
        rtt, offset = min(samples)
        uncertainty = rtt//2+10_000_000  # Android uptime has centisecond precision.
        shell = self.uptime_shell
        with self.lock:
            self.sync_diagnostics = dict(at_ns=ns(), rtt_ms=[s[0]/1e6 for s in samples],
                method="persistent_shell" if shell is not None and shell.process is not None else "spawned_adb",
                best_rtt_ms=rtt/1e6, candidate_offset_ns=offset,
                candidate_uncertainty_ms=uncertainty/1e6,
                accepted=uncertainty <= self.timeout*1e9/2)
        if uncertainty > self.timeout*1e9/2:
            with self.lock:
                self.sync_error = "ADB latency too high to verify input age"
            raise RuntimeError("ADB latency too high to verify input age")
        with self.lock:
            self.sync_error = None
            if self.validator is None:
                self.validator = QuestValidator(offset, uncertainty, self.timeout)
            else:
                self.validator.offset, self.validator.uncertainty = offset, uncertainty
            self.sync_at = ns()
            self.clock_revision += 1
            if self.recording_sink is not None:
                self.recording_sink("quest_clock_sync", clock=self._clock_evidence())

    def start(self):
        if not self.serial:
            lines = self.command("devices").splitlines()[1:]
            devices = [line.split()[0] for line in lines if line.strip().endswith("\tdevice")]
            if len(devices) != 1:
                raise RuntimeError("Specify a unique authorized Quest serial in the configuration")
            self.serial = devices[0]
        self.process_lock = ProcessLock("quest:"+self.serial).acquire()
        self.sync()
        self.command("shell", "am", "start", "-n",
                     PACKAGE+"/com.rail.oculus.teleop.MainActivity")
        self.process = subprocess.Popen(
            [self.adb, "-s", self.serial, "logcat", "-v", "raw", "-T", "1",
             "SawyerQuestV3:I", "*:S"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for target in (self._read, self._stderr, self._sync):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()
        return self

    def _read(self):
        try:
            while not self.stop.is_set():
                line = self.process.stdout.readline(65537)
                read_completed_ns = ns()
                if not line:
                    raise ConnectionError("adb_disconnected")
                if len(line) > 65536:
                    raise ValueError("quest_packet_too_large")
                if not line.lstrip().startswith(b"{"):
                    continue
                try:
                    packet = json.loads(line)
                    with self.lock:
                        value = self.validator.parse(packet)
                        self.ingress_seq += 1
                        value.update(ingress_seq=self.ingress_seq, read_completed_ns=read_completed_ns,
                                     clock_revision=self.clock_revision,
                                     validation_offset_ns=self.validator.offset,
                                     validation_uncertainty_ns=self.validator.uncertainty)
                        if self.recording_sink is not None:
                            self.recording_sink("quest_ingress", input=input_evidence(value))
                        if value["packet_fresh"]:
                            self.input_rate.mark()
                        if len(self.events)+len(value["events"]) > 64:
                            raise RuntimeError("quest_event_queue_full")
                        self.events.extend(value["events"])
                        self.latest = value
                except (ValueError, KeyError, TypeError) as exc:
                    reason = str(exc)
                    if isinstance(exc, json.JSONDecodeError) and len(line.rstrip(b"\r\n")) == 1023:
                        reason = "Quest packet truncated at 1023 bytes. Update Quest app with 'Setup.cmd quest' (Windows) or scripts/install-quest.sh."
                    with self.lock:
                        self.latest = {"valid": False, "reason": reason, "received_ns": ns()}
                        self.ingress_seq += 1
                        self.latest.update(ingress_seq=self.ingress_seq,
                                           read_completed_ns=read_completed_ns)
                        if self.recording_sink is not None:
                            self.recording_sink("quest_ingress_error", input=input_evidence(self.latest),
                                                clock=self._clock_evidence(), line_hex=line.hex())
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                if self.recording_sink is not None:
                    self.recording_sink("quest_stream_error", error=self.error,
                                        ingress_seq=self.ingress_seq, clock=self._clock_evidence())

    def _stderr(self):
        # Drain the pipe; diagnostics remain bounded in memory, not global disk logs.
        while not self.stop.is_set():
            line = self.process.stderr.readline(4096)
            if not line:
                break
            self.error = line.decode("utf-8", "replace").strip()[:500]

    def _sync(self):
        while not self.stop.wait(10):
            try:
                self.sync()
            except Exception as exc:
                with self.lock:
                    self.sync_error = str(exc)

    def diagnostics(self):
        """Bounded live evidence, available even when recording is disabled.

        Source-to-read lag includes transport/clock effects; it alone cannot
        identify Android logcat backlog versus USB/host scheduling delay.
        """
        with self.lock:
            value = self.latest or {}
            now = ns()
            mapped = value.get("mapped_ns")
            read = value.get("read_completed_ns")
            return dict(clock=self._clock_evidence(), last_sync=dict(self.sync_diagnostics),
                sync_error=self.sync_error, stream_error=self.error,
                sync_age_ms=(now-self.sync_at)/1e6,
                ingress_seq=self.ingress_seq, packet_seq=value.get("packet", {}).get("seq"),
                source_to_read_ms=None if mapped is None or read is None else (read-mapped)/1e6,
                read_to_poll_ms=None if read is None else (now-read)/1e6)

    def poll(self):
        with self.lock:
            value = dict(self.latest or {"valid": False, "reason": "quest_waiting"})
            value["events"] = list(self.events)
            self.events.clear()
            now = ns()
            # A recently received packet can already contain an old sample.
            # Recheck source age on every reuse, not just when logcat delivers it.
            source_age = now-value.get("mapped_ns", now)
            uncertainty = value.get("uncertainty_ns", 0)
            if (now-value.get("received_ns", 0) > self.timeout*1e9
                    or input_age_expired(source_age, uncertainty, self.timeout*1e9)
                    or now-self.sync_at > 30_000_000_000
                    or self.process.poll() is not None):
                value.update(valid=False, reason=self.error or
                             (self.sync_error if now-self.sync_at > 30_000_000_000 else None) or "quest_stale")
            return value

    def rates(self):
        active = (not self.stop.is_set() and self.process is not None
                  and self.process.poll() is None)
        return self.input_rate.snapshot(active=active)

    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(2)
        for thread in self.threads:
            thread.join(timeout=1)
        if self.uptime_shell is not None:
            self.uptime_shell.close()
        if self.process_lock:
            self.process_lock.close()
