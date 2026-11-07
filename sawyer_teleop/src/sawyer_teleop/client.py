"""A's robot transport. State and receipts never wait for IK or disk writes."""
import collections
import socket
import threading
import time

from .protocol import VERSION, JOINTS, ProtocolError, configure_socket, close_socket, encode, ns
from .transport import Connection, read_one
from .connection_profiles import backend_label
from .feedback_diagnostics import feedback_timeout, feedback_fault, diagnose_feedback
from .joint_protection import (CAPABILITY, parse_options, check_jump, check_destination,
                               reset_destination)
from .motion_profiles import DIRECT_POSE_MODE
from .protocol import pose, integer
from .a_timing import LOCAL_IK_BUDGET_S, REMOTE_IK_TIMEOUT_S
from .rates import EventRate


class RobotClient:
    def __init__(self, host, control_port=8765, ik_port=8766, timeout=.1, expected_backend=None,
                 ik_mode="remote", feedback_profile="standard", defer_remote_ik=False,
                 feedback_stream=True):
        if ik_mode not in ("remote", "local"):
            raise ValueError("invalid_ik_mode")
        # Recording-only copy of every accepted B feedback message. Control keeps
        # reading ``self.state`` (latest value only); this never feeds control.
        self.feedback_stream_requested = bool(feedback_stream)
        self.feedback_stream_status = {"enabled": False, "reason": "not_requested"}
        self.stream_rows = collections.deque()
        self.stream_capacity = 8192
        self.stream_dropped_b = self.stream_dropped_a = 0
        self.ik_mode, self.local_ik = ik_mode, None
        self.defer_remote_ik = defer_remote_ik
        self.host, self.control_port, self.ik_port = host, control_port, ik_port
        self.timeout = timeout
        feedback_timeout(timeout, feedback_profile, expected_backend, None)
        self.feedback_profile = feedback_profile
        self.expected_backend = expected_backend
        self.connection = None
        self.ik_socket = None
        self.ik_lock = threading.Lock()
        self.lock = threading.RLock()
        self.waiting = {}
        self.ident = 0
        self.command_seq = 0
        self.submitted_count = 0
        self.feedback_received = EventRate()
        self.state = None
        self.state_received_ns = self.arrival_gap_ns = None
        self.receipts = collections.deque()
        self.offset_ns = None
        self.uncertainty_ns = None
        self.sync_ns = 0
        self.rtt_ms = None
        self.stop = threading.Event()
        self.session = None
        self.generation = None
        self.hello = None
        self.error = None
        self.joint_protection = parse_options(None)
        self.joint_target_anchor = None
        self.neutral_destination = None

    def _socket(self, port):
        sock = socket.create_connection((self.host, port), timeout=2)
        configure_socket(sock)
        return sock

    def connect(self):
        try:
            sock = self._socket(self.control_port)
            sock.sendall(encode({"type": "hello", "version": VERSION}))
            hello = read_one(sock)
            if (hello.get("type") != "hello" or hello.get("version") != VERSION
                    or hello.get("joints") != JOINTS or hello.get("units") != "rad"):
                close_socket(sock)
                raise ProtocolError(str(hello))
            if self.expected_backend is not None and hello.get("backend") != self.expected_backend:
                close_socket(sock)
                raise ProtocolError("Backend mismatch: selected " + backend_label(self.expected_backend)
                                    + ", B reported " + backend_label(hello.get("backend"))
                                    + ". Check the B address and running service.")
            try:
                feedback_timeout(self.timeout, self.feedback_profile, self.expected_backend, hello)
            except ValueError as exc:
                close_socket(sock)
                raise ProtocolError(str(exc)) from exc
            self.hello = hello
            self.session, self.generation = hello["session"], hello["generation"]
            self.connection = Connection(sock, self._receive, self._closed).start()
            if self.ik_mode == "remote" and not self.defer_remote_ik:
                self._connect_ik()
            if self.ik_mode == "local":
                from .local_ik import LocalIK
                if not hello.get("local_ik_targets"):
                    raise ProtocolError(hello.get("local_ik_unavailable_reason")
                                        or "B lacks local IK model support. Update B and reconnect.")
                self.local_ik = LocalIK(hello.get("kinematics"))
                if self.local_ik.model["tip_frame"] != hello.get("tip_frame"):
                    raise ProtocolError("local_ik_tool_frame_mismatch")
            self.sync_clock(5)
            deadline = time.monotonic() + 2
            while self.state is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.snapshot()
            if self.local_ik is not None:
                self._check_kinematic_feedback()
            self.enable_feedback_stream()
            threading.Thread(target=self._sync_loop, daemon=True).start()
            return self
        except Exception:
            self.close()
            raise

    def _connect_ik(self):
        close_socket(self.ik_socket)
        sock = self._socket(self.ik_port)
        try:
            sock.sendall(encode({"type": "hello_ik", "version": VERSION, "session": self.session}))
            if read_one(sock).get("type") != "hello_ik":
                raise ProtocolError("ik_handshake_failed")
            self.ik_socket = sock
        except Exception:
            close_socket(sock)
            self.ik_socket = None
            raise

    def _closed(self):
        self.error = self.connection.error if self.connection else "disconnected"
        with self.lock:
            for entry in self.waiting.values():
                entry["event"].set()

    def _receive(self, msg):
        with self.lock:
            kind = msg.get("type")
            if kind == "reply":
                entry = self.waiting.get(msg.get("id"))
                if entry:
                    entry["reply"] = msg
                    entry["event"].set()
            elif msg.get("session") != self.session:
                raise ProtocolError("unexpected_session")
            elif kind == "state":
                self.feedback_received.mark()
                received = ns()
                if self.state_received_ns is not None:
                    self.arrival_gap_ns = received-self.state_received_ns
                self.state_received_ns = received
                stream = msg.get("feedback_stream")
                if stream is not None:
                    msg = {k: v for k, v in msg.items() if k != "feedback_stream"}
                    self._stream_receive(stream, received)
                self.state = msg
            elif kind == "submitted":
                if len(self.receipts) >= 256:
                    raise ProtocolError("receipt_queue_full")
                self.receipts.append(msg)
                self.submitted_count += 1
            else:
                raise ProtocolError("unexpected_message")

    def _stream_receive(self, stream, received):
        if not isinstance(stream, dict) or not isinstance(stream.get("rows"), list):
            raise ProtocolError("invalid_feedback_stream")
        self.stream_dropped_b += integer(stream.get("dropped", 0), "feedback stream dropped")
        for row in stream["rows"]:
            if not isinstance(row, dict) or row.get("kind") not in ("joint", "pose"):
                raise ProtocolError("invalid_feedback_stream_row")
            if len(self.stream_rows) >= self.stream_capacity:
                self.stream_rows.popleft()
                self.stream_dropped_a += 1
            # Clock conversion uses the estimate current at arrival (as states do).
            self.stream_rows.append(dict(row, session=self.session, a_received_ns=received,
                                         clock_offset_ns=self.offset_ns,
                                         clock_uncertainty_ns=self.uncertainty_ns))

    def enable_feedback_stream(self):
        """Opt in once per connection; an older B keeps the latest-value recording path."""
        if not self.feedback_stream_requested:
            self.feedback_stream_status = {"enabled": False, "reason": "disabled_by_config"}
        elif (self.hello or {}).get("feedback_stream") != 1:
            self.feedback_stream_status = {"enabled": False, "reason": "B_lacks_feedback_stream"}
        else:
            try:
                self.request("feedback_stream", enabled=True)
                self.feedback_stream_status = {"enabled": True, "schema": 1, "reason": None}
            except (ProtocolError, TimeoutError, ConnectionError) as exc:
                self.feedback_stream_status = {"enabled": False, "reason": "request_failed: "+str(exc)}
        return dict(self.feedback_stream_status)

    def drain_feedback_stream(self):
        """Return (rows, counters). Counters are cumulative since connect."""
        with self.lock:
            rows = list(self.stream_rows)
            self.stream_rows.clear()
            return rows, {"dropped_b": self.stream_dropped_b, "dropped_a": self.stream_dropped_a,
                          "status": dict(self.feedback_stream_status)}

    def request(self, kind, timeout=.3, **kwargs):
        with self.lock:
            self.ident += 1
            ident = self.ident
            entry = {"event": threading.Event()}
            self.waiting[ident] = entry
        try:
            self.connection.send(dict(kwargs, type=kind, id=ident, session=self.session),
                                 clear_latest=kind in ("hold", "arm"))
            if not entry["event"].wait(timeout):
                self.connection.close("request_timeout:" + kind)
                raise TimeoutError(kind + "_timeout")
            response = entry.get("reply")
            if not response:
                raise ConnectionError(self.error or "connection_closed")
            if not response.get("ok"):
                raise ProtocolError(response.get("error", "request_failed"))
            return response["result"]
        finally:
            with self.lock:
                self.waiting.pop(ident, None)

    def sync_clock(self, count=1):
        observations = []
        for _ in range(count):
            start = ns()
            result = self.request("ping", timeout=.3)
            finish = ns()
            observations.append((finish-start, (start+finish)//2-result["b_ns"], finish))
        rtt, offset, stamp = min(observations)
        with self.lock:
            self.offset_ns, self.uncertainty_ns = offset, rtt//2
            self.sync_ns, self.rtt_ms = stamp, rtt/1e6

    def _sync_loop(self):
        while not self.stop.wait(2):
            try:
                self.sync_clock(3)
            except (OSError, TimeoutError, ValueError, ConnectionError):
                break

    @property
    def feedback_timeout(self):
        return feedback_timeout(self.timeout, self.feedback_profile, self.expected_backend, self.hello)

    def feedback_diagnostics(self, reference=None):
        with self.lock:
            return diagnose_feedback(state=self.state, hello=self.hello or {}, now=ns(),
                connected=bool(self.connection and not self.connection.closed.is_set()),
                offset=self.offset_ns, uncertainty=self.uncertainty_ns, sync_ns=self.sync_ns,
                timeout=self.feedback_timeout, received_ns=self.state_received_ns,
                arrival_gap_ns=self.arrival_gap_ns, reference=reference)

    def snapshot(self, require_fresh=True):
        with self.lock:
            state = self.state
            now = ns()
            fault, age = feedback_fault(state, now,
                bool(self.connection and not self.connection.closed.is_set()),
                self.offset_ns, self.uncertainty_ns, self.sync_ns, self.feedback_timeout)
            if age is None:
                raise ConnectionError("feedback_missing")
            if require_fresh and fault:
                raise ConnectionError(fault)
            # Keep a_received_ns's existing snapshot-time meaning for recordings.
            return dict(state, age_s=age, a_received_ns=now,
                        a_frame_received_ns=self.state_received_ns,
                        clock_offset_ns=self.offset_ns,
                        clock_uncertainty_ns=self.uncertainty_ns)

    def arm(self, trajectory=None, execution_mode="joint", cartesian_send_hz=None,
            joint_protection=None, motion_purpose="teleop"):
        state = self.snapshot()
        options = {}
        guard = parse_options(joint_protection)
        if joint_protection is not None:
            if self.hello.get("joint_protection") != CAPABILITY:
                raise ProtocolError("B lacks joint protection. Update B and reconnect.")
            options["joint_protection"] = guard
        neutral = reset_destination(motion_purpose, trajectory, self.hello.get("neutral_pose"))
        if motion_purpose != "teleop":
            options["motion_purpose"] = motion_purpose
        if cartesian_send_hz is not None and execution_mode == "joint":
            raise ProtocolError("cartesian_send_hz_requires_native_mode")
        if execution_mode != "joint":
            capability = self.hello.get("intera_cartesian", {})
            if (execution_mode not in ("intera_cartesian", DIRECT_POSE_MODE) or self.hello.get("backend") not in ("intera", "gazebo")
                    or capability.get("available") is not True or capability.get("schema") != 1):
                raise ProtocolError("B does not support Intera Cartesian. Update B and check its native Motion Controller.")
            if execution_mode == DIRECT_POSE_MODE and (capability.get('direct_pose_50hz') is not True or cartesian_send_hz != 50):
                raise ProtocolError('direct_pose_50hz_not_supported')
            # Gazebo's simulated controller accepts local-IK joint targets (B still FKs
            # and checks model limits); remote IK there adds ~50 ms and expires input.
            if trajectory is not None or (self.local_ik is not None and self.hello.get("backend") != "gazebo"):
                raise ProtocolError("Intera Cartesian requires remote IK and native trajectory execution.")
            if cartesian_send_hz is not None:
                from .intera_cartesian import validate_send_hz
                cartesian_send_hz = validate_send_hz(cartesian_send_hz)
                if cartesian_send_hz not in capability.get("send_hz_choices", []):
                    raise ProtocolError("B does not support selecting Cartesian submission rates. Update B and reconnect.")
                options["cartesian_send_hz"] = cartesian_send_hz
            options["execution_mode"] = execution_mode
        if self.local_ik is not None:
            self._check_kinematic_feedback()
            options["model_sha256"] = self.local_ik.model["sha256"]
        if trajectory is not None:
            capability = self.hello.get("joint_trajectory", {})
            if not capability.get("per_sample_receipts"):
                raise ProtocolError("B does not support real-time trajectories. Update B and reconnect.")
            profile = trajectory.get("profile", "cubic")
            if profile != "cubic" and profile not in capability.get("profiles", {}):
                raise ProtocolError("B does not support the requested trajectory profile. Update B and reconnect.")
            if profile in ("responsive", "coordinated") and not capability.get("source_sample_timing"):
                raise ProtocolError("B lacks source sample timing for streaming trajectories. Update B and reconnect.")
            options["trajectory"] = trajectory
        result = self.request("arm", feedback_seq=state["sample"]["seq"], **options)
        self.generation = result["generation"]
        if joint_protection is not None and result.get("joint_protection") != guard:
            self.hold("joint_protection_not_confirmed")
            raise ProtocolError("joint_protection_not_confirmed")
        self.joint_protection = guard
        self.neutral_destination = neutral
        self.joint_target_anchor = list(state["sample"]["q"])
        # The reliable arm reply can overtake the queued latest-state frame.
        # Do not expose "following" until telemetry confirms this generation.
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            state = self.snapshot()
            if state["generation"] == self.generation and state["armed"]:
                return self.generation
            if state["generation"] > self.generation or (
                    state["generation"] == self.generation and not state["armed"]):
                raise ProtocolError("arm_cancelled")
            time.sleep(.001)
        raise TimeoutError("arm_feedback_timeout")

    def hold(self, reason="released"):
        if not self.connection or self.connection.closed.is_set():
            return {"holding": False, "reason": "connection_lost"}
        result = self.request("hold", reason=reason)
        self.generation = result["generation"]
        return result

    def reset_scene(self):
        if self.hello.get("backend") != "gazebo" or not self.hello.get("scene_reset", {}).get("available"):
            raise ProtocolError("Scene reset requires an updated Gazebo B. Update B and reconnect.")
        state = self.snapshot()
        return self.request("reset_scene", generation=self.generation,
                            feedback_seq=state["sample"]["seq"])

    def gripper_observation(self, trigger):
        if not self.hello.get("gripper_observation"):
            return
        value = None if trigger is None else (-1 if trigger < .3 else 1)
        self.connection.send({"type": "gripper_observation", "session": self.session,
                              "value": value}, latest="gripper_observation")

    def target(self, q, feedback_seq, gripper=None, source_sample_ns=None):
        self.snapshot()
        try:
            check_destination(q, self.joint_target_anchor, self.joint_protection,
                              "A_target", self.neutral_destination)
        except ProtocolError as exc:
            self.hold(str(exc))
            raise
        if gripper is not None:
            if type(gripper) is not int or gripper not in (-1, 1):
                raise ValueError("invalid_gripper_command")
            if not self.hello.get("gripper_control"):
                raise ProtocolError("gripper_unsupported")
        if source_sample_ns is not None and (type(source_sample_ns) is not int or source_sample_ns < 0):
            raise ValueError("invalid_source_sample_ns")
        with self.lock:
            self.command_seq += 1
            seq = self.command_seq
        self.connection.send({
            "type": "target", "session": self.session, "generation": self.generation,
            "seq": seq, "feedback_seq": feedback_seq, "q": q,
            **({"gripper": gripper} if gripper is not None else {}),
            **({"source_sample_ns": source_sample_ns} if source_sample_ns is not None else {}),
        }, latest="target")
        self.joint_target_anchor = list(q)
        return seq

    def pose_target(self, target, feedback_seq, input_deadline_a_ns, source_sample_ns, gripper=None):
        self.snapshot()
        target = pose(target)
        integer(input_deadline_a_ns, 'input deadline')
        integer(source_sample_ns, 'source sample')
        with self.lock:
            deadline_b = input_deadline_a_ns - self.offset_ns - self.uncertainty_ns
            if deadline_b <= ns() - self.offset_ns:
                raise ProtocolError('quest_stale')
            self.command_seq += 1
            seq = self.command_seq
        self.connection.send(dict(type='pose_target', session=self.session, generation=self.generation,
            seq=seq, feedback_seq=feedback_seq, pose=target, frame_id='base',
            tip_frame=self.hello['tip_frame'], input_deadline_b_ns=deadline_b,
            source_sample_ns=source_sample_ns, gripper=gripper), latest='target')
        return seq

    def drain_receipts(self):
        with self.lock:
            result = list(self.receipts)
            self.receipts.clear()
            return result

    def rates(self):
        connection = self.connection
        result = (connection.target_sent.snapshot(active=not connection.closed.is_set())
                  if connection else {"hz": 0.0, "total": 0})
        with self.lock:
            result["submitted"] = self.submitted_count
            active = connection is not None and not connection.closed.is_set()
            result["feedback_hz"] = self.feedback_received.snapshot(active=active)["hz"]
            # This is B's own successful-submission counter, not receipt arrival Hz.
            fresh = (active and self.state_received_ns is not None
                     and 0 <= ns()-self.state_received_ns <= self.feedback_timeout*1e9)
            result["submission_hz"] = ((self.state or {}).get("submission_rate", {}).get("hz")
                                       if fresh else None)
        return result

    def solve(self, target, seed, feedback_seq, timeout=REMOTE_IK_TIMEOUT_S):
        # A single worker calls this; no growing futures/request queue.
        if not self.ik_lock.acquire(False):
            raise ProtocolError("ik_busy")
        try:
            if self.ik_mode == "local":
                if self.local_ik is None:
                    raise ProtocolError("local_ik_model_unavailable")
                state = self.snapshot()
                generation = self.generation
                if not state.get("armed") or state["generation"] != generation:
                    raise ProtocolError("ik_expired")
                result = self.local_ik.solve(target, seed, timeout=min(timeout, LOCAL_IK_BUDGET_S),
                    cancelled=lambda: self.stop.is_set() or self.generation != generation)
                state = self.snapshot()
                if (self.stop.is_set() or self.generation != generation
                        or state["generation"] != generation or not state.get("armed")):
                    raise ProtocolError("ik_expired")
                if result is None:
                    raise ProtocolError("local_ik_no_continuous_solution: No nearby solution within model limits and time budget")
                check_jump(result, seed, self.joint_protection, "A_IK")
                return result
            if self.ik_socket is None:
                self._connect_ik()
            with self.lock:
                self.ident += 1
                ident = self.ident
                generation = self.generation
            self.ik_socket.settimeout(timeout)
            self.ik_socket.sendall(encode({
                "type": "solve", "id": ident, "session": self.session,
                "generation": generation, "pose": target, "seed": seed,
                "feedback_seq": feedback_seq, "timeout_s": timeout,
            }))
            response = read_one(self.ik_socket, timeout=timeout)
            if response.get("id") != ident or generation != self.generation:
                raise ProtocolError("ik_expired")
            if not response.get("ok"):
                raise ProtocolError(response.get("error", "ik_failed"))
            check_jump(response["q"], seed, self.joint_protection, "A_IK")
            return response["q"]
        except (OSError, ValueError, TimeoutError, ConnectionError):
            close_socket(self.ik_socket)
            self.ik_socket = None
            raise
        finally:
            self.ik_lock.release()

    def _check_kinematic_feedback(self):
        # A model fingerprint alone cannot detect an incorrect frame/calibration.
        # Cross-check with independent B endpoint feedback before permitting arm.
        import numpy as np
        from scipy.spatial.transform import Rotation
        sample = self.snapshot()["sample"]
        transform = self.local_ik.forward(sample["q"])
        translation = np.linalg.norm(transform[:3, 3]-sample["pose"]["p"])
        angle = (Rotation.from_matrix(transform[:3, :3]).inv()
                 * Rotation.from_quat(sample["pose"]["q"])).magnitude()
        # Joint and endpoint ROS topics are asynchronous; these are consistency
        # checks, not solver accuracy tolerances or new feedback validity windows.
        if translation > .01 or angle > .03:
            raise ProtocolError("local_ik_feedback_model_mismatch")

    def close(self):
        self.stop.set()
        if self.connection:
            self.connection.close("operator_shutdown")
        close_socket(self.ik_socket)
