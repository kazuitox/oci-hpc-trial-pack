"""Role integration and real lvol module tests; never execute disk commands."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

ROLE = Path(__file__).resolve().parents[1]
COLLECTIONS = os.environ.get('LOCALDISK_ANSIBLE_COLLECTIONS', '')


class ModuleResult(Exception):
    def __init__(self, failed, result):
        self.failed, self.result = failed, result


class FakeModule:
    check_mode = False

    def __init__(self, existing=False, rc=0):
        self.existing, self.rc, self.calls = existing, rc, []
        self.params = dict(vg='vg_nvmes', lv='lv_nvmes', size=None if existing else '937480192k',
                           opts='--type raid1 --mirrors 1 --nosync', state='present',
                           force=False, shrink=False, active=True, resizefs=False,
                           thinpool=None, snapshot=None, pvs='/dev/nvme0n1,/dev/nvme1n1')

    def boolean(self, value):
        return bool(value)

    def get_bin_path(self, name, required=False):
        return name

    def run_command(self, cmd):
        self.calls.append(cmd)
        if cmd.startswith('vgs '):
            return 0, 'vg_nvmes;2000000000;1900000000;4096\n', ''
        if cmd.startswith('lvs '):
            return 0, 'lv_nvmes;937480192;Rwi-a-r---\n' if self.existing else '', ''
        if cmd.startswith('lvcreate '):
            return self.rc, 'Logical volume "lv_nvmes" created.\n', (
                "  WARNING: New raid1 won't be synchronised. Don't read what you didn't write!\n")
        if cmd.startswith('lvchange '):
            return 0, '', ''
        raise AssertionError('Unexpected disk command: ' + cmd)

    def exit_json(self, **kwargs):
        raise ModuleResult(False, kwargs)

    def fail_json(self, **kwargs):
        raise ModuleResult(True, kwargs)


class LvolWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(COLLECTIONS) / 'ansible_collections/community/general/plugins/modules/system/lvol.py'
        if not COLLECTIONS or not path.is_file():
            raise unittest.SkipTest('set LOCALDISK_ANSIBLE_COLLECTIONS to community.general 4.8.1')
        spec = importlib.util.spec_from_file_location('localdisk_test_lvol', path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def invoke(self, fake):
        with patch.object(self.module, 'AnsibleModule', return_value=fake), \
                patch.object(self.module, 'get_lvm_version', return_value=2003000):
            with self.assertRaises(ModuleResult) as result:
                self.module.main()
        return result.exception

    def test_successful_lvcreate_with_nosync_warning_succeeds(self):
        fake = FakeModule()
        result = self.invoke(fake)
        self.assertFalse(result.failed)
        self.assertTrue(result.result['changed'])
        self.assertTrue(any('--type raid1 --mirrors 1 --nosync' in cmd for cmd in fake.calls))

    def test_nonzero_lvcreate_with_warning_fails(self):
        result = self.invoke(FakeModule(rc=5))
        self.assertTrue(result.failed)
        self.assertEqual(result.result['rc'], 5)

    def test_existing_lv_is_activated_without_recreation_or_resize(self):
        fake = FakeModule(existing=True)
        result = self.invoke(fake)
        self.assertFalse(result.failed)
        self.assertFalse(any(cmd.startswith(('lvcreate ', 'lvresize ', 'lvreduce ')) for cmd in fake.calls))
        self.assertTrue(any(cmd.startswith('lvchange -ay ') for cmd in fake.calls))


class TaskContractTests(unittest.TestCase):
    def test_filesystem_and_mount_follow_lvol_without_force(self):
        tasks = yaml.safe_load((ROLE / 'tasks/lvm.yml').read_text())
        fs = next(i for i, t in enumerate(tasks) if 'community.general.filesystem' in t)
        mount = next(i for i, t in enumerate(tasks) if 'ansible.posix.mount' in t)
        self.assertLess(fs, mount)
        self.assertFalse(tasks[fs]['community.general.filesystem']['force'])
        self.assertEqual(tasks[fs]['community.general.filesystem']['dev'], '/dev/vg_nvmes/lv_nvmes')
        self.assertEqual(tasks[mount]['ansible.posix.mount']['state'], 'mounted')
        creation = next(t for t in tasks if t.get('name') == 'Plan and create a new scratch LV')
        self.assertIn('not (localdisk_lv_exists | bool)', creation['when'])

    def test_inventory_and_callers_keep_public_settings(self):
        root = ROLE.parents[2]
        inventory = (root / 'autoscaling/tf_init/inventory.tpl').read_text()
        self.assertIn('nvme_path=/mnt/localdisk/', inventory)
        self.assertIn('autoscaling = true', inventory)
        for name in ('localdisk', 'log_vol', 'redundancy'):
            self.assertIn(name + '=${' + name + '}', inventory)
        for name in ('new_nodes.yml', 'lite_new_nodes.yml'):
            text = (root / 'playbooks' / name).read_text()
            self.assertIn('when: localdisk | default(true) | bool', text)


if __name__ == '__main__':
    unittest.main()
