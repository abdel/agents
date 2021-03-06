# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Train and Eval REINFORCE.

To run:

```bash
tf_agents/agents/reinforce/examples/train_eval_eager \
 --root_dir=$HOME/tmp/reinforce/gym/ \
 --alsologtostderr
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time

from absl import app
from absl import flags
from absl import logging

import tensorflow as tf

from tf_agents.agents.reinforce import reinforce_agent
from tf_agents.drivers import dynamic_episode_driver
from tf_agents.environments import suite_gym
from tf_agents.environments import tf_py_environment
from tf_agents.metrics import metric_utils
from tf_agents.metrics import tf_metrics
from tf_agents.networks import actor_distribution_network
from tf_agents.replay_buffers import tf_uniform_replay_buffer

flags.DEFINE_string('root_dir', os.getenv('TEST_UNDECLARED_OUTPUTS_DIR'),
                    'Root directory for writing logs/summaries/checkpoints.')
flags.DEFINE_integer('num_iterations', 100000,
                     'Total number train/eval iterations to perform.')
FLAGS = flags.FLAGS


def train_eval(
    root_dir,
    env_name='CartPole-v0',
    num_iterations=1000,
    # TODO(kbanoop): rename to policy_fc_layers.
    actor_fc_layers=(100,),
    # Params for collect
    collect_episodes_per_iteration=2,
    replay_buffer_capacity=2000,
    # Params for train
    learning_rate=1e-3,
    gradient_clipping=None,
    normalize_returns=True,
    # Params for eval
    num_eval_episodes=10,
    eval_interval=100,
    # Params for checkpoints, summaries, and logging
    log_interval=100,
    summary_interval=100,
    summaries_flush_secs=1,
    debug_summaries=True,
    summarize_grads_and_vars=False,
    eval_metrics_callback=None):
  """A simple train and eval for Reinforce."""
  root_dir = os.path.expanduser(root_dir)
  train_dir = os.path.join(root_dir, 'train')
  eval_dir = os.path.join(root_dir, 'eval')

  train_summary_writer = tf.compat.v2.summary.create_file_writer(
      train_dir, flush_millis=summaries_flush_secs * 1000)
  train_summary_writer.set_as_default()

  eval_summary_writer = tf.compat.v2.summary.create_file_writer(
      eval_dir, flush_millis=summaries_flush_secs * 1000)
  eval_metrics = [
      tf_metrics.AverageReturnMetric(buffer_size=num_eval_episodes),
      tf_metrics.AverageEpisodeLengthMetric(buffer_size=num_eval_episodes),
  ]

  global_step = tf.compat.v1.train.get_or_create_global_step()
  with tf.compat.v2.summary.record_if(
      lambda: tf.math.equal(global_step % summary_interval, 0)):
    tf_env = tf_py_environment.TFPyEnvironment(suite_gym.load(env_name))
    eval_tf_env = tf_py_environment.TFPyEnvironment(suite_gym.load(env_name))

    # TODO(kbanoop): Handle distributions without gin.
    actor_net = actor_distribution_network.ActorDistributionNetwork(
        tf_env.time_step_spec().observation,
        tf_env.action_spec(),
        fc_layer_params=actor_fc_layers)

    tf_agent = reinforce_agent.ReinforceAgent(
        tf_env.time_step_spec(),
        tf_env.action_spec(),
        actor_network=actor_net,
        optimizer=tf.compat.v1.train.AdamOptimizer(learning_rate=learning_rate),
        normalize_returns=normalize_returns,
        gradient_clipping=gradient_clipping,
        debug_summaries=debug_summaries,
        summarize_grads_and_vars=summarize_grads_and_vars,
        train_step_counter=global_step)

    replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
        tf_agent.collect_data_spec,
        batch_size=tf_env.batch_size,
        max_length=replay_buffer_capacity)

    tf_agent.initialize()

    train_metrics = [
        tf_metrics.NumberOfEpisodes(),
        tf_metrics.EnvironmentSteps(),
        tf_metrics.AverageReturnMetric(),
        tf_metrics.AverageEpisodeLengthMetric(),
    ]

    eval_policy = tf_agent.policy
    collect_policy = tf_agent.collect_policy

    collect_driver = dynamic_episode_driver.DynamicEpisodeDriver(
        tf_env,
        collect_policy,
        observers=[replay_buffer.add_batch] + train_metrics,
        num_episodes=collect_episodes_per_iteration)

    # Compute evaluation metrics.
    metrics = metric_utils.eager_compute(
        eval_metrics,
        eval_tf_env,
        eval_policy,
        num_episodes=num_eval_episodes,
        summary_writer=eval_summary_writer,
        summary_prefix='Metrics',
    )
    # TODO(sfishman): Move this functionality into eager_compute_summaries
    if eval_metrics_callback is not None:
      eval_metrics_callback(metrics, global_step.numpy())

    time_step = None
    policy_state = collect_policy.get_initial_state(tf_env.batch_size)

    timed_at_step = global_step.numpy()
    time_acc = 0

    for _ in range(num_iterations):
      start_time = time.time()
      time_step, policy_state = collect_driver.run(
          time_step=time_step,
          policy_state=policy_state,
      )
      experience = replay_buffer.gather_all()
      total_loss = tf_agent.train(experience)
      replay_buffer.clear()
      time_acc += time.time() - start_time

      global_step_val = global_step.numpy()
      if global_step_val % log_interval == 0:
        logging.info('step = %d, loss = %f', global_step_val, total_loss.loss)
        steps_per_sec = (global_step_val - timed_at_step) / time_acc
        logging.info('%.3f steps/sec', steps_per_sec)
        tf.contrib.summary.scalar(
            name='global_steps/sec', tensor=steps_per_sec)
        timed_at_step = global_step_val
        time_acc = 0

      if global_step_val % eval_interval == 0:
        metrics = metric_utils.eager_compute(
            eval_metrics,
            eval_tf_env,
            eval_policy,
            num_episodes=num_eval_episodes,
            summary_writer=eval_summary_writer,
            summary_prefix='Metrics',
        )
        # TODO(sfishman): Move this functionality into eager_compute_summaries
        if eval_metrics_callback is not None:
          eval_metrics_callback(metrics, global_step_val)


def main(_):
  tf.compat.v1.enable_resource_variables()
  logging.set_verbosity(logging.INFO)
  tf.compat.v1.enable_eager_execution(
      config=tf.compat.v1.ConfigProto(allow_soft_placement=True))
  train_eval(FLAGS.root_dir, num_iterations=FLAGS.num_iterations)


if __name__ == '__main__':
  flags.mark_flag_as_required('root_dir')
  app.run(main)
