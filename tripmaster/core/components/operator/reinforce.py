"""
TM Reinforce Learner
"""
from abc import abstractmethod

import numpy as np

from tripmaster import O, TMBackendFactory
from tripmaster.core.components.environment.base import TMEnvironment, TMScenario, TMEnvironmentPool, \
     TMEnvironmentPoolGroup, TMBatchEnvironment
from tripmaster.core.components.evaluator import MachineEvaluationStreamInfo
from tripmaster.core.components.machine.data_traits import TMSampleBatchTraits
from tripmaster.core.components.machine.reinforce import TMPolicyMachineInterface

from tripmaster import logging
from tripmaster.core.components.operator.operator import TMOperator, TMEvaluatorMixin, TMLearnerMixin
from tripmaster.core.concepts.component import TMConfigurable, TMSerializableComponent
from tripmaster.core.concepts.contract import TMContractChannel
from tripmaster.core.concepts.data import TMDataStream, TMDataLevel
from tqdm import tqdm
from tripmaster import P, T, M, D
import diting.backend as B 
import diting.backend.nn as nn 
from tripmaster.utils.stream import isolate_iterators

logger = logging.getLogger(__name__)

from tripmaster import P
def train_worker(local_rank, learner, env_pool_group, runtime_options):
    """

    Args:
        local_rank ():
        learner ():
        data_streams ():
        evaluator ():

    Returns:

    """
    logger.warning(f"start trainer {local_rank}")

    learner.train(local_rank, env_pool_group, runtime_options)

class TMReplayBuffer(TMConfigurable):

    @abstractmethod
    def accept(self, data):
        pass

    @abstractmethod
    def __iter__(self):
        pass


class TMMemoryReplayBuffer(TMReplayBuffer):

    def __init__(self, hyper_params):
        super().__init__(hyper_params)
        self.buffer = []

    def clear(self):
        self.buffer = []

    def accept(self, data):

        self.buffer.append(data)

    def __iter__(self):
        yield from self.buffer

    def __getitem__(self, item):
        return self.buffer[item]

    def __len__(self):
        return len(self.buffer)


import copy

class TMExploreStrategy(TMSerializableComponent):

    ObservationBatchTraits = TMSampleBatchTraits
    ActionBatchTraits = TMSampleBatchTraits

    @abstractmethod
    def sample_action(self, action_distribution, **kwargs):
        """
        sample actions from distribution
        Args:
            action_distribution: [batch_size, num_actions, action_space_size]
            **kwargs:
        """
        pass

    def explore(self, machine: TMPolicyMachineInterface, batch_env: TMBatchEnvironment, device):

        explored = []

        observation, info = batch_env.reset()

        # batch_env.set_device(device)

        observation = self.ObservationBatchTraits.to_device(observation, device=device)
        batch_mask = T.zeros(batch_env.batch_size(), dtype=P.Types.Bool, device=device)
        while True:
            masked_observation = self.ObservationBatchTraits.mask_batch(observation, batch_mask)
            machine.eval()
            with O.no_grad():
                action_dist, action_info = machine.action_distribution(masked_observation)
                masked_action, log_prob = self.sample_action(action_dist)
            action = self.ActionBatchTraits.recover_masked_batch(masked_action, batch_mask)
            action_info = self.ActionBatchTraits.recover_masked_batch(action_info, batch_mask)

            new_observation, reward, terminated, truncated, env_info = batch_env.step(action, batch_mask=batch_mask)
            new_observation = self.ObservationBatchTraits.to_device(new_observation, device=device)
            reward = TMSampleBatchTraits.to_device(reward, device=device)
            terminated = TMSampleBatchTraits.to_device(terminated, device=device)
            truncated = TMSampleBatchTraits.to_device(truncated, device=device)

            explored.append({
                "observation": observation,
                "action": action,
                "log_prob": log_prob,
                "new_observation": new_observation,
                "reward": reward,
                "batch_mask": batch_mask,
            })

            batch_mask = T.logical_or(batch_mask, terminated)
            batch_mask = T.logical_or(batch_mask, truncated)
            
            if T.all(batch_mask):
                break

            observation = copy.copy(new_observation)
            observation.update(action_info)

        # compute future rewards for each step

        future_reward = batch_env.future_reward(explored)

        for i, data in enumerate(explored):
            data["future_reward"] = future_reward[:, i]
        return explored



