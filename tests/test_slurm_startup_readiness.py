"""Execute probes and the production Ansible handler/task chain with fake services."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'playbooks/roles/slurm'
spec = importlib.util.spec_from_file_location('slurm_readiness', ROLE / 'files/slurm_readiness.py')
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


class ProbeTests(unittest.TestCase):
    def test_tres_requires_valid_cpu_mem_and_ids(self):
        self.assertTrue(readiness.valid_tres('cpu||1\nmem||2\ngres|gpu|1001\n'))
        for output in ('', 'cpu||1', 'cpu||0\nmem||2', 'cpu||1\nmem||bad',
                       'cpu|other|1\nmem||2', 'cpu||1\nmem||2\nerror', 'cpu||1|\nmem||2|'):
            with self.subTest(output=output):
                self.assertFalse(readiness.valid_tres(output))

    def test_ping_requires_requested_controller(self):
        output = 'Slurmctld(primary) at ctrl is DOWN\nSlurmctld(backup) at backup is UP\n'
        self.assertFalse(readiness.valid_ping(output, 'primary'))
        self.assertTrue(readiness.valid_ping(output, 'backup'))
        for output in ('UP', 'Slurmctld(primary) at ctrl is DOWN',
                       'Slurmctld(primary) at ctrl is UP garbage'):
            self.assertFalse(readiness.valid_ping(output, 'primary'))

    def test_failed_command_with_valid_output_is_retried(self):
        with patch.object(readiness, 'run_command', side_effect=[
            (1, 'cpu||1\nmem||2', 'failure'), (0, 'cpu||1\nmem||2', '')]) as run:
            self.assertEqual(readiness.wait_ready(['fake'], readiness.valid_tres, 1, .1, .01), 2)
            self.assertEqual(run.call_count, 2)

    def test_environment_is_copied_and_clusters_removed(self):
        for value in (None, '', 'another-cluster'):
            with self.subTest(value=value), patch.dict(os.environ, {'SLURM_CONF': '/test/slurm.conf', 'KEEP_ME': 'yes'}):
                if value is None:
                    os.environ.pop('SLURM_CLUSTERS', None)
                else:
                    os.environ['SLURM_CLUSTERS'] = value
                before = dict(os.environ)
                def run(argv, env, timeout):
                    self.assertNotIn('SLURM_CLUSTERS', env)
                    self.assertEqual(env['SLURM_CONF'], '/test/slurm.conf')
                    self.assertEqual(env['KEEP_ME'], 'yes')
                    return 0, 'cpu||1\nmem||2', ''
                with patch.object(readiness, 'run_command', side_effect=run):
                    readiness.wait_ready(['fake'], readiness.valid_tres, 1, .1, .01)
                self.assertEqual(dict(os.environ), before)

    def test_hung_command_and_child_are_killed_within_deadline(self):
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'rc=124.*command timed out'):
            readiness.wait_ready([sys.executable, '-c',
                'import subprocess,time; subprocess.Popen(["sleep","30"]); time.sleep(30)'],
                lambda output: True, .35, .1, .01)
        self.assertLess(time.monotonic() - start, 2)

    def test_missing_command_reports_reason(self):
        with self.assertRaisesRegex(RuntimeError, 'rc=127'):
            readiness.wait_ready(['/does/not/exist'], lambda output: True, .05, .02, .01)


ANSIBLE = shutil.which('ansible-playbook')


@unittest.skipUnless(ANSIBLE, 'ansible-playbook is required for execution-order tests')
class AnsibleOrderTests(unittest.TestCase):
    def run_play(self, scenario, backup=False, clusters=None, notify=True, distribution='Ubuntu', disabled=False):
        with tempfile.TemporaryDirectory(prefix='slurm-readiness-test-') as tmp:
            base = Path(tmp)
            role = base / 'roles/slurm'
            for directory in ('tasks', 'handlers', 'files', 'defaults'):
                shutil.copytree(ROLE / directory, role / directory)
            # Load the real handlers/defaults through the role, without installing Slurm.
            tasks = [{'name': 'Notify production handlers', 'debug': {'msg': 'config changed'},
                      'changed_when': True, 'notify': ['restart munge', 'restart slurm server', 'reconfigure slurm']},
                     {'meta': 'flush_handlers'}] if notify is True else ([{'name': 'Notify reconfigure', 'debug': {'msg': 'topology changed'}, 'changed_when': True, 'notify': 'reconfigure slurm'}, {'meta': 'flush_handlers'}] if notify == 'reconfigure' else [
                      {'include_tasks': 'start-controller.yml'}])
            if notify == 'unchanged':
                server_tasks = yaml.safe_load((ROLE / 'tasks/server.yml').read_text())
                ensure = next(t for t in server_tasks if t.get('name') == 'Ensure unchanged configuration has ready Slurm services')
                tasks = [ensure, {'name': 'Notify topology', 'debug': {'msg': 'topology'},
                    'changed_when': True, 'notify': 'reconfigure slurm'}, {'meta': 'flush_handlers'}]
            (role / 'tasks/main.yml').write_text(yaml.safe_dump(tasks))
            library = base / 'library'
            library.mkdir()
            (library / 'service.py').write_text('''from ansible.module_utils.basic import AnsibleModule
import os,json
m=AnsibleModule(argument_spec=dict(name=dict(required=True),state=dict(required=True),enabled=dict(type='bool')))
with open(os.environ['EVENT_LOG'],'a') as f: f.write(json.dumps(dict(kind='service',**m.params))+'\\n')
if os.environ['SCENARIO'] == 'missing_unit' and m.params['name'] == 'slurmctld' and m.params['state'] != 'stopped':
    m.fail_json(msg='Could not find the requested service slurmctld')
m.exit_json(changed=True)
''')
            bins = base / 'bin'
            bins.mkdir()
            # Absolute Slurm executable paths for both OS cases are redirected to this temporary tree.
            stub = '''#!{python}
import os,sys,json,time
from pathlib import Path
kind = Path(sys.argv[0]).name
with open(os.environ['EVENT_LOG'],'a') as f:
    f.write(json.dumps(dict(kind=kind,args=sys.argv[1:],clusters=os.environ.get('SLURM_CLUSTERS'),conf=os.environ.get('SLURM_CONF'),keep=os.environ.get('KEEP_ME')))+'\\n')
scenario = os.environ['SCENARIO']
if kind == 'systemctl':
    message = 'Failed to reset failed state of unit slurmctld.service: Unit slurmctld.service not loaded.'
    if scenario in ('unit_not_loaded', 'missing_unit', 'reset_wrong_rc'):
        print(message, file=sys.stderr)
        sys.exit(2 if scenario == 'reset_wrong_rc' else 1)
    if scenario == 'reset_permission':
        print('Failed to reset failed state of unit slurmctld.service: Access denied', file=sys.stderr)
        sys.exit(1)
    if scenario == 'reset_timeout': sys.exit(124)
    if scenario == 'reset_other_unit':
        print(message.replace('slurmctld.service', 'other.service'), file=sys.stderr)
        sys.exit(1)
    if scenario == 'reset_mixed_errors':
        print(message + ' Additional failure', file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
state = Path(os.environ['EVENT_LOG']+'.'+kind)
count = int(state.read_text())+1 if state.exists() else 1
state.write_text(str(count))
if kind == 'sacctmgr':
    if scenario == 'dbd_hang': time.sleep(30)
    if scenario == 'dbd_fail': print('database unavailable',file=sys.stderr); sys.exit(1)
    if scenario == 'dbd_invalid': print('cpu||1'); sys.exit(0)
    if scenario == 'dbd_bad_rc': print('cpu||1\\nmem||2'); sys.exit(1)
    if scenario == 'delayed' and count == 1: sys.exit(1)
    print('cpu||1\\nmem||2')
else:
    if sys.argv[1] == 'reconfigure': sys.exit(0)
    if scenario == 'ping_hang': time.sleep(30)
    if scenario == 'ping_fail': print('Slurmctld(primary) at ctrl is DOWN'); sys.exit(0)
    if scenario == 'ping_bad_rc': print('Slurmctld(primary) at ctrl is UP'); sys.exit(1)
    if scenario == 'delayed' and count == 1: print('Slurmctld(primary) at ctrl is DOWN'); sys.exit(0)
    print('Slurmctld(primary) at ctrl is '+('DOWN' if scenario == 'backup_only' else 'UP'))
    print('Slurmctld(backup) at backup is UP')
'''.format(python=sys.executable)
            for name in ('sacctmgr', 'scontrol', 'systemctl'):
                p = bins / name
                p.write_text(stub)
                p.chmod(0o755)
            # The GNU timeout wrapper only surrounds mocked systemctl in this test.
            p = bins / 'timeout'
            p.write_text('#!/bin/sh\nshift\nshift\nexec "$@"\n')
            p.chmod(0o755)
            # reconfigure selects /usr/local or /usr from the controller's facts.
            reconfig = role / 'tasks/reconfigure.yml'
            text = reconfig.read_text().replace("'/usr/local'", repr(str(base / 'ubuntu'))).replace("'/usr'", repr(str(base / 'oracle')))
            for os_path in ('ubuntu', 'oracle'):
                (base / os_path).mkdir()
                (base / os_path / 'bin').symlink_to(bins)
            reconfig.write_text(text)
            inventory = base / 'inventory'
            inventory.write_text('[controller]\n127.0.0.1 ansible_connection=local ansible_distribution=' + distribution + '\n' +
                ('[slurm_backup]\n127.0.0.1\n' if backup else '[slurm_backup]\n'))
            play = [{'hosts': 'all', 'gather_facts': False, 'vars': {
                'ansible_python_interpreter': sys.executable, 'ansible_become': False,
                'slurm': not disabled, 'ansible_distribution': distribution, 'slurm_config': {'changed': False},
                'slurm_exec': str(base), 'slurm_conf_path': str(base / 'conf'),
                'slurm_readiness_timeout': .45, 'slurm_readiness_command_timeout': .12,
                'slurm_readiness_interval': .03},
                'environment': {'PATH': str(bins) + ':' + os.environ['PATH'],
                    'EVENT_LOG': str(base / 'events'), 'SCENARIO': scenario, 'KEEP_ME': 'preserved',
                    **({'SLURM_CLUSTERS': clusters} if clusters is not None else {})},
                'roles': [{'role': 'slurm', 'when': 'slurm | bool'}], 'tasks': [{'command': 'scontrol after', 'changed_when': False}]}]
            path = base / 'play.yml'
            path.write_text(yaml.safe_dump(play))
            env = dict(os.environ, ANSIBLE_LOCAL_TEMP=str(base / 'local'),
                       ANSIBLE_REMOTE_TEMP=str(base / 'remote'), ANSIBLE_LIBRARY=str(library),
                       ANSIBLE_NOCOLOR='1', ANSIBLE_HOST_KEY_CHECKING='False')
            env.pop('SLURM_CLUSTERS', None)
            result = subprocess.run([ANSIBLE, '-i', str(inventory), str(path)], env=env,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
            events = [json.loads(line) for line in (base / 'events').read_text().splitlines()]
            return result, events

    def test_production_handlers_delay_then_reconfigure(self):
        result, events = self.run_play('delayed', clusters='remote')
        self.assertEqual(result.returncode, 0, result.stdout)
        sequence = [(e['kind'], e.get('name'), e.get('state')) for e in events]
        self.assertEqual(sequence[:4], [('service','munge','restarted'), ('service','slurmctld','stopped'),
                         ('service','slurmdbd','restarted'), ('sacctmgr',None,None)])
        start = next(i for i,e in enumerate(events) if e.get('name') == 'slurmctld' and e.get('state') == 'restarted')
        self.assertEqual(sum(e['kind'] == 'sacctmgr' for e in events[:start]), 2)
        reconfigure = next(i for i,e in enumerate(events) if e.get('args') == ['reconfigure'])
        self.assertGreater(sum(e.get('args') == ['ping'] for e in events[:reconfigure]), 1)
        for e in events:
            if e['kind'] in ('sacctmgr','scontrol') and e.get('args') != ['after']:
                self.assertIsNone(e['clusters'])
                self.assertTrue(e['conf'].endswith('/conf/slurm.conf'))
                self.assertEqual(e['keep'], 'preserved')

    def test_first_start_with_unloaded_unit_continues_to_ready_controller(self):
        for distribution, backup in (('OracleLinux', False), ('Ubuntu', False), ('OracleLinux', True)):
            with self.subTest(distribution=distribution, backup=backup):
                result, events = self.run_play('unit_not_loaded', distribution=distribution, backup=backup)
                self.assertEqual(result.returncode, 0, result.stdout)
                reset = next(i for i, event in enumerate(events) if event['kind'] == 'systemctl')
                started = next(i for i, event in enumerate(events)
                               if event.get('name') == 'slurmctld' and event.get('state') == 'restarted')
                self.assertLess(reset, started)
                self.assertTrue(any(event.get('args') == ['ping'] for event in events[started:]))
                self.assertTrue(any(event.get('args') == ['reconfigure'] for event in events[started:]))
                self.assertEqual(events[-1].get('args'), ['after'])

    def test_reset_errors_still_block_start_and_followup(self):
        for scenario in ('reset_permission', 'reset_timeout', 'reset_other_unit',
                         'reset_wrong_rc', 'reset_mixed_errors'):
            with self.subTest(scenario=scenario):
                result, events = self.run_play(scenario)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('Clear previous slurmctld start-limit failure', result.stdout)
                self.assertFalse(any(event.get('name') == 'slurmctld' and
                                     event.get('state') == 'restarted' for event in events))
                self.assertFalse(any(event['kind'] == 'scontrol' for event in events))

    def test_unloaded_unit_does_not_hide_missing_service(self):
        result, events = self.run_play('missing_unit')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('Could not find the requested service slurmctld', result.stdout)
        self.assertFalse(any(event['kind'] == 'scontrol' for event in events))

    def test_accounting_failure_blocks_controller_and_followup(self):
        for scenario in ('dbd_fail', 'dbd_invalid', 'dbd_bad_rc', 'dbd_hang'):
            with self.subTest(scenario=scenario):
                result, events = self.run_play(scenario)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('readiness deadline', result.stdout)
                self.assertFalse(any(e.get('state') == 'restarted' and e.get('name') == 'slurmctld' for e in events))
                self.assertFalse(any(e['kind'] == 'scontrol' for e in events))

    def test_controller_failure_blocks_reconfigure_and_followup(self):
        for scenario in ('ping_fail', 'ping_bad_rc', 'ping_hang'):
            with self.subTest(scenario=scenario):
                result, events = self.run_play(scenario)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('readiness deadline', result.stdout)
                self.assertFalse(any(e.get('args') in (['reconfigure'], ['after']) for e in events))

    def test_backup_accepts_backup_up_without_primary_up(self):
        result, events = self.run_play('backup_only', backup=True, notify=False)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(events[0]['kind'], 'sacctmgr')
        self.assertFalse(any(e.get('name') == 'slurmdbd' for e in events))

    def test_clusters_and_reconfiguration_on_both_os_paths(self):
        for distribution, clusters in (('Ubuntu',None), ('OracleLinux',''), ('Ubuntu','other')):
            with self.subTest(distribution=distribution,clusters=clusters):
                result, events = self.run_play('ready', clusters=clusters, distribution=distribution)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertTrue(any(e.get('args') == ['reconfigure'] for e in events))

    def test_unchanged_configuration_starts_services_before_reconfigure(self):
        result, events = self.run_play('ready', notify='unchanged')
        self.assertEqual(result.returncode, 0, result.stdout)
        services = [(e['name'], e['state']) for e in events if e['kind'] == 'service']
        self.assertEqual(services, [('slurmdbd', 'started'), ('slurmctld', 'started')])
        self.assertEqual(events[1]['kind'], 'sacctmgr')
        self.assertTrue(any(e.get('args') == ['reconfigure'] for e in events))

    def test_disabled_slurm_skips_readiness_and_service_operations(self):
        result, events = self.run_play('dbd_fail', disabled=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([e.get('args') for e in events], [['after']])

    def test_reconfigure_alone_waits_before_followup(self):
        result, events = self.run_play('delayed', notify='reconfigure')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(any(e['kind'] in ('service', 'sacctmgr') for e in events))
        self.assertEqual([e['args'] for e in events], [['ping'], ['ping'], ['reconfigure'], ['after']])

    def test_reconfigure_only_failure_stops_followup(self):
        result, events = self.run_play('ping_fail', notify='reconfigure')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(any(e.get('args') in (['reconfigure'], ['after']) for e in events))


if __name__ == '__main__':
    unittest.main()
