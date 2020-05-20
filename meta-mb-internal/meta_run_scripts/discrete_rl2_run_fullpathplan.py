from meta_mb.baselines.linear_baseline import LinearFeatureBaseline
from meta_mb.meta_envs.point.point_env_1d import MetaPointEnv
from meta_mb.meta_envs.rl2_env import rl2env
from meta_mb.envs.normalized_env import normalize
from meta_mb.algos.ppo import PPO
from meta_mb.trainers.mf_trainer import Trainer
from meta_mb.samplers.meta_samplers.meta_sampler import MetaSampler
from meta_mb.samplers.meta_samplers.rl2_sample_processor import RL2SampleProcessor
from meta_mb.policies.discrete_rnn_policy import DiscreteRNNPolicy
import os
from meta_mb.logger import logger
import json
import numpy as np
from experiment_utils.run_sweep import run_sweep
from meta_mb.utils.utils import set_seed, ClassEncoder
import tensorflow as tf
from babyai.levels.iclr19_levels import Level_GoToIndexedObj 
from babyai.oracle.batch_teacher import BatchTeacher
from babyai.oracle.action_advice import ActionAdvice
from babyai.oracle.cartesian_corrections import CartesianCorrections
from babyai.oracle.physical_correction import PhysicalCorrections
from babyai.oracle.landmark_correction import LandmarkCorrection
from babyai.oracle.demo_corrections import DemoCorrections

from babyai.bot import Bot
import joblib

INSTANCE_TYPE = 'c4.xlarge'
EXP_NAME = 'temp'