class TMReinforceOperator(TMOperator):
    """
    TMReinforceOperator
    """

    def play_once(self, environment: TMBatchEnvironment, device):
        """

            Args:
                environment ():

            Returns:

        """
        observations = environment.reset(return_info=False)

        batch_mask = [obs is None for obs in observations]
        finished = batch_mask[:]

        last_observations = observations
        all_rewards = []
        while not all(finished):
            observations = self.machine.BatchTraits.to_device(observations, device)
            actions = self.machine.policy(observations, batch_mask=finished)
            observations, rewards, truncated, terminated, info = environment.step(actions)
            finished = [f or tru or ter for f, tru, ter in zip(finished, truncated, terminated)]
            last_observations = observations
            all_rewards.append(rewards)
        accumulated_rewards = environment.accumulated_reward(all_rewards)

        return last_observations, accumulated_rewards, batch_mask
    #
    # def fit_memory(self, env_pool: TMEnvironmentPool, scenario: TMScenario):
    #
    #     assert env_pool.level == TMDataLevel.Machine
    #
    #     return env_pool.apply_modeler(self.memory_modeler, scenario)
    #
    # def unfit_memory(self, memory_samplestream: TMDataStream, scenario: TMScenario, with_truth=False):
    #     machine_samplestream = self.memory_modeler.reconstruct_datastream(memory_samplestream,
    #                                                                       scenario=self.scenario)
    #
    #     return machine_samplestream
    #
    # def batchify(self, env_pool: TMEnvironmentPool, scenario: TMScenario):
    #     """
    #
    #     Args:
    #         problem_dataset:
    #
    #     Returns:
    #
    #     """
    #     assert env_pool.level == TMDataLevel.Memory
    #
    #     return env_pool.batchify(self.batch_modeler, scenario)
    #
    #
    # def unbatchify(self, machine_batchstream: TMDataStream, scenario: TMScenario, with_truth=False):
    #     """
    #
    #     Args:
    #         problem_dataset:
    #
    #     Returns:
    #
    #     """
    #
    #     machine_samplestream = self.batch_modeler.reconstruct_datastream(machine_batchstream,
    #                                                                      scenario=self.scenario)
    #
    #     return machine_samplestream

