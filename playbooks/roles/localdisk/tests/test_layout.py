"""Run with python3 -m unittest discover -s tests -v; no disk writes."""
import importlib.util
import json
import pathlib
import unittest

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / 'filter_plugins' / 'localdisk.py'
SPEC = importlib.util.spec_from_file_location('localdisk_filters', str(PLUGIN))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
select = MODULE.localdisk_select
plan = MODULE.localdisk_plan_lvm
merge = MODULE.localdisk_merge_probes


def block(name, kind='disk', **kwargs):
    result = {'name': name, 'type': kind, 'fstype': None, 'label': None,
              'mountpoint': None, 'size': 960000000000, 'ro': '0', 'pttype': None,
              'maj:min': '259:0' if kind == 'disk' else '259:1'}
    result.update(kwargs)
    return result


def device(name='nvme0n1', blocks=None):
    return {'item': name, 'stdout': json.dumps({'blockdevices': blocks or [block('/dev/' + name)]})}


def probe(name='nvme0n1', rc=2, stdout='', stderr='', **kwargs):
    return dict(item=name, rc=rc, stdout=stdout, stderr=stderr, **kwargs)


class ProbeTests(unittest.TestCase):
    def test_actual_ol8_output_without_pttype_and_empty_blkid(self):
        results = []
        for index in range(2):
            # Same schema and string-valued size/ro as the real E6 node.
            root = block('/dev/nvme{}n1'.format(index), size='960000000000',
                         **{'maj:min': '259:' + str(index)})
            del root['pttype']
            results.append(device('nvme{}n1'.format(index), [root]))
        merged = merge(results, [probe('nvme0n1'), probe('nvme1n1')])
        chosen = select(merged, [], [], '/mnt/localdisk')['devices']
        self.assertEqual(len(chosen), 2)
        self.assertTrue(all(d['layout'] == 'individual' and not d['partition_table']
                            and not d['partition_exists'] for d in chosen))
        self.assertNotIn('pttype', json.loads(results[0]['stdout'])['blockdevices'][0])

    def test_blkid_restores_gpt_for_existing_scratch_partition(self):
        result = device(blocks=[block('/dev/nvme0n1'),
                                block('/dev/nvme0n1p1', 'part', fstype='xfs', label='locscratch0')])
        merged = merge([result], [probe(rc=0, stdout='DEVNAME=/dev/nvme0n1\nPTUUID=abc\nPTTYPE=gpt\n')])
        chosen = select(merged, [], [], '/mnt/localdisk')['devices'][0]
        self.assertEqual(chosen['partition_table'], 'gpt')
        self.assertTrue(chosen['partition_exists'])

    def test_empty_gpt_is_not_treated_as_a_blank_disk(self):
        merged = merge([device()], [probe(rc=0, stdout='PTTYPE=gpt\nPTUUID=abc\n')])
        chosen = select(merged, [], [], '/mnt/localdisk')['devices'][0]
        self.assertEqual(chosen['layout'], 'unsupported_partition_layout')
        self.assertEqual(chosen['partition_table'], 'gpt')

    def test_blkid_recovers_signature_missing_from_lsblk(self):
        merged = merge([device()], [probe(rc=0, stdout='TYPE=ext4\nLABEL=database\n')])
        self.assertEqual(select(merged, [], [], '/mnt/localdisk')['devices'], [])

    def test_existing_scratch_pv_is_reused(self):
        merged = merge([device()], [probe(rc=0, stdout='TYPE=LVM2_member\n')])
        chosen = select(merged, [{'pv_name': '/dev/nvme0n1', 'vg_name': 'vg_nvmes'}],
                        [], '/mnt/localdisk')['devices'][0]
        self.assertEqual(chosen['layout'], 'raw_pv')

    def test_probe_errors_never_become_blank_disks(self):
        errors = [probe(rc=8), probe(rc=4), probe(rc=1),
                  probe(stderr='I/O error'), probe(rc=0, stdout='TYPE=xfs', stderr='warning'),
                  probe(stdout='PTTYPE=gpt'), probe(failed=True),
                  probe(rc=0, stdout='DEVNAME=/dev/nvme0n1'),
                  probe(rc=0, stdout='TYPE=xfs\nTYPE=ext4'),
                  probe(rc=0, stdout='TYPE=xfs\ninvalid line'),
                  probe(rc=0, stdout='DEVNAME=/dev/sda\nTYPE=xfs')]
        for result in errors:
            with self.subTest(result=result), self.assertRaises(MODULE.AnsibleFilterError):
                merge([device()], [result])

    def test_missing_or_duplicate_probe_is_rejected(self):
        for probes in ([], [probe('nvme1n1')], [probe(), probe()]):
            with self.subTest(probes=probes), self.assertRaises(MODULE.AnsibleFilterError):
                merge([device()], probes)

    def test_empty_probe_must_not_contradict_lsblk(self):
        for blocks in ([block('/dev/nvme0n1', fstype='xfs')],
                       [block('/dev/nvme0n1', pttype='gpt')],
                       [block('/dev/nvme0n1'), block('/dev/nvme0n1p1', 'part')]):
            with self.subTest(blocks=blocks), self.assertRaises(MODULE.AnsibleFilterError):
                merge([device(blocks=blocks)], [probe()])

    def test_conflicting_signatures_are_rejected(self):
        with self.assertRaises(MODULE.AnsibleFilterError):
            merge([device(blocks=[block('/dev/nvme0n1', fstype='ext4')])],
                  [probe(rc=0, stdout='TYPE=xfs')])


