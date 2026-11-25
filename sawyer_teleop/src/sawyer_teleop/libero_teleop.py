"""Quest operator link and browser station for a headless LIBERO simulation."""
import argparse
import getpass
import hmac
import http.client
import io
import json
import math
import queue
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from .a_timing import INPUT_MAX_AGE_S
from .gripper import trigger_command
from .quest import QuestInput, QuestValidator, heading_mapping

MAX_AGE_NS = int(INPUT_MAX_AGE_S * 1e9)
GRIP_RELEASE = .2
GRIP_ENGAGE = .7


class PoseMapping:
    def __init__(self, position_scale, action_cap):
        self.position_scale = position_scale
        self.action_cap = action_cap
        self.heading = None
        self.epoch = None
        self.gripper = -1.
        self.disengage()

    def disengage(self):
        self.anchor = None
        self.released = False
        self.trigger_matched = False

    def calibrate(self, value):
        if value is None or value["grip"] >= GRIP_RELEASE:
            raise ValueError("Release the right grip with Quest tracking active before calibrating.")
        self.heading = heading_mapping(value["head_forward"])
        self.epoch = value["epoch"]
        self.disengage()

    def action(self, value, interrupted, frame, output_max):
        if interrupted or value is None:
            self.disengage()
        action = np.zeros(7)
        action[-1] = self.gripper
        if value is None:
            return action
        if self.epoch != value["epoch"]:
            self.heading, self.epoch = None, None
            self.disengage()
        if self.heading is None:
            return action
        if value["grip"] < GRIP_RELEASE:
            self.disengage()
            self.released = True
            return action
        hand = value["transform"]
        if self.anchor is None:
            if not self.released or value["grip"] <= GRIP_ENGAGE:
                return action
            self.anchor = (hand.copy(), frame["eef_pos"].copy(), frame["eef_rotation"].copy())
            self.trigger_matched = self.gripper == -1.

        hand_anchor, ee_position, ee_rotation = self.anchor
        translation = self.position_scale * (self.heading @ (hand[:3, 3] - hand_anchor[:3, 3]))
        rotation = self.heading @ hand[:3, :3] @ hand_anchor[:3, :3].T @ self.heading.T
        desired = rotation @ ee_rotation
        rotation_error = Rotation.from_matrix(desired @ frame["eef_rotation"].T).as_rotvec()
        error = np.r_[ee_position + translation - frame["eef_pos"], rotation_error]
        action[:6] = np.clip(.5 * error / output_max, -self.action_cap, self.action_cap)
        # Match a closed gripper before changing it after a clutch operation.
        # AtomicPlace enters control already holding the object.
        if self.trigger_matched or value["gripper"] == self.gripper:
            self.gripper = float(value["gripper"])
            self.trigger_matched = True
        action[-1] = self.gripper
        return action


class Station:
    """HTTP threads exchange bounded input, requests, and snapshots with one sim owner."""
    def __init__(self):
        self.lock = threading.Lock()
        self.validator = QuestValidator(0, 0)
        self.input = None
        self.interrupted = True
        self.commands = queue.Queue(maxsize=1)
        self.status = {"task": "Loading LIBERO"}
        self.images = {}
        self.input_reason = "Waiting for Quest"

    def receive(self, body, now):
        packet = body.get("packet")
        with self.lock:
            if packet is None:
                self.input = None
                self.interrupted = True
                self.input_reason = str(body.get("reason", "Quest input unavailable"))[:150]
                return
            if not isinstance(packet, dict):
                raise ValueError("Expected Quest packet object")
            bound, age = body.get("bound_ns"), body.get("source_age_ns")
            if (type(bound) is not int or type(age) is not int or age < 0
                    or not 0 <= now - bound < MAX_AGE_NS - age):
                raise ValueError("Quest input exceeded its original time budget")
            stamp = packet.get("sample_ns")
            if type(stamp) is not int or stamp <= 0:
                raise ValueError("Invalid Quest source time")
            # The previous B response predates A's age measurement. Their sum
            # bounds total age without treating A and B monotonic clocks as equal.
            self.validator.offset = bound - age - stamp
            value = self.validator.parse(packet, now)
            if not value["valid"]:
                self.input = None
                self.interrupted = True
                self.input_reason = value["reason"]
                return
            value["gripper"] = trigger_command(packet["hands"]["r"]["trigger"])
            value["deadline"] = bound + MAX_AGE_NS - age
            if (self.input is None or now >= self.input["deadline"]
                    or self.input["epoch"] != value["epoch"] or value["grip"] < GRIP_RELEASE):
                self.interrupted = True
            self.input = value
            self.input_reason = "Quest tracking ready"

    def take_input(self, now):
        with self.lock:
            if self.input is None or now >= self.input["deadline"]:
                if self.input is not None:
                    self.input_reason = "Quest input expired"
                self.input = None
                self.interrupted = True
            value, interrupted = self.input, self.interrupted
            self.interrupted = False
            return value, interrupted, self.input_reason

    def publish(self, status, images=None):
        with self.lock:
            self.status = status
            if images is not None:
                self.images = images


