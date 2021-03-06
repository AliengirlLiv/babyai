import joblib
import json
import numpy as np
import os
import tensorflow as tf
import argparse
from meta_mb.samplers.utils import rollout
from experiment_utils.utils import load_exps_data, load_single_exp_data
from babyai.levels import iclr19_levels
from babyai.levels.iclr19_levels import *
from babyai.oracle.batch_teacher import BatchTeacher
from babyai.oracle.post_action_advice import PostActionAdvice
from babyai.bot import Bot
from meta_mb.meta_envs.rl2_env import rl2env
from meta_mb.envs.normalized_env import normalize

"""
 python /home/ignasi/GitRepos/meta-mb/experiment_utils/save_videos.py data/s3/mbmpo-pieter/ --speedup 4 -n 1 --max_path_length 300 --ignore_done
"""


def valid_experiment(params):
    return True
    # values = {'max_path_length': [200],
    #           'dyanmics_hidden_nonlinearity': ['relu'],
    #           'dynamics_buffer_size': [10000],
    #           'env': [{'$class': 'meta_mb.envs.mujoco.walker2d_env.Walker2DEnv'}]}

    # 'env': [{'$class': 'meta_mb.envs.mujoco.walker2d_env.Walker2DEnv'}]}
    # 'env': [{'$class': 'meta_mb.envs.mujoco.ant_env.AntEnv'}]}
    # 'env': [{'$class': 'meta_mb.envs.mujoco.hopper_env.HopperEnv'}]}
    # #
    values = {'max_path_length': [200],
              'num_rollouts': [100],
              'env': [{'$class': 'meta_mb.envs.mujoco.walker2d_env.Walker2DEnv'}]}


    for k, v in values.items():
        if params[k] not in v:
            return False
    return True


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=str)
    parser.add_argument('--max_path_length', '-l', type=int, default=None,
                        help='Max length of rollout')
    parser.add_argument('--num_rollouts', '-n', type=int, default=6,
                        help='Max length of rollout')
    parser.add_argument('--reward_pred', action='store_true', help="Plot the reward predictor's predictions")
    parser.add_argument('--speedup', type=float, default=1,
                        help='Speedup')
    parser.add_argument('--run_name', type=str, default='latest.pkl',
                        help='Name of pickle file to load')
    parser.add_argument('--gap_pkl', type=int, default=1,
                        help='Gap between pkl policies')
    parser.add_argument('--grid_size', type=int, default=None,
                        help='Length of size of grid')
    parser.add_argument('--holdout_obj', action='store_true')
    parser.add_argument('--num_dists', type=int, default=None,
                        help='Number of distractors')
    parser.add_argument('--use-teacher', action='store_true',
                        help='Whether to use a teacher')
    parser.add_argument('--dense_rewards', action='store_true',
                        help='Use intermediate rewards')
    parser.add_argument('--max_pkl', type=int, default=None,
                        help='Maximum value of the pkl policies')
    parser.add_argument('--prompt', type=bool, default=False,
                        help='Whether or not to prompt for more sim')
    parser.add_argument('--ignore_done', action='store_true',
                        help='Whether stop animation when environment done or continue anyway')
    parser.add_argument('--use_pickled_env', action='store_true',
                        help='Whether to use the pickled env or create a new one')
    parser.add_argument('--stochastic', action='store_true', help='Apply stochastic action instead of deterministic')
    parser.add_argument('--feedback_type', type=str, choices=['none', 'random', 'oracle'], default='none', help='Give random feedback (not useful feedback)')
    parser.add_argument('--animated', action='store_true', help='Show video while generating it.')
    parser.add_argument('--start_loc', type=str, choices=['top', 'bottom', 'all'], help='which set of starting points to use (top, bottom, all)', default='all')
    parser.add_argument('--class_name', type=str, help='which env class to use.', default=None)
    parser.add_argument('--reset_every', type=int, default=2,
                        help='How many runs between each rnn state reset.')
    parser.add_argument('--supervised_model', action='store_true', help="Use the supervised model instead of the RL policy")
    args = parser.parse_args()

    assert args.class_name is not None or args.use_pickled_env
    experiment_paths = [load_single_exp_data(args.path, args.run_name)]
    for exp_path in experiment_paths:
        max_path_length = exp_path['json']['max_path_length'] if args.max_path_length is None else args.max_path_length
        if valid_experiment(exp_path['json']):
            for pkl_path in exp_path['pkl']:
                with tf.Session() as sess:
                    print("\n Testing policy %s \n" % pkl_path)
                    data = joblib.load(pkl_path)
                    if args.supervised_model:
                        policy = data['supervised_model']
                    else:
                        policy = data['policy']
                    if args.reward_pred:
                        reward_predictor = data['reward_predictor']
                    else:
                        reward_predictor = None

                    if hasattr(policy, 'switch_to_pre_update'):
                        policy.switch_to_pre_update()
                    if args.use_pickled_env:
                        env = data['env']
                    else:
                        config = data['config']
                        arguments = {
                          "start_loc": 'all',
                          "include_holdout_obj": False,
                          "persist_goal": config['persist_goal'],
                          "persist_objs": config['persist_objs'],
                          "persist_agent": config['persist_agent'],
                          "dropout_goal": config['dropout_goal'],
                          "dropout_correction": config['dropout_correction'],
                          "dropout_independently": config['dropout_independently'],
                          "feedback_type": config["feedback_type"],
                          "feedback_always": config["feedback_always"],
                          "num_meta_tasks": config["rollouts_per_meta_task"],
                          "intermediate_reward": config['intermediate_reward'],
                        }
                        # if not args.use_teacher:
                        #     arguments['feedback_type'] = None
                        all_attr = dir(iclr19_levels)
                        env_class = getattr(iclr19_levels, args.class_name)
                        # env_args = {
                        #     'start_loc': args.start_loc,
                        #     'include_holdout_obj': args.holdout_obj,
                        # }
                        # if args.grid_size is not None:
                        #     env_args['room_size'] = args.grid_size
                        # if args.num_dists is not None:
                        #     env_args['num_dists'] = args.num_dists
                        # e_new = env_class(**env_args)
                        e_new = env_class(**arguments)
                        e_new.use_teacher = args.use_teacher
                        if args.use_teacher:
                            teacher = PostActionAdvice(Bot, e_new)
                            e_new.teacher = teacher
                            e_new.teacher.set_feedback_type(args.feedback_type)
                        env = rl2env(normalize(e_new))

                    video_filename = os.path.join(args.path, 'saved_video.mp4')
                    paths, accuracy = rollout(env, policy, max_path_length=max_path_length, animated=args.animated, speedup=args.speedup,
                                    video_filename=video_filename, save_video=True, ignore_done=args.ignore_done, batch_size=1,
                                        stochastic=args.stochastic, num_rollouts=args.num_rollouts, reset_every=args.reset_every,
                                    record_teacher=True, reward_predictor=reward_predictor, dense_rewards=args.dense_rewards)
                    print('Average Returns: ', np.mean([sum(path['rewards']) for path in paths]))
                    print('Average Path Length: ', np.mean([path['env_infos'][-1]['episode_length'] for path in paths]))
                    print('Average Success Rate: ', np.mean([path['env_infos'][-1]['success'] for path in paths]))
                    print("ACCURACY", accuracy)

                tf.reset_default_graph()



