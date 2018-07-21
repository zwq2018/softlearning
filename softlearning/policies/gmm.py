""" Gaussian mixture policy. """

from contextlib import contextmanager
import numpy as np
import tensorflow as tf

from rllab.misc.overrides import overrides
from rllab.misc import logger
from rllab.core.serializable import Serializable

from softlearning.distributions import GMM
from softlearning.policies import NNPolicy
from softlearning.misc import tf_utils


EPS = 1e-6


class GMMPolicy(NNPolicy, Serializable):
    """
    Gaussian Mixture Model policy

    TODO: change interfaces to match other policies to support returning as
    log_pis for given actions.
    """
    def __init__(self, env_spec, K=2, hidden_layer_sizes=(100, 100), reg=1e-3,
                 squash=True, reparameterize=False, qf=None, name='gmm_policy'):
        """
        Args:
            env_spec (`rllab.EnvSpec`): Specification of the environment
                to create the policy for.
            K (`int`): Number of mixture components.
            hidden_layer_sizes (`list` of `int`): Sizes for the Multilayer
                perceptron hidden layers.
            reg (`float`): Regularization coeffiecient for the GMM parameters.
            squash (`bool`): If True, squash the GMM the gmm action samples
               between -1 and 1 with tanh.
            qf (`ValueFunction`): Q-function approximator.
        """
        Serializable.quick_init(self, locals())

        self._hidden_layers = hidden_layer_sizes
        self._Da = env_spec.action_space.flat_dim
        self._Ds = env_spec.observation_space.flat_dim
        self._K = K
        self._is_deterministic = False
        self._fixed_h = None
        self._squash = squash
        self._qf = qf
        self._reg = reg

        # We can only reparameterize if there was one component in the GMM,
        # in which case one should use softlearning.policies.GaussianPolicy
        assert not reparameterize, "GMMPolicy can't be reparameterized."
        self._reparameterize = reparameterize

        self.name = name
        self.build()

        # TODO.code_consolidation: This should probably call
        # `super(GMMPolicy, self).__init__`
        super(NNPolicy, self).__init__(env_spec)

    def actions_for(self, observations, latents=None,
                    name=None, reuse=tf.AUTO_REUSE,
                    with_log_pis=False, regularize=False):
        name = name or self.name

        with tf.variable_scope(name, reuse=reuse):
            distribution = GMM(
                K=self._K,
                hidden_layers_sizes=self._hidden_layers,
                Dx=self._Da,
                cond_t_lst=(observations,),
                reg=self._reg
            )

        raw_actions = tf.stop_gradient(distribution.x_t)

        actions = tf.tanh(raw_actions) if self._squash else raw_actions

        # TODO: should always return same shape out
        # Figure out how to make the interface for `log_pis` cleaner
        if with_log_pis:
            # TODO.code_consolidation: should come from log_pis_for
            log_pis = distribution.log_p_t
            if self._squash:
                log_pis -= self._squash_correction(raw_actions)
            return actions, log_pis

        return actions

    def build(self):
        self._observations_ph = tf.placeholder(
            dtype=tf.float32,
            shape=(None, self._Ds),
            name='observations',
        )

        self._latents_ph = tf.placeholder(
            dtype=tf.float32,
            shape=(None, self._Da),
            name='latents',
        )

        self.sample_z = tf.random_uniform([], 0, self._K, dtype=tf.int32)

        # TODO.code_consolidation:
        # self.distribution is used very differently compared to the
        # `LatentSpacePolicy`s distribution.
        # This does not use `self.actions_for` because we need to manually
        # access e.g. `self.distribution.mus_t`
        with tf.variable_scope(self.name, reuse=tf.AUTO_REUSE):
            self.distribution = GMM(
                K=self._K,
                hidden_layers_sizes=self._hidden_layers,
                Dx=self._Da,
                cond_t_lst=(self._observations_ph,),
                reg=self._reg,
            )

        raw_actions = tf.stop_gradient(self.distribution.x_t)

        self._actions = tf.tanh(raw_actions) if self._squash else raw_actions
        self._log_pis = self.distribution.log_p_t
        self._raw_actions = raw_actions
        # TODO.code_consolidation:
        # This should be standardized with LatentSpacePolicy/NNPolicy
        # self._determistic_actions = self.actions_for(self._observations_ph,
        #                                              self._latents_ph)

    @overrides
    def get_actions(self, observations, with_log_pis=False, with_raw_actions=False):
        """Sample actions based on the observations.

        If `self._is_deterministic` is True, returns a greedily sampled action
        for the observations. If False, return stochastically sampled action.

        TODO.code_consolidation: This should be somewhat similar with
        `LatentSpacePolicy.get_actions`.
        """
        if self._is_deterministic: # Handle the deterministic case separately.
            if self._qf is None: raise AttributeError

            assert not with_log_pis, 'No log pi for deterministic action'

            feed_dict = {self._observations_ph: observations}

            # TODO.code_consolidation: these shapes should be double checked
            # for case where `observations.shape[0] > 1`
            mus = tf.get_default_session().run(
                self.distribution.mus_t, feed_dict)[0]  # K x Da

            squashed_mus = np.tanh(mus) if self._squash else mus
            qs = self._qf.eval(observations, squashed_mus)

            if self._fixed_h is not None:
                h = self._fixed_h  # TODO.code_consolidation: needs to be tiled
            else:
                h = np.argmax(qs)  # TODO.code_consolidation: check the axis

            actions = squashed_mus[h, :][None]
            raw_actions = mus[h, :][None]

            if with_raw_actions:
                return actions, raw_actions

            return actions

        return super(GMMPolicy, self).get_actions(
            observations, with_log_pis, with_raw_actions)

    @contextmanager
    def deterministic(self, set_deterministic=True, latent=None):
        """Context manager for changing the determinism of the policy.

        See `self.get_action` for further information about the effect of
        self._is_deterministic.

        Args:
            set_deterministic (`bool`): Value to set the self._is_deterministic
                to during the context. The value will be reset back to the
                previous value when the context exits.
            latent (`Number`): Value to set the latent variable to over the
                deterministic context.
        """
        was_deterministic = self._is_deterministic
        old_fixed_h = self._fixed_h

        self._is_deterministic = set_deterministic
        if latent is not None:
            self._fixed_h = latent

        yield

        self._is_deterministic = was_deterministic
        self._fixed_h = old_fixed_h

    def log_diagnostics(self, iteration, batch):
        """Record diagnostic information to the logger.

        Records the mean, min, max, and standard deviation of the GMM
        means, component weights, and covariances.
        """

        feeds = {self._observations_ph: batch['observations']}
        sess = tf_utils.get_default_session()
        mus, log_sigs, log_ws, log_pis = sess.run(
            (
                self.distribution.mus_t,
                self.distribution.log_sigs_t,
                self.distribution.log_ws_t,
                # TODO: Move log_pi and correction under self.log_pi_for()
                (self.distribution.log_p_t
                 - self._squash_correction(self.distribution.x_t)),
            ),
            feeds
        )

        logger.record_tabular('gmm-mus-mean', np.mean(mus))
        logger.record_tabular('gmm-mus-min', np.min(mus))
        logger.record_tabular('gmm-mus-max', np.max(mus))
        logger.record_tabular('gmm-mus-std', np.std(mus))

        logger.record_tabular('gmm-log-w-mean', np.mean(log_ws))
        logger.record_tabular('gmm-log-w-min', np.min(log_ws))
        logger.record_tabular('gmm-log-w-max', np.max(log_ws))
        logger.record_tabular('gmm-log-w-std', np.std(log_ws))

        logger.record_tabular('gmm-log-sigs-mean', np.mean(log_sigs))
        logger.record_tabular('gmm-log-sigs-min', np.min(log_sigs))
        logger.record_tabular('gmm-log-sigs-max', np.max(log_sigs))
        logger.record_tabular('gmm-log-sigs-std', np.std(log_sigs))

        logger.record_tabular('-log-pi-mean', np.mean(-log_pis))
        logger.record_tabular('-log-pi-min', np.min(-log_pis))
        logger.record_tabular('-log-pi-max', np.max(-log_pis))
        logger.record_tabular('-log-pi-std', np.std(-log_pis))