import logging
import datetime

import tqdm
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

from agnfinder.tf_sampling.api import Sampler, SamplingProblem, get_log_prob_fn


class SamplerHMC(Sampler):

    def __init__(self, problem: SamplingProblem, n_burnin, n_samples, n_chains, init_method='random'):
        assert tf.executing_eagerly()  # required for sampling
        self.problem = problem
        self.n_burnin = n_burnin
        self.n_samples = n_samples
        self.n_chains = n_chains
        assert init_method in {'random', 'optimised', 'correct', 'roughly_correct'}
        self.init_method = init_method

    def sample(self):
        start_time = datetime.datetime.now()

        log_prob_fn = get_log_prob_fn(self.problem.forward_model, self.problem.true_observation, self.problem.fixed_params, self.problem.uncertainty)
        initial_state = self.get_initial_state()  # numpy

        with np.printoptions(precision=2, suppress=False):
            logging.info('\nInitial state: ')
            logging.info(initial_state)
            logging.info('Median initial state:')
            logging.info(np.median(initial_state, axis=0))
            if self.problem.true_params is not None:
                logging.info('True params:')
                logging.info(self.problem.true_params)

        initial_samples, is_accepted = self.run_hmc(log_prob_fn, initial_state, burnin_only=True)

        logging.debug(is_accepted.numpy().shape)
        logging.debug(is_accepted.numpy())
        # identify which samples aren't adapted
        accepted_per_galaxy = tf.reduce_mean(input_tensor=is_accepted, axis=0)
        successfully_adapted = accepted_per_galaxy > tf.ones([self.n_chains]) * .6  # min acceptance of 60%

        for n, adapted in enumerate(successfully_adapted.numpy()):
            if not adapted:
                logging.warning('Removing galaxy {} due to low acceptance (p={:.2f})'.format(n, accepted_per_galaxy[n]))

        # filter samples, true_observation (and true_params) to remove them
        initial_samples_filtered = tf.boolean_mask(
            tensor=initial_samples,
            mask=successfully_adapted,
            axis=1
        )
        self.problem.true_observation = tf.boolean_mask(
            tensor=self.problem.true_observation,
            mask=successfully_adapted,
            axis=0
        )
        self.problem.fixed_params = tf.boolean_mask(
            tensor=self.problem.fixed_params,
            mask=successfully_adapted,
            axis=0
        )
        self.problem.uncertainty = tf.boolean_mask(
            tensor=self.problem.uncertainty,
            mask=successfully_adapted,
            axis=0
        )
        self.problem.true_params = tf.boolean_mask(
            tensor=self.problem.true_params,
            mask=successfully_adapted,
            axis=0
        )
        self.n_chains = tf.reduce_sum(input_tensor=tf.cast(successfully_adapted, tf.int32))

        # get new log_prob_fn
        log_prob_fn = get_log_prob_fn(self.problem.forward_model, self.problem.true_observation, self.problem.fixed_params, self.problem.uncertainty)
        
        # continue, for real this time
        final_samples, is_accepted = self.run_hmc(log_prob_fn, initial_samples_filtered[-1], burnin_only=False)

        # TODO am I supposed to be filtering for accepted samples? is_accepted has the same shape as samples, and is binary.
        end_time = datetime.datetime.now()
        logging.info('Total time for galaxies: {}s'.format( (end_time - start_time).total_seconds()))
        metadata = {'is_accepted': is_accepted}
        sample_weights = np.ones((final_samples.shape[:2]))  # 0 and 1 dimensions
        log_evidence = np.ones_like(sample_weights)
        return final_samples, successfully_adapted.numpy(), sample_weights, log_evidence, metadata

    def get_initial_state(self):
        if self.init_method == 'correct':
            assert self.problem.true_params is not None
            initial_state = tf.constant(self.problem.true_params, dtype=tf.float32)
        elif self.init_method == 'roughly_correct':
            assert self.problem.true_params is not None
            initial_state = roughly_correct_start(self.problem.true_params, self.n_chains)
        elif self.init_method == 'random':
            initial_state = tf.random.uniform(self.problem.true_params.shape, minval=0., maxval=1.)
            # initial_state = many_random_starts(self.problem.forward_model, self.problem.true_observation, self.problem.param_dim, self.n_chains)
        elif self.init_method == 'optimised':
            initial_state = optimised_start(
                self.problem.forward_model,
                tf.constant(self.problem.true_observation),
                tf.constant(self.problem.fixed_params),
                tf.constant(self.problem.uncertainty),
                tf.constant(self.problem.param_dim),
                tf.constant(self.n_chains),
                steps=tf.constant(3000)
            )
        else:
            raise ValueError('Initialisation method {} not recognised'.format(self.init_method))
        return initial_state

    def run_hmc(self, log_prob_fn, initial_state, burnin_only=False):

        if burnin_only:
            n_samples = 3000
        else:
            n_samples = self.n_samples
        
        logging.info('Ready to go - beginning sampling at {}'.format(datetime.datetime.now().ctime()))
        start_time = datetime.datetime.now()
        samples, initial_trace = hmc(
            log_prob_fn=log_prob_fn,
            initial_state=initial_state,
            n_samples=n_samples,  # before stopping and checking that adaption has succeeded
            n_burnin=self.n_burnin
        )
        end_time = datetime.datetime.now()
        elapsed = end_time - start_time
        samples = samples.numpy()
        ms_per_sample = 1000 * elapsed.total_seconds() / np.prod(samples.shape)  # not counting burn-in as a sample, so really quicker
        logging.info('Sampling {} x {} chains complete in {}, {:.3f} ms per sample'.format(n_samples, self.n_chains, elapsed, ms_per_sample))
        
        is_accepted = tf.cast(initial_trace['is_accepted'], dtype=tf.float32)
        record_acceptance(is_accepted.numpy())

        return samples, is_accepted

