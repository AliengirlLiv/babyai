from meta_mb.samplers.base import BaseSampler
from meta_mb.samplers.meta_samplers.meta_vectorized_env_executor import MetaParallelEnvExecutor, \
    MetaIterativeEnvExecutor
from meta_mb.logger import logger
from meta_mb.utils import utils
from collections import OrderedDict
# from meta_mb.algos.dummy import CopyPolicy

from pyprind import ProgBar
import numpy as np
import time
import itertools


class MetaSampler(BaseSampler):
    """
    Sampler for Meta-RL

    Args:
        env (meta_mb.meta_envs.base.MetaEnv) : environment object
        policy (maml_zoo.policies.base.Policy) : policy object
        batch_size (int) : number of trajectories per task
        meta_batch_size (int) : number of meta tasks
        max_path_length (int) : max number of steps per trajectory
        envs_per_task (int) : number of meta_envs to run vectorized for each task (influences the memory usage)
    """

    def __init__(
        self,
        env,
        policy,
        rollouts_per_meta_task,
        meta_batch_size,
        max_path_length,
        envs_per_task=None,
        parallel=False,
        reward_predictor=None,
        obs_preprocessor=None
    ):
        super(MetaSampler, self).__init__(env, policy, rollouts_per_meta_task, max_path_length)
        assert hasattr(env, 'set_task')

        self.rollouts_per_meta_task = rollouts_per_meta_task
        self.max_path_length = max_path_length
        self.envs_per_task = rollouts_per_meta_task if envs_per_task is None else envs_per_task
        assert self.envs_per_task == 1, "When we changed to the new model, we didn't check if the format could handle > 1 envs_per_task.  If it does, feel free to remove this."
        self.meta_batch_size = meta_batch_size
        self.parallel = False
        self.total_timesteps_sampled = 0
        self.reward_predictor = reward_predictor
        # self.supervised_model = supervised_model
        # setup vectorized environment
        self.obs_preprocessor = obs_preprocessor
        self.policy = None
        if self.parallel:
            self.vec_env = MetaParallelEnvExecutor(env, self.meta_batch_size, self.envs_per_task, self.max_path_length)
        else:
            self.vec_env = MetaIterativeEnvExecutor(env, self.meta_batch_size, self.envs_per_task, self.max_path_length)

    def update_tasks(self):
        """
        Samples a new goal for each meta task
        """
        # We can reset tasks internally within the envs, so we don't need to sample them here.
        self.vec_env.set_tasks([None] * self.meta_batch_size)

    def mask_teacher(self, obs, feedback_list=[]):
        for feedback_type in feedback_list:
            indices = feedback_type['indices']
            null_value = feedback_type['null']
            if isinstance(obs, list):
                assert len(obs[0].shape) == 1
                for o in obs:
                    o[indices] = null_value
            elif isinstance(obs, np.ndarray):
                assert len(obs.shape) == 3
                obs[:, :, indices] = null_value
        return obs

    def obtain_samples(self, log=False, log_prefix='', random=False, advance_curriculum=False,
                       policy=None, teacher_dict={}, max_action=False, show_instrs=True, temperature=1):
        """
        Collect batch_size trajectories from each task

        Args:
            log (boolean): whether to log sampling times
            log_prefix (str) : prefix for logger
            random (boolean): whether the actions are random

        Returns: 
            (dict) : A dict of paths of size [meta_batch_size] x (batch_size) x [5] x (max_path_length)
        """

        # initial setup / preparation
        paths = OrderedDict()
        for i in range(self.meta_batch_size):
            paths[i] = []

        n_samples = 0
        running_paths = [_get_empty_running_paths_dict() for _ in range(self.vec_env.num_envs)]

        total_paths = self.rollouts_per_meta_task * self.meta_batch_size * self.envs_per_task
        pbar = ProgBar(total_paths)
        policy_time, env_time = 0, 0

        if policy is None:
            policy = self.policy
        policy.reset(dones=[True] * self.meta_batch_size)
        if self.reward_predictor is not None:
            self.reward_predictor.reset(dones=[True] * self.meta_batch_size)
        # initial reset of meta_envs
        if advance_curriculum:
            self.vec_env.advance_curriculum()
        obses = self.vec_env.reset()

        num_paths = 0
        itrs = 0
        while num_paths < total_paths:
            itrs += 1
            t = time.time()
            obses = self.obs_preprocessor(obses, teacher_dict, show_instrs=show_instrs)
            if random:
                actions = np.stack([[self.env.action_space.sample()] for _ in range(len(obses.obs))], axis=0)
                agent_infos = [[{'mean': np.zeros_like(self.env.action_space.sample()),
                                 'log_std': np.zeros_like(
                                     self.env.action_space.sample())}] * self.envs_per_task] * self.meta_batch_size
            else:
                actions, agent_infos = policy.get_actions_t(obses, temp=temperature)
                if max_action:
                    actions = np.array([np.argmax(d['probs']) for d in agent_infos], dtype=np.int32)


            policy_time += time.time() - t

            # step environments
            t = time.time()
            next_obses, rewards, dones, env_infos = self.vec_env.step(actions)
            env_time += time.time() - t

            new_samples = 0
            new_paths = 0
            for idx, action, reward, env_info, agent_info, done in zip(itertools.count(), actions,
                                                                                    rewards, env_infos, agent_infos,
                                                                                    dones):
                observation = obses[idx]
                # append new samples to running paths
                if isinstance(reward, np.ndarray):
                    reward = reward[0]
                running_paths[idx]["observations"].append(observation)
                running_paths[idx]["actions"].append(action)
                running_paths[idx]["rewards"].append(reward)
                running_paths[idx]["dones"].append(done)
                running_paths[idx]["env_infos"].append(env_info)
                del agent_info['memory']
                running_paths[idx]["agent_infos"].append(agent_info)

                # if running path is done, add it to paths and empty the running path
                if done:
                    curr_path = paths[idx // self.envs_per_task]
                    if len(curr_path) >= self.rollouts_per_meta_task:
                        continue
                    paths[idx // self.envs_per_task].append(dict(
                        observations=np.asarray(running_paths[idx]["observations"]),
                        actions=np.asarray(running_paths[idx]["actions"]),
                        rewards=np.asarray(running_paths[idx]["rewards"]),
                        dones=np.asarray(running_paths[idx]["dones"]),
                        env_infos=utils.stack_tensor_dict_list(running_paths[idx]["env_infos"]),
                        agent_infos=utils.stack_tensor_dict_list(running_paths[idx]["agent_infos"]),
                    ))
                    num_paths += 1
                    new_paths += 1
                    new_samples += len(running_paths[idx]["rewards"])
                    running_paths[idx] = _get_empty_running_paths_dict()

            pbar.update(new_paths)
            n_samples += new_samples
            obses = next_obses
        pbar.stop()

        self.total_timesteps_sampled += n_samples
        if log:
            logger.logkv(log_prefix + "PolicyExecTime", policy_time)
            logger.logkv(log_prefix + "EnvExecTime", env_time)

        return paths


    def advance_curriculum(self):
        self.vec_env.advance_curriculum()


def _get_empty_running_paths_dict():
    return dict(observations=[], actions=[], rewards=[], dones=[], env_infos=[], agent_infos=[])