class SelectionTests(unittest.TestCase):
    def select(self, results=None, pvs=None, mounts=None):
        return select(results or [device()], pvs or [], mounts or [], '/mnt/localdisk')

    def test_empty_disk(self):
        chosen = self.select()['devices'][0]
        self.assertEqual(chosen['layout'], 'individual')
        self.assertFalse(chosen['partition_exists'])
        self.assertEqual(chosen['mount'], '/mnt/localdisk')

    def test_natural_order_namespaces_and_large_indices(self):
        results = [device(n) for n in ['nvme100n1', 'nvme10n1', 'nvme0n2', 'nvme2n1']]
        chosen = self.select(results)['devices']
        self.assertEqual([x['path'] for x in chosen],
                         ['/dev/nvme0n2', '/dev/nvme2n1', '/dev/nvme10n1', '/dev/nvme100n1'])
        self.assertEqual(chosen[0]['mount'], '/mnt/localdisk0n2')
        self.assertTrue(all(len(x['label']) <= 12 for x in chosen))

    def test_boot_volume_is_excluded(self):
        blocks = [block('/dev/nvme0n1', pttype='gpt'),
                  block('/dev/nvme0n1p1', 'part', fstype='xfs', mountpoint='/')]
        self.assertEqual(self.select([device(blocks=blocks)])['devices'], [])

    def test_bind_mount_is_excluded_even_if_lsblk_shows_scratch(self):
        blocks = [block('/dev/nvme0n1', pttype='gpt'), block('/dev/nvme0n1p1', 'part',
                  fstype='xfs', label='locscratch0', mountpoint='/mnt/localdisk')]
        mounts = [{'maj:min': '259:1', 'target': '/mnt/localdisk'},
                  {'maj:min': '259:1', 'target': '/other-data'}]
        self.assertEqual(self.select([device(blocks=blocks)], mounts=mounts)['devices'], [])

    def test_foreign_vg_is_excluded(self):
        blocks = [block('/dev/nvme0n1', fstype='LVM2_member')]
        pvs = [{'pv_name': '/dev/nvme0n1', 'vg_name': 'database'}]
        self.assertEqual(self.select([device(blocks=blocks)], pvs)['devices'], [])

    def test_existing_unmounted_filesystem_is_excluded(self):
        blocks = [block('/dev/nvme0n1', fstype='ext4')]
        self.assertEqual(self.select([device(blocks=blocks)])['devices'], [])

    def test_existing_scratch_partition_can_be_reused(self):
        blocks = [block('/dev/nvme0n1', pttype='gpt'),
                  block('/dev/nvme0n1p1', 'part', fstype='xfs', label='locscratch0')]
        chosen = self.select([device(blocks=blocks)])['devices'][0]
        self.assertTrue(chosen['partition_exists'])
        self.assertEqual(chosen['layout'], 'individual')

    def test_empty_partition_after_previous_failure_is_not_a_raw_disk(self):
        blocks = [block('/dev/nvme0n1', pttype='gpt'), block('/dev/nvme0n1p1', 'part')]
        chosen = self.select([device(blocks=blocks)])['devices'][0]
        self.assertTrue(chosen['partition_exists'])
        self.assertEqual(chosen['partition_table'], 'gpt')

    def test_existing_raw_pv_and_raid_images_can_be_reused(self):
        blocks = [block('/dev/nvme0n1', fstype='LVM2_member'),
                  block('/dev/mapper/vg_nvmes-lv_nvmes_rimage_0', 'lvm', fstype='xfs'),
                  block('/dev/mapper/vg_nvmes-lv_nvmes', 'lvm', fstype='xfs',
                        mountpoint='/mnt/localdisk', **{'maj:min': '253:5'})]
        pvs = [{'pv_name': '/dev/nvme0n1', 'vg_name': 'vg_nvmes'}]
        chosen = self.select([device(blocks=blocks)], pvs)['devices'][0]
        self.assertEqual(chosen['layout'], 'raw_pv')

    def test_read_only_disk_is_excluded(self):
        self.assertEqual(self.select([device(blocks=[block('/dev/nvme0n1', ro='1')])])['devices'], [])

    def test_pv_ownership_takes_precedence_over_stale_lsblk_fstype(self):
        pvs = [{'pv_name': '/dev/nvme0n1', 'vg_name': 'vg_nvmes'}]
        self.assertEqual(self.select(pvs=pvs)['devices'][0]['layout'], 'raw_pv')

    def test_partition_pv_is_excluded_even_without_lsblk_fstype(self):
        blocks = [block('/dev/nvme0n1', pttype='gpt'), block('/dev/nvme0n1p1', 'part')]
        pvs = [{'pv_name': '/dev/nvme0n1p1', 'vg_name': 'vg_nvmes'}]
        self.assertEqual(self.select([device(blocks=blocks)], pvs)['devices'], [])

    def test_existing_foreign_mount_at_target_is_excluded(self):
        mounts = [{'maj:min': '8:1', 'target': '/mnt/localdisk'}]
        self.assertEqual(self.select(mounts=mounts)['devices'], [])

    def test_other_namespace_is_not_confused_with_n1(self):
        chosen = self.select([device('nvme0n10')])['devices'][0]
        self.assertEqual(chosen['mount'], '/mnt/localdisk0n10')


