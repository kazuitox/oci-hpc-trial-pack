"""Exact topology identities and failure preservation, including real Ansible I/O."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / 'playbooks/library/slurm_topology.py'
spec = importlib.util.spec_from_file_location('slurm_topology', MODULE)
topology = importlib.util.module_from_spec(spec)
spec.loader.exec_module(topology)

SOURCE = '''# retained comment
SwitchName=inactive-compute-e5 Nodes=compute-e5-node-[1-3] LinkSpeed=100 # retain
SwitchName=inactive-compute-e5-lite Nodes=compute-e5-lite-node-[1-3]
SwitchName=compute-1-e5 Nodes=compute-e5-node-4
SwitchName=compute-1-e5-lite Nodes=compute-e5-lite-node-4
'''


def expand(value):
    nodes = []
    for part in value.split(','):
        match = re.fullmatch(r'(.*)\[(\d+)-(\d+)\]', part)
        if match:
            nodes.extend(match[1] + str(i) for i in range(int(match[2]), int(match[3]) + 1))
        else:
            nodes.append(part)
    return nodes


def update(source=SOURCE, *, action='add', keyword='e5', nodes=None, racks=None):
    return topology.plan_update(source, nodes or ['compute-' + keyword + '-node-1'],
                                action, 'compute-1-' + keyword,
                                'inactive-compute-' + keyword, racks or {},
                                expand, lambda values: ','.join(sorted(values)))


class TopologyIdentityTests(unittest.TestCase):
    def test_add_and_remove_preserve_other_keyword_and_existing_nodes(self):
        for keyword, other in [('e5', 'e5-lite'), ('e5-lite', 'e5')]:
            for action in ['add', 'remove']:
                with self.subTest(keyword=keyword, action=action):
                    result = update(keyword=keyword, action=action)
                    for line in SOURCE.splitlines():
                        if 'SwitchName=inactive-compute-' + other + ' ' in line or 'SwitchName=compute-1-' + other + ' ' in line:
                            self.assertIn(line + '\n', result)
                    self.assertIn('compute-' + keyword + '-node-4', result)
                    self.assertIn('# retained comment\n', result)
                    self.assertIn('LinkSpeed=100 # retain', result)

    def test_empty_inactive_switch_is_valid_and_operations_are_idempotent(self):
        source = 'SwitchName=inactive-compute-e5 Nodes=\n'
        removed = update(source, action='remove')
        self.assertIn('Nodes=compute-e5-node-1', removed)
        self.assertEqual(update(removed, action='remove'), removed)
        added = update(removed)
        self.assertIn('SwitchName=inactive-compute-e5 Nodes=\n', added)
        self.assertEqual(update(added), added)

    def test_missing_active_switch_is_expected_but_missing_inactive_is_fatal(self):
        self.assertIn('SwitchName=compute-1-e5 Nodes=', update(SOURCE.replace('SwitchName=compute-1-e5 Nodes=compute-e5-node-4\n', '')))
        with self.assertRaisesRegex(ValueError, 'Missing switch'):
            update(SOURCE.replace('SwitchName=inactive-compute-e5 Nodes=compute-e5-node-[1-3] LinkSpeed=100 # retain\n', ''))

    def test_duplicate_or_malformed_switch_is_fatal(self):
        for line in ['SwitchName=inactive-compute-e5 Nodes=x\n',
                     'SwitchName=inactive-compute-e5 Nodes=x Nodes=y\n',
                     'SwitchName=inactive-compute-e5 Switches=a Nodes=x\n',
                     'SwitchName=inactive-compute-e5 broken\n']:
            with self.subTest(line=line), self.assertRaises(ValueError):
                update(SOURCE + line)

    def test_literal_regex_characters_and_tabs(self):
        source = '  SwitchName=inactive-compute-e5.x\tNodes=n1\nSwitchName=inactive-compute-e5ax Nodes=n2\n'
        result = topology.plan_update(source, ['n1'], 'add', 'compute-1-e5.x',
                                      'inactive-compute-e5.x', {}, expand, ','.join)
        self.assertIn('SwitchName=inactive-compute-e5ax Nodes=n2\n', result)
        self.assertIn('  SwitchName=inactive-compute-e5.x\tNodes=\n', result)

    def test_slurm_errors_or_bad_output_abort_planning(self):
        def fail(value):
            raise ValueError('Slurm failed')
        for expansion, condensation in [(fail, ','.join), (expand, fail),
                                        (lambda value: [], ','.join),
                                        (expand, lambda value: ''),
                                        (expand, lambda value: 'wrong-node')]:
            with self.subTest(expansion=expansion, condensation=condensation), self.assertRaises(ValueError):
                topology.plan_update(SOURCE, ['compute-e5-node-1'], 'add', 'compute-1-e5',
                                      'inactive-compute-e5', {}, expansion, condensation)

    def test_racks_and_parent_are_exact_and_siblings_remain(self):
        source = SOURCE.replace('SwitchName=compute-1-e5 Nodes=compute-e5-node-4\n',
                                'SwitchName=compute-1-e5 Switches=compute-1-e5:r1\n'
                                'SwitchName=compute-1-e5:r1 Nodes=compute-e5-node-4\n'
                                'SwitchName=compute-1-e5:r10 Nodes=compute-e5-node-5\n')
        added = update(source, racks={'compute-e5-node-1': 'compute-1-e5:r1'})
        self.assertIn('SwitchName=compute-1-e5:r1 Nodes=compute-e5-node-1,compute-e5-node-4', added)
        self.assertIn('SwitchName=compute-1-e5:r10 Nodes=compute-e5-node-5\n', added)
        removed = update(added, action='remove', nodes=['compute-e5-node-1', 'compute-e5-node-4'])
        self.assertNotIn('SwitchName=compute-1-e5:r1 ', removed)
        self.assertIn('SwitchName=compute-1-e5 Switches=compute-1-e5:r10\n', removed)
        self.assertIn('SwitchName=compute-1-e5-lite Nodes=compute-e5-lite-node-4\n', removed)

    def test_last_rack_removes_parent(self):
        source = 'SwitchName=inactive-compute-e5 Nodes=\nSwitchName=compute-1-e5 Switches=compute-1-e5:r1\nSwitchName=compute-1-e5:r1 Nodes=compute-e5-node-1\n'
        self.assertEqual(update(source, action='remove'), 'SwitchName=inactive-compute-e5 Nodes=compute-e5-node-1\n')

    def test_reclaim_uses_whole_keyword_and_preserves_other_nodes(self):
        result = topology.plan_update(SOURCE, ['compute-e5-lite-node-4'], 'reclaim', None, None,
                                      {}, expand, ','.join)
        self.assertIn('SwitchName=compute-1-e5 Nodes=compute-e5-node-4\n', result)
        self.assertIn('SwitchName=inactive-compute-e5 Nodes=compute-e5-node-[1-3] LinkSpeed=100 # retain\n', result)
        self.assertNotIn('SwitchName=compute-1-e5-lite ', result)
        self.assertIn('compute-e5-lite-node-4', result)
        self.assertEqual(topology.plan_update(result, ['compute-e5-lite-node-4'], 'reclaim', None, None, {}, expand, ','.join), result)

    def test_wrong_or_incomplete_rack_mapping_is_rejected(self):
        for racks in [{'compute-e5-node-1': 'compute-1-e5-lite:r1'}, {'other': 'compute-1-e5:r1'}]:
            with self.subTest(racks=racks), self.assertRaises(ValueError):
                update(racks=racks)


@unittest.skipUnless(shutil.which('ansible-playbook'), 'ansible-playbook is required for integration tests')
class AnsibleTopologyTests(unittest.TestCase):
    def run_play(self, task_file, source=SOURCE, *, failure='', missing_type=False, check=False):
        with tempfile.TemporaryDirectory(prefix='topology-identity-') as directory:
            root = Path(directory)
            path = root / 'topology.conf'
            path.write_text(source)
            path.chmod(0o640)
            command = root / 'scontrol'
            command.write_text('#!' + sys.executable + '\n' + '''import os,re,sys
if os.environ.get('TOPOLOGY_FAIL') == sys.argv[2]:
    sys.exit(1)
value=sys.argv[3]
if sys.argv[2] == 'hostname':
    for part in value.split(','):
        m=re.fullmatch(r'(.*)\\[(\\d+)-(\\d+)\\]', part)
        if m:
            print('\\n'.join(m[1]+str(i) for i in range(int(m[2]),int(m[3])+1)))
        else:
            print(part)
else:
    print(','.join(sorted(set(value.split(',')))))
''')
            command.chmod(0o755)
            tasks = yaml.safe_load((ROOT / task_file).read_text())
            # Exercise the actual production queue resolution, validation and update tasks.
            tasks = [task for task in tasks if task.get('name') in (
                'Resolve the inactive switch for this queue and instance type',
                'Require one instance keyword', 'Update topology using exact switch identities',
                'Return unreachable nodes using exact switch identities')]
            for task in tasks:
                task.pop('become', None)
                task.pop('notify', None)
            play = [{ 'hosts': 'localhost', 'gather_facts': False,
                     'vars': {'ansible_python_interpreter': sys.executable,
                              'slurm_conf_path': str(root), 'cluster_name': 'compute-1-e5',
                              'queue': 'compute', 'instance_type': 'E5',
                              'nodes_to_add': ['compute-e5-node-1'],
                              'topology_racks': {'compute-e5-node-1': 'compute-1-e5:r1'},
                              'unreachable_slurm_nodes': ['compute-e5-node-4'],
                              'queues': [{'name': 'compute', 'instance_types': [
                                  {'name': 'E5-other' if missing_type else 'E5', 'instance_keyword': 'e5'},
                                  {'name': 'E5-lite', 'instance_keyword': 'e5-lite'}]},
                                  {'name': 'another-queue', 'instance_types': [
                                      {'name': 'E5', 'instance_keyword': 'another'}]}]},
                     'tasks': tasks}]
            play_path = root / 'play.yml'
            play_path.write_text(yaml.safe_dump(play, sort_keys=False))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       ANSIBLE_LIBRARY=str(MODULE.parent), ANSIBLE_LOCAL_TEMP=str(root / 'local'),
                       ANSIBLE_REMOTE_TEMP=str(root / 'remote'), TOPOLOGY_FAIL=failure)
            result = subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(play_path)] +
                                    (['--check'] if check else []), env=env, capture_output=True, text=True, timeout=45)
            return result, path.read_text(), path.stat().st_mode & 0o777

    def test_standard_lite_and_delete_entrypoints_preserve_prefix_neighbor(self):
        for name in ['compute.yml', 'lite_compute.yml', 'destroy.yml', 'destroy-rack-aware.yml']:
            with self.subTest(name=name):
                result, output, mode = self.run_play('playbooks/roles/slurm/tasks/' + name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('SwitchName=inactive-compute-e5-lite Nodes=compute-e5-lite-node-[1-3]\n', output)
                self.assertIn('SwitchName=compute-1-e5-lite Nodes=compute-e5-lite-node-4\n', output)
                self.assertEqual(mode, 0o640)

    def test_rack_create_and_unreachable_entrypoints(self):
        for name in ['playbooks/roles/slurm/tasks/compute-rack-aware.yml',
                     'playbooks/roles/destroy_unreachable/tasks/slurm.yml',
                     'playbooks/roles/destroy_unreachable/tasks/slurm-rack-aware.yml']:
            with self.subTest(name=name):
                source = SOURCE.replace('SwitchName=compute-1-e5 Nodes=compute-e5-node-4\n', '') if 'compute-rack' in name else SOURCE
                result, output, _ = self.run_play(name, source)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('SwitchName=inactive-compute-e5-lite Nodes=compute-e5-lite-node-[1-3]\n', output)
                if 'compute-rack' in name:
                    self.assertIn('SwitchName=compute-1-e5:r1 Nodes=compute-e5-node-1\n', output)

    def test_failures_leave_file_byte_for_byte_unchanged(self):
        missing = SOURCE.replace('SwitchName=inactive-compute-e5 Nodes=compute-e5-node-[1-3] LinkSpeed=100 # retain\n', '')
        for name in ['compute.yml', 'lite_compute.yml', 'destroy.yml', 'destroy-rack-aware.yml']:
            for source, failure, missing_type in [(missing, '', False),
                                                  (SOURCE + 'SwitchName=inactive-compute-e5 Nodes=x\n', '', False),
                                                  (SOURCE, 'hostname', False), (SOURCE, 'hostlistsorted', False),
                                                  (SOURCE, '', True)]:
                with self.subTest(name=name, failure=failure, missing_type=missing_type):
                    result, output, mode = self.run_play('playbooks/roles/slurm/tasks/' + name, source,
                                                         failure=failure, missing_type=missing_type)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(output, source)
                    self.assertEqual(mode, 0o640)

    def test_concurrent_module_updates_keep_both_keywords(self):
        with tempfile.TemporaryDirectory(prefix='topology-concurrent-') as directory:
            root = Path(directory)
            path = root / 'topology.conf'
            path.write_text(SOURCE)
            command = root / 'scontrol'
            command.write_text('#!' + sys.executable + '\n' +
                               'import sys,time\n' +
                               'sys.path.insert(0, ' + repr(str(ROOT / 'tests')) + ')\n' +
                               'from test_autoscaling_topology_identity import expand\n' +
                               'time.sleep(0.05)\n' +
                               "print('\\n'.join(expand(sys.argv[3])) if sys.argv[2] == 'hostname' else sys.argv[3])\n")
            command.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            running = []
            for keyword in ['e5', 'e5-lite']:
                params = {'path': str(path), 'action': 'add',
                          'cluster': 'compute-1-' + keyword,
                          'inactive': 'inactive-compute-' + keyword,
                          'nodes': ['compute-' + keyword + '-node-1']}
                process = subprocess.Popen([sys.executable, str(MODULE)], env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                process.stdin.write(json.dumps({'ANSIBLE_MODULE_ARGS': params}))
                process.stdin.close()
                process.stdin = None
                running.append(process)
            for process in running:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            output = path.read_text()
            for keyword in ['e5', 'e5-lite']:
                self.assertIn('SwitchName=compute-1-' + keyword + ' Nodes=compute-' +
                              keyword + '-node-1,compute-' + keyword + '-node-4', output)
                self.assertIn('SwitchName=inactive-compute-' + keyword + ' Nodes=compute-' +
                              keyword + '-node-2,compute-' + keyword + '-node-3', output)

    def test_check_mode_never_writes(self):
        result, output, _ = self.run_play('playbooks/roles/slurm/tasks/compute.yml', check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(output, SOURCE)


if __name__ == '__main__':
    unittest.main()