class TMReinforceEvaluatorMixin(TMEvaluatorMixin):
    """
    TMSupervisedEvaluatorMixin
    """

    def __init__(self, hyper_params, host: TMReinforceOperator=None, **kwargs):
        assert host is not None

        self.host = host

        super().__init__(hyper_params, **kwargs)


    def evaluate_envs(self, batch_env_pool: TMEnvironmentPool, scenario, local_rank, epoch, step):
        """
            Args:

        """
        batch_traits = self.host.machine.BatchTraits

        P.OptimizerBehaviors.set_inference_mode(self.host.machine)
        batch_env_pool.scenario = scenario

        eval_env_nums = 0
        with P.OptimizerBehaviors.no_grad():
            for batch_env in batch_env_pool.envs():
                truth = batch_env.truth()
                if truth is not None:
                    truth = self.host.reallocate_data(truth, local_rank)

                try:
                    observations, accumulated_rewards, batch_mask = self.host.play_once(batch_env, self.host.device(local_rank))

                    avg_reward = sum(accumulated_rewards) / batch_env.batch_size()
                    eval_env_nums += batch_env.batch_size()

                    observations.update(truth)
                    yield {"objective": avg_reward, "sample_num": batch_env.batch_size()}, truth, observations

                    if self.hyper_params.eval_env_nums and eval_env_nums >= self.hyper_params.eval_env_nums:
                        break

                except RuntimeError as e:
                    if 'out of memory' in str(e):
                        shapes = batch_traits.data_shape(truth)
                        logger.error(f"Out of Memory: Data Shapes = {shapes}")
                    logger.exception(e)
                    raise e
                except Exception as e:
                    logger.exception(e)
                    raise e

    def evaluate(self, batch_env_pool: TMEnvironmentPool, scenario, local_rank, epoch, step):

        # if local_rank != 0:
        #     return

        batch_env_pool.scenario = scenario

        truth_machine_datastream = TMDataStream()
        truth_machine_datastream.level = TMDataLevel.Batch

        truth_machine_datastream.eval_channels = ["eval"]

        inference_machine_datastream = TMDataStream()
        inference_machine_datastream.level = TMDataLevel.Batch
        inference_machine_datastream.eval_channels = ["eval"]
        channeled_reward_streams = dict()

        reward_stream, truth_stream, inferenced_stream = isolate_iterators(
                self.evaluate_envs(batch_env_pool, scenario, local_rank, epoch, step),
                3
            )
        channeled_reward_streams["eval"] = reward_stream
        truth_machine_datastream["eval"] = truth_stream
        inference_machine_datastream["eval"] = inferenced_stream

        info = MachineEvaluationStreamInfo(objective_stream=channeled_reward_streams,
                                           truth_stream=truth_machine_datastream,
                                           inferenced_stream=inference_machine_datastream,
                                           local_rank=local_rank, device=None, epoch=epoch, step=step)
        evaluation_results = self.evaluate_signal.send(info)
        return evaluation_results