# don't decorate with tf.function
def hmc(log_prob_fn, initial_state, n_samples=int(10e3), n_burnin=int(1e3)):

    assert len(initial_state.shape) == 2  # should be (chain, variables)

    initial_step_size = 1.  # starting point, will be updated by step size adaption
    initial_step_sizes = tf.fill(initial_state.shape, initial_step_size)  # each chain will have own step size
    # this is crucial now that each chain is potentially a different observation

    # NUTS
    # step size adaption from
    # https://github.com/tensorflow/probability/issues/549
    # transition_kernel = tfp.mcmc.NoUTurnSampler(
    #     target_log_prob_fn=log_prob_fn,
    #     step_size=initial_step_sizes
    # )
    # adaptive_kernel = tfp.mcmc.DualAveragingStepSizeAdaptation(
    #     transition_kernel,
    #     num_adaptation_steps=int(n_burnin * 0.8),
    #     step_size_setter_fn=lambda pkr, new_step_size: pkr._replace(step_size=new_step_size),
    #     step_size_getter_fn=lambda pkr: pkr.step_size,
    #     log_accept_prob_getter_fn=lambda pkr: pkr.log_accept_ratio
    # )

    # or HMC
    transition_kernel = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=log_prob_fn,
        step_size=initial_step_sizes,
        num_leapfrog_steps=10
    )
    adaptive_kernel = tfp.mcmc.DualAveragingStepSizeAdaptation(
        transition_kernel,
        num_adaptation_steps=int(n_burnin * 0.8),
    )

    # seperate function so it can be decorated
    @tf.function(experimental_compile=True)
    def run_chain():
        # Run the chain (with burn-in).
        # chain_output = tfp.mcmc.sample_chain(
        #     num_results=n_samples,
        #     num_burnin_steps=n_burnin,
        #     current_state=initial_state,
        #     kernel=adaptive_kernel,
        #     # https://github.com/tensorflow/probability/blob/f90448698cc2a16e20939686ef0d5005aad95f29/tensorflow_probability/python/mcmc/nuts.py#L72
        #     trace_fn=lambda _, prev_kernel_results: {'is_accepted': prev_kernel_results.inner_results.is_accepted, 'step_size': prev_kernel_results.inner_results.step_size}
        # )
        chain_output = tfp.mcmc.sample_chain(
            num_results=n_samples,
            num_burnin_steps=n_burnin,
            current_state=initial_state,
            kernel=adaptive_kernel,
            parallel_iterations=1,  # makes no difference at all to performance, when tested - worth checking before final run
            # https://github.com/tensorflow/probability/blob/f90448698cc2a16e20939686ef0d5005aad95f29/tensorflow_probability/python/mcmc/nuts.py#L72
            trace_fn=lambda _, prev_kernel_results: {'is_accepted': prev_kernel_results.inner_results.is_accepted},
            # num_steps_between_results=5  # thinning factor of 5, to run much longer
        )

        samples, trace = chain_output
        return samples, trace

    samples, trace = run_chain()

    return samples, trace