class CapacityTests(unittest.TestCase):
    def plan(self, count, redundant=True, counts=None, nosync=True):
        paths = ['/dev/nvme{}n1'.format(i) for i in range(count)]
        pvs = [{'pv_name': path, 'vg_name': 'vg_nvmes',
                'pv_pe_count': str((counts or [1000] * count)[i]), 'pv_pe_alloc_count': '0'}
               for i, path in enumerate(paths)]
        return plan(paths, pvs, '4194304.00', redundant, nosync=nosync)

    def test_two_disks_use_raid1_with_metadata_reserved(self):
        layout = self.plan(2)
        self.assertEqual(layout['opts'], '--type raid1 --mirrors 1 --nosync')
        self.assertEqual(layout['size_kib'], 996 * 4096)

    def test_three_disks_keep_redundancy_and_leave_one_unallocated(self):
        layout = self.plan(3)
        self.assertEqual(layout['opts'], '--type raid1 --mirrors 1 --nosync')
        self.assertEqual(layout['unallocated_pvs'], ['/dev/nvme2n1'])

    def test_four_disks_use_two_mirrored_stripes(self):
        layout = self.plan(4)
        self.assertEqual(layout['opts'], '--type raid10 --mirrors 1 --stripes 2 --nosync')
        self.assertEqual(layout['size_kib'], 996 * 4096 * 2)

    def test_eight_disks_use_four_mirrored_stripes(self):
        self.assertEqual(self.plan(8)['opts'], '--type raid10 --mirrors 1 --stripes 4 --nosync')

    def test_initial_sync_can_be_enabled_explicitly(self):
        self.assertEqual(self.plan(2, nosync=False)['opts'], '--type raid1 --mirrors 1')
        self.assertEqual(self.plan(4, nosync=False)['opts'],
                         '--type raid10 --mirrors 1 --stripes 2')

    def test_actual_e6_capacity_matches_successful_manual_creation(self):
        self.assertEqual(self.plan(2, counts=[228881, 228881])['size_kib'], 937480192)

    def test_odd_large_count_leaves_one_unallocated(self):
        self.assertEqual(self.plan(5)['unallocated_pvs'], ['/dev/nvme4n1'])

    def test_nonredundant_stripes_follow_disk_count(self):
        for count in (2, 3, 4, 8, 12, 16):
            with self.subTest(count=count):
                layout = self.plan(count, False)
                self.assertEqual(layout['opts'], '--type striped --stripes ' + str(count))
                self.assertEqual(layout['size_kib'], 1000 * 4096 * count)

    def test_unequal_disks_fit_the_smallest_pv(self):
        self.assertEqual(self.plan(2, counts=[1000, 500])['size_kib'], 496 * 4096)

    def test_insufficient_space_fails_before_creation(self):
        with self.assertRaises(MODULE.AnsibleFilterError):
            self.plan(2, counts=[4, 1000])

    def test_a_foreign_vg_is_not_used_in_capacity_calculation(self):
        with self.assertRaises(MODULE.AnsibleFilterError):
            plan(['/dev/a', '/dev/b'], [{'pv_name': '/dev/a', 'vg_name': 'foreign'}],
                 4194304, True)


if __name__ == '__main__':
    unittest.main()
