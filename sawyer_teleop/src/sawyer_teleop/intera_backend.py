"""Minimal ROS/Intera adapter; imports ROS only when explicitly selected on B."""
import json
import math
import os
import threading
from urllib.parse import urlparse

from .protocol import JOINTS, ns, pose, vector
from .neutral import read_neutral_pose
from .kinematics_model import read_kinematics_model


# Bounded robot/B clock skew allowance for the lab demo. Motion references
# retain their original monotonic deadlines; this is not clock correction.
FEEDBACK_FUTURE_TOLERANCE_S = .05


class InteraBackend:
    name = "intera"

    def __init__(self, tip_frame="right_hand", state_timeout=.1, status_timeout=.5,
                 gripper_port=None, gripper_command_timeout=.25):
        import rospy
        from sensor_msgs.msg import JointState
        from intera_core_msgs.msg import (
            EndpointState, RobotAssemblyState, CollisionDetectionState,
            IODeviceStatus, JointCommand)
        from intera_core_msgs.srv import SolvePositionIK, SolvePositionIKRequest
        if not urlparse(os.environ.get("ROS_MASTER_URI", "")).hostname:
            raise RuntimeError("Load the existing lab ROS environment first")
        if not rospy.core.is_initialized():
            rospy.init_node("sawyer_teleop_bridge", anonymous=True, disable_signals=True)
        if rospy.get_param("/use_sim_time", False):
            raise RuntimeError("Real robot backend cannot use a simulation clock")
        active_tip = rospy.get_param("/robot/limb/right/tip_name", tip_frame)
        if active_tip != tip_frame:
            raise RuntimeError("Tool frame mismatch: " + str(active_tip))
        self.tip_frame = tip_frame
        self.ros = rospy
        self.lock = threading.RLock()
        self.state_timeout = state_timeout
        self.status_timeout = status_timeout
        self.q = self.dq = self.endpoint = None
        self.seq = 0
        self.stamps, self.received, self.captured, self.invalid = {}, {}, {}, {}
        self.robot = None
        self.cuff = self.collision = None
        self.command_class = JointCommand
        self.request_class = SolvePositionIKRequest
        self.publisher = rospy.Publisher(
            "/robot/limb/right/joint_command", JointCommand, queue_size=1, tcp_nodelay=True)
        self.version = rospy.get_param("/manifest/robot_software/version/HLR_VERSION_STRING", "unknown")
        self.subscribers = [
            rospy.Subscriber("/robot/joint_states", JointState, self._joint, queue_size=1,
                             tcp_nodelay=True),
            rospy.Subscriber("/robot/limb/right/endpoint_state", EndpointState,
                             self._endpoint, queue_size=1, tcp_nodelay=True),
            rospy.Subscriber("/robot/state", RobotAssemblyState, self._robot, queue_size=1),
            rospy.Subscriber("/robot/limb/right/collision_detection_state",
                             CollisionDetectionState, self._collision, queue_size=1),
            rospy.Subscriber("/io/robot/cuff/state", IODeviceStatus, self._cuff, queue_size=1),
        ]
        self.cuff_subscriber = self.subscribers[-1]
        self.neutral_pose = read_neutral_pose(rospy.get_param)
        self.kinematics = read_kinematics_model(rospy.get_param("/robot_description", ""), tip_frame)
        from .intera_cartesian import discover_cartesian
        self.cartesian, self.cartesian_capability = discover_cartesian(rospy, tip_frame)
        # Reuse one connection: a per-call proxy added master lookup and TCP
        # setup to every remote IK request (about 50 Hz targets on the lab robot).
        from .ros_service import PersistentService
        self.ik = PersistentService(rospy, "/ExternalTools/right/PositionKinematicsNode/IKService",
                                    SolvePositionIK)
        self.gripper = None
        # No serial access unless explicitly configured. Keep capability absent
        # for older installations with no external gripper.
        if gripper_port:
            from .dynamixel_gripper import DynamixelGripper
            try:
                self.gripper = DynamixelGripper(gripper_port, gripper_command_timeout)
            except Exception:
                self.close()
                raise
            self.publish_gripper = self.gripper.command
            self.hold_gripper = self.gripper.hold

    def description(self):
        return {"model": "Sawyer", "intera_version": self.version,
                "gripper": getattr(self, "gripper", None) is not None,
                "gripper_model": "Dynamixel external" if getattr(self, "gripper", None) else None,
                "command_mode": "POSITION_MODE"}

    def _accept(self, name, stamp, update):
        with self.lock:
            source_now = self.ros.Time.now().to_sec()
            age = source_now - stamp
            limit = self.state_timeout if name in ("joint", "pose") else self.status_timeout
            if (not math.isfinite(source_now) or not math.isfinite(stamp) or stamp <= 0
                    or stamp > source_now + FEEDBACK_FUTURE_TOLERANCE_S or age > limit):
                self.invalid[name] = name + "_source_time_invalid"
                return
            previous = self.stamps.get(name, -1)
            if stamp <= previous:
                if stamp < previous:
                    self.invalid[name] = name + "_source_time_regressed"
                return
            update()
            self.stamps[name] = stamp
            self.received[name] = ns()
            # Keep the raw source stamp for ordering/diagnostics, but never
            # create a future monotonic capture or extend the stale deadline.
            self.captured[name] = self.received[name]-int(max(0., age)*1e9)
            self.invalid.pop(name, None)

    def _joint(self, msg):
        try:
            if len(msg.name) != len(set(msg.name)):
                raise ValueError("duplicate joints")
            positions, velocities = dict(zip(msg.name, msg.position)), dict(zip(msg.name, msg.velocity))
            q = vector([positions[n] for n in JOINTS], 7)
            dq = vector([velocities[n] for n in JOINTS], 7)
            def update():
                self.q, self.dq = q, dq
                self.seq += 1
            self._accept("joint", msg.header.stamp.to_sec(), update)
        except (ValueError, TypeError, KeyError):
            with self.lock:
                self.invalid["joint"] = "joint_invalid"

    def _endpoint(self, msg):
        try:
            # The official right endpoint_state topic is base-relative. Some
            # Intera versions leave frame_id empty (the SDK Limb accepts it).
            frame = msg.header.frame_id
            if not msg.valid or (frame != "" and frame.lstrip("/") != "base"):
                raise ValueError("endpoint invalid")
            p, q = msg.pose.position, msg.pose.orientation
            value = pose({"p": [p.x, p.y, p.z], "q": [q.x, q.y, q.z, q.w]})
            self._accept("pose", msg.header.stamp.to_sec(),
                         lambda: setattr(self, "endpoint", value))
        except (ValueError, TypeError):
            with self.lock:
                self.invalid["pose"] = "endpoint_invalid"

    def _robot(self, msg):
        with self.lock:
            self.robot = {
                "enabled": bool(msg.enabled), "ready": bool(msg.ready),
                "homed": bool(msg.homed), "low_voltage": bool(msg.lowVoltage),
                "stopped": bool(msg.stopped), "error": bool(msg.error),
                "estop": bool(msg.estop_button or msg.estop_source),
                "estop_button": int(msg.estop_button),
                "estop_source": int(msg.estop_source),
            }
            self.received["robot"] = ns()

    def _collision(self, msg):
        self._accept("collision", msg.header.stamp.to_sec(),
                     lambda: setattr(self, "collision", bool(msg.collision_state)))

    def _cuff(self, msg):
        try:
            signal = next(v for v in msg.signals if v.name == "right_cuff")
            value = json.loads(signal.data)
            if signal.status.tag != "ready" or value not in ([0], [1], [False], [True]):
                raise ValueError()
            # IODeviceStatus is cached/event-driven in Intera's IOInterface;
            # its creation time is not a periodic feedback heartbeat.
            stamp = msg.time.to_sec()
            with self.lock:
                source_now = self.ros.Time.now().to_sec()
                if (not math.isfinite(source_now) or not math.isfinite(stamp) or stamp <= 0
                        or stamp > source_now + FEEDBACK_FUTURE_TOLERANCE_S):
                    self.invalid["cuff"] = "cuff_source_time_invalid"
                    return
                previous = self.stamps.get("cuff", -1)
                if stamp < previous:
                    self.invalid["cuff"] = "cuff_source_time_regressed"
                    return
                if stamp == previous and self.cuff is not None and self.cuff != bool(value[0]):
                    self.invalid["cuff"] = "cuff_inconsistent"
                    return
                self.cuff = bool(value[0])
                self.stamps["cuff"] = stamp
                self.received["cuff"] = ns()
                self.invalid.pop("cuff", None)
        except (ValueError, TypeError, StopIteration):
            with self.lock:
                self.invalid["cuff"] = "cuff_invalid"

    def snapshot(self):
        with self.lock:
            now = ns()
            # Require a live IO subscription and a state received on it. A
            # disconnect invalidates the cached value until another message.
            if not self.cuff_subscriber.get_num_connections():
                self.cuff = None
                self.invalid["cuff"] = "cuff_connection_lost"
            elif self.cuff is None:
                self.invalid.setdefault("cuff", "cuff_missing")
            fault = next(iter(self.invalid.values()), None)
            if self.ros.is_shutdown():
                fault = "ros_shutdown"
            if not self.publisher.get_num_connections():
                fault = fault or "command_subscriber_missing"
            for name in ("joint", "pose", "robot", "collision"):
                limit = self.state_timeout if name in ("joint", "pose") else self.status_timeout
                timestamp = self.received.get(name, 0) if name == "robot" else self.captured.get(name, 0)
                if not 0 <= now-timestamp <= limit*1e9:
                    fault = fault or name + "_stale"
            r = self.robot
            if (not r or not r["enabled"] or not r["ready"] or not r["homed"]
                    or r["low_voltage"] or r["estop"] or r["stopped"] or r["error"]):
                fault = fault or "robot_not_ready"
            if self.cuff:
                fault = fault or "hand_guiding"
            if self.collision:
                fault = fault or "robot_collision"
            gripper = getattr(self, "gripper", None)
            gripper_state = gripper.snapshot() if gripper is not None else {"supported": False}
            if gripper is not None and not gripper_state["valid"]:
                fault = fault or gripper_state["fault"]
            return {
                "seq": self.seq,
                "captured_ns": min(self.captured.get("joint", 0), self.captured.get("pose", 0)),
                "available_ns": max(self.received.get("joint", 0), self.received.get("pose", 0)),
                "source_stamp": self.stamps.get("joint", 0),
                "pose_source_stamp": self.stamps.get("pose", 0),
                "q": self.q, "dq": self.dq, "pose": self.endpoint,
                "valid": fault is None and self.q is not None and self.endpoint is not None,
                "fault": fault, "robot": r, "gripper": gripper_state,
                "feedback_diagnostics": {
                    "invalid": dict(self.invalid), "source_stamps": dict(self.stamps),
                    "received_ns": dict(self.received), "captured_ns": dict(self.captured),
                    "cuff": self.cuff, "collision": self.collision,
                },
            }

    def publish(self, q):
        q = vector(q, 7)
        with self.lock:
            if not self.snapshot()["valid"]:
                raise RuntimeError("robot_feedback_invalid")
            msg = self.command_class()
            msg.header.stamp = self.ros.Time.now()
            msg.names, msg.position, msg.mode = JOINTS, q, msg.POSITION_MODE
            self.publisher.publish(msg)

    def solve(self, target, seed):
        from geometry_msgs.msg import PoseStamped
        from sensor_msgs.msg import JointState
        target, seed = pose(target), vector(seed, 7)
        req = self.request_class()
        p = PoseStamped()
        p.header.stamp, p.header.frame_id = self.ros.Time.now(), "base"
        p.pose.position.x, p.pose.position.y, p.pose.position.z = target["p"]
        p.pose.orientation.x, p.pose.orientation.y, p.pose.orientation.z, p.pose.orientation.w = target["q"]
        req.pose_stamp.append(p)
        req.tip_names.append(self.tip_frame)
        req.seed_mode = req.SEED_USER
        joints = JointState()
        joints.name, joints.position = JOINTS, seed
        req.seed_angles.append(joints)
        response = self.ik(req)
        if not response.result_type or response.result_type[0] <= 0 or not response.joints:
            return None
        values = dict(zip(response.joints[0].name, response.joints[0].position))
        return vector([values[name] for name in JOINTS], 7)

    def close(self):
        native = getattr(self, "cartesian", None)
        if native is not None and native.command_sent:
            try:
                native.stop()
            except Exception:
                self.ros.logerr("Intera Cartesian shutdown STOP failed")
        gripper = getattr(self, "gripper", None)
        if gripper is not None:
            try:
                gripper.close()
                if gripper.snapshot()["fault"]:
                    self.ros.logerr(gripper.snapshot()["fault"])
            except Exception as exc:
                self.ros.logerr("Gripper shutdown failed: %s" % exc)
        if getattr(self, "ik", None) is not None:
            self.ik.close()
        if self.publisher:
            self.publisher.unregister()
        for sub in self.subscribers:
            sub.unregister()