class StationServer(ThreadingHTTPServer):
    request_queue_size = 12

    def __init__(self, port, station, key):
        self.station = station
        self.key = "Bearer " + key
        super().__init__(("127.0.0.1", port), StationHandler)

    def get_request(self):
        conn, address = super().get_request()
        conn.settimeout(15)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn, address


class StationHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _reply(self, code, payload, content_type="application/json"):
        if content_type == "application/json":
            payload = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if hmac.compare_digest(header.encode(), self.server.key.encode()):
            return True
        self.close_connection = True
        self._reply(401, {"error": "LIBERO station key required"})
        return False

    def do_GET(self):
        if self.path == "/":
            html = Path(__file__).parent / "web" / "libero.html"
            self._reply(200, html.read_bytes(), "text/html; charset=utf-8")
            return
        if not self._authorized():
            return
        station = self.server.station
        with station.lock:
            status = station.status
            image = station.images.get(self.path)
        if self.path == "/status":
            self._reply(200, dict(status, bound_ns=time.monotonic_ns()))
        elif image is not None:
            self._reply(200, image, "image/jpeg")
        else:
            self._reply(404, {"error": "View not available"})

    def do_POST(self):
        if not self._authorized():
            return
        station = self.server.station
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get_content_type() != "application/json" or not 0 < length <= 16_384:
                self.close_connection = True
                raise ValueError("Expected a bounded JSON request")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected JSON object")
            if self.path == "/quest":
                station.receive(body, time.monotonic_ns())
                self._reply(200, {"bound_ns": time.monotonic_ns()})
            elif self.path == "/command":
                command = body.get("command")
                if command not in ("calibrate", "reset", "start", "stop"):
                    raise ValueError("Unknown command")
                station.commands.put_nowait(command)
                self._reply(202, {"queued": command})
            else:
                self._reply(404, {"error": "Unknown endpoint"})
        except queue.Full:
            self._reply(409, {"error": "A command is already pending"})
        except (KeyError, TypeError, ValueError) as exc:
            self._reply(400, {"error": str(exc)})


def station_loop(args, station, stop):
    from PIL import Image
    from robosuite.utils.errors import RandomizationError

    from .libero_sim import CAMERAS, CONTROL_HZ, Episode, LiberoSim

    sim = LiberoSim(args.bddl)
    mapper = PoseMapping(args.position_scale, args.action_cap)
    episode = None
    seed = args.seed
    last_saved, message = None, "Connect Quest input, then calibrate."
    reason = "shutdown"

    def reset_scene():
        nonlocal message
        mapper.disengage()
        try:
            sim.reset(seed)
        except (RuntimeError, RandomizationError) as exc:
            message = f"Scene {seed} initialization failed: {exc}. Use Next scene."
            print(message, flush=True)
            return None
        mapper.gripper = sim.resting_gripper
        return sim.snapshot()

    try:
        frame = reset_scene()
        n_ticks = 0
        while not stop.is_set():
            started = time.monotonic()
            value, interrupted, input_reason = station.take_input(time.monotonic_ns())
            try:
                command = station.commands.get_nowait()
            except queue.Empty:
                command = None
            if command == "calibrate":
                try:
                    mapper.calibrate(value)
                    message = "Calibrated. Release, then squeeze to move."
                except ValueError as exc:
                    message = str(exc)
            elif command == "stop":
                if episode is not None:
                    last_saved = episode.finish("stop")
                    episode = None
                    message = "Saved " + last_saved
            elif command == "reset" or (command == "start" and episode is None):
                if episode is not None:
                    last_saved = episode.finish("reset")
                    episode = None
                if command == "reset":
                    seed = (seed + 1) % (2**32)
                frame = reset_scene()
                if frame is not None:
                    if command == "start":
                        episode = Episode(args.output, sim, seed, frame, mapper.heading)
                        message = "Recording. Release, then squeeze to move."
                    else:
                        message = "New scene. Release, then squeeze to move."
            if frame is None:
                station.publish(dict(task=sim.env.language_instruction, seed=seed, input=input_reason,
                                     scene_ready=False, calibrated=mapper.heading is not None,
                                     moving=False, recording=False, steps=0, task_success=False,
                                     message=message, last_saved=last_saved), {})
                stop.wait(1 / CONTROL_HZ)
                continue
            # A reset may take several control periods. Re-read the latest
            # input so a release or tracking loss during the reset wins.
            stamp = time.monotonic_ns()
            if command is not None:
                value, interruption_after_command, input_reason = station.take_input(stamp)
                interrupted |= interruption_after_command
            if value is not None and stamp >= value["deadline"]:
                value, interrupted = None, True
            action = mapper.action(value, interrupted, frame, sim.output_max)
            frame = sim.step(action)
            if episode is not None:
                episode.add(action, frame, None if value is None else value["packet"],
                            -1 if value is None else stamp + MAX_AGE_NS - value["deadline"],
                            mapper.anchor is not None, stamp)
            images = None
            if n_ticks % 2 == 0:
                images = {}
                for key in CAMERAS:
                    buffer = io.BytesIO()
                    Image.fromarray(np.ascontiguousarray(frame[key][::-1])).save(buffer, "JPEG", quality=85)
                    images["/" + key] = buffer.getvalue()
            station.publish(dict(task=sim.env.language_instruction, seed=seed, input=input_reason,
                                 scene_ready=True, calibrated=mapper.heading is not None, moving=mapper.anchor is not None,
                                 recording=episode is not None, steps=episode.count if episode else 0,
                                 task_success=frame["success"], message=message, last_saved=last_saved),
                            images)
            n_ticks += 1
            stop.wait(max(0., 1 / CONTROL_HZ - (time.monotonic() - started)))
    except BaseException:
        reason = "error"
        raise
    finally:
        try:
            if episode is not None:
                if reason == "error":
                    # The last append may be incomplete. Do not publish it.
                    episode.file.close()
                else:
                    episode.finish(reason)
        finally:
            sim.close()


