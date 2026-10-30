"""A controller: input and robot feedback stay independent of IK and recording."""
import collections
import concurrent.futures
import copy
import json
import re
from pathlib import Path
import threading
import time

import numpy as np
from scipy.spatial.transform import Rotation

from .joint_protection import protection_options
from .protocol import ns
from .quest import heading_mapping, matrix, input_evidence
from .recording import Episode
from .locking import ProcessLock
from .input_status import describe_input
from .motion import PoseSmoother, input_stamp
from .trajectory import trajectory_options
from .motion_profiles import DIRECT_POSE_MODE, CARTESIAN_RATES, MOTION_PROFILES, selected_motion_profile, motion_profile_reason
from .gripper import trigger_command
from .pause import bridge_pause_message
from .neutral import neutral_target, NEUTRAL_TOLERANCE_RAD, NEUTRAL_TIMEOUT_S
from .feedback_diagnostics import reference_from_snapshot, reference_remaining_ms
from .a_timing import INPUT_MAX_AGE_S, INPUT_FUTURE_TOLERANCE_NS, IK_TASK_TIMEOUT_NS, input_age_expired


class Operator:
    def __init__(self, robot, quest, cameras, output, hz=50, position_scale=1.0,
                 motion=None, trajectory=None, gazebo_max_acceleration_rad_s2=None,
                 motion_profile=None, joint_protection=True):
        if not 1 <= hz <= 120 or not np.isfinite(position_scale) or position_scale <= 0:
            raise ValueError("invalid operator parameters")
        self.robot, self.quest, self.cameras = robot, quest, cameras
        self.output = Path(output)
        self.hz, self.scale = hz, position_scale
        self.ik_period_ns = int(np.ceil(1e9/hz))
        self.next_ik_ns = 0
        self.last_ik_sample = None
        self.direct_next_ns = 0
        self.direct_last_sample = None
        self.smoother = PoseSmoother(motion)
        self.base_trajectory_options = trajectory_options(trajectory)
        # Returning to the named neutral pose is a one-shot motion on the
        # physical arm. Use a conservative reset-only profile so transient tracking
        # lag does not trip B's unchanged 0.35 rad following-error guard.
        self.reset_trajectory_options = trajectory_options(dict(
            self.base_trajectory_options,
            profile="cubic",
            max_velocity_rad_s=[v*.25 for v in self.base_trajectory_options["max_velocity_rad_s"]],
            max_acceleration_rad_s2=[a*.25 for a in self.base_trajectory_options["max_acceleration_rad_s2"]]))
        self.trajectory_options = trajectory_options(self.base_trajectory_options)
        self.gazebo_trajectory_options = (None if gazebo_max_acceleration_rad_s2 is None else
            trajectory_options(dict(self.base_trajectory_options,
                                    max_acceleration_rad_s2=gazebo_max_acceleration_rad_s2)))
        # Real-arm fast mode has independent limits: 2x Standard acceleration.
        # Do not import the Gazebo acceleration override or change reset limits.
        self.intera_fast_trajectory_options = trajectory_options(dict(self.base_trajectory_options, enabled=True,
            max_acceleration_rad_s2=[2*a for a in self.base_trajectory_options['max_acceleration_rad_s2']]))
        self.motion_profile_explicit = motion_profile is not None
        self.selected_motion_profile = selected_motion_profile(motion_profile, gazebo_max_acceleration_rad_s2)
        self.motion_profile = "standard"  # Applied value requires the backend handshake.
        self.joint_protection = protection_options(joint_protection)
        self.ik_seed = None
        self.last_target_reference = None
        self.last_tick_ns = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.requests = collections.deque(maxlen=16)
        self.lock = threading.Lock()
        self.status = {"mode": "starting", "message": "Connecting", "calibrated": False,
                       "calibration_result": "Not calibrated", "robot_connected": False,
                       "connected_host": None,
                       "recording_error": "", "message_level": "event"}
        self.ui_events = collections.deque(maxlen=300)
        self.ui_event_seq = 0
        self.last_buttons = None
        self.episode = self.finishing = self.review = None
        self.batch = None
        self.record_started = self.record_elapsed = self.next_record_at = 0.
        self.saved_count = self.discarded_count = self.attempt_count = 0
        self.saved_seconds = 0.
        self.finish_save = False
        self.auto_recording = False
        self.following = False
        self.arming = False
        self.resetting = None
        self.reset_cancel = threading.Event()
        self.reset_requested = threading.Event()
        self.release_seen = False
        self.epoch = self.calibration_epoch = None
        self.mapping = None
        self.robot_origin = self.vr_origin = None
        self.engagement = None
        self.segment = 0
        self.commands = collections.OrderedDict()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ik")
        self.future = None
        self.future_info = None
        self.last_events = 0
        self.thread = None
        self.error = None
        self.current_input = {}
        self.arm_wait_ms = None
        self.process_lock = None

    def request(self, action, **parameters):
        if action == "stop_reset":
            # Stop cannot be blocked by a full GUI request queue or a slow arm reply.
            self.reset_cancel.set()
            self.wake.set()
            return
        with self.lock:
            if len(self.requests) == self.requests.maxlen:
                raise RuntimeError("operator_request_queue_full")
            self.requests.append((action, parameters) if parameters else action)
            if action == "reset_arm":
                self.reset_requested.set()
        self.wake.set()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _status(self, message_level="event", **values):
        with self.lock:
            if "message" in values:
                values["message_level"] = message_level
                if (values["message"], message_level) != (self.status.get("message"), self.status.get("message_level")):
                    self.ui_event_seq += 1
                    self.ui_events.append({"seq": self.ui_event_seq, "time": time.strftime("%H:%M:%S"),
                                           "text": values["message"], "kind": message_level})
            self.status.update(values)

    def _record_error(self, message):
        # Keep recording errors visible even when live control status changes.
        self._status(recording_error=message, message=message, message_level="error")

    def drain_ui_events(self):
        with self.lock:
            result = list(self.ui_events)
            self.ui_events.clear()
            return result

    def view(self):
        with self.lock:
            result = dict(self.status)
        result["resetting"] = bool(result.get("resetting") or self.reset_requested.is_set())
        # Read counters at display time, so stalled control cannot freeze a rate.
        result["input_rate"] = (self.quest.rates() if hasattr(self.quest, "rates")
                                else {"hz": 0.0, "total": 0})
        result["target_rate"] = (self.robot.rates() if hasattr(self.robot, "rates")
                                 else {"hz": 0.0, "total": 0})
        if hasattr(self.robot, "feedback_diagnostics"):
            future, info = self.future, self.future_info
            pending = info if self.following and future is not None and info else None
            reference = pending.get("reference") if pending else self.last_target_reference
            timing = self.robot.feedback_diagnostics(reference)
            timing["reference_kind"] = "pending IK" if pending else "last sent" if reference else "latest sample"
            timing["last_sent_remaining_ms"] = reference_remaining_ms(self.last_target_reference, ns(),
                (self.robot.hello or {}).get("reference_timeout_s", .12))
            timing["ik_elapsed_ms"] = (ns()-pending["started"])/1e6 if pending else None
            timing["ik_solve_ms"] = timing["ik_dispatch_wait_ms"] = None
            if pending and future.done():
                try:
                    _, started, finished = future.result()
                    timing["ik_solve_ms"] = (finished-started)/1e6
                    timing["ik_dispatch_wait_ms"] = (ns()-finished)/1e6
                except Exception:
                    pass
            result.update(feedback_diagnostics=timing, robot_connected=timing["connected"],
                          feedback_valid=timing["fresh"], feedback_age_ms=timing["age_ms"])
            if not timing["fresh"]:
                result.update(joints=None, reset_available=False)
        return result

    def _input_timing(self, value, now):
        mapped = value.get("mapped_ns")
        uncertainty = value.get("uncertainty_ns", 0)
        age = None if mapped is None else now-mapped
        limit = getattr(self.quest, "timeout", INPUT_MAX_AGE_S)*1e9
        return dict(age_ms=None if age is None else age/1e6,
                    uncertainty_ms=uncertainty/1e6,
                    expired=None if age is None else age+uncertainty > limit,
                    future=None if age is None else age < -uncertainty-INPUT_FUTURE_TOLERANCE_NS,
                    mapped_ns=mapped, received_ns=value.get("received_ns"),
                    ingress_seq=value.get("ingress_seq"), epoch=value.get("epoch"),
                    reason=value.get("reason"), valid=value.get("valid"))

    def _pause(self, reason, message_level="event", bridge_state=None):
        was_following = self.following or self.arming or self.resetting is not None
        was_scene_reset = self.resetting is not None and self.resetting.get("phase") == "scene"
        now = ns()
        details = dict(reason=reason, operator_interval_ms=self.status.get("operator_interval_ms"),
                       arm_wait_ms=self.arm_wait_ms, input=self._input_timing(self.current_input, now),
                       ik_queue_wait_ms=None, ik_call_ms=None, ik_result_wait_ms=None, ik_task_ms=None)
        info = self.future_info if self.following else None
        if info:
            timing = info["timing"]
            details["ik_input"] = self._input_timing(dict(mapped_ns=timing.get("input_mapped_ns"),
                uncertainty_ns=timing.get("input_uncertainty_ns") or 0,
                received_ns=timing.get("input_received_ns")), now)
            details["ik_task_ms"] = (now-info["started"])/1e6
            started, finished = timing.get("ik_started_ns"), timing.get("ik_finished_ns")
            if self.future is not None and self.future.done():
                try:
                    _, started, finished = self.future.result()
                except Exception:
                    pass
            details.update(ik_queue_wait_ms=None if started is None else (started-info["started"])/1e6,
                           ik_call_ms=None if started is None or finished is None else (finished-started)/1e6,
                           ik_result_wait_ms=None if finished is None else (now-finished)/1e6)
        if hasattr(self.quest, "diagnostics"):
            details["quest_transport"] = self.quest.diagnostics()
        if was_following and hasattr(self.robot, "feedback_diagnostics"):
            reference = (self.future_info or {}).get("reference") or self.last_target_reference
            details.update(self.robot.feedback_diagnostics(reference))
        # Repeated invalid input while already paused must not erase the
        # original failed IK/arm evidence before the operator can inspect it.
        if was_following or not self.status.get("last_pause_diagnostics"):
            self._status(last_pause_diagnostics=details)
        self.following = False
        self.arming = False
        self.resetting = None
        self.release_seen = False
        self.smoother.reset()
        self.ik_seed = None
        self.last_target_reference = None
        result = {"holding": False, "reason": reason}
        if was_following:
            if bridge_state is not None and not bridge_state.get("armed", True):
                # B already cancelled and changed generation. Preserve its cause;
                # a second hold would overwrite it and create a duplicate event.
                result = {key: bridge_state.get(key) for key in
                          ("generation", "holding", "reason", "pause_details")}
            else:
                try:
                    result = self.robot.hold(reason)
                except Exception as exc:
                    result["error"] = str(exc)
                if not was_scene_reset and result.get("pause_details") and result.get("reason") != reason:
                    reason = bridge_pause_message(result)
            if self.episode:
                self.episode.event("hold", cause=reason, result=result)
                if self.future_info is not None:
                    self.episode.event("ik_discarded", cause=reason,
                                       feedback_seq=self.future_info["feedback_seq"],
                                       generation=self.future_info["generation"],
                                       timing=dict(self.future_info["timing"]),
                                       finished=bool(self.future and self.future.done()))
        self._status(mode="paused", following=False, resetting=False,
                     message=reason, message_level=message_level)
        return result

    def _record_bridge_events(self, state):
        if self.episode:
            for event in state.get("events", []):
                if event["seq"] > self.last_events:
                    self.episode.event("bridge_event", event=event)
                    self.last_events = event["seq"]

    def _receipts(self):
        for receipt in self.robot.drain_receipts():
            interpolated = "execution_seq" in receipt
            info = (self.commands.get(receipt["seq"]) if interpolated
                    else self.commands.pop(receipt["seq"], None))
            if self.episode and info and info.get("recording_id") == self.episode.ident:
                if receipt.get('command_kind') == 'direct_cartesian_pose':
                    self.episode.event('submitted_pose', receipt=receipt, input=info,
                        semantics='Cartesian goal; fixed joint_bias is not a joint action')
                    continue
                command_seq = receipt.get("execution_seq", receipt["seq"])
                self.episode.command(dict(receipt, seq=command_seq), info["segment"],
                                     info["offset"], info["uncertainty"])
                self.episode.event("submitted", command_seq=command_seq,
                                   source_command_seq=receipt["seq"],
                                   submitted_q=receipt["q"],
                                   ik_seed=info.get("ik_seed"),
                                   submitted_gripper=receipt.get("gripper"),
                                   trajectory=receipt.get("trajectory"),
                                   native_cartesian=receipt.get("native_cartesian"),
                                   quest=info["quest"], target_pose=info["target"],
                                   raw_target_pose=info["raw_target"], segment=info["segment"],
                                   timing=dict(info["timing"],
                                       submitted_a_ns=receipt["b_ns"]+info["offset"],
                                       submit_clock_uncertainty_ns=info["uncertainty"],
                                       receipt_received_ns=ns()))
        while len(self.commands) > 128:
            self.commands.popitem(last=False)  # Superseded targets never become training actions.

    def _record_status(self):
        elapsed = time.monotonic()-self.record_started if self.episode else self.record_elapsed
        self._status(recording=self.episode is not None,
                     recording_phase=("recording" if self.episode else "review" if self.review else
                                      "finishing" if self.finishing else "countdown" if self.auto_recording else
                                      "complete" if self.batch and self.saved_count >= self.batch["count"] else "idle"),
                     elapsed_s=elapsed, saved_count=self.saved_count,
                     discarded_count=self.discarded_count, attempt_count=self.attempt_count,
                     saved_seconds=self.saved_seconds,
                     ready_in_s=max(0., self.next_record_at-time.monotonic()),
                     batch=self.batch, auto_recording=self.auto_recording)

    def _start_recording(self, options, input_value=None):
        if self.resetting is not None:
            raise RuntimeError("Stop arm reset or wait for it to finish before recording.")
        if self.episode or self.review or self.finishing:
            raise RuntimeError("Save or discard the current recording first")
        if time.monotonic() < self.next_record_at:
            raise RuntimeError("The interval has not ended yet")
        self.robot.snapshot()
        if self.mapping is None:
            raise RuntimeError("Calibrate before starting a recording")
        if input_value is not None and (not input_value.get("valid") or self.calibration_epoch != input_value.get("epoch")):
            raise RuntimeError("Quest input or calibration is invalid. Restore it before restarting the recording.")
        name = str(options.get("name", "")).strip()
        automatic_name = not name
        if automatic_name:
            name = time.strftime("batch_%Y%m%d_%H%M%S")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
            raise ValueError("Batch name must be 1–80 letters, numbers, underscores, dots or hyphens")
        count = int(options.get("count", 1))
        seconds = float(options.get("seconds", 60))
        pause = float(options.get("pause", 5))
        if not (1 <= count <= 10000 and 0 < seconds <= 120 and 0 <= pause <= 300):
            raise ValueError("Count: 1–10000; duration: greater than 0 and up to 120 s; interval: 0–300 s")
        batch = dict(name=name, count=count, seconds=seconds, pause=pause,
                     task=str(options.get("task", "")),
                     root=str(Path(options.get("root", self.output)).expanduser().resolve()))
        for camera in self.cameras:
            camera.latest()
        if automatic_name or batch != self.batch:
            root = Path(batch["root"])
            root.mkdir(parents=True, exist_ok=True)
            base_name, suffix = name, 0
            while True:
                destination = root/name
                try:
                    destination.mkdir(exist_ok=False)
                    break
                except FileExistsError as exc:
                    if not automatic_name:
                        raise ValueError(f"Batch folder already exists: {destination}\nChoose a different batch name or output folder and retry. Existing data will not be overwritten.") from exc
                    suffix += 1
                    name = f"{base_name}_{suffix:02d}"
            batch["name"] = name
            self.batch = batch
            self.saved_count = self.discarded_count = self.attempt_count = 0
            self.saved_seconds = 0.
        if self.saved_count >= count:
            raise RuntimeError("Batch target reached. Set a new batch name.")
        self.attempt_count += 1
        self.commands.clear()
        self.record_started = time.monotonic()
        self.record_elapsed = 0.
        self.episode = Episode(Path(batch["root"])/name, {
            "robot": self.robot.hello, "mapping": self.mapping.tolist(),
            "position_scale": self.scale, "operator_hz": self.hz,
            "motion": dict(self.smoother.description(), ik_seed="not_used_fixed_engagement_bias" if self.motion_profile == DIRECT_POSE_MODE else "previous_target"
                           if self.motion_profile == DIRECT_POSE_MODE or self.joint_protection["enabled"] or self.smoother.enabled or self.trajectory_options["enabled"] else "feedback"),
            "trajectory": self.trajectory_options,
            "joint_protection": dict(self.joint_protection),
            "motion_profile": self.motion_profile,
            "native_cartesian": (dict(self.robot.hello.get("intera_cartesian", {}),
                options=dict(self.robot.hello.get("intera_cartesian", {}).get("options", {}),
                             send_hz=CARTESIAN_RATES[self.motion_profile]))
                if self.motion_profile in CARTESIAN_RATES else None),
            "command_semantics": ("direct_cartesian_pose_events_only" if self.motion_profile == DIRECT_POSE_MODE else "cartesian_goal_joint_bias" if self.motion_profile in CARTESIAN_RATES
                                  else "joint_reference"),
            "motion_profile_status": "baseline" if self.motion_profile == "standard" else "experimental",
            "ik": {"mode": "bypassed" if self.motion_profile == DIRECT_POSE_MODE else getattr(self.robot, "ik_mode", "remote"),
                   "model_sha256": (self.robot.hello.get("kinematics") or {}).get("sha256")},
            "ik_dispatch": "bypassed" if self.motion_profile == DIRECT_POSE_MODE else "completion_wakeup_with_fresh_input_check",
            "ik_schedule": "bypassed" if self.motion_profile == DIRECT_POSE_MODE else "latest_sample_after_completion_or_minimum_interval",
            "quest_epoch": self.epoch, "backend": self.robot.hello["backend"],
            "quest_ingress_schema": 1 if hasattr(self.quest, "set_recording_sink") else None,
            "batch": batch, "attempt": self.attempt_count,
            "camera_config": [{"name": c.name, "device": c.device, "width": c.width,
                               "height": c.height, "fps": c.fps, "backend": c.backend}
                              for c in self.cameras],
        }, [c.name for c in self.cameras], fps={c.name:c.fps for c in self.cameras})
        for camera in self.cameras:
            camera.set_sink(self.episode.frame)
        self.auto_recording = True
        self.episode.event("recording_started",
                           active_engagement=self.engagement if self.following else None)
        if hasattr(self.quest, "set_recording_sink"):
            self.quest.set_recording_sink(self.episode.event)
        self._status(message="Recording started; teleop remains independent", recording_error="")

    def _stop_batch(self):
        self.auto_recording = False
        self.next_record_at = 0.
        self._status(message="Batch recording stopped; teleop remains independent")

    def _end_recording(self):
        if not self.episode:
            return
        self._receipts()
        self.record_elapsed = time.monotonic()-self.record_started
        for camera in self.cameras:
            camera.set_sink(None)
        if hasattr(self.quest, "set_recording_sink"):
            self.quest.set_recording_sink(None)
        self.episode.event("recording_ended", duration_s=self.record_elapsed)
        self.episode.seal()
        self.review, self.episode = self.episode, None
        self._status(message="Episode ended; processing data")

    def _finish(self, save):
        if self.finishing:
            return
        self._end_recording()
        if self.review:
            self.review.finish(save, {"holding": False, "capture_closed": True,
                                      "reason": "explicit_recording_end"})
            self.finishing, self.review = self.review, None
            self.finish_save = save
            self.next_record_at = time.monotonic()+self.batch["pause"] if self.auto_recording else 0.
            self._status(message="Saving" if save else "Discarding")

    def _finish_poll(self):
        if self.finishing and self.finishing.done.is_set():
            finished, self.finishing = self.finishing, None
            if finished.result == "discarded":
                self.discarded_count += 1
            elif finished.result and not finished.error:
                self.saved_count += 1
                self.saved_seconds += self.record_elapsed
            if finished.error or not finished.result or self.saved_count >= self.batch["count"]:
                self.auto_recording = False
                self.next_record_at = 0.
            if finished.result == "discarded":
                self._status(message="Episode discarded"+("; batch stopped due to a recording error" if finished.error else ""),
                             last_result="discarded", recording_error="")
            elif finished.error or not finished.result:
                if finished.result and self.finish_save:
                    message = "Incomplete data retained but not counted; batch stopped: "+str(finished.error)
                else:
                    action = "Save" if self.finish_save else "Discard"
                    message = action+" failed; batch stopped: "+str(finished.error or "File processing returned no result")
                self._record_error(message)
                self._status(last_result=str(finished.result))
            else:
                message = ("Batch target reached" if self.saved_count >= self.batch["count"] else
                           "Episode saved" if self.auto_recording else "Episode saved; batch stopped")
                self._status(message=message, last_result=str(finished.result), recording_error="")
            try:
                destination = Path(self.batch["root"])/self.batch["name"]
                temporary = destination/"batch.json.tmp"
                temporary.write_text(json.dumps(dict(self.batch, saved=self.saved_count,
                    discarded=self.discarded_count, attempts=self.attempt_count,
                    saved_seconds=self.saved_seconds), ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(destination/"batch.json")
            except OSError as exc:
                self.auto_recording = False
                self.next_record_at = 0.
                self._record_error("Could not write batch index; batch stopped: "+str(exc))

    def _calibrate(self, input_value):
        if self.following:
            raise RuntimeError("Release grip before calibrating")
        if not input_value.get("valid"):
            raise RuntimeError(input_value["reason"])
        self.mapping = heading_mapping(input_value["head_forward"])
        self.calibration_epoch = input_value["epoch"]
        self.release_seen = False
        self._status(message="Calibrated. Release grip, then squeeze to start.", calibrated=True,
                     calibration_result="Calibrated · "+time.strftime("%H:%M:%S"))
        if self.episode:
            self.episode.event("calibrated", mapping=self.mapping.tolist())

    def _engage(self, value, state):
        if self.mapping is None or self.calibration_epoch != value["epoch"]:
            raise RuntimeError("Look along the robot base +X direction and click Calibrate.")
        if self.future and not self.future.done():
            raise RuntimeError("Wait for the previous IK request to finish before squeezing grip again.")
        self.future = self.future_info = None
        self.last_ik_sample = None
        for camera in self.cameras:
            camera.latest()
        if (self.selected_motion_profile in CARTESIAN_RATES or self.selected_motion_profile == "fast_follow") and self.motion_profile != self.selected_motion_profile:
            raise RuntimeError(self.view().get("motion_profile_unavailable_reason") or "Intera Cartesian unavailable")
        arm_started = ns()
        # Mark the arm attempt for fail-closed cleanup even if arm() raises.
        self.arming = True
        self.release_seen = False
        try:
            self._arm_for_engagement()
        finally:
            self.arm_wait_ms = (ns()-arm_started)/1e6
        state = self.robot.snapshot()
        # arm() waits for B's acknowledgement/telemetry. Never carry the
        # pre-arm controller pose or lifetime across this blocking boundary.
        value = self.quest.poll()
        self.current_input = value
        with self.lock:
            self.requests.extend(value.get("events", []))
        if self.stop.is_set():
            raise RuntimeError("Stopped during arm confirmation")
        if not value.get("valid"):
            raise RuntimeError(value.get("reason", "quest_stale"))
        if value["epoch"] != self.calibration_epoch:
            self.epoch = value["epoch"]
            self.mapping = None
            self._status(calibrated=False, calibration_result="Reference frame changed; recalibration required")
            raise RuntimeError("Quest reference frame changed; recalibrate")
        timing = self._input_timing(value, ns())
        if timing["expired"] or timing["future"]:
            raise RuntimeError("quest_stale")
        if value["grip"] <= .7:
            raise RuntimeError("Grip released during arm confirmation; release grip, then squeeze again.")
        if state["generation"] != self.robot.generation or not state.get("armed", False):
            raise RuntimeError("Arm confirmation lost before engagement")
        self.robot_origin, self.vr_origin = matrix(state["sample"]["pose"]), value["transform"].copy()
        self.smoother.reset(state["sample"]["pose"], input_stamp(value))
        self.ik_seed = list(state["sample"]["q"])
        self.arming = False
        self.following = True
        self.segment += 1
        self.engagement = {"input": copy.deepcopy(input_evidence(value)),
                           "feedback": copy.deepcopy({key: state[key] for key in (
                               "session", "b_ns", "generation", "a_received_ns", "a_frame_received_ns",
                               "clock_offset_ns", "clock_uncertainty_ns", "sample") if key in state}),
                           "robot_origin": self.robot_origin.tolist(),
                           "vr_origin": self.vr_origin.tolist(),
                           "mapping": self.mapping.tolist(), "position_scale": self.scale,
                           "segment": self.segment, "generation": self.robot.generation}
        if self.episode:
            self.episode.event("engaged", **self.engagement)
        self._status(mode="following", message="Teleop active", quest=describe_input(value))
        return value, state

    def _arm_for_engagement(self):
        if self.motion_profile == DIRECT_POSE_MODE:
            self.robot.arm(execution_mode=DIRECT_POSE_MODE, cartesian_send_hz=50, joint_protection=self.joint_protection)
            self.direct_next_ns = 0
            self.direct_last_sample = None
        elif self.motion_profile in CARTESIAN_RATES:
            self.robot.arm(execution_mode="intera_cartesian", cartesian_send_hz=CARTESIAN_RATES[self.motion_profile],
                           joint_protection=self.joint_protection)
        elif self.trajectory_options["enabled"]:
            self.robot.arm(trajectory=self.trajectory_options, joint_protection=self.joint_protection)
        else:
            self.robot.arm(joint_protection=self.joint_protection)

    def _target_pose(self, value):
        vr = value["transform"]
        target = self.robot_origin.copy()
        target[:3, 3] += self.scale*self.mapping@(vr[:3, 3]-self.vr_origin[:3, 3])
        target[:3, :3] = (self.mapping@vr[:3, :3]@self.vr_origin[:3, :3].T
                          @self.mapping.T@self.robot_origin[:3, :3])
        return {"p": target[:3, 3].tolist(),
                "q": Rotation.from_matrix(target[:3, :3]).as_quat().tolist()}

    def _start_reset(self):
        if self.episode or self.review or self.finishing or self.auto_recording:
            raise ValueError("Finish saving or discarding and end the recording batch before Reset arm.")
        q = neutral_target(self.robot.hello.get("neutral_pose"))
        if self.robot.hello.get("backend") == "gazebo":
            scene = self.robot.hello.get("scene_reset") or {}
            if not scene.get("available"):
                raise ValueError(scene.get("reason") or "Scene reset requires an updated Gazebo B. Update B and reconnect.")
        if self.resetting is not None:
            return
        self._pause("Preparing arm reset")
        self.resetting = {"q": q, "started_ns": ns(), "next_send_ns": 0,
                          "settled_ns": None}
        self._status(mode="resetting", following=False, resetting=True,
                     message="Returning to neutral. Click Stop to cancel; controller grip is inactive.")
        # A fixed destination uses one shared-duration curve for all joints.
        # Keep the configured limits even when teleop interpolation is disabled.
        self.robot.arm(trajectory=dict(self.reset_trajectory_options, enabled=True, profile="cubic"),
                       joint_protection=self.joint_protection, motion_purpose="neutral_reset")
        self.commands.clear()

    def _reset_tick(self, state):
        if self.stop.is_set() or self.reset_cancel.is_set():
            self.reset_cancel.clear()
            self._pause("Arm reset stopped. Release grip, then squeeze to resume teleop.")
            return
        if self.resetting.get("phase") == "scene":
            if ns()-self.resetting["started_ns"] > 5_000_000_000:
                raise TimeoutError("Scene reset timed out; scene may be partially restored.")
            if state["generation"] < self.robot.generation:
                # A reliable hold/reset reply can overtake the latest-state slot.
                return
            if state["generation"] != self.robot.generation or state.get("armed", False):
                raise RuntimeError("Scene reset lost the arm hold.")
            result = state.get("scene_reset") or {}
            if result.get("id") == self.resetting["scene_id"]:
                if result.get("state") == "failed":
                    raise RuntimeError("Scene reset failed: " + result.get("error", "unknown error"))
                if result.get("state") == "completed":
                    self._pause("Arm is at neutral; scene restored. Release grip, then squeeze to resume teleop.",
                                bridge_state=state)
                    return
            return
        if state["generation"] != self.robot.generation or not state.get("armed", False):
            self._pause(bridge_pause_message(state), "error", bridge_state=state)
            return
        now = ns()
        reset = self.resetting
        if now-reset["started_ns"] > NEUTRAL_TIMEOUT_S*1e9:
            self._pause("Arm reset timed out before reaching neutral.", "error")
            return
        sample = state["sample"]
        reached = (max(abs(a-b) for a,b in zip(sample["q"], reset["q"])) < NEUTRAL_TOLERANCE_RAD
                   and max(abs(v) for v in sample["dq"]) < .02)
        if not reached:
            reset["settled_ns"] = None
        elif reset["settled_ns"] is None:
            reset["settled_ns"] = now
            reset["settled_seq"] = sample["seq"]
        elif now-reset["settled_ns"] >= 100_000_000 and sample["seq"] != reset["settled_seq"]:
            held = self._pause("Arm is at neutral. Release grip, then squeeze to resume teleop.")
            if self.robot.hello.get("backend") == "gazebo":
                if not held.get("holding"):
                    raise RuntimeError("Scene reset requires a confirmed arm hold.")
                if self.stop.is_set() or self.reset_cancel.is_set():
                    return
                self.resetting = {"phase": "scene", "started_ns": ns(), "scene_id": None}
                self._status(mode="resetting", resetting=True, message="Arm is at neutral; restoring scene objects.")
                self.resetting["scene_id"] = self.robot.reset_scene()["id"]
            return
        if now >= reset["next_send_ns"]:
            self.robot.target(reset["q"], sample["seq"])
            reset["next_send_ns"] = now + 20_000_000

    def _solve_timed(self, target, seed, feedback_seq):
        started = ns()
        q = self.robot.solve(target, seed, feedback_seq)
        return q, started, ns()

    def tick(self, queue_ik=True):
        if self.stop.is_set():
            return
        now = ns()
        self._status(operator_interval_ms=None if self.last_tick_ns is None else (now-self.last_tick_ns)/1e6)
        self.last_tick_ns = now
        value = self.quest.poll()
        self.current_input = value
        diagnostics = describe_input(value)
        buttons = tuple(diagnostics["hands"][side]["buttons"] for side in ("l", "r"))
        if buttons != self.last_buttons:
            with self.lock:
                self.ui_event_seq += 1
                labels = []
                for bits, names in zip(buttons, (("Left X", "Left Y", "Left stick"), ("Right A", "Right B", "Right stick"))):
                    labels.extend(name+":"+("unknown" if bits is None else ("pressed" if bits & mask else "released"))
                                  for name, mask in zip(names, (1, 2, 4)))
                self.ui_events.append({"seq": self.ui_event_seq, "time": time.strftime("%H:%M:%S"),
                                       "text": "Buttons · "+" / ".join(labels)})
            self.last_buttons = buttons
        self._status(quest=diagnostics, following=self.following,
                     calibrated=bool(self.mapping is not None and value.get("valid")
                                     and self.calibration_epoch == value.get("epoch")),
                     calibration_exists=self.mapping is not None)
        with self.lock:
            actions = list(self.requests)
            self.requests.clear()
        parameters = {}
        normalized = []
        for item in actions:
            if isinstance(item, tuple):
                action, options = item
                parameters[action] = options
                normalized.append(action)
            else:
                normalized.append(item)
        actions = normalized + value.get("events", [])
        if self.reset_cancel.is_set():
            self.reset_cancel.clear()
            if self.resetting is not None or "reset_arm" in actions:
                self._pause("Arm reset stopped. Release grip, then squeeze to resume teleop.")
                self.reset_requested.clear()
                return
        self._finish_poll()
        if "set_motion_profile" in actions:
            options = parameters.get("set_motion_profile", {})
            result = {"request_id": options.get("request_id"), "accepted": False}
            try:
                self._change_motion_profile(options.get("profile"), value, options.get("joint_protection"))
                result["accepted"] = True
            except (ValueError, RuntimeError) as exc:
                result["error"] = str(exc)
                self._status(message=str(exc), message_level="warning")
            result["profile"] = self.motion_profile
            if "joint_protection" in options:
                result["joint_protection"] = self.joint_protection["enabled"]
            self._status(motion_profile_result=result)
        try:
            if "stop_batch" in actions or "end_recording" in actions:
                # Cancel continuation before saving; also covers a countdown click
                # arriving just after the next capture has started.
                self._stop_batch()
                self._finish(True)
            elif "save" in actions or "discard" in actions:
                self._finish("save" in actions)
            elif "start_recording" in actions:
                self._start_recording(parameters.get("start_recording", {}), value)
            if self.episode and time.monotonic()-self.record_started >= self.batch["seconds"]:
                self._finish(True)
            if (self.auto_recording and not self.episode and not self.review and not self.finishing
                    and time.monotonic() >= self.next_record_at):
                self._start_recording(self.batch, value)
        except Exception as exc:
            self.auto_recording = False
            self.next_record_at = 0.
            self._record_error("Recording operation failed; batch stopped: "+str(exc))
        self._record_status()
        try:
            state = self.robot.snapshot()
            self.robot.gripper_observation(diagnostics["hands"]["r"]["trigger"])
            capability = self.robot.hello.get("neutral_pose") or {}
            if self.robot.hello.get("backend") == "gazebo" and capability.get("available"):
                scene = self.robot.hello.get("scene_reset") or {}
                if not scene.get("available"):
                    capability = {"available": False, "reason": scene.get("reason") or
                        "Scene reset requires an updated Gazebo B. Update B and reconnect."}
            self._status(reset_available=bool(capability.get("available")
                         and not (self.episode or self.review or self.finishing or self.auto_recording)),
                         reset_unavailable_reason=capability.get("reason", "Update B to provide its official neutral pose."))
            self._status(robot_connected=True, backend=self.robot.hello.get("backend", "unknown"),
                         native_stopping=state.get("native_stopping", False),
                         native_stop_failed=state.get("native_stop_failed", False),
                         feedback_age_ms=round(state["age_s"]*1000, 1),
                         rtt_ms=round(self.robot.rtt_ms or 0, 1),
                         joints=state["sample"]["q"],
                         recording=self.episode is not None)
            if self.episode:
                self.episode.state(state)
                self.episode.event("quest_input", packet=value.get("packet", {}),
                    input={k: v for k, v in input_evidence(value).items() if k != "packet"})
                self._record_bridge_events(state)
                if self.episode.error:
                    self.auto_recording = False
                    self.next_record_at = 0.
                    message = "Recording write failed. Save the incomplete data or discard this episode: "+self.episode.error
                    if self.view().get("recording_error") != message:
                        self._record_error(message)
                    raise RuntimeError(self.episode.error)
            self._receipts()
            if "reset_arm" in actions:
                try:
                    self._start_reset()
                except ValueError as exc:
                    if self.resetting is not None:
                        self._pause(str(exc), "error")
                    else:
                        self._status(message=str(exc), message_level="warning")
                    return
                state = self.robot.snapshot()
            if self.resetting is not None:
                self._reset_tick(state)
                return
            if not value.get("valid"):
                raise RuntimeError(value["reason"])
            if value["epoch"] != self.epoch:
                self.epoch = value["epoch"]
                self.mapping = None
                self._status(calibrated=False, calibration_result="Reference frame changed; recalibration required")
                self._pause("Quest reference frame changed; recalibrate")
            if "calibrate" in actions:
                self._calibrate(value)
            if self.following and state["generation"] != self.robot.generation:
                self._pause(bridge_pause_message(state), "error", bridge_state=state)
                return
            if value["grip"] < .2:
                if self.following:
                    self._pause("Grip released; recording continues" if self.episode else "Grip released; not recording")
                self.release_seen = True
            elif value["grip"] > .7 and self.release_seen:
                value, state = self._engage(value, state)
                diagnostics = describe_input(value)
            if not self.following:
                return
            for camera in self.cameras:
                camera.latest()
            if "reset_orientation" in actions:
                self.vr_origin[:3, :3] = value["transform"][:3, :3]
                self.robot_origin[:3, :3] = matrix(state["sample"]["pose"])[:3, :3]
                # Drop any result based on the previous orientation origin.
                self._pause("Direction reset. Release grip, then squeeze again.")
                return
            raw_target = self._target_pose(value)
            target = self.smoother.update(raw_target, input_stamp(value))
            if self.motion_profile == DIRECT_POSE_MODE:
                self._direct_pose_tick(value, state, target, raw_target)
                return
            if self.future is not None:
                if not self.future.done():
                    if ns()-self.future_info["started"] > IK_TASK_TIMEOUT_NS:
                        raise RuntimeError("ik_deadline")
                    return
                future, info = self.future, self.future_info
                q, started, finished = future.result()
                if info["generation"] != self.robot.generation or ns()-info["started"] > IK_TASK_TIMEOUT_NS:
                    raise RuntimeError("ik_result_expired")
                info["timing"].update(ik_started_ns=started, ik_finished_ns=finished)
                self._status(last_ik_solve_ms=(finished-started)/1e6,
                             last_ik_dispatch_wait_ms=(ns()-finished)/1e6)
                # An old solved input cannot borrow the lifetime of a newer poll.
                mapped = info["timing"].get("input_mapped_ns")
                uncertainty = info["timing"].get("input_uncertainty_ns")
                if mapped is not None and uncertainty is not None:
                    age = ns()-mapped
                    if input_age_expired(age, uncertainty,
                                         getattr(self.quest, "timeout", INPUT_MAX_AGE_S)*1e9):
                        raise RuntimeError("ik_input_expired")
                if self.stop.is_set():
                    return
                remaining = reference_remaining_ms(info.get("reference"), ns(),
                    self.robot.hello.get("reference_timeout_s", .12))
                if remaining is not None and remaining < 0:
                    # New telemetry cannot renew the original solved reference.
                    raise RuntimeError("ik_reference_expired")
                info["timing"]["target_sent_ns"] = ns()
                trigger = diagnostics["hands"]["r"]["trigger"]
                gripper = (trigger_command(trigger)
                           if self.robot.hello.get("gripper_control") else None)
                source_timing = ({"source_sample_ns": info["source_sample_ns"]}
                                 if self.trajectory_options.get("profile") in ("responsive", "coordinated") else {})
                seq = self.robot.target(q, info["feedback_seq"], gripper=gripper, **source_timing)
                self.commands[seq] = info
                self.last_target_reference = info.get("reference")
                self.ik_seed = list(q)
                self.future = self.future_info = None
            if not queue_ik:
                return
            queued = ns()
            sample_stamp = input_stamp(value)
            # Completion may start the next solve immediately once the interval
            # has elapsed. Never queue a backlog or solve the same sample twice.
            if queued < self.next_ik_ns or sample_stamp == self.last_ik_sample:
                return
            info = {"recording_id": self.episode.ident if self.episode else None,
                    "generation": self.robot.generation, "started": queued,
                    "feedback_seq": state["sample"]["seq"], "segment": self.segment,
                    "reference": (reference_from_snapshot(state) if "a_received_ns" in state else None),
                    "offset": state["clock_offset_ns"], "uncertainty": state["clock_uncertainty_ns"],
                    "quest": value.get("packet", {}), "target": target, "raw_target": raw_target,
                    "source_sample_ns": sample_stamp,
                    "timing": {"input_received_ns": value.get("received_ns"),
                        "input_mapped_ns": value.get("mapped_ns"),
                        "input_uncertainty_ns": value.get("uncertainty_ns"),
                        "ik_queued_ns": queued, "feedback_age_ms": state["age_s"]*1000,
                        "network_rtt_ms": self.robot.rtt_ms}}
            self.future_info = info
            seed = (self.ik_seed if (self.joint_protection["enabled"] or self.smoother.enabled or self.trajectory_options["enabled"])
                    and self.ik_seed is not None else state["sample"]["q"])
            info["ik_seed"] = list(seed)
            self.future = self.executor.submit(self._solve_timed, target, seed,
                                               info["feedback_seq"])
            self.next_ik_ns = queued + self.ik_period_ns
            self.last_ik_sample = sample_stamp
            self.future.add_done_callback(lambda _future: self.wake.set())
        except Exception as exc:
            if "calibrate" in actions:
                self._status(calibration_result="Calibration failed: "+str(exc))
            # Even stale feedback can carry the B pause cause and captured ages.
            # Never use this unvalidated snapshot to authorize motion.
            bridge_state = None
            try:
                cached = self.robot.snapshot(require_fresh=False)
                self._record_bridge_events(cached)
                if ((self.following or self.arming or self.resetting is not None) and not cached.get("armed", True)
                        and cached.get("generation") != self.robot.generation):
                    bridge_state = cached
            except Exception:
                pass
            self._pause(bridge_pause_message(bridge_state) if bridge_state else str(exc),
                        message_level="error", bridge_state=bridge_state)
            try:
                self.robot.snapshot()
            except Exception:
                # Freshness is not socket liveness. view() reads diagnostic ages
                # on demand, even if this control loop is waiting for a hold reply.
                self._status(feedback_valid=False, joints=None)
        finally:
            if "reset_arm" in actions:
                self.reset_requested.clear()

    def _direct_pose_tick(self, value, state, target, raw_target):
        now = ns()
        stamp = input_stamp(value)
        if stamp == self.direct_last_sample:
            return
        mapped, uncertainty = value.get('mapped_ns'), value.get('uncertainty_ns')
        if type(mapped) is not int or type(uncertainty) is not int or uncertainty < 0:
            raise RuntimeError('direct_pose_input_clock_unavailable')
        if input_age_expired(now-mapped, uncertainty, INPUT_MAX_AGE_S*1e9):
            raise RuntimeError('quest_stale')
        trigger = value['packet']['hands']['r']['trigger']
        gripper = trigger_command(trigger) if self.robot.hello.get('gripper_control') else None
        seq = self.robot.pose_target(target, state['sample']['seq'],
            mapped + int(INPUT_MAX_AGE_S*1e9) - uncertainty, stamp, gripper)
        self.commands[seq] = dict(recording_id=self.episode.ident if self.episode else None,
            segment=self.segment, offset=state['clock_offset_ns'], uncertainty=state['clock_uncertainty_ns'],
            quest=value.get('packet', {}), target=target, raw_target=raw_target,
            timing=dict(input_mapped_ns=mapped, input_uncertainty_ns=uncertainty, target_sent_ns=now))
        while len(self.commands) > 128:
            self.commands.popitem(last=False)
        self.direct_last_sample = stamp
        # B alone owns the 50 Hz SDK cadence. Two independent 50 Hz gates lose updates.

    def _select_trajectory_options(self):
        # Backend-specific fast tuning requires a matching confirmed handshake.
        # Reset arm continues to use the original limits on every backend.
        simulated = (getattr(self.robot, "expected_backend", None) == "gazebo"
                     and (self.robot.hello or {}).get("backend") == "gazebo")
        options = self.base_trajectory_options
        self.motion_profile = "standard"
        if simulated and self.selected_motion_profile == "fast_follow" and self.gazebo_trajectory_options is not None:
            options = self.gazebo_trajectory_options
            self.motion_profile = "fast_follow"
        real = (getattr(self.robot, "expected_backend", None) == "intera"
                and (self.robot.hello or {}).get("backend") == "intera")
        if real and self.selected_motion_profile == "fast_follow" and self.motion_profile_explicit:
            options = self.intera_fast_trajectory_options
            self.motion_profile = "fast_follow"
        capability = (self.robot.hello or {}).get("intera_cartesian", {})
        reason = motion_profile_reason(self.selected_motion_profile,
                                       (self.robot.hello or {}).get("backend"), capability)
        if self.selected_motion_profile == 'fast_follow' and not reason:
            if not (simulated or real):
                reason = 'Fast follow requires a confirmed backend connection.'
            elif real and not self.motion_profile_explicit:
                reason = 'Select Fast follow explicitly for Real Sawyer.'
            elif simulated and self.gazebo_trajectory_options is None:
                reason = 'Fast follow requires configured Gazebo acceleration limits.'
        if self.selected_motion_profile in CARTESIAN_RATES and not reason:
            if not (real or (simulated and capability.get("simulated") is True)):
                reason = "Intera Cartesian requires a confirmed Real Sawyer or simulated Motion Controller connection."
            else:
                self.motion_profile = self.selected_motion_profile
                options = dict(self.base_trajectory_options, enabled=False)
        self.trajectory_options = trajectory_options(options)
        self._status(joint_protection=dict(self.joint_protection),
                     motion_profile=self.motion_profile, intera_cartesian=capability,
                     motion_profile_unavailable_reason=reason)

    def _change_motion_profile(self, profile, value, joint_protection=None):
        guard = (self.joint_protection if joint_protection is None else protection_options(joint_protection))
        if profile not in MOTION_PROFILES:
            raise ValueError("Unknown motion profile: " + str(profile))
        selected_motion_profile(profile, self.gazebo_trajectory_options)
        backend = self.robot.hello.get("backend")
        if getattr(self.robot, "expected_backend", None) != backend:
            raise ValueError("Motion profile requires a confirmed backend connection.")
        reason = motion_profile_reason(profile, backend, self.robot.hello.get("intera_cartesian"))
        if profile == 'fast_follow' and backend == 'gazebo' and self.gazebo_trajectory_options is None:
            reason = 'Fast follow requires configured Gazebo acceleration limits.'
        if reason:
            raise ValueError(reason)
        if (self.following or self.resetting is not None or self.reset_requested.is_set()
                or self.episode or self.review or self.finishing or self.auto_recording):
            raise ValueError("Release grip, stop arm reset and end recording before switching motion profile.")
        if not value.get("valid") or value.get("grip", 1.) >= .2:
            raise ValueError("Release the right grip with valid Quest tracking before switching motion profile.")
        state = self.robot.snapshot()
        if state.get("armed", True) or state.get("native_stopping") or state.get("native_stop_failed"):
            raise ValueError("Wait for B to confirm hold before switching motion profile.")
        self.joint_protection = guard
        self.motion_profile_explicit = True
        self.selected_motion_profile = profile
        self._select_trajectory_options()
        # No arm/target is issued here. The next grip engagement passes the selected
        # limits through the existing arm protocol, with fresh origins and generation.
        self._status(message="Motion profile applied: " + MOTION_PROFILES[profile][0])

    def _run(self):
        try:
            self.process_lock = ProcessLock("data:"+str(self.output.resolve())).acquire()
            self.robot.connect()
            # Latch a completed B handshake, even if Quest startup later fails.
            # A live transport alone is not proof that connect() succeeded.
            self._status(connected_host=self.robot.host)
            self._select_trajectory_options()
            self._status(robot_connected=True, backend=self.robot.hello.get("backend", "unknown"),
                         message="B connected; starting Quest input")
            self.quest.start()
            for camera in self.cameras:
                camera.start()
            self._status(mode="paused", message="Look along base +X and calibrate")
            deadline = ns()
            while not self.stop.is_set():
                # Clear before checking the future/requests so no completion is lost.
                self.wake.clear()
                scheduled = ns() >= deadline
                self.tick()
                now = ns()
                if scheduled:
                    period_ns = min(self.ik_period_ns, 20_000_000) if self.resetting is not None else self.ik_period_ns
                    deadline += period_ns
                    if deadline <= now:
                        deadline = now + period_ns
                wake_at = deadline
                if self.resetting is not None and "next_send_ns" in self.resetting:
                    # Reset renews its lease even when teleop IK is configured slowly.
                    wake_at = min(wake_at, self.resetting["next_send_ns"])
                # Keep polling grip/health while IK runs. If a fast solve finished
                # early, wake exactly when the next solve is eligible, not at the
                # next fixed polling tick. Overdue/busy IK must not cause a spin.
                if self.following and self.future is None and now < self.next_ik_ns:
                    wake_at = min(wake_at, self.next_ik_ns)
                self.wake.wait(max(0, (wake_at-ns())/1e9))
        except Exception as exc:
            self.error = str(exc)
            self._pause(self.error, message_level="error")
            self._status(mode="error", message=self.error, message_level="error")
        finally:
            # Unexpected exits preserve incomplete data for explicit recovery, never train by default.
            if self.resetting is not None:
                self._pause("Arm reset stopped: operator disconnected")
            if self.review:
                self.review.finish(True, {"holding": False, "reason": "operator_shutdown"})
                self.finishing, self.review = self.review, None
            if self.episode:
                self._pause("operator_shutdown")
                for camera in self.cameras:
                    camera.set_sink(None)
                if hasattr(self.quest, "set_recording_sink"):
                    self.quest.set_recording_sink(None)
                self.episode.finish(True, {"holding": False, "reason": "operator_shutdown"})
                self.finishing, self.episode = self.episode, None
            self.reset_requested.clear()
            self._status(robot_connected=False, following=False, calibrated=False, joints=None,
                         calibration_result="Session ended; recalibrate after reconnecting")
            self.robot.close()
            self.quest.close()
            for camera in self.cameras:
                camera.close()
            self.executor.shutdown(wait=False, cancel_futures=True)
            if self.finishing:
                self.finishing.done.wait(10)
            if self.process_lock:
                self.process_lock.close()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)
