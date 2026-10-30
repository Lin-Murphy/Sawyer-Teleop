"""Restore scene model poses without resetting the robot or simulation clock."""
import copy


class GazeboScene:
    def __init__(self, ros):
        from gazebo_msgs.msg import ModelState
        from gazebo_msgs.srv import GetWorldProperties, GetModelState, SetModelState
        self.model_state = ModelState
        self.poses = {}
        self.capability = {"available": False}
        try:
            for name in ("get_world_properties", "get_model_state", "set_model_state"):
                ros.wait_for_service("/gazebo/" + name, timeout=2)
            self.world = ros.ServiceProxy("/gazebo/get_world_properties", GetWorldProperties)
            self.get = ros.ServiceProxy("/gazebo/get_model_state", GetModelState)
            self.set = ros.ServiceProxy("/gazebo/set_model_state", SetModelState)
            world = self.world()
            self._check(world)
            if "sawyer" not in world.model_names:
                raise RuntimeError("scene_robot_model_missing")
            for name in world.model_names:
                if name == "sawyer":
                    continue
                state = self.get(name, "world")
                self._check(state)
                self.poses[name] = copy.deepcopy(state.pose)
            self.capability = {"available": True, "models": list(self.poses),
                               "reference": "B startup model poses", "resets_clock": False}
        except Exception as exc:
            self.poses.clear()
            self.capability = {"available": False, "reason": "Scene reset unavailable: " + str(exc)}

    @staticmethod
    def _check(response):
        if not response.success:
            raise RuntimeError(response.status_message)

    def reset(self, cancelled):
        if not self.capability["available"]:
            raise RuntimeError(self.capability["reason"])
        world = self.world()
        self._check(world)
        missing = set(self.poses) - set(world.model_names)
        if missing:
            raise RuntimeError("scene_models_missing: " + ", ".join(sorted(missing)))
        restored = []
        for name, pose in self.poses.items():
            if cancelled.is_set():
                raise RuntimeError("scene_reset_cancelled")
            state = self.model_state()
            state.model_name, state.reference_frame = name, "world"
            state.pose = copy.deepcopy(pose)
            # A fresh ModelState has zero linear/angular velocity.
            self._check(self.set(state))
            restored.append(name)
        if cancelled.is_set():
            raise RuntimeError("scene_reset_cancelled")
        return restored
