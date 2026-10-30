"""Explicit robomimic 0.3 registration; importing the normal A/B does not require it."""
from robomimic.envs.env_base import EnvBase
from .policy_env import JointTargetEnvironment, ENV_NAME, ENV_TYPE


class EnvSawyerGazebo(EnvBase):
    def __init__(self, env_name=ENV_NAME, render=False, render_offscreen=False,
                 use_image_obs=False, postprocess_visual_obs=True, **kwargs):
        if env_name != ENV_NAME:
            raise ValueError("unknown_sawyer_environment")
        if render or render_offscreen or use_image_obs:
            raise ValueError("phase3_environment_supports_low_dim_observations_only")
        self.env = JointTargetEnvironment(**kwargs)

    def step(self, action):
        return self.env.step(action)

    def reset(self):
        return self.env.reset()

    def reset_to(self, state):
        raise NotImplementedError("Joint observations cannot restore the full Gazebo world; use reset()")

    def get_state(self):
        raise NotImplementedError("Full Gazebo physics state serialization is not implemented")

    def render(self, mode="human", height=None, width=None, camera_name=None):
        raise NotImplementedError("Use the Gazebo viewer; this adapter is state-only")

    def get_observation(self):
        return self.env.get_observation()

    def get_reward(self):
        return 0.

    def get_goal(self):
        raise NotImplementedError("No task goal has been defined")

    def set_goal(self, **kwargs):
        raise NotImplementedError("No task goal has been defined")

    def is_done(self):
        return self.env.done

    def is_success(self):
        return {"task": False}  # No task-success detector; never infer success from horizon.

    @property
    def action_dimension(self):
        return 7

    @property
    def name(self):
        return ENV_NAME

    @property
    def type(self):
        return ENV_TYPE

    @property
    def version(self):
        return "1"

    @property
    def rollout_exceptions(self):
        return (RuntimeError, ConnectionError, TimeoutError, ValueError)

    def serialize(self):
        return self.env.serialize()

    def close(self):
        self.env.close()

    @classmethod
    def create_for_data_processing(cls, **kwargs):
        raise NotImplementedError("Export recorded A episodes; offline observations cannot restore Gazebo")


def register():
    """Register in this process only; robomimic 0.3 has no public registry."""
    import robomimic.utils.env_utils as utils
    if getattr(utils.get_env_class, "_sawyer_registered", False):
        return
    original = utils.get_env_class
    def get_env_class(env_meta=None, env_type=None, env=None):
        kind = utils.get_env_type(env_meta=env_meta, env_type=env_type, env=env)
        if kind == ENV_TYPE:
            return EnvSawyerGazebo
        return original(env_meta=env_meta, env_type=env_type, env=env)
    get_env_class._sawyer_registered = True
    utils.get_env_class = get_env_class
