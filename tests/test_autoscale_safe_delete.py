import datetime
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = (Path(__file__).resolve().parents[1] / 'autoscaling/crontab'
          / 'autoscale_slurm_disable-resize.sh')


def load_script():
    module = types.ModuleType('autoscale_safe_delete')
    source = SCRIPT.read_text(encoding='utf-8')
    source = source.split('\nautoscaling = getAutoscaling()\n', 1)[0]
    exec(compile(source, str(SCRIPT), 'exec'), module.__dict__)
    return module


def snapshot(state='IDLE', cpu='0', busy='2026-01-01T00:00:00', start='2026-01-01T00:00:00', reason=''):
    return {'NodeName': 'n1', 'State': state, 'CPUAlloc': cpu,
            'LastBusyTime': busy, 'SlurmdStartTime': start, 'Reason': reason}


class Process:
    def __init__(self, statuses):
        self.statuses = iter(statuses)

    def poll(self):
        return next(self.statuses)


class AutoscaleSafeDeleteTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script()
        self.module.idle_time = 600
        self.now = datetime.datetime(2026, 1, 1, 0, 10, 1)

    def test_last_busy_time_starts_idle_timer_after_completing(self):
        node = snapshot(busy='2026-01-01T00:00:00', start='2025-12-31T00:00:00')
        self.assertEqual(self.module.getIdleTime('n1', node, self.now), 601)
        # CG is a job state, not an idle state, even if CPUAlloc is zero.
        with mock.patch.object(self.module, 'runSlurm', return_value='40|COMPLETING|n1\n'):
            with self.assertRaises(self.module.SafetyCheckError):
                self.module.ensureNoJobs(['n1'])

    def test_running_and_suspended_jobs_are_protected_without_cpu_count_logic(self):
        for state in ('RUNNING', 'SUSPENDED'):
            with self.subTest(state=state), mock.patch.object(
                    self.module, 'runSlurm', return_value='41|{}|n1\n'.format(state)):
                with self.assertRaises(self.module.SafetyCheckError):
                    self.module.ensureNoJobs(['n1'])

    def test_failure_flags_and_unknown_times_use_fenced_recovery_not_fake_age(self):
        failed = snapshot('DOWN+DRAIN+INVALID_REG+FAIL', busy='Unknown', start='Unknown')
        self.assertIsNone(self.module.getIdleTime('n1', failed, self.now))
        self.assertTrue(self.module.nodeMayBeDeleted('n1', failed))
        self.assertFalse(self.module.nodeMayBeDeleted(
            'n1', snapshot('ALLOCATED+FAIL', cpu='0', busy='Unknown', start='Unknown')))

    def test_job_started_during_drain_check_rolls_back_only_our_drain(self):
        own_reason = 'autoscale-delete-test'
        nodes = ['n1']
        initial = snapshot(reason='')
        drained = snapshot('IDLE+DRAIN', reason=own_reason)
        with tempfile.TemporaryDirectory() as directory:
            self.module.clusters_path = directory
            os.mkdir(os.path.join(directory, 'cluster'))
            with mock.patch.object(self.module, 'getTopology', return_value=nodes), \
                 mock.patch.object(self.module, 'getNodeSnapshot', side_effect=[initial, drained, drained]), \
                 mock.patch.object(self.module, 'ensureNoJobs', side_effect=[None, self.module.SafetyCheckError('41 started')]), \
                 mock.patch.object(self.module, 'runSlurm') as run, \
                 mock.patch.object(self.module.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='test')):
                self.assertFalse(self.module.deleteClusterSafely('cluster'))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['scontrol', 'update', 'NodeName=n1', 'State=DRAIN', 'Reason=' + own_reason], commands)
        self.assertIn(['scontrol', 'update', 'NodeName=n1', 'State=UNDRAIN', 'Reason='], commands)

    def test_partial_drain_failure_restores_only_node_drained_by_this_run(self):
        own_reason = 'autoscale-delete-test'
        initial = {'n1': snapshot(reason=''), 'n2': snapshot(reason='existing')}
        after = snapshot('IDLE+DRAIN', reason=own_reason)
        def command(args):
            if args[2] == 'NodeName=n2' and args[3] == 'State=DRAIN':
                raise self.module.SafetyCheckError('controller rejected n2')
            return ''
        with tempfile.TemporaryDirectory() as directory:
            self.module.clusters_path = directory
            os.mkdir(os.path.join(directory, 'cluster'))
            with mock.patch.object(self.module, 'getTopology', return_value=['n1', 'n2']), \
                 mock.patch.object(self.module, 'getNodeSnapshot', side_effect=[initial['n1'], initial['n2'], after]), \
                 mock.patch.object(self.module, 'ensureNoJobs'), \
                 mock.patch.object(self.module, 'runSlurm', side_effect=command) as run, \
                 mock.patch.object(self.module.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='test')):
                self.assertFalse(self.module.deleteClusterSafely('cluster'))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['scontrol', 'update', 'NodeName=n1', 'State=UNDRAIN', 'Reason='], commands)
        self.assertNotIn(['scontrol', 'update', 'NodeName=n2', 'State=UNDRAIN', 'Reason=existing'], commands)

    def test_start_failure_and_resize_lock_rejection_undo_our_drain(self):
        for process in (OSError('cannot start'), Process([1])):
            with self.subTest(process=type(process).__name__), tempfile.TemporaryDirectory() as directory:
                own_reason = 'autoscale-delete-test'
                self.module.clusters_path = directory
                os.mkdir(os.path.join(directory, 'cluster'))
                initial = snapshot(reason='')
                drained = snapshot('IDLE+DRAIN', reason=own_reason)
                popen = (mock.patch.object(self.module.subprocess, 'Popen', side_effect=process)
                         if isinstance(process, OSError) else
                         mock.patch.object(self.module.subprocess, 'Popen', return_value=process))
                with mock.patch.object(self.module, 'getTopology', return_value=['n1']), \
                     mock.patch.object(self.module, 'getNodeSnapshot', side_effect=[initial, drained, drained]), \
                     mock.patch.object(self.module, 'ensureNoJobs'), \
                     mock.patch.object(self.module, 'runSlurm') as run, \
                     popen, \
                     mock.patch.object(self.module.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='test')):
                    self.assertFalse(self.module.deleteClusterSafely('cluster'))
                commands = [call.args[0] for call in run.call_args_list]
                self.assertIn(['scontrol', 'update', 'NodeName=n1', 'State=UNDRAIN', 'Reason='], commands)

    def test_unknown_handoff_keeps_drain_in_place(self):
        self.module.delete_acceptance_timeout = 0
        with tempfile.TemporaryDirectory() as directory:
            self.module.clusters_path = directory
            os.mkdir(os.path.join(directory, 'cluster'))
            own_reason = 'autoscale-delete-test'
            initial = snapshot(reason='')
            drained = snapshot('IDLE+DRAIN', reason=own_reason)
            with mock.patch.object(self.module, 'getTopology', return_value=['n1']), \
                 mock.patch.object(self.module, 'getNodeSnapshot', side_effect=[initial, drained]), \
                 mock.patch.object(self.module, 'ensureNoJobs'), \
                 mock.patch.object(self.module, 'runSlurm') as run, \
                 mock.patch.object(self.module.subprocess, 'Popen', return_value=Process([None])), \
                 mock.patch.object(self.module.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='test')):
                self.assertFalse(self.module.deleteClusterSafely('cluster'))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertNotIn(['scontrol', 'update', 'NodeName=n1', 'State=UNDRAIN', 'Reason='], commands)


if __name__ == '__main__':
    unittest.main()