def _serve(args, key):
    station = Station()
    stop = threading.Event()
    with StationServer(args.port, station, key) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous = {sig: signal.signal(sig, lambda *_: stop.set())
                    for sig in (signal.SIGINT, signal.SIGTERM)}
        print(f"LIBERO station http://127.0.0.1:{server.server_port}", flush=True)
        try:
            station_loop(args, station, stop)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            server.shutdown()
            thread.join()


def forward_quest(args, key):
    quest = QuestInput(serial=args.serial, adb=args.adb)
    conn = http.client.HTTPConnection("127.0.0.1", args.port, timeout=.12)
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    bound = last_sent = warning = None

    def request(method, path, body=None):
        conn.request(method, path, None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        if response.status >= 400:
            raise ValueError(data.get("error", response.status))
        return data["bound_ns"]

    try:
        quest.start()
        print("Forwarding Quest input to LIBERO. Ctrl-C to stop.", flush=True)
        while True:
            started = time.monotonic()
            try:
                if bound is None:
                    bound = request("GET", "/status")
                value = quest.poll()
                if not value["valid"]:
                    bound = request("POST", "/quest", {"packet": None, "reason": value["reason"]})
                elif value["ingress_seq"] != last_sent:
                    age = max(0, time.monotonic_ns() - value["mapped_ns"] + value["uncertainty_ns"])
                    bound = request("POST", "/quest", dict(packet=value["packet"],
                                                          bound_ns=bound, source_age_ns=age))
                    last_sent = value["ingress_seq"]
                warning = None
            except (OSError, http.client.HTTPException, ValueError) as exc:
                if str(exc) != warning:
                    warning = str(exc)
                    print(f"LIBERO link paused: {exc}", flush=True)
                conn.close()
                bound = None
            time.sleep(max(0., .025 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
        quest.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the native LIBERO simulator and browser station")
    task = serve.add_mutually_exclusive_group(required=True)
    task.add_argument("--bddl", type=Path)
    task.add_argument("--atomic", choices=("grasp", "pull", "place"))
    serve.add_argument("--output", required=True, type=Path)
    serve.add_argument("--port", type=int, default=8782)
    serve.add_argument("--key-file", required=True, type=Path)
    serve.add_argument("--seed", type=int, default=0)
    serve.add_argument("--position-scale", type=float, default=1.)
    serve.add_argument("--action-cap", type=float, default=.35)
    quest = sub.add_parser("quest", help="Forward the existing Quest APK's ADB input")
    quest.add_argument("--port", type=int, default=8782)
    quest.add_argument("--serial")
    quest.add_argument("--adb", default="adb")
    quest.add_argument("--key-file", type=Path)
    replay = sub.add_parser("replay", help="Compare a saved episode to a new native simulation")
    replay.add_argument("episode", type=Path)
    args = parser.parse_args()
    if args.command == "replay":
        from .libero_sim import replay_episode

        result = replay_episode(args.episode)
        print(json.dumps(result, indent=2))
        if not result["passed"]:
            raise SystemExit(1)
        return
    key = (args.key_file.read_text(encoding="utf-8").strip() if args.key_file
           else getpass.getpass("LIBERO station key: ").strip())
    if len(key) < 16 or not key.isascii() or not key.isalnum():
        parser.error("The station key must contain at least 16 ASCII letters or digits.")
    if args.command == "quest":
        forward_quest(args, key)
    else:
        if (not 0 <= args.seed < 2**32 or not math.isfinite(args.position_scale)
                or args.position_scale <= 0 or not 0 < args.action_cap <= 1):
            parser.error("Use a uint32 seed, positive position scale, and action cap in (0, 1].")
        if args.atomic:
            from .libero_sim import atomic_bddl
            args.bddl = atomic_bddl(args.atomic)
        _serve(args, key)


if __name__ == "__main__":
    main()
