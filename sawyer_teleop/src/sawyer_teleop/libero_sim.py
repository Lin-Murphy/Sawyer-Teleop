"""Native LIBERO simulation, causal episode storage, and saved-action replay."""
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

import h5py
import numpy as np

CONTROL_HZ = 20
CAMERAS = {"agentview_rgb": "agentview", "eye_in_hand_rgb": "robot0_eye_in_hand"}
SCHEMA = "libero_quest_osc_pose_v1"
PHYSICS_KEYS = ("ctrl", "qacc_warmstart", "qfrc_applied", "xfrc_applied")
CONTROLLER_KEYS = ("initial_joint", "goal_pos", "goal_ori", "relative_ori", "ori_ref")
ATOMIC_BDDL = {
    "grasp": "soup90_v2/grasp_target_object.bddl",
    "pull": "open_the_middle_drawer_of_the_cabinet.bddl",
    "place": "soup90_plane_v3/place_target_object_in_basket.bddl",
}


def atomic_bddl(skill):
    from libero.libero import get_libero_path

    return Path(get_libero_path("bddl_files")) / "libero_atomic" / ATOMIC_BDDL[skill]


class LiberoSim:
    def __init__(self, bddl):
        from libero.libero.envs.env_wrapper import ControlEnv

        self.bddl = Path(bddl).resolve()
        # Explicit simulator arguments keep the data format independent of wrapper defaults.
        self.env = ControlEnv(
            bddl_file_name=str(self.bddl), robots=["Panda"], controller="OSC_POSE",
            control_freq=CONTROL_HZ, has_renderer=False, has_offscreen_renderer=True,
            use_camera_obs=False, hard_reset=False, ignore_done=True, horizon=1000,
            camera_names=list(CAMERAS.values()), camera_heights=128, camera_widths=128,
        )
        self.native = self.env.env
        self.robot = self.env.robots[0]
        controller = self.robot.controller
        self.input_min = np.asarray(controller.input_min)
        self.input_max = np.asarray(controller.input_max)
        self.output_min = np.asarray(controller.output_min)
        self.output_max = np.asarray(controller.output_max)
        limits = (self.input_min, self.input_max, self.output_min, self.output_max)
        if (self.native.action_dim != 7 or self.native.control_freq != CONTROL_HZ
                or controller.name != "OSC_POSE" or not controller.use_delta
                or any(bound.shape != (6,) or not np.isfinite(bound).all() for bound in limits)
                or not np.allclose(self.input_min, -1) or not np.allclose(self.input_max, 1)
                or not np.allclose(self.output_min, -self.output_max)
                or np.any(self.output_max <= 0)):
            self.env.close()
            raise RuntimeError("LIBERO must resolve to native Panda delta OSC_POSE7 at 20 Hz")

    def reset(self, seed):
        # One seed means one attempt. Use the native reset so failures surface.
        self.env.seed(seed)
        self.native.reset()
        self.robot = self.env.robots[0]
        self.resting_gripper = 1. if getattr(self.native, "_is_grasped", lambda: False)() else -1.

    def snapshot(self):
        sim, robot = self.env.sim, self.robot
        sim.forward()
        # robosuite's render call assumes the owning GL context is current.
        sim._render_context_offscreen.gl_ctx.make_current()
        frame = {key: sim.render(camera_name=name, width=128, height=128).copy()
                 for key, name in CAMERAS.items()}
        frame.update(
            joint_states=sim.data.qpos[robot._ref_joint_pos_indexes].copy(),
            gripper_states=sim.data.qpos[robot._ref_gripper_joint_pos_indexes].copy(),
            eef_pos=sim.data.site_xpos[robot.eef_site_id].copy(),
            eef_rotation=sim.data.site_xmat[robot.eef_site_id].reshape(3, 3).copy(),
            sim_state=sim.get_state().flatten().copy(),
            sim_time=float(sim.data.time), success=bool(self.env.check_success()),
        )
        return frame

    def start_metadata(self):
        sim, robot = self.env.sim, self.robot
        task_snapshot = getattr(self.native, "get_episode_state", None)
        return dict(
            control_hz=CONTROL_HZ, controller="OSC_POSE", action_dim=7,
            input_min=self.input_min.tolist(), input_max=self.input_max.tolist(),
            output_min=self.output_min.tolist(), output_max=self.output_max.tolist(),
            controller_state={key: getattr(robot.controller, key) for key in CONTROLLER_KEYS},
            gripper_command=robot.gripper.current_action,
            physics={key: getattr(sim.data, key) for key in PHYSICS_KEYS},
            fixtures={obj.root_body: dict(
                pos=sim.model.body_pos[sim.model.body_name2id(obj.root_body)],
                quat=sim.model.body_quat[sim.model.body_name2id(obj.root_body)],
            ) for obj in self.native.fixtures_dict.values()},
            cur_time=self.native.cur_time, timestep=self.native.timestep,
            joint_names=list(sim.model.joint_names),
            task_snapshot=task_snapshot() if task_snapshot else None,
            preparation=getattr(self.native, "last_preparation", None),
            initial_gripper_action=self.resting_gripper,
        )

    def restore_start(self, start, state):
        sim, robot = self.env.sim, self.robot
        if list(sim.model.joint_names) != start["joint_names"]:
            raise ValueError("Replay joint order differs")
        # Fixture transforms are MuJoCo model parameters and are absent from
        # both flattened sim state and LIBERO's construction XML.
        for body, pose in start["fixtures"].items():
            index = sim.model.body_name2id(body)
            sim.model.body_pos[index] = pose["pos"]
            sim.model.body_quat[index] = pose["quat"]
        sim.set_state_from_flattened(state)
        frame = self.snapshot()
        robot.controller.update(force=True)
        robot.controller.reset_goal()
        for key, value in start["controller_state"].items():
            setattr(robot.controller, key, None if value is None else np.asarray(value))
        robot.gripper.current_action = np.asarray(start["gripper_command"])
        self.native.cur_time, self.native.timestep = start["cur_time"], start["timestep"]
        for key, value in start["physics"].items():
            getattr(sim.data, key)[:] = value
        return frame

    def step(self, action):
        self.env.step(action)
        return self.snapshot()

    def close(self):
        self.env.close()


