import numpy as np
import pickle as pickle
from multiprocessing import Process, Pipe
import copy


class MetaIterativeEnvExecutor(object):
    """
    Wraps multiple environments of the same kind and provides functionality to reset / step the environments
    in a vectorized manner. Internally, the environments are executed iteratively.

    Args:
        env (meta_mb.meta_envs.base.MetaEnv): meta environment object
        meta_batch_size (int): number of meta tasks
        envs_per_task (int): number of environments per meta task
        max_path_length (int): maximum length of sampled environment paths - if the max_path_length is reached,
                               the respective environment is reset
    """

    def __init__(self, env, meta_batch_size, envs_per_task, max_path_length):
        self.envs = np.asarray([env.copy() for _ in range(meta_batch_size * envs_per_task)])
        seeds = np.random.choice(range(10 ** 6), size=meta_batch_size, replace=False)
        self.meta_batch_size = meta_batch_size
        for new_env, seed in zip(self.envs, seeds):
            new_env.seed(int(seed))
            new_env.set_task()
            new_env.reset()
        self.ts = np.zeros(len(self.envs), dtype='int')  # time steps
        self.max_path_length = max_path_length

    def step(self, actions):
        """
        Steps the wrapped environments with the provided actions

        Args:
            actions (list): lists of actions, of length meta_batch_size x envs_per_task

        Returns
            (tuple): a length 4 tuple of lists, containing obs (np.array), rewards (float), dones (bool),
             env_infos (dict). Each list is of length meta_batch_size x envs_per_task
             (assumes that every task has same number of meta_envs)
        """
        assert len(actions) == self.num_envs

        all_results = [env.step(a) for (a, env) in zip(actions, self.envs)]

        # stack results split to obs, rewards, ...
        obs, rewards, dones, env_infos = list(map(list, zip(*all_results)))

        # reset env when done or max_path_length reached
        dones = np.asarray(dones)
        self.ts += 1
        dones = np.logical_or(self.ts >= self.max_path_length, dones)
        for i in np.argwhere(dones).flatten():
            self.envs[i].set_task()
            obs[i] = self.envs[i].reset()
            self.ts[i] = 0

        return obs, rewards, dones, env_infos

    def set_tasks(self, tasks=[None]):
        """
        Sets a list of tasks to each environment

        Args:
            tasks (list): list of the tasks for each environment
        """
        envs_per_task = np.split(self.envs, len(tasks))
        for task, envs in zip(tasks, envs_per_task):
            for env in envs:
                env.set_task(task)

    def reset(self):
        """
        Resets the environments

        Returns:
            (list): list of (np.ndarray) with the new initial observations.
        """
        for env in self.envs:
            env.set_task()
        obses = [env.reset() for env in self.envs]
        self.ts[:] = 0
        return obses

    def advance_curriculum(self):
        """
        Advances the curriculum
        """
        advances = [env.advance_curriculum() for env in self.envs]
        return advances

    def set_dropout(self, dropout_proportion):
        """
        Changes the dropout level
        """
        advances = [env.set_dropout_proportion(dropout_proportion) for env in self.envs]
        return advances

    def render(self):
        """
        Changes the dropout level
        """
        imgs = [env.render('rgb_array') for env in self.envs]
        return imgs

    def seed(self, seeds=None):
        if seeds is None:
            seeds = np.random.choice(range(10 ** 6), size=self.meta_batch_size, replace=False)
        for env, seed in zip(self.envs, seeds):
            env.seed(int(seed))

    @property
    def num_envs(self):
        """
        Number of environments

        Returns:
            (int): number of environments
        """
        return len(self.envs)


