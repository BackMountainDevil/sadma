from functools import partial
from multiprocessing import Pipe, Process
import numpy as np
from envs import REGISTRY as env_REGISTRY


def env_worker(remote, env_fn):
    # Make environment
    env = env_fn.x()
    while True:
        cmd, data = remote.recv()
        if cmd == "step":
            actions = data
            reward, terminated, env_info = env.step(actions)
            state = env.get_state()
            avail_actions = env.get_avail_actions()
            obs = env.get_obs()
            remote.send(
                {
                    "state": state,
                    "avail_actions": avail_actions,
                    "obs": obs,
                    "reward": reward,
                    "terminated": terminated,
                    "info": env_info,
                }
            )
        elif cmd == "reset":
            env.reset()
            remote.send(
                {
                    "state": env.get_state(),
                    "avail_actions": env.get_avail_actions(),
                    "obs": env.get_obs(),
                }
            )
        elif cmd == "close":
            env.close()
            remote.close()
            break
        elif cmd == "get_env_info":
            remote.send(env.get_env_info())
        elif cmd == "get_stats":
            remote.send(env.get_stats())
        elif cmd == "switch_mode":
            mode = data
            env.switch_mode(mode)
        elif cmd == "get_profit":
            remote.send(env.get_profit())
        elif cmd == "get_reward_dict":
            remote.send(env._env.reward_monitor)
        elif cmd == "visualize_render":
            env.visualize_render(data)
        elif cmd == "get_storage_capacity":
            remote.send(env._env.storage_capacity)
        elif cmd == "set_storage_capacity":
            env.set_storage_capacity(data)
        else:
            raise NotImplementedError


class CloudpickleWrapper:
    """
    Uses cloudpickle to serialize contents (otherwise multiprocessing tries to use pickle)
    """

    def __init__(self, x):
        self.x = x

    def __getstate__(self):
        import cloudpickle
        return cloudpickle.dumps(self.x)

    def __setstate__(self, ob):
        import pickle
        self.x = pickle.loads(ob)


