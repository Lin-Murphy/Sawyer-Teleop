"""Gazebo-only adapter. ROS simulation time and wall-time liveness are separate."""
import bisect
import collections
import math
import threading

from .protocol import JOINTS, ns, pose, vector
from .neutral import read_neutral_pose, gazebo_neutral_pose
from .kinematics_model import read_kinematics_model
from .gripper import SimulationGripper
from .feedback_stream import FeedbackStreamSource, finite_vector, twist_from


class SimulationClock:
    def __init__(self, timeout=.1, clock=ns):
        self.timeout = timeout
        self.wall = clock
        self.history = collections.deque(maxlen=4096)
        self.fault = None

    def update(self, stamp):
        if not math.isfinite(stamp) or stamp < 0:
            self.fault = "simulation_clock_invalid"
        elif self.history and stamp < self.history[-1][0]:
            # Latched: a world reset requires restarting B, not reusing old commands.
            self.fault = "simulation_clock_regressed_restart_bridge"
        elif stamp > 0 and (not self.history or stamp > self.history[-1][0]):
            self.history.append((stamp, self.wall()))

    def status(self):
        if self.fault:
            return self.fault
        if not self.history or self.wall()-self.history[-1][1] > self.timeout*1e9:
            return "simulation_clock_paused_or_stale"
        return None

    def captured(self, stamp):
        if self.status() or not math.isfinite(stamp) or stamp <= 0:
            raise ValueError("simulation_clock_unavailable")
        latest = self.history[-1][0]
        # ROS callbacks for /clock and joint/pose can arrive in either order.
        if not -.02 <= latest-stamp <= self.timeout:
            raise ValueError("simulation_source_time_invalid")
        history = list(self.history)
        index = bisect.bisect_right([item[0] for item in history], stamp)-1
        if index < 0:
            raise ValueError("simulation_source_predates_clock_history")
        # Map through observed /clock samples; never subtract simulated seconds
        # from a wall clock (real-time factor is not necessarily one).
        return min(self.wall(), history[index][1])


