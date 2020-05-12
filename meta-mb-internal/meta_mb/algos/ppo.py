from meta_mb.logger import logger
from meta_mb.algos.base import Algo
from meta_mb.optimizers.rl2_first_order_optimizer import RL2FirstOrderOptimizer
from meta_mb.optimizers.first_order_optimizer import FirstOrderOptimizer
from meta_mb.utils import Serializable
from meta_mb.policies.discrete_rnn_policy import DiscreteRNNPolicy
import tensorflow as tf
from collections import OrderedDict


class PPO(Algo, Serializable):
    """
    Algorithm for PPO MAML

    Args:
        policy (Policy): policy object
        name (str): tf variable scope
        learning_rate (float): learning rate for the meta-objective
        exploration (bool): use exploration / pre-update sampling term / E-MAML term
        inner_type (str): inner optimization objective - either log_likelihood or likelihood_ratio
        inner_lr (float) : gradient step size used for inner step
        meta_batch_size (int): number of meta-learning tasks
        num_inner_grad_steps (int) : number of gradient updates taken per maml iteration
        trainable_inner_step_size (boolean): whether make the inner step size a trainable variable
    """
    def __init__(
            self,
            policy,
            name="ppo",
            learning_rate=1e-3,
            clip_eps=0.2,
            max_epochs=5,
            max_epochs_r = 20,
            entropy_bonus=0.,
            reward_predictor=None,
            **kwargs
            ):

        # TODO: Check to avoid duplicates of variables and scopes
        self.reward_predictor = reward_predictor
        Serializable.quick_init(self, locals())
        super(PPO, self).__init__(policy)

        self.recurrent = getattr(self.policy, 'recurrent', False)
        if self.recurrent:
            backprop_steps = kwargs.get('backprop_steps', 32)
            self.optimizer = RL2FirstOrderOptimizer(learning_rate=learning_rate, max_epochs=max_epochs,
                                                    backprop_steps=backprop_steps)
            if self.reward_predictor is not None:
                self.optimizer_r = RL2FirstOrderOptimizer(learning_rate=learning_rate, max_epochs=max_epochs_r, backprop_steps=backprop_steps)
        else:
            self.optimizer = FirstOrderOptimizer(learning_rate=learning_rate, max_epochs=max_epochs)
        # TODO figure out what this does
        self._optimization_keys = ['observations', 'actions', 'advantages', 'rewards', 'agent_infos', 'env_infos']
        self._optimization_r_keys = ['observations', 'actions', 'advantages', 'rewards', 'agent_infos', 'env_infos']
        self.name = name
        self._clip_eps = clip_eps
        self.entropy_bonus = entropy_bonus

        self.build_graph()

    def build_graph(self):
        """
        Creates the computation graph

        Notes:
            Pseudocode:
            for task in meta_batch_size:
                make_vars
                init_init_dist_sym
            for step in num_inner_grad_steps:
                for task in meta_batch_size:
                    make_vars
                    update_init_dist_sym
            set objectives for optimizer
        """

        """ Create Variables """

        """ ----- Build graph for the meta-update ----- """
        self.op_phs_dict = OrderedDict()
        if isinstance(self.policy, DiscreteRNNPolicy):
            discrete = True
        else:
            discrete = False
        obs_ph, action_ph, adv_ph, r_ph, obs_r_ph, dist_info_old_ph, all_phs_dict = self._make_input_placeholders('train',
                                                                                                  recurrent=self.recurrent, 
                                                                                                  discrete=discrete)
        self.op_phs_dict.update(all_phs_dict)

        if self.recurrent:
            distribution_info_vars, hidden_ph, next_hidden_var = self.policy.distribution_info_sym(obs_ph)
            # TODO: Check if anything is problematic here, when obs is concatenating previous reward
            if self.reward_predictor is not None:
                distribution_info_vars_r, hidden_ph_r, next_hidden_var_r = self.reward_predictor.distribution_info_sym(obs_r_ph)
        else:
            distribution_info_vars = self.policy.distribution_info_sym(obs_ph)
            hidden_ph, next_hidden_var = None, None

        """ Outer objective """
        # TODO: Check if anything changes for discrete
        likelihood_ratio = self.policy.distribution.likelihood_ratio_sym(action_ph, dist_info_old_ph,
                                                                         distribution_info_vars)
        # TODO: Check if anything changes for discrete
        clipped_obj = tf.minimum(likelihood_ratio * adv_ph,
                                 tf.clip_by_value(likelihood_ratio,
                                                  1 - self._clip_eps,
                                                  1 + self._clip_eps ) * adv_ph)
        # TODO: Check that the discrete entropy looks fine
        surr_obj = - tf.reduce_mean(clipped_obj) - self.entropy_bonus * \
                        tf.reduce_mean(self.policy.distribution.entropy_sym(distribution_info_vars))
        if self.reward_predictor is not None:
            r_obj =  -tf.reduce_mean(tf.exp(5*r_ph)*self.reward_predictor.distribution.log_likelihood_sym(tf.cast(r_ph, tf.int32), distribution_info_vars_r))
            self.optimizer_r.build_graph(
                loss=r_obj,
                target=self.reward_predictor,
                input_ph_dict=self.op_phs_dict, # TODO: Check this
                hidden_ph=hidden_ph_r,
                next_hidden_var=next_hidden_var_r
            )

        self.optimizer.build_graph(
            loss=surr_obj,
            target=self.policy,
            input_ph_dict=self.op_phs_dict,
            hidden_ph=hidden_ph,
            next_hidden_var=next_hidden_var
        )

    def optimize_policy(self, samples_data, log=True, prefix='', verbose=False):
        """
        Performs MAML outer step

        Args:
            samples_data (list) : list of lists of lists of samples (each is a dict) split by gradient update and
             meta task
            log (bool) : whether to log statistics

        Returns:
            None
        """
        input_dict = self._extract_input_dict(samples_data, self._optimization_keys, prefix='train')

        if verbose: logger.log("Optimizing")
        loss_before = self.optimizer.optimize(input_val_dict=input_dict)

        if verbose: logger.log("Computing statistics")
        loss_after = self.optimizer.loss(input_val_dict=input_dict)

        if log:
            logger.logkv(prefix+'LossBefore', loss_before)
            logger.logkv(prefix+'LossAfter', loss_after)


    def optimize_reward(self, samples_data, log=True, prefix='', verbose=False):
        """
        Performs MAML outer step

        Args:
            samples_data (list) : list of lists of lists of samples (each is a dict) split by gradient update and
             meta task
            log (bool) : whether to log statistics

        Returns:
            None
        """
        input_dict = self._extract_input_dict(samples_data, self._optimization_r_keys, prefix='train')

        if verbose: logger.log("Optimizing")
        loss_before = self.optimizer_r.optimize(input_val_dict=input_dict)

        if verbose: logger.log("Computing statistics")
        loss_after = self.optimizer_r.loss(input_val_dict=input_dict)

        if log:
            logger.logkv(prefix+'RewardLossBefore', loss_before)
            logger.logkv(prefix+'RewardLossAfter', loss_after)


    def __getstate__(self):
        state = dict()
        state['init_args'] = Serializable.__getstate__(self)
        print('getstate\n')
        print(state['init_args'])
        state['policy'] = self.policy.__getstate__()
        state['optimizer'] = self.optimizer.__getstate__()
        return state

    def __setstate__(self, state):
        Serializable.__setstate__(self, state['init_args'])
        self.policy.__setstate__(state['policy'])
        self.optimizer.__getstate__(state['optimizer'])