class ReplenishmentRunner:
    def __init__(self, args, evaluate=False):
        self.args = args
        self.evaluate = evaluate
        if evaluate:
            self.env_batch_size = self.args.evaluate_batch_size
        else:
            self.env_batch_size = self.args.env_batch_size
        self._init_env()

    def _init_env(self):
        self.parent_conns, self.worker_conns = zip(*[Pipe() for _ in range(self.env_batch_size)])

        env_fn = env_REGISTRY[self.args.env]
        env_args = [self.args.env_args.copy() for _ in range(self.env_batch_size)]
        for i in range(self.env_batch_size):
            env_args[i]["seed"] += i

        self.ps = [Process(target=env_worker, args=(worker_conn, CloudpickleWrapper(partial(env_fn, **env_arg))))
                   for env_arg, worker_conn in zip(env_args, self.worker_conns)]

        for p in self.ps:
            p.daemon = True
            p.start()
        if self.args.asynchronous_env:
            self.step = self.async_step
        else:
            self.step = self.sync_step

        self.env_total_step = 0
        self.terminated = np.ones(self.env_batch_size, dtype=np.bool_)
        self.envs_step = np.zeros(self.env_batch_size, dtype=np.int32)

        self.episode_returns = [0.] * self.env_batch_size
        self.episode_lengths = [0] * self.env_batch_size

    def reset_all_env(self):
        self.envs_step = np.zeros(self.env_batch_size, dtype=np.int32)
        self.terminated = np.ones(self.env_batch_size, dtype=np.bool_)
        self.live_env = self.env_batch_size

    def sync_step(self, actions=None):
        terminated_env_info = []
        step_data = {
            "state": [],
            "avail_actions": [],
            "obs": [],
            "reward": [],
            "individual_rewards": [],
            "terminated": [],
        }
        if np.all(self.terminated):
            self.envs_step[:] = 0
            for parent_conn in self.parent_conns:
                parent_conn.send(("reset", None))

            for idx, parent_conn in enumerate(self.parent_conns):
                data = parent_conn.recv()
                step_data["reward"].append((0.,))
                step_data["individual_rewards"].append(np.zeros((self.args.n_agents, 1)))
                step_data["terminated"].append((False,))
                step_data["state"].append(data["state"])
                step_data["avail_actions"].append(data["avail_actions"])
                step_data["obs"].append(data["obs"])
            self.terminated[:] = False
        else:
            for idx, parent_conn in enumerate(self.parent_conns):
                if not self.terminated[idx]:
                    parent_conn.send(("step", actions[idx]))
                    self.envs_step[idx] += 1
                    self.env_total_step += 1
            for idx, parent_conn in enumerate(self.parent_conns):
                if not self.terminated[idx]:
                    data = parent_conn.recv()
                    step_data["reward"].append((data["reward"],))
                    step_data["individual_rewards"].append(np.expand_dims(data["info"].pop("individual_rewards"), axis=-1))
                    self.episode_lengths[idx] += 1
                    self.episode_returns[idx] += data["reward"]
                    env_terminated = False

                    if data["terminated"]:
                        terminated_env_info.append(self.get_terminated_env_info(idx, data["info"]))

                    if data["terminated"] and not data["info"].get("episode_limit", False):
                        env_terminated = True
                    self.terminated[idx] = env_terminated
                    step_data["terminated"].append((env_terminated,))
                    step_data["state"].append(data["state"])
                    step_data["avail_actions"].append(data["avail_actions"])
                    step_data["obs"].append(data["obs"])
                else:
                    # add fake data
                    step_data["state"].append(np.zeros(self.args.state_shape))
                    step_data["avail_actions"].append(np.ones((self.args.n_agents, self.args.n_actions)))
                    step_data["obs"].append(np.zeros((self.args.n_agents, self.args.obs_shape)))
                    step_data["reward"].append((0.,))
                    step_data["terminated"].append((False,))
                    step_data["individual_rewards"].append(np.zeros((self.args.n_agents, 1)))
                    self.envs_step[idx] = 0

        for k, v in step_data.items():
            step_data[k] = np.array(v)

        return step_data, terminated_env_info

    def async_step(self, actions=None):
        terminated_env_info = []

        step_data = {
            "state": [],
            "avail_actions": [],
            "obs": [],
            "reward": [],
            "terminated": [],
            "individual_rewards": [],
        }

        for idx, parent_conn in enumerate(self.parent_conns):
            if not self.terminated[idx]:
                parent_conn.send(("step", actions[idx]))
                self.envs_step[idx] += 1
            else:
                parent_conn.send(("reset", None))
                self.envs_step[idx] = 0

        for idx, parent_conn in enumerate(self.parent_conns):
            data = parent_conn.recv()
            self.env_total_step += 1

            if not self.terminated[idx]:
                step_data["reward"].append((data["reward"],))
                step_data["individual_rewards"].append(np.expand_dims(data["info"]["individual_rewards"], axis=-1))
                self.episode_lengths[idx] += 1
                self.episode_returns[idx] += data["reward"]
                env_terminated = False

                if data["terminated"]:
                    terminated_env_info.append(self.get_terminated_env_info(idx, data["info"]))

                if data["terminated"] and not data["info"].get("episode_limit", False):
                    env_terminated = True
                self.terminated[idx] = env_terminated
                step_data["terminated"].append((env_terminated,))
                step_data["state"].append(data["state"])
                step_data["avail_actions"].append(data["avail_actions"])
                step_data["obs"].append(data["obs"])
            else:
                step_data["reward"].append((0.,))
                step_data["terminated"].append((False,))
                step_data["individual_rewards"].append(np.zeros((self.args.n_agents, 1)))
                step_data["state"].append(data["state"])
                step_data["avail_actions"].append(data["avail_actions"])
                step_data["obs"].append(data["obs"])
                self.terminated[idx] = False

        for k, v in step_data.items():
            step_data[k] = np.array(v)

        return step_data, terminated_env_info

    def alive_env(self):
        return int(np.sum(self.terminated == False))

    def close_env(self):
        for parent_conn in self.parent_conns:
            parent_conn.send(("close", None))

    def get_terminated_env_info(self, env_idx, env_info):
        terminated_env_info = {}
        terminated_env_info["episode_length"] = self.episode_lengths[env_idx]
        terminated_env_info["episode_return"] = self.episode_returns[env_idx]
        self.episode_lengths[env_idx] = 0
        self.episode_returns[env_idx] = 0
        terminated_env_info.update(
            {k: terminated_env_info.get(k, 0) + env_info.get(k, 0) for k in set(terminated_env_info) | set(env_info)})
        if self.evaluate:
            current_env_info = {}
            for k, v in terminated_env_info.items():
                new_k = "test_" + k
                current_env_info[new_k] = v
            return current_env_info
        return terminated_env_info
