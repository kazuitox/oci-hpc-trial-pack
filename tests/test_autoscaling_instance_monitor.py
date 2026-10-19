import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import autoscaling_instance_monitor as monitor


class InstanceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(deployment_id='deployment-a', region='region-a', compartment_id='comp-a',
                           topic_id='topic-a', instance_principal=True, grace_seconds=900,
                           repeat_seconds=21600, max_sample_gap_seconds=1800,
                           build_timeout_seconds=10800, delete_timeout_seconds=3600)
        self.instance = dict(id='ocid1.instance.test.a', cluster='batch-q-1-hpc', region='region-a',
                             aliases={'10.0.0.1'}, **{'lifecycle-state': 'RUNNING', 'shape': 'BM.HPC'})
        self.node = dict(NodeName='real-host', NodeAddr='10.0.0.1', NodeHostName='real-host',
                         State='IDLE', CPUAlloc='0')
        self.clusters = {'batch-q-1-hpc': {'markers': {}, 'members': {}}}
        self.queues = [{'name': 'batch-q', 'instance_types': [dict(instance_keyword='hpc', permanent=False,
                                                              region='region-a', targetCompartment='comp-a')]}]

    def find(self, nodes=None, busy=None):
        return monitor.findings([self.instance], self.clusters, self.queues,
                                [self.node] if nodes is None else nodes, busy or set(), 20000, self.config)

    def reason(self, **kwargs):
        return next(iter(self.find(**kwargs).values()))['reason']

    def test_slurm_missing_after_cleanup(self):
        self.assertEqual(self.reason(nodes=[]), 'SLURM_NODE_MISSING')

    def test_directory_gone_even_with_allocated_node(self):
        self.clusters.clear()
        self.node.update(State='ALLOCATED', CPUAlloc='8')
        self.assertEqual(self.reason(), 'LOCAL_RECORD_MISSING')

    def test_both_remain_failed_node(self):
        for state in ('DOWN', 'IDLE+DRAIN', 'IDLE*', 'INVALID_REG', 'UNKNOWN'):
            self.node['State'] = state
            self.assertEqual(self.reason(), 'SLURM_UNHEALTHY')

    def test_idle_cluster_stays(self):
        self.assertEqual(self.reason(), 'IDLE_CLUSTER_RETAINED')

    def test_permanent_idle_excluded_but_failure_visible(self):
        self.queues[0]['instance_types'][0]['permanent'] = True
        self.assertEqual(self.find(), {})
        self.node['State'] = 'DOWN'
        self.assertEqual(self.reason(), 'SLURM_UNHEALTHY')

    def test_allocated_mixed_completing_and_suspended_not_idle(self):
        for state in ('ALLOCATED', 'MIXED', 'COMPLETING', 'IDLE+POWERING_DOWN'):
            self.node['State'] = state
            self.assertEqual(self.find(), {})
        self.node['State'] = 'IDLE'
        self.assertEqual(self.find(busy={'real-host'}), {})
        self.node['CPUAlloc'] = '1'
        self.assertEqual(self.find(), {})

    def test_busy_sibling_blocks_whole_cluster_idle(self):
        sibling = dict(self.instance, id='ocid1.instance.test.b', aliases={'10.0.0.2'})
        other = dict(self.node, NodeName='other', NodeAddr='10.0.0.2', State='MIXED')
        self.assertEqual(monitor.findings([self.instance, sibling], self.clusters, self.queues,
                                         [self.node, other], set(), 20000, self.config), {})

    def test_create_delete_grace_then_stalled(self):
        for marker, reason, timeout in [('currently_building', 'BUILD_STALLED', 10800),
                                        ('currently_destroying', 'DELETE_STALLED', 3600)]:
            self.clusters['batch-q-1-hpc']['markers'] = {marker: 20000 - timeout + 1}
            self.assertEqual(self.find(nodes=[]), {})
            self.clusters['batch-q-1-hpc']['markers'][marker] -= 1
            self.assertEqual(self.reason(nodes=[]), reason)

    def test_stopped_and_terminating_observed(self):
        for state in ('STOPPED', 'STOPPING', 'TERMINATING', 'PROVISIONING'):
            self.instance['lifecycle-state'] = state
            self.assertEqual(self.reason(), 'OCI_NOT_RUNNING')

    def test_unknown_queue_not_assumed_permanent_or_idle(self):
        self.queues.clear()
        self.assertEqual(self.reason(), 'QUEUE_CONFIG_MISSING')

    def test_duplicate_ip_across_instances_does_not_hide_missing_node(self):
        other = dict(self.instance, id='different-instance')
        with self.assertRaises(ValueError):
            monitor.findings([self.instance, other], self.clusters, self.queues,
                             [self.node], set(), 20000, self.config)

    def test_ambiguous_node_fails(self):
        with self.assertRaises(ValueError):
            self.find(nodes=[self.node, self.node])

    def test_notification_grace_repeat_recovery(self):
        sent = []
        send = lambda msg: sent.append(msg) or True
        current = self.find()
        state = monitor.reconcile({}, current, 0, self.config, send)
        self.assertFalse(sent)
        state = monitor.reconcile(state, current, 899, self.config, send)
        self.assertFalse(sent)
        state = monitor.reconcile(state, current, 900, self.config, send)
        self.assertEqual(len(sent), 1)
        state = monitor.reconcile(state, current, 1200, self.config, send)
        self.assertEqual(len(sent), 1)
        self.config['repeat_seconds'] = 1000
        state = monitor.reconcile(state, current, 1900, self.config, send)
        self.assertEqual(len(sent), 2)
        state = monitor.reconcile(state, {}, 2000, self.config, send)
        self.assertEqual(sent[-1]['title'], 'HPC instance monitor: RECOVERED')
        self.assertEqual(state['records'], {})

    def test_failed_delivery_retries_and_failed_recovery_retained(self):
        current = self.find()
        state = monitor.reconcile({}, current, 0, self.config, lambda _: False)
        state = monitor.reconcile(state, current, 900, self.config, lambda _: False)
        self.assertIsNone(next(iter(state['records'].values()))['last_sent'])
        state = monitor.reconcile(state, current, 1000, self.config, lambda _: True)
        self.assertEqual(next(iter(state['records'].values()))['last_sent'], 1000)
        state = monitor.reconcile(state, {}, 1100, self.config, lambda _: False)
        self.assertTrue(state['records'])
        state = monitor.reconcile(state, {}, 1200, self.config, lambda _: True)
        self.assertFalse(state['records'])

    def test_interrupted_short_incident_never_notified(self):
        state = monitor.reconcile({}, self.find(), 0, self.config, lambda _: self.fail())
        state = monitor.reconcile(state, {}, 100, self.config, lambda _: self.fail())
        self.assertFalse(state['records'])

    def test_long_gap_or_clock_rollback_resets_evidence(self):
        for now in (4000, -1):
            state = monitor.reconcile({}, self.find(), 0, self.config, lambda _: self.fail())
            state = monitor.reconcile(state, self.find(), now, self.config, lambda _: self.fail())
            self.assertEqual(next(iter(state['records'].values()))['samples'], 1)

    def test_inventory_only_compute_sections(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'batch-q-1-hpc'
            folder.mkdir()
            (folder / 'inventory').write_text('[controller]\nctrl oci_instance_id=controller\n'
                                             '[compute_configured]\ninst-x ansible_host=10.0.0.1 oci_instance_id=node\n'
                                             '[all:vars]\nx oci_instance_id=wrong\n')
            data = monitor.local_clusters(temp)
            self.assertEqual(data[folder.name]['members'], {'node': ['inst-x', '10.0.0.1']})

    def collect_setup(self, temp):
        self.config.update(state_dir=temp, clusters_path=temp, queues_conf=str(Path(temp) / 'queues.conf'))
        import yaml
        Path(self.config['queues_conf']).write_text(yaml.safe_dump({'queues': self.queues}))

    def test_collect_exact_owner_and_paginated_scopes_legacy_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            self.collect_setup(temp)
            folder = Path(temp) / 'batch-q-1-hpc'
            folder.mkdir()
            (folder / 'inventory').write_text('[compute_to_add]\nnode oci_instance_id=legacy ansible_host=10.0.0.1\n'
                                             'bad oci_instance_id=conflict\n')
            data = []
            for oid, owner in [('owned', 'deployment-a'), ('foreign', 'other'), ('legacy', None),
                               ('conflict', 'other'), ('unknown', None), ('terminated', 'deployment-a')]:
                tags = {'parent_cluster': 'batch-q-1-hpc'}
                if owner:
                    tags[monitor.OWNER_TAG] = owner
                data.append({'id': oid, 'freeform-tags': tags,
                             'lifecycle-state': 'TERMINATED' if oid == 'terminated' else 'RUNNING'})
            def fake_oci(config, region, args):
                self.assertIn('--all', args)
                return [{'private-ip': '10.0.0.1', 'hostname-label': 'real-host'}] if 'list-vnics' in args else data
            with mock.patch.object(monitor, 'oci', side_effect=fake_oci), mock.patch.object(monitor, 'slurm_snapshot', return_value=([], set())):
                collected = monitor.collect(self.config)[0]
            self.assertEqual({i['id'] for i in collected}, {'owned', 'legacy'})
            self.assertIn('real-host', collected[0]['aliases'])

    def test_multiregion_compartment_queue_scopes(self):
        with tempfile.TemporaryDirectory() as temp:
            self.queues[0]['instance_types'].append(dict(instance_keyword='remote', permanent=False,
                                                        region='region-b', targetCompartment='comp-b'))
            self.collect_setup(temp)
            with mock.patch.object(monitor, 'oci', return_value=[]) as cli, mock.patch.object(monitor, 'slurm_snapshot', return_value=([], set())):
                monitor.collect(self.config)
            self.assertEqual({(c.args[1], c.args[2][-1]) for c in cli.call_args_list},
                             {('region-a', 'comp-a'), ('region-b', 'comp-b')})

    def test_slurm_jobs_and_invalid_responses(self):
        outputs = ['NodeName=n State=IDLE CPUAlloc=0 NodeAddr=1\n', '1|SUSPENDED|n[1-2]\n2|COMPLETED|n3\n', 'n1\nn2\n']
        with mock.patch.object(monitor, 'run', side_effect=outputs):
            nodes, busy = monitor.slurm_snapshot()
        self.assertEqual(busy, {'n1', 'n2'})
        for outputs in (['garbage'], ['NodeName=n State=IDLE CPUAlloc=0', 'garbage']):
            with mock.patch.object(monitor, 'run', side_effect=outputs), self.assertRaises(ValueError):
                monitor.slurm_snapshot()

    def test_cli_timeout_local_env_and_python36_arguments(self):
        def fake_run(command, **kwargs):
            self.assertNotIn('capture_output', kwargs)
            self.assertNotIn('text', kwargs)
            self.assertNotIn('SLURM_CLUSTERS', kwargs['env'])
            self.assertNotIn('SQUEUE_STATES', kwargs['env'])
            self.assertEqual(kwargs['env']['SLURM_CONF'], 'keep')
            self.assertEqual(kwargs['env']['LC_ALL'], 'C.UTF-8')
            self.assertEqual(kwargs['env']['LANG'], 'C.UTF-8')
            self.assertEqual(kwargs['encoding'], 'utf-8')
            self.assertEqual(kwargs['timeout'], 90)
            return subprocess.CompletedProcess(command, 0, 'ok', '')
        with mock.patch.dict(os.environ, SLURM_CLUSTERS='other', SQUEUE_STATES='R', SLURM_CONF='keep', LC_ALL='C', LANG='C'), mock.patch.object(monitor.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(monitor.run(['scontrol']), 'ok')

    def test_failed_command_reports_operation_exit_and_sanitized_stderr(self):
        stderr = ('ServiceError: NotAuthorizedOrNotFound\npassword=do-not-show\n'
                  '-----BEGIN RSA PRIVATE KEY-----\nkey-body-do-not-show\n-----END RSA PRIVATE KEY-----\n')
        with mock.patch.object(monitor.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', stderr)):
            with self.assertRaises(monitor.CommandError) as caught:
                monitor.run(['/usr/bin/oci', 'compute', 'instance', 'list', '--compartment-id', 'private-id'])
        message = str(caught.exception)
        self.assertIn('oci compute instance list failed (exit 1)', message)
        self.assertIn('NotAuthorizedOrNotFound', message)
        self.assertNotIn('do-not-show', message)
        self.assertNotIn('private-id', message)

    def test_missing_command_and_timeout_have_actionable_diagnostics(self):
        for error, expected in [(FileNotFoundError(2, 'No such file or directory'), 'could not start'),
                                (subprocess.TimeoutExpired(['squeue', 'private-argv'], 90), 'timed out after 90 seconds')]:
            with mock.patch.object(monitor.subprocess, 'run', side_effect=error):
                with self.assertRaises(monitor.CommandError) as caught:
                    monitor.run(['squeue', 'private-argv'])
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn('private-argv', str(caught.exception))

    def test_dry_run_failure_reports_stage_and_command_without_sending(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as temp:
            self.collect_setup(temp)
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps(self.config))
            def fail(config):
                config['_stage'] = 'SLURM_SNAPSHOT'
                raise monitor.CommandError('scontrol show nodes failed (exit 1): connection refused')
            output = io.StringIO()
            with mock.patch.object(monitor, 'collect', side_effect=fail), mock.patch.object(monitor, 'publish') as send, contextlib.redirect_stderr(output):
                self.assertEqual(monitor.main(['--config', str(path), '--dry-run']), 2)
            self.assertIn('SLURM_SNAPSHOT', output.getvalue())
            self.assertIn('scontrol show nodes failed (exit 1): connection refused', output.getvalue())
            self.assertFalse(send.called)
            self.assertFalse((Path(temp) / 'state.json').exists())

    def test_api_bad_shape_not_treated_as_empty(self):
        with mock.patch.object(monitor, 'run', return_value='{"data": null}'), self.assertRaises(ValueError):
            monitor.oci(self.config, 'r', ['compute', 'instance', 'list'])

    def test_main_failure_freezes_incidents_and_sends_health_then_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            self.collect_setup(temp)
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps(self.config))
            state_path = Path(temp) / 'state.json'
            prior = monitor.reconcile({}, self.find(), 0, self.config, lambda _: True)
            state_path.write_text(json.dumps(prior))
            with mock.patch.object(monitor, 'collect', side_effect=RuntimeError('secret')), mock.patch.object(monitor.time, 'time', return_value=100), mock.patch.object(monitor, 'publish', return_value=True) as send:
                self.assertEqual(monitor.main(['--config', str(path)]), 2)
                self.assertFalse(send.called)
            self.assertEqual(json.loads(state_path.read_text())['records'], prior['records'])
            with mock.patch.object(monitor, 'collect', side_effect=RuntimeError('secret')), mock.patch.object(monitor.time, 'time', return_value=1000), mock.patch.object(monitor, 'publish', return_value=True) as send:
                self.assertEqual(monitor.main(['--config', str(path)]), 2)
                self.assertIn('MONITOR_UNAVAILABLE', send.call_args.args[1]['title'])
                self.assertNotIn('secret', send.call_args.args[1]['body'])
            snapshot = ([], {}, self.queues, [], set())
            with mock.patch.object(monitor, 'collect', return_value=snapshot), mock.patch.object(monitor, 'publish', return_value=True) as send:
                self.assertEqual(monitor.main(['--config', str(path)]), 0)
                self.assertIn('RECOVERED', send.call_args.args[1]['title'])

    def test_dry_run_and_disabled_do_not_mutate_or_notify(self):
        with tempfile.TemporaryDirectory() as temp:
            self.collect_setup(temp)
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps(self.config))
            snapshot = ([self.instance], self.clusters, self.queues, [self.node], set())
            with mock.patch.object(monitor, 'collect', return_value=snapshot), mock.patch.object(monitor, 'publish') as send:
                self.assertEqual(monitor.main(['--config', str(path), '--dry-run']), 0)
                self.assertFalse(send.called)
                self.assertFalse((Path(temp) / 'state.json').exists())
            path.write_text('{"enabled": false}')
            with mock.patch.object(monitor, 'collect') as collect:
                self.assertEqual(monitor.main(['--config', str(path)]), 0)
                self.assertFalse(collect.called)

    def test_configuration_deploy_and_all_three_launch_tags(self):
        import yaml
        schema = yaml.safe_load((ROOT / 'schema.yaml').read_text())
        self.assertNotIn('autoscaling_instance_monitor_enabled', schema['variables'])
        deploy = (ROOT / 'autoscaling-instance-monitor.tf').read_text()
        self.assertIn('*/5 * * * *', deploy)
        self.assertIn('oci.home', deploy)
        self.assertIn('!var.autoscaling_failure_notifications_enabled || var.slurm', deploy)
        self.assertIn('sudo rm -f /etc/cron.d/', deploy)
        for name in ('instance-pool-configuration.tf', 'cluster-network-configuration.tf', 'compute-nodes.tf'):
            source = (ROOT / 'autoscaling/tf_init' / name).read_text()
            self.assertIn('"oci_hpc_autoscaling_deployment"', source)
            self.assertIn('var.slurm_notification_deployment_id', source)


    def test_admin_notification_ui_reuses_existing_controls_and_topic(self):
        import yaml
        schema = yaml.safe_load((ROOT / 'schema.yaml').read_text())
        variables = schema['variables']
        groups = [g for g in schema['variableGroups']
                  if '${autoscaling_failure_notifications_enabled}' in g.get('variables', [])]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['title'], 'Autoscaling 管理者メール通知')
        self.assertEqual(group['variables'], ['${autoscaling_failure_notifications_enabled}',
                                            '${autoscaling_failure_admin_email}'])
        self.assertEqual(group['visible'], '${autoscaling}')
        self.assertFalse(variables['autoscaling_failure_notifications_enabled']['default'])
        self.assertNotIn('autoscaling_instance_monitor_enabled', variables)
        self.assertEqual(variables['autoscaling_failure_admin_email']['default'], '')
        self.assertEqual(variables['autoscaling_failure_admin_email']['visible'],
                         '${autoscaling_failure_notifications_enabled}')
        deploy = (ROOT / 'autoscaling-instance-monitor.tf').read_text()
        self.assertRegex(deploy, r'topic_id\s*=\s*local.autoscaling_failure_topic_id')
        self.assertNotIn('resource "oci_ons_notification_topic"', deploy)
        self.assertNotIn('resource "oci_ons_subscription"', deploy)
        self.assertNotIn('autoscaling_instance_monitor_enabled', deploy)
        self.assertRegex(deploy, r'count\s*=\s*var.autoscaling_failure_notifications_enabled \? 1 : 0')
        self.assertRegex(deploy, r'enabled\s*=\s*var.autoscaling_failure_notifications_enabled')
        self.assertIn('var.autoscaling_failure_notifications_enabled ? "sudo install', deploy)
        variables_tf = (ROOT / 'variables.tf').read_text()
        self.assertNotIn('variable "autoscaling_instance_monitor_enabled"', variables_tf)


    def test_real_cli_stubs_publish_retry_recovery_and_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temp:
            self.collect_setup(temp)
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps(self.config))
            bindir = Path(temp) / 'bin'
            bindir.mkdir()
            stub = '''#!PYTHON
import json, os, sys
from pathlib import Path
root=Path(os.environ['MONITOR_TEST_ROOT'])
name=Path(sys.argv[0]).name
args=sys.argv[1:]
assert os.environ['LC_ALL'] == 'C.UTF-8'
assert os.environ['LANG'] == 'C.UTF-8'
if name == 'oci':
    if 'publish' in args:
        if (root/'deny-publish').exists():
            sys.exit(1)
        data=json.loads(Path(args[args.index('--from-json')+1][7:]).read_text())
        with (root/'delivered').open('a') as stream:
            stream.write(json.dumps(data)+'\\n')
        print('{}')
    elif 'list-vnics' in args:
        print(json.dumps({'data':[{'private-ip':'10.0.0.1'}]}))
    else:
        print(json.dumps({'data':[] if (root/'terminated').exists() else [{'id':'test-instance','lifecycle-state':'RUNNING','freeform-tags':{'oci_hpc_autoscaling_deployment':'deployment-a','parent_cluster':'batch-q-1-hpc'}}]}))
elif name == 'scontrol':
    print('No nodes in the system')
elif name == 'squeue':
    pass
else:
    sys.exit(2)
'''.replace('PYTHON', sys.executable)
            for name in ('oci', 'scontrol', 'squeue'):
                executable = bindir / name
                executable.write_text(stub)
                executable.chmod(0o700)
            with mock.patch.dict(os.environ, PATH=str(bindir), MONITOR_TEST_ROOT=temp):
                with mock.patch.object(monitor.time, 'time', return_value=0):
                    self.assertEqual(monitor.main(['--config', str(path)]), 0)
                (Path(temp)/'deny-publish').touch()
                with mock.patch.object(monitor.time, 'time', return_value=900):
                    self.assertEqual(monitor.main(['--config', str(path)]), 0)
                state = json.loads((Path(temp)/'state.json').read_text())
                self.assertIsNone(next(iter(state['records'].values()))['last_sent'])
                (Path(temp)/'deny-publish').unlink()
                with mock.patch.object(monitor.time, 'time', return_value=1200):
                    self.assertEqual(monitor.main(['--config', str(path)]), 0)
                delivered = [json.loads(line) for line in (Path(temp)/'delivered').read_text().splitlines()]
                self.assertEqual(len(delivered), 1)
                self.assertEqual(delivered[0]['topicId'], 'topic-a')
                self.assertIn('LOCAL_RECORD_MISSING', delivered[0]['title'])
                (Path(temp)/'terminated').touch()
                with mock.patch.object(monitor.time, 'time', return_value=1500):
                    self.assertEqual(monitor.main(['--config', str(path)]), 0)
                self.assertIn('RECOVERED', json.loads((Path(temp)/'delivered').read_text().splitlines()[-1])['title'])
                with (Path(temp)/'.lock').open('a+') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with mock.patch.object(monitor, 'collect') as collect:
                        self.assertEqual(monitor.main(['--config', str(path)]), 0)
                        self.assertFalse(collect.called)
                self.assertEqual((Path(temp)/'state.json').stat().st_mode & 0o777, 0o600)
                self.assertFalse(list(Path(temp).glob('.publish-*')))

    def test_legacy_orphan_and_explicit_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            self.collect_setup(temp)
            self.config['legacy_instance_clusters'] = {'legacy-orphan': 'batch-q-1-hpc'}
            data = [{'id':'legacy-orphan', 'lifecycle-state':'RUNNING', 'freeform-tags':{}},
                    {'id':'excluded', 'lifecycle-state':'RUNNING', 'freeform-tags':{
                        monitor.OWNER_TAG:'deployment-a', 'parent_cluster':'batch-q-1-hpc'}},
                    {'id':'initial', 'lifecycle-state':'RUNNING', 'freeform-tags':{
                        monitor.OWNER_TAG:'deployment-a', 'parent_cluster':'initial-cluster'}}]
            self.config['excluded_instance_ids'] = ['excluded']
            self.config['excluded_cluster_names'] = ['initial-cluster']
            with mock.patch.object(monitor, 'oci', side_effect=lambda c,r,a: [{'private-ip':'10.0.0.1'}] if 'list-vnics' in a else data), mock.patch.object(monitor, 'slurm_snapshot', return_value=([],set())):
                instances = monitor.collect(self.config)[0]
            self.assertEqual([i['id'] for i in instances], ['legacy-orphan'])
            self.assertEqual(instances[0]['cluster'], 'batch-q-1-hpc')

    def test_explicit_historical_scopes_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp:
            self.collect_setup(temp)
            self.config['additional_scopes'] = [{'region':'old-region', 'compartment_id':'old-compartment'}]
            with mock.patch.object(monitor, 'oci', return_value=[]) as cli, mock.patch.object(monitor, 'slurm_snapshot', return_value=([],set())):
                monitor.collect(self.config)
            self.assertIn('old-region', [call.args[1] for call in cli.call_args_list])
            path = Path(temp)/'config.json'
            for key, value in [('grace_seconds', 0), ('legacy_instance_clusters', []),
                               ('additional_scopes', ['bad']), ('instance_principal', 'false')]:
                path.write_text(json.dumps(dict(self.config, **{key:value})))
                with self.assertRaises(ValueError):
                    monitor.load_config(str(path))

    def test_corrupted_state_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            self.collect_setup(temp)
            path = Path(temp)/'config.json'
            path.write_text(json.dumps(self.config))
            state = Path(temp)/'state.json'
            for content in ('broken-state', '[]', '{"version":1,"records":{"invalid":{}}}',
                            '{"version":1,"records":{},"health":null}'):
                state.write_text(content)
                with mock.patch.object(monitor, 'publish') as send, mock.patch.object(monitor, 'collect') as collect:
                    self.assertEqual(monitor.main(['--config', str(path)]), 2)
                    self.assertFalse(send.called)
                    self.assertFalse(collect.called)
                    self.assertEqual(state.read_text(), content)


if __name__ == '__main__':
    unittest.main()
