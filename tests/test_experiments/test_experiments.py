# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import json
import os
import tempfile
import time


from tensorflow.contrib.learn.python.learn.estimators import run_config as run_config_lib
from tensorflow.core.protobuf import config_pb2
from tensorflow.python.client import session
from tensorflow.python.ops import variables
from tensorflow.python.platform import test
from tensorflow.python.platform import tf_logging
from tensorflow.python.training import saver
from tensorflow.python.training import server_lib
from tensorflow.python.training import session_run_hook
from tensorflow.python.util import compat

from polyaxon.estimators import Estimator
from polyaxon.experiments import Experiment
from polyaxon.experiments.export_utils import make_export_strategy
from polyaxon.libs.configs import RunConfig
from polyaxon.libs.utils import get_arguments


class SheepCounter(object):
    """To be patched in for the time module, replacing sleep() and time()."""

    def __init__(self):
        self._total_time = 0
        self._sleeptimes = []
        self._time_calls = 0

    def sleep(self, t):
        self._total_time += t
        self._sleeptimes += [t]

    def time(self):
        self._time_calls += 1
        return self._total_time

    @property
    def sleep_times(self):
        return self._sleeptimes

    @property
    def time_calls(self):
        return self._time_calls


def _check_method_supports_args(method, kwargs):
    """Checks that the given method supports the given args."""
    supported_args = tuple(get_arguments(method))
    for kwarg in kwargs:
        if kwarg not in supported_args:
            raise ValueError(
                'Argument `{}` is not supported in method {}.'.format(kwarg, method))


class EstimatorTestClass(Estimator):
    def __init__(self, config=None, max_evals=5, eval_dict=None):
        self.eval_count = 0
        self.fit_count = 0
        self._max_evals = max_evals
        self.export_count = 0
        self._config = config or RunConfig()
        self._model_dir = tempfile.mkdtemp()
        self._eval_dict = eval_dict
        tf_logging.info('Create Core Estimator')

    def fake_checkpoint(self):
        save_path = os.path.join(self.model_dir, 'model.ckpt')
        with session.Session() as sess:
            var = variables.Variable(1.0, name='var0')
            save = saver.Saver({var.op.name: var})
            var.initializer.run()
            save.save(sess, save_path, global_step=0)

    def evaluate(self, **kwargs):
        _check_method_supports_args(Estimator.evaluate, kwargs)
        tf_logging.info('evaluate called with args: %s' % kwargs)
        if 'hooks' in kwargs:
            self.eval_hooks = kwargs['hooks']
        self.eval_count += 1
        if self.eval_count > self._max_evals:
            tf_logging.info('Ran %d evals. Done.' % self.eval_count)
            raise StopIteration()
        return self._eval_dict

    def train(self, **kwargs):
        _check_method_supports_args(Estimator.train, kwargs)
        self.fake_checkpoint()
        tf_logging.info('fit called with args: %s' % kwargs)
        self.fit_count += 1

        return [(key, kwargs[key]) for key in sorted(kwargs.keys())]

    def export_savedmodel(self, export_dir_base, serving_input_receiver_fn, **kwargs):
        _check_method_supports_args(Estimator.export_savedmodel, kwargs)
        tf_logging.info('export_savedmodel called with args: %s, %s, %s' %
                        (export_dir_base, serving_input_receiver_fn, kwargs))
        self.export_count += 1
        return os.path.join(compat.as_bytes(export_dir_base), compat.as_bytes('bogus_timestamp'))


class _NoopHook(session_run_hook.SessionRunHook):
    pass