class MetaParallelEnvExecutor(object):
    """
    Wraps multiple environments of the same kind and provides functionality to reset / step the environments
    in a vectorized manner. Thereby the environments are distributed among meta_batch_size processes and
    executed in parallel.

    Args:
        env (meta_mb.meta_envs.base.MetaEnv): meta environment object
        meta_batch_size (int): number of meta tasks
        envs_per_task (int): number of environments per meta task
        max_path_length (int): maximum length of sampled environment paths - if the max_path_length is reached,
                             the respective environment is reset
    """

    def __init__(self, env, meta_batch_size, envs_per_task, max_path_length):
        self.n_envs = meta_batch_size * envs_per_task
        self.meta_batch_size = meta_batch_size
        self.envs_per_task = envs_per_task
        self.remotes, self.work_remotes = zip(*[Pipe() for _ in range(meta_batch_size)])
        seeds = np.random.choice(range(10**6), size=meta_batch_size, replace=False)

        self.ps = [
            Process(target=worker, args=(work_remote, remote, pickle.dumps(env), envs_per_task, max_path_length, seed))
            for (work_remote, remote, seed) in zip(self.work_remotes, self.remotes, seeds)]  # Why pass work remotes?

        for p in self.ps:
            p.daemon = True  # if the main process crashes, we should not cause things to hang
            p.start()
        for remote in self.work_remotes:
            remote.close()
        self.set_level_distribution(env.index)
        self.set_tasks()
        self.reset()

    def step(self, actions):
        """
        Executes actions on each env

        Args:
            actions (list): lists of actions, of length meta_batch_size x envs_per_task

        Returns
            (tuple): a length 4 tuple of lists, containing obs (np.array), rewards (float), dones (bool), env_infos (dict)
                      each list is of length meta_batch_size x envs_per_task (assumes that every task has same number of meta_envs)
        """
        assert len(actions) == self.num_envs

        # split list of actions in list of list of actions per meta tasks
        chunks = lambda l, n: [l[x: x + n] for x in range(0, len(l), n)]
        actions_per_meta_task = chunks(actions, self.envs_per_task)

        # step remote environments
        for remote, action_list in zip(self.remotes, actions_per_meta_task):
            remote.send(('step', action_list))

        results = [remote.recv() for remote in self.remotes]

        obs, rewards, dones, env_infos = map(lambda x: sum(x, []), zip(*results))

        return obs, rewards, dones, env_infos

    def reset(self):
        """
        Resets the environments of each worker

        Returns:
            (list): list of (np.ndarray) with the new initial observations.
        """
        for remote in self.remotes:
            remote.send(('reset', None))
        results = [remote.recv() for remote in self.remotes]
        return sum(results, [])

    def advance_curriculum(self):
        """
        Advances the curriculum of each worker
        """
        for remote in self.remotes:
            remote.send(('advance_curriculum', None))
        [remote.recv() for remote in self.remotes]
        return None

    def set_dropout(self, dropout_proportion):
        """
        Changes the dropout level
        """
        for remote in self.remotes:
            remote.send(('set_dropout_proportion', dropout_proportion))
        for remote in self.remotes:
            remote.recv()

    def set_tasks(self, tasks=None):
        """
        Sets a list of tasks to each worker

        Args:
            tasks (list): list of the tasks for each worker
        """
        if tasks is None:
            tasks = [None] * len(self.remotes)
        for remote, task in zip(self.remotes, tasks):
            remote.send(('set_task', task))
        for remote in self.remotes:
            remote.recv()

    def seed(self, seeds):
        """
        Sets a list of tasks to each worker

        Args:
            tasks (list): list of the tasks for each worker
        """
        for remote, seed in zip(self.remotes, seeds):
            remote.send(('seed', seed))
        for remote in self.remotes:
            remote.recv()

    def set_level_distribution(self, index):
        """
        Sets a list of tasks to each worker

        Args:
            tasks (list): list of the tasks for each worker
        """
        for remote in self.remotes:
            remote.send(('set_level_distribution', index))
        for remote in self.remotes:
            remote.recv()

    def render(self):
        for remote in self.remotes:
            remote.send(('render', 'rgb_array'))
        imgs = [remote.recv() for remote in self.remotes]
        return imgs

    @property
    def num_envs(self):
        """
        Number of environments

        Returns:
            (int): number of environments
        """
        return self.n_envs


def worker(remote, parent_remote, env_pickle, n_envs, max_path_length, seed):
    """
    Instantiation of a parallel worker for collecting samples. It loops continually checking the task that the remote
    sends to it.

    Args:
        remote (multiprocessing.Connection):
        parent_remote (multiprocessing.Connection):
        env_pickle (pkl): pickled environment
        n_envs (int): number of environments per worker
        max_path_length (int): maximum path length of the task
        seed (int): random seed for the worker
    """
    parent_remote.close()

    envs = [pickle.loads(env_pickle) for _ in range(n_envs)]
    for env in envs:
        env.seed(int(seed))
    np.random.seed(seed)

    ts = np.zeros(n_envs, dtype='int')

    while True:
        # receive command and data from the remote
        cmd, data = remote.recv()

        # do a step in each of the environment of the worker
        if cmd == 'step':
            all_results = [env.step(a) for (a, env) in zip(data, envs)]
            obs, rewards, dones, infos = map(list, zip(*all_results))
            ts += 1
            for i in range(n_envs):
                if dones[i] or (ts[i] >= max_path_length):
                    dones[i] = True
                    envs[i].set_task()
                    obs[i] = envs[i].reset()
                    ts[i] = 0
            remote.send((obs, rewards, dones, infos))

        # reset all the environments of the worker
        elif cmd == 'reset':
            for env in envs:
                env.set_task()
            obs = [env.reset() for env in envs]
            ts[:] = 0
            remote.send(obs)

        # set the specified task for each of the environments of the worker
        elif cmd == 'set_task':
            for env in envs:
                env.set_task(data)
            remote.send(None)

        elif cmd == 'advance_curriculum':
            for env in envs:
                env.advance_curriculum()
            remote.send(None)

        # close the remote and stop the worker
        elif cmd == 'close':
            remote.close()
            break

        elif cmd == 'seed':
            for env in envs:
                env.seed(int(data))
            remote.send(None)

        elif cmd == 'set_level_distribution':
            for env in envs:
                env.set_level_distribution(data)
            remote.send(None)

        elif cmd == 'render':
            img = [env.render('rgb_array') for env in envs]
            remote.send(img)

        else:
            raise NotImplementedError(cmd)