class GazeboBackend(FeedbackStreamSource):
    name = "gazebo"

    def __init__(self, tip_frame="right_hand", state_timeout=.1):
        import rospy
        from rosgraph_msgs.msg import Clock
        from sensor_msgs.msg import JointState
        from intera_core_msgs.msg import EndpointState, JointCommand
        from intera_core_msgs.srv import SolvePositionIK, SolvePositionIKRequest
        if not rospy.core.is_initialized():
            rospy.init_node("sawyer_gazebo_bridge", anonymous=True, disable_signals=True)
        if rospy.get_param("/use_sim_time", False) is not True:
            raise RuntimeError("Gazebo backend requires /use_sim_time=true")
        if rospy.get_param("/robot/limb/right/tip_name", None) != tip_frame:
            raise RuntimeError("Simulation tool frame mismatch")
        self.tip_frame, self.ros = tip_frame, rospy
        self.state_timeout = state_timeout
        self.lock = threading.RLock()
        self.clock = SimulationClock(state_timeout)
        self.seq = 0
        self.q = self.dq = self.endpoint = None
        self.twist = None
        self.stamps, self.received, self.captured, self.invalid = {}, {}, {}, {}
        self.pending_sources = {}
        self.command_class, self.request_class = JointCommand, SolvePositionIKRequest
        self.publisher = rospy.Publisher("/robot/limb/right/joint_command", JointCommand,
                                         queue_size=1, tcp_nodelay=True)
        from std_msgs.msg import Float64
        self.gripper = SimulationGripper([
            rospy.Publisher("/robot/generic_"+side+"_finger_controller/command",
                            Float64, queue_size=1, tcp_nodelay=True)
            for side in ("left", "right")
        ], Float64, state_timeout)
        self.subscribers = [
            rospy.Subscriber("/clock", Clock, self._clock, queue_size=1, tcp_nodelay=True),
            rospy.Subscriber("/robot/joint_states", JointState, self._joint,
                             queue_size=1, tcp_nodelay=True),
            rospy.Subscriber("/robot/limb/right/endpoint_state", EndpointState,
                             self._endpoint, queue_size=1, tcp_nodelay=True),
        ]
        self.neutral_pose = gazebo_neutral_pose(
            read_neutral_pose(rospy.get_param), rospy.get_param("/robot_description", ""))
        self.kinematics = read_kinematics_model(rospy.get_param("/robot_description", ""), tip_frame)
        # Same persistent remote IK connection as the real backend.
        from .ros_service import PersistentService
        self.ik = PersistentService(rospy, "/ExternalTools/right/PositionKinematicsNode/IKService",
                                    SolvePositionIK)
        from .gazebo_scene import GazeboScene
        self.scene = GazeboScene(rospy)
        self.scene_reset_capability = self.scene.capability
        # Only the project's simulated Motion Controller may serve native goals here;
        # it exercises the SDK/action path, not Intera's own planner or feel.
        from .intera_cartesian import discover_cartesian
        self.cartesian, self.cartesian_capability = discover_cartesian(rospy, tip_frame)
        if self.cartesian is not None:
            if rospy.get_param("/motion/motion_command/simulated", False) is not True:
                self.cartesian, self.cartesian_capability = None, dict(
                    available=False, reason="Motion Controller in Gazebo is not the project simulated server.")
            else:
                self.cartesian_capability.update(
                    simulated=True, controller="sawyer_sim_motion_server (project planner, not Intera)")

    def reset_scene(self, cancelled):
        return self.scene.reset(cancelled)

    def description(self):
        return {"model": "Sawyer", "simulator": "Gazebo Classic", "simulation": True,
                "gripper": True, "gripper_model": "generic two-finger simulation",
                "command_mode": "TRAJECTORY_MODE", "velocity_source": "B planned reference", "clock": "ROS /clock",
                "clock_reset": "restart bridge required", "hardware_status": "not applicable",
                "captured_time": "wall-time observation of matching /clock"}

    def _clock(self, msg):
        with self.lock:
            self.clock.update(msg.clock.to_sec())
            if self.clock.status():
                return
            for name, (stamp, update, received) in list(self.pending_sources.items()):
                if ns()-received > self.state_timeout*1e9:
                    self.pending_sources.pop(name)
                    self.invalid[name] = name+"_stale"
                elif stamp <= self.clock.history[-1][0]:
                    self.pending_sources.pop(name)
                    self._accept(name, stamp, update, received)

    def _accept(self, name, stamp, update, received=None):
        with self.lock:
            received = ns() if received is None else received
            previous = self.stamps.get(name, -1)
            if stamp < previous:
                self.clock.fault = "simulation_source_regressed_restart_bridge"
                return
            if stamp == previous:
                return  # Duplicate stamps never refresh feedback.
            pending = self.pending_sources.get(name)
            if pending is not None and stamp == pending[0]:
                return  # A repeated pending sample cannot renew its original age.
            try:
                captured = self.clock.captured(stamp)
            except ValueError as exc:
                if (str(exc) == "simulation_source_time_invalid"
                        and 0 < stamp-self.clock.history[-1][0] <= self.state_timeout):
                    # Separate ROS topics can overtake /clock. Retain only the
                    # latest candidate; never publish it before its clock arrives.
                    self.pending_sources[name] = (stamp, update, received)
                    return
                self.pending_sources.pop(name, None)
                self.invalid[name] = str(exc)
                return
            self.pending_sources.pop(name, None)
            update()
            self.seq += 1
            self.stamps[name] = stamp
            self.received[name] = received
            self.captured[name] = min(captured, received)
            self.invalid.pop(name, None)
            self._stream_commit(name, stamp, received, self.captured[name])

    def _joint(self, msg):
        try:
            if (len(msg.name) != len(set(msg.name)) or len(msg.name) != len(msg.position)
                    or len(msg.name) != len(msg.velocity)):
                raise ValueError("invalid joints")
            positions, velocities = dict(zip(msg.name, msg.position)), dict(zip(msg.name, msg.velocity))
            q, dq = vector([positions[n] for n in JOINTS], 7), vector([velocities[n] for n in JOINTS], 7)
            received = ns()
            head = finite_vector([positions.get("head_pan")], 1)
            def update():
                self.q, self.dq = q, dq
                self._stream_stage("joint", {"q": q, "dq": dq, "head_pan": head[0] if head else None})
                self.gripper.observe(positions, min(received, self.clock.captured(msg.header.stamp.to_sec())))
            self._accept("joint", msg.header.stamp.to_sec(), update, received)
        except (ValueError, TypeError, KeyError):
            with self.lock:
                self.pending_sources.pop("joint", None)
                self.invalid["joint"] = "joint_invalid"

    def _endpoint(self, msg):
        try:
            if not msg.valid or msg.header.frame_id.lstrip("/") != "base":
                raise ValueError("endpoint invalid")
            p, q = msg.pose.position, msg.pose.orientation
            value = pose({"p": [p.x, p.y, p.z], "q": [q.x, q.y, q.z, q.w]})
            twist = twist_from(msg)
            def update():
                self.endpoint, self.twist = value, twist
                self._stream_stage("pose", {"p": value["p"], "q": value["q"], "twist": twist})
            self._accept("pose", msg.header.stamp.to_sec(), update)
        except (ValueError, TypeError):
            with self.lock:
                self.pending_sources.pop("pose", None)
                self.invalid["pose"] = "endpoint_invalid"

    def snapshot(self):
        with self.lock:
            now = ns()
            fault = self.clock.status() or next(iter(self.invalid.values()), None)
            if self.ros.is_shutdown():
                fault = "ros_shutdown"
            if not self.publisher.get_num_connections():
                fault = fault or "command_subscriber_missing"
            for name in ("joint", "pose"):
                if not 0 <= now-self.captured.get(name, 0) <= self.state_timeout*1e9:
                    fault = fault or name+"_stale"
                if self.clock.history and not -.02 <= self.clock.history[-1][0]-self.stamps.get(name, 0) <= self.state_timeout:
                    fault = fault or name+"_simulation_time_stale"
            return {"seq": self.seq,
                    "captured_ns": min(self.captured.get("joint", 0), self.captured.get("pose", 0)),
                    "available_ns": max(self.received.get("joint", 0), self.received.get("pose", 0)),
                    "source_stamp": self.stamps.get("joint", 0),
                    "pose_source_stamp": self.stamps.get("pose", 0),
                    "q": self.q, "dq": self.dq, "pose": self.endpoint,
                    "twist": getattr(self, "twist", None),
                    "valid": fault is None and self.q is not None and self.endpoint is not None,
                    "fault": fault, "robot": {"simulation": True},
                    "gripper": self.gripper.snapshot()}

    def publish_gripper(self, value):
        with self.lock:
            if not self.snapshot()["valid"]:
                raise RuntimeError("simulation_feedback_invalid")
            self.gripper.command(value)

    def hold_gripper(self):
        with self.lock:
            self.gripper.hold()

    def publish(self, q):
        # Hold/direct positions explicitly request zero velocity. The Gazebo
        # POSITION_MODE synthesizes a new velocity from position error, which
        # can keep driving after a one-shot hold; do not use that implicit layer.
        self.publish_reference(q, [0.] * 7, [0.] * 7)

    def publish_reference(self, q, dq, ddq):
        q, dq, ddq = vector(q, 7), vector(dq, 7), vector(ddq, 7)
        with self.lock:
            if not self.snapshot()["valid"]:
                raise RuntimeError("simulation_feedback_invalid")
            msg = self.command_class()
            msg.header.stamp = self.ros.Time.now()
            msg.names, msg.position, msg.mode = JOINTS, q, msg.TRAJECTORY_MODE
            msg.velocity, msg.acceleration = dq, ddq
            self.publisher.publish(msg)

    def solve(self, target, seed):
        from geometry_msgs.msg import PoseStamped
        from sensor_msgs.msg import JointState
        target, seed = pose(target), vector(seed, 7)
        if not self.snapshot()["valid"]:
            raise RuntimeError("simulation_feedback_invalid")
        req = self.request_class()
        p = PoseStamped()
        p.header.stamp, p.header.frame_id = self.ros.Time.now(), "base"
        p.pose.position.x, p.pose.position.y, p.pose.position.z = target["p"]
        p.pose.orientation.x, p.pose.orientation.y, p.pose.orientation.z, p.pose.orientation.w = target["q"]
        req.pose_stamp, req.tip_names = [p], [self.tip_frame]
        req.seed_mode = req.SEED_USER
        joints = JointState()
        joints.name, joints.position = JOINTS, seed
        req.seed_angles = [joints]
        result = self.ik(req)
        if not result.result_type or result.result_type[0] <= 0 or not result.joints:
            return None
        values = dict(zip(result.joints[0].name, result.joints[0].position))
        return vector([values[n] for n in JOINTS], 7)

    def close(self):
        native = getattr(self, "cartesian", None)
        if native is not None and native.command_sent:
            try:
                native.stop()
            except Exception:
                self.ros.logerr("Simulated Cartesian shutdown STOP failed")
        self.hold_gripper()
        self.gripper.close()
        if getattr(self, "ik", None) is not None:
            self.ik.close()
        self.publisher.unregister()
        for sub in self.subscribers:
            sub.unregister()