class TestExperiment(test.TestCase):
    def _cluster_spec(self):
        return {run_config_lib.TaskType.PS: ['host1:2222', 'host2:2222'],
                run_config_lib.TaskType.WORKER: ['host3:2222', 'host4:2222', 'host5:2222']}

    def _estimators_for_tests(self, config=None, eval_dict=None):
        return [EstimatorTestClass(config=config, eval_dict=eval_dict)]

    def test_train(self):
        for est in self._estimators_for_tests():
            ex = Experiment(est, train_input_fn='train_input', train_steps='train_steps',
                            eval_input_fn='eval_input')
            fit_args = ex.train(delay_secs=0)
            self.assertEqual(1, est.fit_count)
            self.assertIn(('max_steps', 'train_steps'), fit_args)
            self.assertEqual(0, est.eval_count)

    def test_train_delay(self):
        for est in self._estimators_for_tests():
            ex = Experiment(est, train_input_fn='train_input', eval_input_fn='eval_input')
            for delay in [0, 1, 3]:
                sheep = SheepCounter()
                with test.mock.patch.object(time, 'time', sheep.time):
                    with test.mock.patch.object(time, 'sleep', sheep.sleep):
                        ex.train(delay_secs=delay)
                        self.assertAlmostEqual(delay, sheep.time(), delta=1e-4)

    def test_train_default_delay(self):
        for task_id in [0, 1, 3]:
            tf_config = {'task': {'index': task_id}}
            with test.mock.patch.dict('os.environ',
                                      {'TF_CONFIG': json.dumps(tf_config)}):
                config = RunConfig()
            for est in self._estimators_for_tests(config):
                ex = Experiment(
                    est, train_input_fn='train_input', eval_input_fn='eval_input')

                sheep = SheepCounter()
                with test.mock.patch.object(time, 'time', sheep.time):
                    with test.mock.patch.object(time, 'sleep', sheep.sleep):
                        ex.train()
                        self.assertAlmostEqual(task_id * 5, sheep.time(), delta=1e-4)

    @test.mock.patch.object(server_lib, 'Server')
    def test_train_starts_server(self, mock_server):
        # Arrange.
        tf_config = {
            'cluster': self._cluster_spec(),
            'environment': run_config_lib.Environment.CLOUD,
            'task': {
                'type': run_config_lib.TaskType.WORKER,
                'index': 1
            }
        }
        with test.mock.patch.dict('os.environ', {'TF_CONFIG': json.dumps(tf_config)}):
            config = run_config_lib.RunConfig(
                master='host4:2222', num_cores=15, gpu_memory_fraction=0.314)

        for est in self._estimators_for_tests(config):
            ex = Experiment(est, train_input_fn='train_input', eval_input_fn='eval_input')

            # Act.
            # We want to make sure we discount the time it takes to start the server
            # in our accounting of the delay, so we set a small delay here.
            sheep = SheepCounter()
            with test.mock.patch.object(time, 'time', sheep.time):
                with test.mock.patch.object(time, 'sleep', sheep.sleep):
                    ex.train(delay_secs=1)
                    # Ensure that the delay takes into account the time to start server.
                    self.assertAlmostEqual(1, sheep.time(), delta=1e-4)

            # Assert.
            expected_config_proto = config_pb2.ConfigProto()
            expected_config_proto.inter_op_parallelism_threads = 15
            expected_config_proto.intra_op_parallelism_threads = 15
            expected_config_proto.gpu_options.per_process_gpu_memory_fraction = 0.314
            mock_server.assert_called_with(
                config.cluster_spec,
                job_name=run_config_lib.TaskType.WORKER,
                task_index=1,
                config=expected_config_proto,
                start=False)
            mock_server.assert_has_calls([test.mock.call().start()])

    @test.mock.patch.object(server_lib, 'Server')
    def test_train_server_does_not_start_without_cluster_spec(self, mock_server):
        config = run_config_lib.RunConfig(master='host4:2222')
        for est in self._estimators_for_tests(config):
            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input')
            ex.train()

            # The server should not have started because there was no ClusterSpec.
            self.assertFalse(mock_server.called)

    @test.mock.patch.object(server_lib, 'Server')
    def test_train_server_does_not_start_with_empty_master(self, mock_server):
        tf_config = {'cluster': self._cluster_spec()}
        with test.mock.patch.dict('os.environ',
                                  {'TF_CONFIG': json.dumps(tf_config)}):
            config = run_config_lib.RunConfig(master='')
        for est in self._estimators_for_tests(config):
            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input')
            ex.train()
            # The server should not have started because master was the empty string.
            self.assertFalse(mock_server.called)

    def test_train_raises_if_job_name_is_missing(self):
        tf_config = {
            'cluster': self._cluster_spec(),
            'environment': run_config_lib.Environment.CLOUD,
            'task': {
                'index': 1
            }
        }
        with test.mock.patch.dict(
            'os.environ',
            {'TF_CONFIG': json.dumps(tf_config)}), self.assertRaises(ValueError):
            config = run_config_lib.RunConfig(
                master='host3:2222'  # Normally selected by task type.
            )
            for est in self._estimators_for_tests(config):
                ex = Experiment(
                    est,
                    train_input_fn='train_input',
                    eval_input_fn='eval_input')
                ex.train()

    def test_evaluate(self):
        for est in self._estimators_for_tests():
            est.fake_checkpoint()
            noop_hook = _NoopHook()
            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input',
                eval_hooks=[noop_hook],
                eval_steps='steps',
                eval_delay_secs=0)
            ex.evaluate()
            self.assertEqual(0, est.fit_count)
            self.assertEqual(1, est.eval_count)
            self.assertEqual([noop_hook], est.eval_hooks)

    def test_evaluate_delay(self):
        for est in self._estimators_for_tests():
            est.fake_checkpoint()
            noop_hook = _NoopHook()
            ex = Experiment(
                est, train_input_fn='train_input', eval_input_fn='eval_input',
                eval_hooks=[noop_hook])

            for delay in [0, 1, 3]:
                sheep = SheepCounter()
                with test.mock.patch.object(time, 'time', sheep.time):
                    with test.mock.patch.object(time, 'sleep', sheep.sleep):
                        ex.evaluate(delay_secs=delay)
                self.assertAlmostEqual(delay, sheep.time(), delta=1e-4)
                self.assertEqual([noop_hook], est.eval_hooks)

    def test_continuous_eval(self):
        for est in self._estimators_for_tests(eval_dict={'global_step': 100}):
            est.fake_checkpoint()
            noop_hook = _NoopHook()
            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input',
                eval_hooks=[noop_hook],
                eval_delay_secs=0,
                continuous_eval_throttle_secs=0)
            self.assertRaises(StopIteration, ex.continuous_eval,
                              evaluate_checkpoint_only_once=False)
            self.assertEqual(0, est.fit_count)
            self.assertEqual(6, est.eval_count)
            self.assertEqual([noop_hook], est.eval_hooks)

    def test_continuous_eval_ends_after_train_step(self):
        for est in self._estimators_for_tests(eval_dict={'global_step': 100}):
            est.fake_checkpoint()
            noop_hook = _NoopHook()
            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input',
                eval_hooks=[noop_hook],
                eval_delay_secs=0,
                continuous_eval_throttle_secs=0,
                train_steps=100)
            ex.continuous_eval()
            self.assertEqual(0, est.fit_count)
            self.assertEqual(1, est.eval_count)
            self.assertEqual([noop_hook], est.eval_hooks)

    def test_continuous_eval_throttle_delay(self):
        for delay in [0, 1, 2]:
            for est in self._estimators_for_tests():
                est.fake_checkpoint()
                noop_hook = _NoopHook()
                ex = Experiment(
                    est,
                    train_input_fn='train_input',
                    eval_input_fn='eval_input',
                    eval_hooks=[noop_hook],
                    continuous_eval_throttle_secs=delay,
                    eval_delay_secs=0)
                sheep = SheepCounter()
                with test.mock.patch.object(time, 'time', sheep.time):
                    with test.mock.patch.object(time, 'sleep', sheep.sleep):
                        self.assertRaises(
                            StopIteration,
                            ex.continuous_eval,
                            evaluate_checkpoint_only_once=False)
                        self.assertAlmostEqual(5 * delay, sheep.time(), delta=1e-4)

    def test_continuous_eval_predicate_fn(self):
        for est in self._estimators_for_tests():
            est.fake_checkpoint()
            noop_hook = _NoopHook()

            def _predicate_fn(unused_eval_result):
                return est.eval_count < 3  # pylint: disable=cell-var-from-loop

            ex = Experiment(est, train_input_fn='train_input', eval_input_fn='eval_input',
                            eval_hooks=[noop_hook], eval_delay_secs=0,
                            continuous_eval_throttle_secs=0)
            ex.continuous_eval(evaluate_checkpoint_only_once=False,
                               continuous_eval_predicate_fn=_predicate_fn)
            self.assertEqual(0, est.fit_count)
            self.assertEqual(3, est.eval_count)
            self.assertEqual([noop_hook], est.eval_hooks)

    def test_train_hooks_extend_does_not_mutate_input_hooks(self):
        for est in self._estimators_for_tests():
            noop_hook = _NoopHook()
            input_hooks = [noop_hook]

            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input',
                train_hooks=input_hooks)
            self.assertAllEqual([noop_hook], ex._train_hooks)

            another_noop_hook = _NoopHook()
            # Assert that the extend API mutates the hooks, but not the input hooks
            ex.extend_train_hooks([another_noop_hook])
            self.assertAllEqual([noop_hook, another_noop_hook], ex._train_hooks)
            self.assertAllEqual([noop_hook], input_hooks)

    def test_invalid_export_strategies(self):
        for est in self._estimators_for_tests():
            with self.assertRaisesRegexp(ValueError, 'ExportStrategy'):
                Experiment(
                    est,
                    train_input_fn='train_input',
                    eval_input_fn='eval_input',
                    train_steps=100,
                    eval_steps=100,
                    export_strategies='not_an_export_strategy')
            with self.assertRaisesRegexp(ValueError, 'ExportStrategy'):
                Experiment(
                    est,
                    train_input_fn='train_input',
                    eval_input_fn='eval_input',
                    train_steps=100,
                    eval_steps=100,
                    export_strategies=['not_an_export_srategy'])

    def test_export_strategies_reset(self):
        for est in self._estimators_for_tests():
            export_strategy_1 = make_export_strategy(est, None, exports_to_keep=None)

            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input',
                train_steps=100,
                eval_steps=100,
                export_strategies=(export_strategy_1,))
            ex.train_and_evaluate()
            self.assertEqual(1, est.export_count)

            # After reset with empty list (None), the count does not change and the
            # user provided export strategy list should remain intact.
            old_es = ex.reset_export_strategies()
            ex.train_and_evaluate()
            self.assertAllEqual([export_strategy_1], old_es)
            self.assertEqual(1, est.export_count)

            # After reset with list, the count should increase with the number of
            # items.
            export_strategy_2 = make_export_strategy(est, None, exports_to_keep=None)
            export_strategy_3 = make_export_strategy(est, None, exports_to_keep=None)

            old_es = ex.reset_export_strategies([export_strategy_2, export_strategy_3])
            ex.train_and_evaluate()
            self.assertAllEqual([], old_es)
            self.assertEqual(3, est.export_count)

    def test_train_and_evaluate(self):
        for est in self._estimators_for_tests():
            noop_hook = _NoopHook()
            export_strategy = make_export_strategy(est, None, exports_to_keep=None)
            ex = Experiment(est, train_input_fn='train_input', eval_input_fn='eval_input',
                            eval_hooks=[noop_hook], train_steps=100, eval_steps=100,
                            export_strategies=export_strategy)
            ex.train_and_evaluate()
            self.assertEqual(1, est.fit_count)
            self.assertEqual(1, est.eval_count)
            self.assertEqual(1, est.export_count)
            self.assertEqual([noop_hook], est.eval_hooks)

    def test_train_and_evaluate_with_no_eval_during_training(self):
        for est in self._estimators_for_tests():
            noop_hook = _NoopHook()
            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input',
                eval_hooks=[noop_hook],
                train_steps=100,
                eval_steps=100)
            ex.train_and_evaluate()
            self.assertEqual(1, est.fit_count)
            self.assertEqual(1, est.eval_count)

    def test_min_eval_frequency_defaults(self):
        def dummy_model_fn(features, labels):  # pylint: disable=unused-argument
            pass

        # The default value when model_dir is on GCS is 1000
        estimator = Estimator(dummy_model_fn, 'gs://dummy_bucket')
        ex = Experiment(estimator, train_input_fn=None, eval_input_fn=None)
        self.assertEquals(ex._eval_every_n_steps, 1)

        # The default value when model_dir is not on GCS is 1
        estimator = Estimator(dummy_model_fn, '/tmp/dummy')
        ex = Experiment(estimator, train_input_fn=None, eval_input_fn=None)
        self.assertEquals(ex._eval_every_n_steps, 1)

        # Make sure default not used when explicitly set
        estimator = Estimator(dummy_model_fn, 'gs://dummy_bucket')
        ex = Experiment(
            estimator,
            eval_every_n_steps=123,
            train_input_fn=None,
            eval_input_fn=None)
        self.assertEquals(ex._eval_every_n_steps, 123)

        # Make sure default not used when explicitly set as 0
        estimator = Estimator(dummy_model_fn, 'gs://dummy_bucket')
        ex = Experiment(
            estimator,
            eval_every_n_steps=0,
            train_input_fn=None,
            eval_input_fn=None)
        self.assertEquals(ex._eval_every_n_steps, 0)

    def test_continuous_train_and_eval(self):
        for est in self._estimators_for_tests(eval_dict={'global_step': 100}):
            noop_hook = _NoopHook()
            export_strategy = make_export_strategy(est, None, exports_to_keep=None)
            ex = Experiment(est, train_input_fn='train_input', eval_input_fn='eval_input',
                            eval_hooks=[noop_hook], train_steps=100, eval_steps=100,
                            export_strategies=export_strategy)
            ex.continuous_train_and_evaluate()
            self.assertEqual(1, est.fit_count)
            self.assertEqual(1, est.eval_count)
            self.assertEqual(1, est.export_count)
            self.assertEqual([noop_hook], est.eval_hooks)

    def test_continuous_train_and_eval_with_predicate_fn(self):
        for est in self._estimators_for_tests(eval_dict={'global_step': 100}):
            export_strategy = make_export_strategy(est, None, exports_to_keep=None)
            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input',
                train_steps=100000000000,  # a value will make `ex` never stops.
                eval_steps=100,
                export_strategies=export_strategy)

            def predicate_fn(eval_result):
                del eval_result  # unused. for fn signature.
                return False

            ex.continuous_train_and_evaluate(continuous_eval_predicate_fn=predicate_fn)
            self.assertEqual(0, est.fit_count)
            self.assertEqual(0, est.eval_count)
            self.assertEqual(1, est.export_count)

    def test_continuous_train_and_eval_with_adapted_steps_per_iteration(self):
        mock_estimator = test.mock.Mock(Estimator)
        type(mock_estimator).model_dir = test.mock.PropertyMock(return_value='test_dir')

        total_steps = 100000000000000
        ex = Experiment(mock_estimator, train_input_fn='train_input', eval_input_fn='eval_input',
                        train_steps=total_steps, train_steps_per_iteration=None)

        def predicate_fn(eval_result):
            # Allows the first invoke only.
            return eval_result is None

        ex.continuous_train_and_evaluate(continuous_eval_predicate_fn=predicate_fn)
        mock_estimator.train.assert_called_once_with(
            input_fn='train_input',
            steps=int(total_steps / 10),
            max_steps=None,
            hooks=[])

    def test_continuous_train_and_eval_with_steps_per_iteration_from_user(self):
        mock_estimator = test.mock.Mock(Estimator)
        type(mock_estimator).model_dir = test.mock.PropertyMock(
            return_value='test_dir')

        total_steps = 100000000000000
        ex = Experiment(
            mock_estimator,
            train_input_fn='train_input',
            eval_input_fn='eval_input',
            train_steps_per_iteration=1234,
            train_steps=total_steps)

        def predicate_fn(eval_result):
            # Allows the first invoke only.
            return eval_result is None

        ex.continuous_train_and_evaluate(continuous_eval_predicate_fn=predicate_fn)
        mock_estimator.train.assert_called_once_with(
            input_fn='train_input',
            steps=1234,
            max_steps=test.mock.ANY,
            hooks=test.mock.ANY)

    def test_continuous_train_and_eval_with_default_steps_per_iteration(self):
        mock_estimator = test.mock.Mock(Estimator)
        type(mock_estimator).model_dir = test.mock.PropertyMock(
            return_value='test_dir')

        ex = Experiment(
            mock_estimator,
            train_input_fn='train_input',
            eval_input_fn='eval_input',
            train_steps_per_iteration=None,
            train_steps=None)

        def predicate_fn(eval_result):
            # Allows the first invoke only.
            return eval_result is None

        ex.continuous_train_and_evaluate(continuous_eval_predicate_fn=predicate_fn)
        mock_estimator.train.assert_called_once_with(
            input_fn='train_input',
            steps=1000,
            max_steps=test.mock.ANY,
            hooks=test.mock.ANY)

    def test_continuous_train_and_eval_with_invalid_predicate_fn(self):
        for est in self._estimators_for_tests():
            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input')
            with self.assertRaisesRegexp(ValueError,
                                         '`continuous_eval_predicate_fn` must be a callable'):
                ex.continuous_train_and_evaluate(continuous_eval_predicate_fn='fn')

    def test_continuous_train_and_eval_with_invalid_train_steps_iterations(self):
        for est in self._estimators_for_tests():
            with self.assertRaisesRegexp(ValueError,
                                         '`train_steps_per_iteration` must be an integer.'):
                Experiment(est, train_input_fn='train_input',eval_input_fn='eval_input',
                           train_steps_per_iteration='123')

    @test.mock.patch.object(server_lib, 'Server')
    def test_run_std_server(self, mock_server):
        # Arrange.
        tf_config = {
            'cluster': self._cluster_spec(),
            'task': {
                'type': run_config_lib.TaskType.PS,
                'index': 1
            }
        }
        with test.mock.patch.dict('os.environ',
                                  {'TF_CONFIG': json.dumps(tf_config)}):
            config = RunConfig(
                master='host2:2222',
                num_cores=15,
                gpu_memory_fraction=0.314, )
        for est in self._estimators_for_tests(config):
            ex = Experiment(
                est, train_input_fn='train_input', eval_input_fn='eval_input')

            # Act.
            ex.run_std_server()

            # Assert.
            mock_server.assert_has_calls(
                [test.mock.call().start(), test.mock.call().join()])

    @test.mock.patch.object(server_lib, 'Server')
    def test_run_std_server_raises_without_cluster_spec(self, mock_server):
        config = run_config_lib.RunConfig(master='host4:2222')
        for est in self._estimators_for_tests(config):
            with self.assertRaises(ValueError):
                ex = Experiment(
                    est,
                    train_input_fn='train_input',
                    eval_input_fn='eval_input')
                ex.run_std_server()

    def test_test(self):
        for est in self._estimators_for_tests():
            export_strategy = make_export_strategy(est, None, exports_to_keep=None)
            ex = Experiment(est, train_input_fn='train_input', eval_input_fn='eval_input',
                            export_strategies=export_strategy)
            ex.test()
            self.assertEqual(1, est.fit_count)
            self.assertEqual(1, est.eval_count)
            self.assertEqual(1, est.export_count)

    def test_continuous_eval_evaluates_checkpoint_once(self):
        for est in self._estimators_for_tests(eval_dict={'global_step': 100}):
            est.fake_checkpoint()

            result = {
                'called': 0,
                'called_with_eval_result': 0,
            }

            # pylint: disable=cell-var-from-loop
            def _predicate_fn(eval_result):
                result['called'] += 1
                if eval_result:
                    # If eval_result is not empty nor None, the checkpoint has been
                    # evaluated.
                    result['called_with_eval_result'] += 1
                # With 300 times of evaluation, this should prove something.
                return result['called'] < 300

            # pylint: enable=cell-var-from-loop

            ex = Experiment(
                est,
                train_input_fn='train_input',
                eval_input_fn='eval_input',
                eval_delay_secs=0,
                continuous_eval_throttle_secs=0)
            ex.continuous_eval(evaluate_checkpoint_only_once=True,
                               continuous_eval_predicate_fn=_predicate_fn)

            self.assertEqual(0, est.fit_count)
            self.assertEqual(1, est.eval_count)
            self.assertEqual(300, result['called'])
            self.assertEqual(1, result['called_with_eval_result'])
