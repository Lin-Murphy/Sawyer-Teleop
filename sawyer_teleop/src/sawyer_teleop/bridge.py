"""B-side arbitration and optional online interpolation. No blocking IK or I/O."""
import collections
import hashlib
import json
import sys
import threading
import time
import uuid

from .protocol import JOINTS, VERSION, ProtocolError, integer, vector, ns
from .trajectory import CoordinatedJointTrajectory, JointTrajectory, ResponsiveJointTrajectory, make_trajectory
from .neutral import neutral_target, NEUTRAL_TOLERANCE_RAD
from .joint_protection import (CAPABILITY, parse_options, check_jump, check_destination,
                               reset_destination, JointJumpError)
from .direct_pose import DIRECT_POSE_MODE, accept_pose, pose_tick
from .intera_cartesian import MODE as CARTESIAN_MODE, validate_send_hz
from .rates import EventRate
from .feedback_stream import SCHEMA as FEEDBACK_STREAM_SCHEMA



def _log_stop_failure(reason, exc):
    # The latch outlives sessions, but telemetry events are cleared on reconnect;
    # keep the cause in B's own console log for diagnosis.
    sys.stderr.write("B native STOP latched (restart B to unlock): %s: %r\n" % (reason, exc))
    sys.stderr.flush()


class BridgeCore:
    def __init__(self, backend, state_timeout=0.1, command_timeout=0.2, clock=ns, control_hz=100, reference_timeout=.12):
        if not 0 < state_timeout <= command_timeout:
            raise ValueError("state timeout must be positive and <= command timeout")
        if not 0 < reference_timeout <= command_timeout:
            raise ValueError("reference timeout must be positive and <= command timeout")
        self.reference_timeout_ns = int(reference_timeout * 1e9)
        self.pause_details = None
        self.backend = backend
        self.state_timeout_ns = int(state_timeout * 1e9)
        self.command_timeout_ns = int(command_timeout * 1e9)
        self.clock = clock
        self.control_hz = control_hz
        self.lock = threading.RLock()
        self.session = None
        self.generation = 0
        self.armed = False
        self.reason = "disconnected"
        self.pending = None
        self.last_command_ns = 0
        self.last_seq = -1
        self.submitted_seq = -1
        self.samples = collections.OrderedDict()
        self.latest = None
        self.last_source_seq = -1
        self.hold_target = None
        self.events = collections.deque(maxlen=64)
        self.event_seq = 0
        self.rejected = 0
        self.submitted = 0
        self.submission_rate = EventRate(clock=lambda: self.clock()/1e9)
        self.gripper_observation = None
        self.accepted = 0
        self.last_target = None
        self.last_submission = None
        self.trajectory = None
        self.execution_mode = "joint"
        self.joint_protection = parse_options(None)
        self.joint_target_anchor = self.joint_output_anchor = None
        self.neutral_destination = None
        self.motion_purpose = "teleop"
        self.cartesian = getattr(backend, "cartesian", None)
        self.native_job = None
        self.next_native_ns = 0
        self.native_send_hz = None
        self.native_period_ns = 0
        self.last_native_submit_ns = None
        self.native_stopping = False
        self.native_stop_failed = False
        self.native_stop_started_ns = 0
        self.last_arm_configuration = None
        self.active_target = None
        self.last_tick_ns = None
        self.execution_seq = 0
        self.active_model_sha256 = None
        self.scene_reset = None
        self.scene_cancel = threading.Event()
        # Intera's service can reject self-colliding IK solutions. The portable
        # kinematic solver does not yet reproduce that precheck on real hardware.
        self.local_ik_targets = backend.name != "intera"
        self.local_ik_reason = (None if self.local_ik_targets else
            "Local IK on Real Sawyer requires validated self-collision checking. Select Remote on B.")
        self.kinematics = getattr(backend, "kinematics", {"available": False})
        self.model_bounds = [(j["lower"], j["upper"]) for j in self.kinematics.get("chain", [])
                             if j["type"] == "revolute"]
        self._refresh()

    def _event(self, reason, details=None):
        self.event_seq += 1
        event = {"seq": self.event_seq, "reason": reason, "b_ns": self.clock()}
        if details is not None:
            event["details"] = details
        self.events.append(event)

    def _pause_context(self, target=None):
        # Capture before cancellation or a second snapshot changes the evidence.
        now = self.clock()
        target = target if target is not None else (self.pending or self.active_target)
        ref = target.get("feedback_seq") if target else None
        source = self.samples.get(ref) if type(ref) is int else None
        def age(sample):
            return (now-sample["captured_ns"])/1e6 if sample else None
        return {
            "at_b_ns": now,
            "latest_feedback_seq": self.latest.get("seq") if self.latest else None,
            "latest_feedback_age_ms": age(self.latest),
            "latest_feedback_valid": self.latest.get("valid") if self.latest else False,
            "latest_feedback_fault": self.latest.get("fault") if self.latest else None,
            "latest_robot_state": self.latest.get("robot") if self.latest else None,
            "latest_feedback_q": self.latest.get("q") if self.latest else None,
            "latest_feedback_diagnostics": self.latest.get("feedback_diagnostics") if self.latest else None,
            "reference_feedback_seq": ref,
            "reference_feedback_age_ms": age(source),
            "reference_feedback_valid": source.get("valid") if source else False,
            "reference_feedback_fault": source.get("fault") if source else None,
            "source_command_seq": target.get("seq") if target else None,
            "pending_source_command_seq": self.pending.get("seq") if self.pending else None,
            "active_source_command_seq": self.active_target.get("seq") if self.active_target else None,
            "target_received_age_ms": ((now-target["received_ns"])/1e6
                                       if target and "received_ns" in target else None),
            "command_age_ms": (now-self.last_command_ns)/1e6,
            "control_gap_ms": ((now-self.last_tick_ns)/1e6 if self.last_tick_ns is not None else None),
            "thresholds_ms": {"latest_feedback": self.state_timeout_ns/1e6,
                              "source_reference": self.reference_timeout_ns/1e6,
                              "command": self.command_timeout_ns/1e6,
                              "control_gap": self.state_timeout_ns/1e6},
        }

    def _feedback_error(self, sample, prefix, timeout_ns):
        if not sample:
            return prefix + "_missing"
        age = self.clock()-sample["captured_ns"]
        if age < 0:
            return prefix + "_clock_regressed"
        if age > timeout_ns:
            return prefix + "_expired"
        if sample.get("valid") is not True:
            return prefix + "_invalid"
        return None

    def _refresh(self):
        self.latest = self.backend.snapshot()
        sample = self.latest
        if sample is None:
            return
        seq = integer(sample["seq"], "source seq")
        if seq < self.last_source_seq:
            self.samples.clear()
            sample = dict(sample, valid=False, fault="source_sequence_regressed")
            self.latest = sample
        elif seq > self.last_source_seq:
            self.last_source_seq = seq
            self.samples[seq] = sample
            while len(self.samples) > 64:
                self.samples.popitem(last=False)

    def _valid(self, sample):
        if not sample or sample.get("valid") is not True:
            return False
        return 0 <= self.clock() - sample["captured_ns"] <= self.state_timeout_ns

    def _reference(self, ref):
        integer(ref, "feedback reference")
        sample = self.samples.get(ref)
        # Latest feedback and historical goal authority have independent lifetimes.
        reason = (self._feedback_error(self.latest, "feedback_latest", self.state_timeout_ns)
                  or self._feedback_error(sample, "feedback_reference", self.reference_timeout_ns))
        if reason:
            raise ProtocolError(reason)
        return sample

    def connect(self):
        with self.lock:
            if self.session is not None:
                raise ProtocolError("bridge_busy")
            self._refresh()
            self.session = uuid.uuid4().hex
            self.submission_rate = EventRate(clock=lambda: self.clock()/1e9)
            self.generation += 1
            self.armed = False
            self.pending = None
            self.last_seq = self.submitted_seq = -1
            self.reason = "release_to_arm"
            self.pause_details = None
            self.events.clear()
            self.feedback_stream_enabled = False  # Opt-in per session (recording only).
            self.gripper_observation = None
            self.last_target = self.last_submission = None
            self.trajectory = self.active_target = None
            self.execution_seq = 0
            native_capability = dict(getattr(self.backend, "cartesian_capability", {
                "available": False, "reason": "This B has no native Intera Motion Controller."}))
            if "send_hz_choices" in native_capability:
                native_capability["send_hz_choices"] = [hz for hz in native_capability["send_hz_choices"]
                                                       if hz <= self.control_hz]
            return {"type": "hello", "version": VERSION, "session": self.session,
                    "generation": self.generation, "joints": JOINTS, "units": "rad",
                    "base_frame": "base", "tip_frame": self.backend.tip_frame,
                    "backend": self.backend.name, "ik": True, "gripper_observation": True,
                    "neutral_pose": getattr(self.backend, "neutral_pose", {"available": False}),
                    "scene_reset": getattr(self.backend, "scene_reset_capability", {"available": False})
                        if self.backend.name == "gazebo" else {"available": False},
                    "kinematics": self.kinematics, "local_ik_targets": self.local_ik_targets,
                    "local_ik_unavailable_reason": self.local_ik_reason,
                    "gripper_control": callable(getattr(self.backend, "publish_gripper", None)),
                    "state_timeout_s": self.state_timeout_ns/1e9,
                    "reference_timeout_s": self.reference_timeout_ns/1e9,
                    "command_timeout_s": self.command_timeout_ns/1e9,
                    "intera_cartesian": native_capability,
                    "joint_protection": dict(CAPABILITY),
                    "feedback_stream": FEEDBACK_STREAM_SCHEMA if self._has_feedback_stream() else None,
                    "joint_trajectory": {"algorithm": JointTrajectory.algorithm,
                                         "control_hz": self.control_hz,
                                         "source_sample_timing": True,
                                         "profiles": {"cubic": JointTrajectory.algorithm,
                                                      "responsive": ResponsiveJointTrajectory.algorithm,
                                                      "coordinated": CoordinatedJointTrajectory.algorithm},
                                         "per_sample_receipts": True},
                    "description": self.backend.description()}

    def _hold(self, reason, target=None, details=None):
        self.scene_cancel.set()
        if not self.armed and self.pause_details is not None:
            # A stale telemetry frame or an in-flight IK error can race the B stop.
            # Acknowledging it must not overwrite the first fault or refresh motion.
            return self.hold_target is not None
        context = self._pause_context(target)
        context.update(details or {})
        self.pending = None
        self.trajectory = self.active_target = None
        self.last_tick_ns = None
        self.armed = False
        self.generation += 1
        self.reason = reason
        self.hold_target = None
        if callable(getattr(self.backend, "hold_gripper", None)):
            try:
                self.backend.hold_gripper()
            except Exception:
                self._event("gripper_hold_failed")
        if self.execution_mode in (CARTESIAN_MODE, DIRECT_POSE_MODE):
            # Cancel even with stale feedback. A STOP submission is not a confirmed stop.
            self.native_job = None
            self.native_stop_started_ns = self.clock()
            try:
                self.cartesian.stop()
                self.native_stopping = True
            except Exception as exc:
                self.native_stopping = False
                self.native_stop_failed = True
                self.reason = reason + ":intera_cartesian_stop_failed"
                context["stop_error"] = str(exc)
                _log_stop_failure(self.reason, exc)
            context["holding"] = False
            self.pause_details = context
            self._event(reason, context)
            return False
        self._refresh()
        if self._valid(self.latest):
            # Cancel immediately: holding current feedback must never wait for a ramp.
            self.hold_target = vector(self.latest["q"], 7)
            try:
                self.backend.publish(self.hold_target)
                confirmed = True
            except Exception:
                self.hold_target = None
                confirmed = False
                self.reason = reason + ":hold_submit_failed"
        else:
            confirmed = False  # Never publish stale feedback as a hold position.
        context["holding"] = confirmed
        self.pause_details = context
        self._event(reason, context)
        return confirmed

    def _has_feedback_stream(self):
        return callable(getattr(self.backend, "drain_feedback_stream", None))

    def drain_feedback_stream(self):
        """Rows accepted since the last call, for the session that opted in; else None.

        Called by the server's tick thread after ``tick()``; it never touches control state.
        """
        with self.lock:
            if not (self.session and getattr(self, "feedback_stream_enabled", False)):
                return None
            rows, dropped = self.backend.drain_feedback_stream()
            if not rows and not dropped:
                return None
            return {"schema": FEEDBACK_STREAM_SCHEMA, "rows": rows, "dropped": dropped}

    def disconnect(self, session):
        with self.lock:
            if self.session == session:
                self.feedback_stream_enabled = False
                self.scene_cancel.set()
                try:
                    if self.armed:
                        self._hold("disconnected")
                    else:
                        self.pending = self.hold_target = None
                        self.trajectory = self.active_target = None
                        self.generation += 1
                        self.reason = "disconnected"
                        self._event("disconnected")
                finally:
                    self.session = None

    def request(self, msg):
        with self.lock:
            if not self.session or msg.get("session") != self.session:
                raise ProtocolError("session_mismatch")
            kind = msg.get("type")
            if kind == "gripper_observation":
                value = msg.get("value")
                if value is not None and (type(value) is not int or value not in (-1, 1)):
                    raise ProtocolError("invalid_gripper_observation")
                self.gripper_observation = {"value": value, "received_ns": self.clock()}
                return None  # Display only: never calls the robot backend.
            if kind == "ping":
                return {"b_ns": self.clock()}
            if kind == "feedback_stream":
                enabled = msg.get("enabled")
                if type(enabled) is not bool:
                    raise ProtocolError("invalid_feedback_stream_request")
                if enabled and not self._has_feedback_stream():
                    raise ProtocolError("feedback_stream_unavailable")
                if enabled and not self.feedback_stream_enabled:
                    self.backend.clear_feedback_stream()  # Rows start at this session.
                self.feedback_stream_enabled = enabled
                return {"enabled": enabled, "schema": FEEDBACK_STREAM_SCHEMA}
            if kind == "hold":
                confirmed = self._hold(str(msg.get("reason", "operator_hold"))[:80])
                return {"generation": self.generation, "holding": confirmed,
                        "reason": self.reason, "pause_details": self.pause_details}
            if kind == "reset_scene":
                return self._start_scene_reset(msg)
            if kind == "arm":
                if self.scene_reset and self.scene_reset["state"] == "running":
                    raise ProtocolError("scene_reset_in_progress")
                self._refresh()
                self._reference(msg.get("feedback_seq"))
                if self.armed:
                    raise ProtocolError("already_armed")
                model_sha = msg.get("model_sha256")
                if model_sha is not None and not self.local_ik_targets:
                    raise ProtocolError("local_ik_not_supported: " + self.local_ik_reason)
                if model_sha is not None and (not self.kinematics.get("available")
                        or model_sha != self.kinematics.get("sha256") or len(self.model_bounds) != 7):
                    raise ProtocolError("kinematic_model_mismatch")
                mode = msg.get("execution_mode", "joint")
                if mode not in ("joint", CARTESIAN_MODE, DIRECT_POSE_MODE):
                    raise ProtocolError("unknown_execution_mode")
                if self.native_stopping or self.native_stop_failed:
                    raise ProtocolError("intera_cartesian_stop_unconfirmed")
                native_send_hz = None
                if "cartesian_send_hz" in msg and mode not in (CARTESIAN_MODE, DIRECT_POSE_MODE):
                    raise ProtocolError("cartesian_send_hz_requires_native_mode")
                if mode in (CARTESIAN_MODE, DIRECT_POSE_MODE):
                    capability = getattr(self.backend, "cartesian_capability", {})
                    if (self.backend.name not in ("intera", "gazebo") or self.cartesian is None
                            or capability.get("available") is not True or capability.get("schema") != 1):
                        raise ProtocolError("intera_cartesian_unavailable")
                    if msg.get("trajectory") is not None:
                        raise ProtocolError("intera_cartesian_cannot_combine_joint_trajectory")
                    native_send_hz = self.cartesian.options["send_hz"]
                    if "cartesian_send_hz" in msg:
                        try:
                            native_send_hz = validate_send_hz(msg["cartesian_send_hz"])
                        except ValueError as exc:
                            raise ProtocolError(str(exc)) from exc
                        if native_send_hz not in capability.get("send_hz_choices", []):
                            raise ProtocolError("intera_cartesian_send_hz_unsupported")
                    if native_send_hz > self.control_hz:
                        raise ProtocolError("intera_cartesian_send_hz_exceeds_control_hz")
                if mode == DIRECT_POSE_MODE:
                    if (capability.get('direct_pose_50hz') is not True or native_send_hz != 50
                            or not callable(getattr(self.cartesian, 'prepare_pose', None))):
                        raise ProtocolError('direct_pose_50hz_not_supported')
                # Old clients and policy playback retain direct target semantics.
                trajectory = msg.get("trajectory")
                candidate = make_trajectory(trajectory) if trajectory is not None else None
                if candidate is not None and not candidate.options["enabled"]:
                    candidate = None
                if candidate is not None:
                    candidate.reset(self.latest["q"], self.clock())
                guard = parse_options(msg.get("joint_protection"))
                purpose = msg.get("motion_purpose", "teleop")
                neutral = reset_destination(purpose, trajectory, getattr(self.backend, "neutral_pose", None))
                if neutral is not None:
                    self._check_model_target(neutral)
                self.joint_protection = guard
                self.motion_purpose = purpose
                self.neutral_destination = neutral
                self.joint_target_anchor = list(self.latest["q"])
                self.joint_output_anchor = list(self.latest["q"])
                self.direct_bias = list(self.latest["q"])
                self.direct_feedback_anchor = list(self.latest["q"])
                self.trajectory = candidate
                self.execution_mode = mode
                self.native_job = None
                self.next_native_ns = 0
                self.native_send_hz = native_send_hz
                self.native_period_ns = int(1e9/native_send_hz) if native_send_hz else 0
                self.last_native_submit_ns = None
                self.active_model_sha256 = model_sha
                self.active_target = None
                self.last_tick_ns = self.clock()
                self.generation += 1
                self.armed = True
                self.reason = "following"
                self.pause_details = None
                self.last_command_ns = self.clock()
                self.pending = None
                self.hold_target = None
                configuration = {
                    "backend": self.backend.name, "tip_frame": self.backend.tip_frame,
                    "execution_mode": mode, "control_hz": self.control_hz,
                    "motion_purpose": purpose, "joint_protection": dict(guard),
                    "trajectory": dict(candidate.options) if candidate is not None else None,
                    "trajectory_algorithm": candidate.algorithm if candidate is not None else None,
                    "cartesian_options": (dict(self.cartesian.options, send_hz=native_send_hz)
                                          if mode in (CARTESIAN_MODE, DIRECT_POSE_MODE) else None),
                    **({"command_semantics": "direct_cartesian_pose",
                        "joint_bias_policy": "fixed_at_arm", "joint_bias": list(self.direct_bias)}
                       if mode == DIRECT_POSE_MODE else {}),
                    "model_sha256": model_sha,
                    "timeouts_ms": {"latest_feedback": self.state_timeout_ns/1e6,
                                    "source_reference": self.reference_timeout_ns/1e6,
                                    "command": self.command_timeout_ns/1e6},
                }
                self.last_arm_configuration = {
                    "session": self.session, "generation": self.generation, "at_b_ns": self.clock(),
                    "configuration": configuration,
                    "sha256": hashlib.sha256(json.dumps(configuration, sort_keys=True,
                        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest(),
                }
                self._event("control_configuration", self.last_arm_configuration)
                return {"generation": self.generation, "joint_protection": dict(guard)}
            if kind == 'pose_target':
                return accept_pose(self, msg)
            if kind == 'target' and self.execution_mode == DIRECT_POSE_MODE:
                raise ProtocolError('joint_target_in_direct_pose_session')
            if kind != "target":
                raise ProtocolError("unknown_message")
            seq = integer(msg.get("seq"), "command seq")
            if msg.get("generation") != self.generation or not self.armed:
                self.rejected += 1
                return {"accepted": False, "reason": "not_armed", "seq": seq}
            if seq <= self.last_seq:
                self.rejected += 1
                return {"accepted": False, "reason": "old_command", "seq": seq}
            self._refresh()
            try:
                self._reference(msg.get("feedback_seq"))
                q = vector(msg.get("q"), 7, "joint target")
                self._check_model_target(q)
                check_destination(q, self.joint_target_anchor, self.joint_protection,
                                  "B_target", self.neutral_destination)
                if "source_sample_ns" in msg:
                    integer(msg["source_sample_ns"], "source_sample_ns")
                gripper = msg.get("gripper")
                if gripper is not None:
                    if type(gripper) is not int or gripper not in (-1, 1):
                        raise ProtocolError("invalid_gripper_command")
                    if not callable(getattr(self.backend, "publish_gripper", None)):
                        raise ProtocolError("gripper_unsupported")
            except ProtocolError as exc:
                self.rejected += 1
                self._hold("joint_jump" if isinstance(exc, JointJumpError) else str(exc),
                           target=msg, details=getattr(exc, "details", None))
                raise
            self.joint_target_anchor = list(q)
            self.last_seq = seq
            self.pending = dict(msg, q=q, received_ns=self.clock())
            self.accepted += 1
            self.last_target = {"seq": seq, "q": list(q),
                                "received_ns": self.pending["received_ns"]}
            return None  # Receipt is emitted only after actual backend submission.

    def _start_scene_reset(self, msg):
        if (self.backend.name != "gazebo" or not
                getattr(self.backend, "scene_reset_capability", {}).get("available")):
            raise ProtocolError("scene_reset_unsupported")
        if self.scene_reset and self.scene_reset["state"] == "running":
            raise ProtocolError("scene_reset_in_progress")
        if self.armed or self.hold_target is None or msg.get("generation") != self.generation:
            raise ProtocolError("scene_reset_requires_hold")
        self._refresh()
        self._reference(msg.get("feedback_seq"))
        q = neutral_target(self.backend.neutral_pose)
        if (max(abs(a-b) for a, b in zip(self.latest["q"], q)) >= NEUTRAL_TOLERANCE_RAD
                or max(abs(v) for v in self.latest["dq"]) >= .02):
            raise ProtocolError("scene_reset_requires_neutral")
        job = {"id": uuid.uuid4().hex, "state": "running"}
        self.scene_reset = job
        self.scene_cancel = cancelled = threading.Event()

        def restore():
            # ROS services must not block feedback, stop, or the 100 Hz loop.
            try:
                models = self.backend.reset_scene(cancelled)
                if cancelled.is_set():
                    raise RuntimeError("scene_reset_cancelled")
                result = {"state": "completed", "models": models}
            except Exception as exc:
                result = {"state": "failed", "error": str(exc)}
            with self.lock:
                job.update(result)
                self._event("scene_reset_" + result["state"], dict(job))

        threading.Thread(target=restore, name="scene-reset", daemon=True).start()
        return {"id": job["id"]}

    def _check_model_target(self, q):
        # A local-IK session binds to the startup model. B independently checks
        # both final goals and interpolated references, including braking tails.
        if self.active_model_sha256 is not None:
            if any(not lo-1e-9 <= value <= hi+1e-9
                   for value, (lo, hi) in zip(q, self.model_bounds)):
                raise ProtocolError("joint_model_limit_exceeded")

    def tick(self):
        with self.lock:
            self._refresh()
            receipt = None
            if self.native_stopping:
                self._poll_native_stop()
            if self.scene_reset and self.scene_reset["state"] == "running" and not self._valid(self.latest):
                self.scene_cancel.set()
            if self.armed:
                now = self.clock()
                if not self._valid(self.latest):
                    self._hold(self._feedback_error(self.latest, "feedback_latest", self.state_timeout_ns))
                elif now - self.last_command_ns > self.command_timeout_ns:
                    self._hold("command_timeout")
                elif ((self.trajectory is not None or self.execution_mode in (CARTESIAN_MODE, DIRECT_POSE_MODE)) and self.last_tick_ns is not None
                      and not 0 <= now - self.last_tick_ns <= self.state_timeout_ns):
                    self._hold("trajectory_control_gap")
                elif self.execution_mode in (CARTESIAN_MODE, DIRECT_POSE_MODE):
                    receipt = pose_tick(self, now) if self.execution_mode == DIRECT_POSE_MODE else self._cartesian_tick(now)
                elif self.trajectory is not None:
                    receipt = self._trajectory_tick(now)
                elif self.pending is not None:
                    msg, self.pending = self.pending, None
                    try:
                        self._reference(msg["feedback_seq"])
                        check_jump(msg["q"], self.joint_output_anchor, self.joint_protection, "B_submit")
                    except ProtocolError as exc:
                        self._hold("joint_jump" if isinstance(exc, JointJumpError) else str(exc),
                                   target=msg, details=getattr(exc, "details", None))
                    else:
                        before = self.latest
                        if not self._submit_gripper(msg):
                            return None, self.telemetry()
                        self.backend.publish(msg["q"])
                        self.joint_output_anchor = list(msg["q"])
                        submitted_ns = self.clock()
                        self.last_command_ns = submitted_ns
                        self.submitted_seq = msg["seq"]
                        self.submitted += 1
                        self.submission_rate.mark()
                        self.last_submission = {
                            "seq": msg["seq"], "q": list(msg["q"]),
                            "submitted_ns": submitted_ns,
                            "receive_to_submit_ms": (submitted_ns-msg["received_ns"])/1e6,
                        }
                        receipt = {
                            "type": "submitted", "session": self.session,
                            "seq": msg["seq"], "generation": self.generation,
                            "feedback_seq": msg["feedback_seq"],
                            "q": msg["q"], "b_ns": submitted_ns, "before": before,
                            "gripper": msg.get("gripper"),
                        }
                self.last_tick_ns = now if self.armed else None
            return receipt, self.telemetry()

    def _poll_native_stop(self):
        try:
            if self.cartesian.status() == "stopped":
                self.native_stopping = False
                # The native controller acknowledged STOP; feedback must still be fresh.
                if self._valid(self.latest):
                    self.hold_target = list(self.latest["q"])
                if self.pause_details is not None:
                    self.pause_details["holding"] = self.hold_target is not None
                self._event("intera_cartesian_stopped")
            elif self.clock()-self.native_stop_started_ns > 2_000_000_000:
                raise RuntimeError("intera_cartesian_stop_timeout")
        except Exception as exc:
            self.native_stopping = False
            self.native_stop_failed = True
            self.reason = "intera_cartesian_stop_failed"
            self._event(self.reason, {"error": str(exc)})
            _log_stop_failure(self.reason, exc)

    def _cartesian_tick(self, now):
        """Prepare outside this loop; revalidate and send under the stop/generation lock."""
        msg = self.pending or self.active_target
        receipt = None
        tick_ns = int(1e9/self.control_hz)
        # Quantize the requested cadence to the nearest control tick. A few
        # microseconds of jitter must not add a whole 10 ms polling interval.
        due_slack_ns = tick_ns//2
        try:
            status = self.cartesian.status() if self.active_target is not None else "idle"
            if self.native_job is not None:
                future, prepared, generation, started = self.native_job
                msg = prepared
                if generation != self.generation:
                    self.native_job = None
                elif now-started > self.state_timeout_ns:
                    raise RuntimeError("intera_cartesian_fk_timeout")
                elif future.done() and now+due_slack_ns >= self.next_native_ns:
                    self.native_job = None
                    # One in-flight preparation may complete while newer inputs
                    # replace the single pending slot. Requiring an unchanged input
                    # seq here would starve all sends at 120 Hz. The original lease
                    # still applies; no queued history or freshness renewal is allowed.
                    msg = prepared
                    waypoint = future.result()
                    self._refresh()
                    reference = self._reference(msg["feedback_seq"])
                    if self.clock()-msg["received_ns"] > self.command_timeout_ns:
                        raise RuntimeError("command_timeout")
                    self._check_model_target(msg["q"])
                    check_jump(msg["q"], self.joint_output_anchor, self.joint_protection, "B_native_submit")
                    before = self.latest
                    if not self._submit_gripper(msg):
                        return None
                    self.cartesian.send(waypoint)
                    self.joint_output_anchor = list(msg["q"])
                    submitted_ns = self.clock()
                    interval_ms = ((submitted_ns-self.last_native_submit_ns)/1e6
                                   if self.last_native_submit_ns is not None else None)
                    self.last_native_submit_ns = submitted_ns
                    # Keep a cadence instead of restarting the timer after SDK I/O.
                    # Skip missed slots; never issue catch-up goals or replay a queue.
                    self.next_native_ns = ((self.next_native_ns or now)+self.native_period_ns)
                    if self.next_native_ns <= submitted_ns+due_slack_ns:
                        missed = (submitted_ns+due_slack_ns-self.next_native_ns)//self.native_period_ns+1
                        self.next_native_ns += missed*self.native_period_ns
                    self.active_target = msg
                    self.submitted_seq = msg["seq"]
                    self.submitted += 1
                    self.submission_rate.mark()
                    self.last_submission = dict(seq=msg["seq"], q=list(msg["q"]),
                        submitted_ns=submitted_ns, command_kind="cartesian_goal",
                        receive_to_submit_ms=(submitted_ns-msg["received_ns"])/1e6)
                    receipt = dict(type="submitted", session=self.session, seq=msg["seq"],
                        generation=self.generation, feedback_seq=msg["feedback_seq"],
                        q=list(msg["q"]), b_ns=submitted_ns, before=before,
                        gripper=msg.get("gripper"), native_cartesian={
                            "command_kind": "cartesian_goal", "goal_q": list(msg["q"]),
                            "fk_ms": (submitted_ns-started)/1e6,
                            "send_hz": self.native_send_hz,
                            "submit_interval_ms": interval_ms,
                            "reference_age_ms": (submitted_ns-reference["captured_ns"])/1e6,
                            "status_before_send": status,
                            "source_sample_ns": msg.get("source_sample_ns")})
            if self.active_target is not None:
                # Even a still-running native goal loses authority when its original
                # reference expires. New input alone cannot renew the old goal.
                self._reference(self.active_target["feedback_seq"])
            if (self.native_job is None and self.pending is not None
                    and self.clock()+tick_ns+due_slack_ns >= self.next_native_ns
                    and not self.cartesian.busy):
                msg, self.pending = self.pending, None
                self._reference(msg["feedback_seq"])
                self.last_command_ns = msg["received_ns"]
                # Prepare one tick before the quantized submission slot. Preparing immediately
                # after a slow-rate send would unnecessarily age the next goal.
                started = self.clock()
                self.native_job = (self.cartesian.prepare(msg["q"]), msg, self.generation, started)
            return receipt
        except Exception as exc:
            self._hold("joint_jump" if isinstance(exc, JointJumpError) else "intera_cartesian_failed",
                       target=msg, details=dict(getattr(exc, "details", {}), native_error=str(exc)))
            return receipt  # Preserve a completed submission if the next prepare failed.

    def _submit_gripper(self, msg):
        # Only an accepted, current arm target can authorize actuation.
        # The older display-only gripper_observation message remains read-only.
        if msg.get("gripper") is not None:
            try:
                self.backend.publish_gripper(msg["gripper"])
            except Exception:
                self._hold("gripper_submit_failed")
                return False
        return True

    def _trajectory_tick(self, now):
        """Called under the core lock: hold and generation changes preempt all samples."""
        msg = self.pending or self.active_target
        details = {}
        try:
            if self.pending is not None:
                msg, self.pending = self.pending, None
                source = self._reference(msg["feedback_seq"])
                # Relative input spacing estimates velocity only. It never
                # grants freshness: the B feedback reference is checked separately.
                goal_stamp = msg.get("source_sample_ns", source["captured_ns"])
                self.trajectory.retarget(msg["q"], now, goal_stamp)
                self.active_target = msg
                # Only incoming operator intent renews the watchdog, never interpolation.
                self.last_command_ns = msg["received_ns"]
            msg = self.active_target
            if msg is None:
                return None
            # Fresh feedback alone cannot extend an old goal's authority to move.
            self._reference(msg["feedback_seq"])
            q, dq, ddq = self.trajectory.sample(now)
            self._check_model_target(q)
            check_jump(q, self.joint_output_anchor, self.joint_protection, "B_trajectory_submit")
            following_error = max(abs(a-b) for a, b in zip(q, self.latest["q"]))
            if following_error > self.trajectory.options["max_following_error_rad"]:
                details.update(following_error_rad=following_error,
                               max_following_error_rad=self.trajectory.options["max_following_error_rad"])
                raise RuntimeError("trajectory_following_error")
            before = self.latest
            if not self._submit_gripper(msg):
                return None
            publish_reference = getattr(self.backend, "publish_reference", None)
            if callable(publish_reference):
                publish_reference(q, dq, ddq)
            else:
                self.backend.publish(q)
            self.joint_output_anchor = list(q)
            submitted_ns = self.clock()
            self.execution_seq += 1
            self.submitted_seq = msg["seq"]
            self.submitted += 1
            self.submission_rate.mark()
            self.last_submission = {
                "seq": msg["seq"], "execution_seq": self.execution_seq, "q": q,
                "submitted_ns": submitted_ns,
                "receive_to_submit_ms": (submitted_ns-msg["received_ns"])/1e6,
            }
            return {
                "type": "submitted", "session": self.session, "seq": msg["seq"],
                "execution_seq": self.execution_seq, "generation": self.generation,
                "feedback_seq": msg["feedback_seq"], "q": q, "b_ns": submitted_ns,
                "before": before,
                "gripper": msg.get("gripper"),
                "trajectory": {"algorithm": self.trajectory.algorithm,
                               "goal_q": list(msg["q"]), "dq": dq, "ddq": ddq,
                               "sample_b_ns": now, "duration_s": self.trajectory.duration,
                               "source_sample_ns": msg.get("source_sample_ns")},
            }
        except (ValueError, RuntimeError, ArithmeticError) as exc:
            if str(exc) == "trajectory_unfeasible" and exc.__cause__ is not None:
                details["planner_error"] = str(exc.__cause__)[:4000]
            details.update(getattr(exc, "details", {}))
            self._hold("joint_jump" if isinstance(exc, JointJumpError) else str(exc), target=msg, details=details)
            return None

    def telemetry(self):
        with self.lock:
            return {
                "type": "state", "session": self.session, "b_ns": self.clock(),
                "sample": self.latest, "generation": self.generation,
                "armed": self.armed, "reason": self.reason,
                "pause_details": self.pause_details,
                "holding": self.hold_target is not None,
                "submitted_seq": self.submitted_seq,
                "trajectory_active": self.trajectory is not None,
                "execution_mode": self.execution_mode,
                "joint_protection": dict(self.joint_protection),
                "motion_purpose": self.motion_purpose,
                "native_send_hz": self.native_send_hz,
                "native_stopping": self.native_stopping,
                "native_stop_failed": self.native_stop_failed,
                "last_arm_configuration": self.last_arm_configuration,
                "scene_reset": dict(self.scene_reset) if self.scene_reset else None,
                "counts": {"submitted": self.submitted, "rejected": self.rejected},
                "submission_rate": self.submission_rate.snapshot(active=self.session is not None),
                "events": list(self.events),
            }

    def diagnostics(self):
        """Read cached state only: never sample or publish to the backend here."""
        with self.lock:
            state = self.telemetry()
            now = self.clock()
            state.update(
                backend=self.backend.name,
                connected=self.session is not None,
                feedback_valid=self._valid(self.latest),
                feedback_age_ms=(now-self.latest["captured_ns"])/1e6 if self.latest else None,
                gripper_observation=(self.gripper_observation if self.session and
                    self.gripper_observation and now-self.gripper_observation["received_ns"] < 500_000_000
                    else None),
                accepted=self.accepted,
                last_target=self.last_target,
                last_submission=self.last_submission,
            )
            return state