class TMReinforceLearnerMixin(TMLearnerMixin):
    """
    Learner for Reinforce Algorithm
    """

    ReplayBuffer = TMMemoryReplayBuffer
    ExploreStrategy = None

    def __init__(self, hyper_params, **kwargs):

        super().__init__(hyper_params, **kwargs)

        self.replay_buffer = self.ReplayBuffer(self.hyper_params.replay_buffer)
        self.explore_strategy = self.ExploreStrategy(self.hyper_params.explore_strategy)

    def explore(self, environment: TMEnvironment, local_rank):
        """

        Returns:

        """
        device = self.device(local_rank)


        P.OptimizerBehaviors.set_inference_mode(machine=self.machine)
        for explored_data in self.explore_strategy.explore(self.machine, environment, device):

            self.replay_buffer.accept(explored_data)

    def train_step(self, local_rank, channel, data_loader):
        """

        Args:
            local_rank ():
            channel ():
            data_loader ():

        Returns:

        """
        P.OptimizerBehaviors.set_train_mode(self.machine)
        batch_traits = self.machine.BatchTraits

        with tqdm(desc=f"Channel {channel}, Epoch {self.epoch}", leave=False,
                  postfix=dict(batch_size=0, batch_loss=0, average_loss=0), unit="batch") as t:

            total_loss = 0.0
            total_sample_num = 0
            for i, batch in enumerate(data_loader):
                # measure data loading time
                self.optimization_strategy.on_batch_start(self.machine, batch, i)

                if self.optimization_strategy.finish():
                    logger.info("optimizer terminate criterion satisfied. Optimization Finish.")
                    break

                try:
                    batch = self.reallocate_data(batch, local_rank)

                    batch_size = batch_traits.batch_size(batch)
                    observation = batch["observation"]
                    action = batch["action"]

                    future_reward = batch["future_reward"]
                    log_prob = batch["log_prob"]
                    batch_mask = batch["batch_mask"]

                    observation = self.machine.ObservationBatchTraits.mask_batch(observation, batch_mask)
                    action = self.machine.ActionBatchTraits.mask_batch(action, batch_mask)

                    # log_prob = self.machine.action_prob(observation, action)
                    V = self.machine.critic(observation)          
                    A_k = future_reward - V.detach()                                                                       # ALG STEP 5

                    # One of the only tricks I use that isn't in the pseudocode. Normalizing advantages
                    # isn't theoretically necessary, but in practice it decreases the variance of 
                    # our advantages and makes convergence much more stable and faster. I added this because
                    # solving some environments was too unstable without it.
                    A_k = (A_k - A_k.mean()) / (A_k.std() + 1e-10)

                    batch_loss = 0
                    # This is the loop where we update our network for some n epochs
                    for _ in range(self.hyper_params.n_updates_per_iteration):                                                       # ALG STEP 6 & 7
                        # Calculate V_phi and pi_theta(a_t | s_t)
                        V, curr_log_probs = self.machine.evaluate_action(observation, action)

                        # Calculate the ratio pi_theta(a_t | s_t) / pi_theta_k(a_t | s_t)
                        # NOTE: we just subtract the logs, which is the same as
                        # dividing the values and then canceling the log with e^log.
                        # For why we use log probabilities instead of actual probabilities,
                        # here's a great explanation: 
                        # https://cs.stackexchange.com/questions/70518/why-do-we-use-the-log-in-gradient-based-reinforcement-algorithms
                        # TL;DR makes gradient ascent easier behind the scenes.

                        ratios = B.exp(curr_log_probs - log_prob)

                        # Calculate surrogate losses.
                        surr1 = ratios * A_k
                        surr2 = B.clamp(ratios, 1 - self.hyper_params.clip, 1 + self.hyper_params.clip) * A_k

                        # Calculate actor and critic losses.
                        # NOTE: we take the negative min of the surrogate losses because we're trying to maximize
                        # the performance function, but Adam minimizes the loss. So minimizing the negative
                        # performance function maximizes it.
                        actor_loss = (-B.min(surr1, surr2)).mean()
                        critic_loss = nn.MSELoss()(V, future_reward)
                        entropy_loss = -B.mean(-log_prob)

                        loss = actor_loss + self.hyper_params.ent_coef * entropy_loss + \
                                self.hyper_params.vf_coef * critic_loss



                    # J = (log_prob * future_reward).mean()
                    # J.requires_grad = True


                    # batch.update(output)  # sometimes the truth is generated by machine
                    # deep_merge_dict(batch, output)
                    # loss = self.machine.loss(output, batch)

                        loss = self.distributed_strategy.sync_loss(loss)

                        self.optimization_strategy.on_batch_end(self.machine, batch, loss, i)

                        batch_loss += loss.detach().item() * batch_size

                    batch_loss = batch_loss / self.hyper_params.n_updates_per_iteration
                    total_loss += batch_loss 
                    total_sample_num += batch_size
                    avg_loss = total_loss / total_sample_num if total_sample_num > 0 else 0.0
                    t.set_postfix(batch_J=loss.item(), batch_size=batch_size, average_loss=avg_loss, refresh=False)
                    t.update()

                except RuntimeError as e:
                    if 'out of memory' in str(e):
                        shapes = batch_traits.shape(batch)
                        batch_size = batch_traits.batch_size(batch)
                        logger.error(
                            f"Out of Memory for {i}-th Batch, Batch Size = {batch_size}, Data Shapes = {shapes}")

                    # logger.exception(e)
                    raise e

                except Exception as e:
                    shapes = batch_traits.shape(batch)
                    logger.error(f"Learn for {i}-th Batch, Data Shapes = {shapes}")
                    #
                    # logger.exception(e)
                    raise e



        return total_loss / total_sample_num if total_sample_num > 0 else 0.0


    def train(self, local_rank, env_pool_group: TMEnvironmentPoolGroup, runtime_options):
        """

        Args:
            local_rank ():
            model ():
            optimization ():
            train_data_loader ():
            dev_data_loader ():
            test_data_loader ():
            problem_evaluator ():
            task_evaluator ():
            learn_conf ():

        Returns:

        """

        self.local_rank = local_rank

        # training_setting = self.optimization_strategy.hyper_params

        use_gpu = self.distributed_strategy.use_gpu
        world_size = self.distributed_strategy.world_size

        self.runtime_options = runtime_options
        # if training_setting.seed:
        #     seed = training_setting.seed
        #     random.seed(seed)
        #     #            torch.manual_seed(seed)
        #     #            cudnn.deterministic = True
        #     warnings.warn('You have chosen to seed training. '
        #                   'This will turn on the CUDNN deterministic setting, '
        #                   'which can slow down your training considerably! '
        #                   'You may see unexpected behavior when restarting '
        #                   'from checkpoints.')

        if use_gpu:
            device = "cuda:" + str(local_rank)
        else:
            device = "cpu"
        D.set_device(device)
        self.distributed_strategy.init(local_rank)

        # self.optimization_strategy.act_on(self.machine)
        self.epoch = 0
        self.step = 0

        assert runtime_options.batching.type == "fixed_size"
        batch_size = runtime_options.batching.strategies.fixed_size.batch_size
        import torch 

        while True:
            with torch.autograd.detect_anomaly():
                for batch_env in tqdm(env_pool_group.choose(batch_size), "Env Batch:"):

                    self.optimization_strategy.on_epoch_start(self.machine, self.epoch)

                    if self.optimization_strategy.finish():
                        logger.info("optimizer terminate criterion satisfied. Optimization Finish.")
                        break

                    self.explore(batch_env, local_rank)
                    training_datastream = TMDataStream(level=TMDataLevel.Machine)
                    training_datastream["replay_buffer"] = self.replay_buffer
                    training_datastream.learn_channels = ("replay_buffer",)
                    #
                    # training_datastream = self.host.memory_modeler.model_datastream(training_datastream,
                    #                                                                 scenario=TMScenario.Learning)
                    # training_datastream = self.host.batch_modeler.model_datastream(training_datastream,
                    #                                                                scenario=TMScenario.Learning)

                    loss = self.train_step(local_rank, "replay_buffer", training_datastream["replay_buffer"])

                    self.optimization_strategy.on_epoch_end(self.machine, self.epoch)

                    self.step += 1
                    if self.evaluation_trigger.trigger(self):
                        P.OptimizerBehaviors.set_inference_mode(self.machine)
                        self.eval_and_select_model(env_pool_group, local_rank, self.epoch, self.step)
                        P.OptimizerBehaviors.set_train_mode(self.machine)

            self.epoch += 1

            # if self.evaluation_trigger.trigger(self):
            #     P.OptimizerBehaviors.set_inference_mode(self.machine)
            #     self.eval_and_select_model(batch_env_pool, local_rank, self.epoch, self.step)
            #     P.OptimizerBehaviors.set_train_mode(self.machine)

    def operate(self, env_pool_group: TMEnvironmentPoolGroup, runtime_options):
        """

        Args:
            problem ():
            raw ():
            problem_modeler ():

        Returns:

        """

        # logger.info(f"operate with parameter {self.runtime_options}")
#        assert machine_stream.level == TMDataLevel.Batch, f"wrong data level, {machine_stream.level}"

        self.distributed_strategy.run(train_worker, env_pool_group, runtime_options)


class TMReinforceLearner(TMReinforceOperator, TMReinforceLearnerMixin, TMReinforceEvaluatorMixin):
    """
    TM Reinforced Learner
    """

    def __init__(self, hyper_params, machine=None, states=None, **kwargs):
        assert machine is not None
        super().__init__(hyper_params, machine=machine, states=states, host=self,
                         scenario=TMScenario.Learning, **kwargs)

    def operate(self, env_pool_group: TMEnvironmentPoolGroup, runtime_options):
        """

        Args:
            problem ():
            raw ():
            problem_modeler ():

        Returns:

        """

        # logger.info(f"operate with parameter {self.runtime_options}")
        # assert env_pool.level == TMDataLevel.Machine, f"wrong data level, {env_pool.level}"

        # env_pool = self.fit_memory(env_pool, scenario=self.scenario)
        # env_pool = self.batchify(env_pool, scenario=self.scenario)

        self.distributed_strategy.run(train_worker, env_pool_group, runtime_options)
