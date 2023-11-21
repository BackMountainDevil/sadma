from smac.env import StarCraft2Env
import numpy as np


class SMAC(StarCraft2Env):
    def __init__(self, **kwargs):
        super(SMAC, self).__init__(**kwargs)

    def get_scheme(self):
        env_info = super().get_env_info()
        scheme = {
            "state": {"vshape": env_info["state_shape"], "dtype": np.float32},
            "obs": {"vshape": env_info["obs_shape"], "dtype": np.float32, "group": "agents"},
            "actions": {"vshape": (1,), "dtype": np.int64, "group": "agents"},
            "avail_actions": {"vshape": (env_info["n_actions"],), "dtype": np.int32, "group": "agents"},
            "reward": {"vshape": (1,), "dtype": np.float64},
            "terminated": {"vshape": (1,), "dtype": np.int32}
        }
        groups = {"agents": env_info["n_agents"]}
        return scheme, groups