def record_acceptance(is_accepted):
    logging.info('Mean acceptance ratio over all chains: {:.4f}'.format(np.mean(is_accepted)))
    mean_acceptance_per_chain = np.mean(is_accepted, axis=0)
    for chain_i, chain_acceptance in enumerate(mean_acceptance_per_chain):
        if chain_acceptance < 0.01:
            logging.critical(f'HMC failed to adapt for chain {chain_i} - is step size too small? Is there some burn-in?')
        elif chain_acceptance < 0.3:
            logging.critical('Acceptance ratio is low for chain {}: ratio {:.2f}'.format(chain_i, chain_acceptance))


def optimised_start(forward_model, observations, fixed_params, uncertainty, param_dim, n_chains, steps):
    start_time = datetime.datetime.now()
    best_params = find_best_params(forward_model, observations, fixed_params, uncertainty, param_dim, n_chains, steps)
    # chosen_params = tf.reshape(all_best_params, [n_chains, param_dim])
    end_time = datetime.datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    logging.info('Done {} optimisations over {} steps in {} seconds.'.format(n_chains, steps, elapsed))
    # exit()
    return best_params


def find_best_params(forward_model, observations, fixed_params, uncertainty, param_dim, batch_dim, steps, n_attempts=15):
    log_prob_fn = get_log_prob_fn(forward_model, observations, fixed_params, uncertainty)


    # repeatedly run ADAM
    all_params = [[] for n in range(batch_dim)]
    all_costs = [[] for n in range(batch_dim)]
    for _ in tqdm.tqdm(range(n_attempts)):
        initial_params = tf.Variable(tf.random.uniform([batch_dim, param_dim], dtype=tf.float32), dtype=tf.float32)
        initial_params, latest_costs = find_minima(lambda x: -log_prob_fn(x), initial_params, steps, param_dim, batch_dim)  # a very important minus sign...

        initial_params = initial_params.numpy()
        latest_costs = latest_costs.numpy()
        
        for n in range(batch_dim):
            all_params[n].append(initial_params[n])
            all_costs[n].append(latest_costs[n])


    lowest_costs_by_galaxy = [np.argmin(x) for x in all_costs]
    best_params_by_galaxy = [x[cost] for x, cost in zip(all_params, lowest_costs_by_galaxy)]
    best_params = np.array(best_params_by_galaxy)
    print(best_params.shape)

        # successful_indices = tf.range(batch_dim)[success]
        # failed_indices = tf.range(batch_dim)[~success]

        # successful_rows = latest_params[success]
        # failed_rows = latest_params[~success]  # only for shape

        # new_starts = tf.Variable(tf.random.uniform(tf.shape(failed_rows), dtype=tf.float32))

        # latest_params = tf.dynamic_stitch(
        #     [successful_indices, failed_indices],
        #     [successful_rows, new_starts]
        # )

        # rows = tf.split(latest_params)

        # for row_n, row in enumerate(rows):
        #     if not success[row_n]:
        #         latest_params[row_n] = tf.Variable(tf.random.uniform([param_dim], dtype=tf.float32))

    return best_params