def run_experiment(**config):
    exp_dir = os.getcwd() + '/data/' + EXP_NAME + str(config['seed'])
    logger.configure(dir=exp_dir, format_strs=['stdout', 'log', 'csv', 'tensorboard'], snapshot_mode='last_gap', snapshot_gap=50)
    json.dump(config, open(exp_dir + '/params.json', 'w'), indent=2, sort_keys=True, cls=ClassEncoder)
    set_seed(config['seed'])
    config_sess = tf.ConfigProto()
    config_sess.gpu_options.allow_growth = True
    config_sess.gpu_options.per_process_gpu_memory_fraction = config.get('gpu_frac', 0.95)
    sess = tf.Session(config=config_sess)
    reward_predictor = None
    with sess.as_default() as sess:
        if config['saved_path'] is not None:
            saved_model = joblib.load(config['saved_path'])
            policy = saved_model['policy']
            baseline = saved_model['baseline']
            env = saved_model['env']
            start_itr = saved_model['itr']
            obs_dim = env._wrapped_env.reset().shape[0]
            obs_dim = obs_dim + np.prod(env.action_space.n) + 1 + 1 # obs + act + rew + done
            if 'reward_predictor' not in saved_model:
                reward_predictor = DiscreteRNNPolicy(
                    name="reward-predictor",
                    action_dim=2,
                    obs_dim=obs_dim - 1,
                    meta_batch_size=config['meta_batch_size'],
                    hidden_sizes=config['hidden_sizes'],
                    cell_type=config['cell_type']
                )
            else:
                reward_predictor = saved_model['reward_predictor']
        else:
            baseline = config['baseline']()
            e_new = Level_GoToIndexedObj(start_loc='bottom', num_dists=5)
            e_new_copy = Level_GoToIndexedObj(start_loc='bottom', num_dists=5)
            feedback_class = None
            if config["feedback_type"] is None:
                teacher = None
            else:
                if config["feedback_type"] == 'ActionAdvice':
                    teacher = BatchTeacher([ActionAdvice(Bot, e_new)])
                elif config["feedback_type"] == 'DemoCorrections':
                    teacher = BatchTeacher([DemoCorrections(Bot, e_new)])
                elif config["feedback_type"] == 'LandmarkCorrection':
                    teacher = BatchTeacher([LandmarkCorrection(Bot, e_new)])
                elif config["feedback_type"] == 'CartesianCorrections':
                    teacher = BatchTeacher([CartesianCorrections(Bot, e_new)])
                elif config["feedback_type"] == 'PhysicalCorrections':
                    teacher = BatchTeacher([PhysicalCorrections(Bot, e_new)])
                elif config["feedback_type"] == 'Combined':
                    bots = []
                    for fb in config["feedback_combine"]:
                        if fb == 'ActionAdvice':
                            bot_curr = ActionAdvice(Bot, e_new, env_copy=e_new_copy)
                        elif fb == 'DemoCorrections':
                            bot_curr = DemoCorrections(Bot, e_new)
                        elif fb == 'LandmarkCorrection':
                            bot_curr = LandmarkCorrection(Bot, e_new)
                        elif fb == 'CartesianCorrections':
                            bot_curr = CartesianCorrections(Bot, e_new)
                        elif fb == 'PhysicalCorrections':
                            bot_curr = PhysicalCorrections(Bot, e_new)
                        bots.append(bot_curr)
                    teacher = BatchTeacher(bots)
                else:
                    assert NotImplementedError("THIS IS NOT MPLEMENTED")
            e_new.teacher = teacher
            # TODO: Unhardcode this ceil-reward thing. It basically sends the reward to 0/1
            env = rl2env(normalize(e_new), ceil_reward=True)
            obs_dim = e_new.reset().shape[0]
            obs_dim = obs_dim + np.prod(env.action_space.n) + 1 + 1 # obs + act + rew + done
            policy = DiscreteRNNPolicy(
                    name="meta-policy",
                    action_dim=np.prod(env.action_space.n),
                    obs_dim=obs_dim,
                    meta_batch_size=config['meta_batch_size'],
                    hidden_sizes=config['hidden_sizes'],
                    cell_type=config['cell_type']
                )
            reward_predictor = DiscreteRNNPolicy(
                name="reward-predictor",
                action_dim=2,
                obs_dim=obs_dim - 1,
                meta_batch_size=config['meta_batch_size'],
                hidden_sizes=config['hidden_sizes'],
                cell_type=config['cell_type']
            )
            start_itr = 0

        sampler = MetaSampler(
            env=env,
            policy=policy,
            rollouts_per_meta_task=config['rollouts_per_meta_task'],
            meta_batch_size=config['meta_batch_size'],
            max_path_length=config['max_path_length'],
            parallel=config['parallel'],
            envs_per_task=1,
            reward_predictor=reward_predictor
        )

        sample_processor = RL2SampleProcessor(
            baseline=baseline,
            discount=config['discount'],
            gae_lambda=config['gae_lambda'],
            normalize_adv=config['normalize_adv'],
            positive_adv=config['positive_adv'],
        )

        algo = PPO(
            policy=policy,
            learning_rate=config['learning_rate'],
            max_epochs=config['max_epochs'],
            backprop_steps=config['backprop_steps'],
            reward_predictor=reward_predictor
        )

        trainer = Trainer(
            algo=algo,
            policy=policy,
            env=env,
            sampler=sampler,
            sample_processor=sample_processor,
            n_itr=config['n_itr'],
            sess=sess,
            start_itr=start_itr,
        )
        trainer.train()


if __name__ == '__main__':

    sweep_params = {
        'algo': ['rl2'],
        'seed': [1, 2, 3],
        'saved_path': [None],
        'baseline': [LinearFeatureBaseline],
        'env': [MetaPointEnv],
        'meta_batch_size': [100],
        "hidden_sizes": [(64,), (128,)],
        'backprop_steps': [50, 100, 200],
        "rollouts_per_meta_task": [2],
        "parallel": [False],
        "max_path_length": [200],
        "discount": [0.99],
        "gae_lambda": [1.0],
        "normalize_adv": [True],
        "positive_adv": [False],
        "learning_rate": [1e-3],
        "max_epochs": [5],
        "cell_type": ["lstm"],
        "num_minibatches": [1],
        "n_itr": [1000],
        'exp_tag': ['v0'],
        'log_rand': [0, 1, 2, 3],
        "feedback_type": ['Combined'],
        "feedback_combine": [('ActionAdvice',)]
        #'timeskip': [1, 2, 3, 4]
    }
    run_sweep(run_experiment, sweep_params, EXP_NAME, INSTANCE_TYPE)