class Episode:
    def __init__(self, output, sim, seed, first, mapping):
        output.mkdir(parents=True, exist_ok=True)
        name = time.strftime("episode_%Y%m%dT%H%M%SZ_", time.gmtime()) + uuid.uuid4().hex[:10]
        self.destination = output / name
        self.folder = output / (".pending_" + name)
        self.folder.mkdir()
        self.count = 0
        self.first_success = 0 if first["success"] else None
        self.last_success = bool(first["success"])
        bddl_copy = self.folder / sim.bddl.name
        shutil.copyfile(sim.bddl, bddl_copy)
        self.metadata = dict(
            schema=SCHEMA, seed=seed, bddl_file=bddl_copy.name,
            bddl_sha256=hashlib.sha256(bddl_copy.read_bytes()).hexdigest(),
            task=sim.env.language_instruction, problem=sim.env.problem_name,
            atomic_version=getattr(sim.native, "atomic_version", None),
            cameras=CAMERAS, rgb_orientation="native_opengl",
            heading_mapping=None if mapping is None else mapping.tolist(),
            start=sim.start_metadata(),
        )
        (self.folder / "model.xml").write_text(sim.native.model.get_xml(), encoding="utf-8")
        (self.folder / "start.json").write_text(
            json.dumps(self.metadata, indent=2, default=lambda value: value.tolist()),
            encoding="utf-8",
        )
        self.file = h5py.File(self.folder / "trajectory.hdf5", "x")
        try:
            self.file.attrs["schema"] = SCHEMA
            self.file.attrs["alignment"] = "o_t -> a_t -> o_(t+1); T actions, T+1 observations"
            self.file.attrs["control_hz"] = CONTROL_HZ
            self.obs = self.file.create_group("observations")
            self.actions = self.file.create_group("actions")
            self.actions.create_dataset("quest_packet", shape=(0,), maxshape=(None,),
                                        dtype=h5py.string_dtype("utf-8"))
            for key, example in dict(native_osc_pose=np.zeros(7), observation_frame_id=0,
                                     quest_age_upper_ns=0, controller_active=False,
                                     wall_monotonic_ns=0).items():
                self._create(self.actions, key, example)
            for key, value in first.items():
                self._create(self.obs, key, value, image=key in CAMERAS)
                self._append(self.obs[key], 0, value)
        except BaseException:
            self.file.close()
            raise

    @staticmethod
    def _create(group, key, value, image=False):
        value = np.asarray(value)
        kwargs = {"chunks": (1,) + value.shape, "compression": "lzf"} if image else {}
        group.create_dataset(key, shape=(0,) + value.shape, maxshape=(None,) + value.shape,
                             dtype=value.dtype, **kwargs)

    @staticmethod
    def _append(dataset, index, value):
        dataset.resize(index + 1, axis=0)
        dataset[index] = value

    def add(self, action, frame, packet, age_upper_ns, active, stamp):
        for key, value in dict(native_osc_pose=action, observation_frame_id=self.count,
                               quest_packet=json.dumps(packet, separators=(",", ":")),
                               quest_age_upper_ns=age_upper_ns, controller_active=active,
                               wall_monotonic_ns=stamp).items():
            self._append(self.actions[key], self.count, value)
        for key, value in frame.items():
            self._append(self.obs[key], self.count + 1, value)
        self.count += 1
        self.last_success = bool(frame["success"])
        if self.last_success and self.first_success is None:
            self.first_success = self.count

    def finish(self, reason):
        self.file.close()
        result = dict(schema=SCHEMA, actions=self.count, observations=self.count + 1,
                      complete=reason == "stop", reason=reason, task_success=self.last_success,
                      first_success_frame=self.first_success)
        (self.folder / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        self.folder.rename(self.destination)
        return self.destination.name


def replay_episode(folder):
    """Restore the recorded start once and compare every saved observation."""
    folder = Path(folder)
    metadata = json.loads((folder / "start.json").read_text(encoding="utf-8"))
    bddl = folder / metadata["bddl_file"]
    if hashlib.sha256(bddl.read_bytes()).hexdigest() != metadata["bddl_sha256"]:
        raise ValueError("Recorded BDDL differs from its digest")
    maxima = dict(sim_state=0., joint_states=0., gripper_states=0., sim_time=0.,
                  agentview_rgb=0., eye_in_hand_rgb=0., success=0.,
                  eef_pos=0., eef_rotation=0.)
    sim = LiberoSim(bddl)
    try:
        sim.reset(metadata["seed"])
        if sim.native.model.get_xml() != (folder / "model.xml").read_text(encoding="utf-8"):
            raise ValueError("Seeded reset produced different model XML")
        with h5py.File(folder / "trajectory.hdf5", "r") as saved:
            actions = saved["actions/native_osc_pose"]
            observations = saved["observations"]
            n_actions = len(actions)
            ids = saved["actions/observation_frame_id"][:]
            if (any(len(observations[key]) != n_actions + 1 for key in maxima)
                    or not np.array_equal(ids, np.arange(n_actions))):
                raise ValueError("Observation and action frame IDs are out of alignment")
            frame = sim.restore_start(metadata["start"], observations["sim_state"][0])
            for index in range(n_actions + 1):
                for key in maxima:
                    error = np.max(np.abs(np.asarray(frame[key], dtype=float)
                                          - np.asarray(observations[key][index], dtype=float)))
                    if not np.isfinite(error):
                        raise ValueError(f"Non-finite {key} at observation {index}")
                    maxima[key] = max(maxima[key], float(error))
                if index < n_actions:
                    frame = sim.step(actions[index])
    finally:
        sim.close()
    exact = {"agentview_rgb", "eye_in_hand_rgb", "success"}
    return dict(passed=all(error <= (0 if key in exact else 1e-8)
                           for key, error in maxima.items()),
                actions=n_actions, observations=n_actions + 1, max_abs_error=maxima)