# TODO convert to be tf.function() friendly, creating no ops
# TODO measure convergence and adjust steps, learning rate accordingly
def find_minima(func, initial_guess, steps, param_dim, batch_dim):

    # if method == 'bfgs':
        
    #     def value_and_gradients_function(x):
    #         # print(x)
    #         with tf.GradientTape() as tape:
    #             func_value = func(x)
    #             grads = tape.gradient(func_value, [x])[0]
    #             assert grads is not None
    #             return (func_value, grads)
    #     # func_value, gradients = value_and_gradients_function(initial_guess)
    #     # print(gradients)
    #     optim_results = tfp.optimizer.bfgs_minimize(
    #         value_and_gradients_function,
    #         initial_position=initial_guess,
    #         tolerance=1e-8
    #     )
    #     return optim_results.position

    # logging.info('Initial guess neglogprob: {}'.format(['{:4.1f}'.format(x) for x in func(initial_guess)]))
    func_value = tf.Variable(np.zeros(batch_dim), dtype=tf.float32)  # euclid bands hardcoded
    grads = tf.Variable(np.zeros([batch_dim, param_dim]), dtype=tf.float32)
    method = tf.keras.optimizers.Adam()

    initialise_adam_for_tf(func, initial_guess, method)
    for _ in range(steps):
        update_initial_guess(func, initial_guess, grads, method)
    # logging.info('Final neglogprob: {}'.format(['{:4.1f}'.format(x) for x in func(initial_guess)]))
    # final check
    # success = func_value < max_cost
    func_value.assign(func(initial_guess))
    return initial_guess, func_value

@tf.function(experimental_compile=True)
def update_initial_guess(func, initial_guess, grads, method):
    with tf.GradientTape(watch_accessed_variables=False) as tape:
        tape.watch(initial_guess)
        grads.assign(tape.gradient(func(initial_guess), [initial_guess])[0])
        # print(tf.shape(grads))
        # grads_and_vars = [(grads, initial_guess)]
        # print(tf.shape(grads_and_vars))
    method.apply_gradients([(grads, initial_guess)])  # inplace
    # initial_guess = tf.clip_by_value(initial_guess)
    initial_guess.assign(tf.clip_by_value(initial_guess, 0.01, 0.99))
        # return initial_guess, func_value
        # return func_value

def initialise_adam_for_tf(func, initial_guess, method):
    with tf.GradientTape() as tape:
        tape.watch(initial_guess)
        grads = tape.gradient(func(initial_guess), [initial_guess])[0]
        # print(tf.shape(grads))
        # grads_and_vars = [(grads, initial_guess)]
        # print(tf.shape(grads_and_vars))
    method.apply_gradients([(grads, initial_guess)])  # inplace


def many_random_starts(forward_model, observation, param_dim, n_chains, overproposal_factor=None):
    raise NotImplementedError('Deprecated now that each chain is a galaxy')
    # if overproposal_factor is None:
    #     assert n_chains < 100
    #     overproposal_factor = int(10 - (7 * 0.01 * n_chains))  # down from 10. Customised for laptop memory TODO tweak for Glamdring?
    # overproposed_initial_state = tf.random.uniform(shape=(n_chains * overproposal_factor, param_dim))
    # initial_state = keep_top_params(
    #     overproposed_initial_state,
    #     forward_model,
    #     observation,
    #     n_chains * overproposal_factor,  # specified explicitly but might not need to
    #     n_chains)
    # return initial_state

def keep_top_params(all_params, forward_model, true_observation, fixed_params, uncertainty, initial_dim, final_dim):
    log_prob_fn = get_log_prob_fn(forward_model, true_observation, fixed_params, uncertainty)
    initial_log_probs = log_prob_fn(all_params)
    initial_state = tf.gather(all_params, tf.argsort(initial_log_probs, direction='DESCENDING'))[:final_dim]
    return initial_state

def roughly_correct_start(true_params, n_chains):
    not_quite_true_params = true_params + np.random.rand(*true_params.shape) * 0.03
    initial_state = tf.constant(not_quite_true_params, dtype=tf.float32)
    return initial_state
