"""Pure, read-only layout decisions for the localdisk role.

The task files perform all device changes. Keeping the selection and capacity
arithmetic here makes them testable without touching real block devices.
"""
import copy
import hashlib
import json
import re

from ansible.errors import AnsibleFilterError


def localdisk_merge_probes(results, probes):
    """Enrich old lsblk JSON with read-only blkid -p -o export results.

    rc=2 is accepted only with empty stdout/stderr and no contradictory
    lsblk signature/partition information. Never substitute an empty disk
    for missing, ambiguous (rc=8), malformed, or failed probe results.
    """
    by_name = {}
    for probe in probes:
        name = probe.get('item')
        if not isinstance(name, str) or name in by_name:
            raise AnsibleFilterError('Missing or duplicate NVMe probe identity')
        by_name[name] = probe
    names = [result['item'] for result in results]
    if len(set(names)) != len(names) or set(names) != set(by_name):
        raise AnsibleFilterError('NVMe device list does not match blkid results')
    enriched = copy.deepcopy(results)
    for result in enriched:
        name = result['item']
        path = '/dev/' + name
        probe = by_name[name]
        rc = probe.get('rc')
        stdout = probe.get('stdout', '').strip()
        stderr = probe.get('stderr', '').strip()
        if probe.get('failed') or rc not in (0, 2) or stderr or (rc == 2 and stdout):
            raise AnsibleFilterError('Unsafe blkid result for {}: rc={}, stderr={}'.format(
                path, rc, stderr))
        report = json.loads(result['stdout'])
        disks = [block for block in report['blockdevices'] if block['name'] == path]
        if len(disks) != 1 or disks[0]['type'] != 'disk':
            raise AnsibleFilterError('lsblk did not return a unique whole disk: ' + path)
        disk = disks[0]
        tags = {}
        for line in stdout.splitlines():
            key, separator, value = line.partition('=')
            if not separator or not key or key in tags:
                raise AnsibleFilterError('Malformed blkid output for ' + path)
            tags[key] = value
        if tags.get('DEVNAME', path) != path:
            raise AnsibleFilterError('blkid returned a different device for ' + path)
        if rc == 0 and not (tags.get('TYPE') or tags.get('PTTYPE')):
            raise AnsibleFilterError('blkid succeeded without a recognized signature on ' + path)
        if rc == 2 and (disk.get('fstype') or disk.get('pttype') or any(
                block['type'] == 'part' for block in report['blockdevices'])):
            raise AnsibleFilterError('blkid and lsblk disagree about existing data on ' + path)
        if disk.get('fstype') and tags.get('TYPE') != disk['fstype']:
            raise AnsibleFilterError('Conflicting filesystem signatures on ' + path)
        if disk.get('pttype') and tags.get('PTTYPE') != disk['pttype']:
            raise AnsibleFilterError('Conflicting partition tables on ' + path)
        disk['pttype'] = tags.get('PTTYPE') or None
        if tags.get('TYPE'):
            disk['fstype'] = tags['TYPE']
        if tags.get('LABEL'):
            disk['label'] = tags['LABEL']
        result['stdout'] = json.dumps(report)
    return enriched


def localdisk_select(results, pvs, mounts, mount_path):
    owners = {pv['pv_name'].strip(): pv['vg_name'].strip() for pv in pvs}
    active_mounts = {}
    for mount in mounts:
        active_mounts.setdefault(mount['maj:min'], []).append(mount['target'])
    devices, excluded = [], []
    lv_paths = {'/dev/vg_nvmes/lv_nvmes', '/dev/mapper/vg_nvmes-lv_nvmes'}
    for result in results:
        name = result['item']
        match = re.fullmatch(r'nvme([0-9]+)n([1-9][0-9]*)', name)
        if not match:
            raise AnsibleFilterError('Unexpected NVMe name: ' + name)
        index, namespace = match.groups()
        suffix = index if namespace == '1' else index + 'n' + namespace
        target = mount_path if suffix == '0' else mount_path + suffix
        label = 'locscratch' + suffix
        if len(label) > 12:
            label = 'scratch' + hashlib.sha256(name.encode()).hexdigest()[:5]
        path = '/dev/' + name
        blocks = json.loads(result['stdout'])['blockdevices']
        by_name = {block['name']: block for block in blocks}
        if path not in by_name:
            raise AnsibleFilterError('lsblk did not return ' + path)
        disk = by_name[path]
        partitions = [block for block in blocks if block['type'] == 'part']
        reason = None
        partition_mm = by_name.get(path + 'p1', {}).get('maj:min')
        scratch_lv_mms = {block.get('maj:min') for block in blocks
                          if block['name'] in lv_paths and owners.get(path) == 'vg_nvmes'}
        for mount in mounts:
            if (mount['target'] == target
                    and mount['maj:min'] not in scratch_lv_mms | {partition_mm}):
                reason = 'scratch mount point is occupied by another device: ' + target
        for block in blocks:
            if reason:
                break
            block_path = block['name']
            fs = block.get('fstype') or ''
            owner = owners.get(block_path, '')
            allowed_mount = None
            if block_path == path + 'p1':
                allowed_mount = target
            elif block_path in lv_paths and owners.get(path) == 'vg_nvmes':
                allowed_mount = mount_path
            targets = set(active_mounts.get(block.get('maj:min'), []))
            if block.get('mountpoint'):
                targets.add(block['mountpoint'])
            if str(block.get('ro')).lower() in ('1', 'true'):
                reason = 'read-only device ' + block_path
            elif any(value != allowed_mount for value in targets):
                reason = 'device mounted outside its scratch path: ' + block_path
            elif owner and owner != 'vg_nvmes':
                reason = 'physical volume belongs to ' + owner
            elif block_path in owners and block_path != path:
                reason = 'partitioned LVM physical volume: ' + block_path
            elif block['type'] not in ('disk', 'part', 'lvm'):
                reason = 'unsupported holder/device type: ' + block['type']
            elif block['type'] == 'lvm' and owners.get(path) != 'vg_nvmes':
                reason = 'LVM holder not owned by the scratch volume group'
            elif fs == 'LVM2_member':
                if block_path != path or block_path not in owners:
                    reason = 'partitioned or unrecognized LVM physical volume'
            elif fs:
                scratch_partition = (block_path == path + 'p1' and fs == 'xfs'
                                     and (block.get('label') in (label, 'localscratch')
                                          or targets == {target}))
                scratch_lv_path = (block_path in lv_paths or re.fullmatch(
                    r'/dev/mapper/vg_nvmes-lv_nvmes_rimage_[0-9]+', block_path))
                scratch_lv = (scratch_lv_path and fs == 'xfs'
                              and owners.get(path) == 'vg_nvmes')
                if not (scratch_partition or scratch_lv):
                    reason = 'existing non-scratch filesystem/signature on ' + block_path
            if reason:
                break
        if reason:
            excluded.append({'device': path, 'reason': reason})
            continue
        partition_exists = path + 'p1' in by_name
        if path in owners and not partitions:
            layout = 'raw_pv'
        elif not partitions and not disk.get('pttype') and not disk.get('fstype'):
            layout = 'individual'  # Empty disk, usable by either branch.
        elif (len(partitions) == 1 and partition_exists
              and disk.get('pttype') == 'gpt' and not disk.get('fstype')):
            layout = 'individual'
        else:
            layout = 'unsupported_partition_layout'
        devices.append({'path': path, 'mount': target, 'label': label,
                        'layout': layout, 'partition_exists': partition_exists,
                        'partition_table': disk.get('pttype') or ''})
    devices.sort(key=lambda device: tuple(map(int, re.fullmatch(
        r'/dev/nvme([0-9]+)n([0-9]+)', device['path']).groups())))
    return {'devices': devices, 'excluded': excluded}


def localdisk_plan_lvm(paths, pvs, extent_bytes, redundancy, reserve_mib=16, nosync=True):
    """Size an LV from the smallest selected PV, accounting for RAID copies.

    An odd redundant disk count leaves one PV unallocated. It is not a hot
    spare. This avoids dropping redundancy or creating mismatched RAID stripes.
    """
    paths = list(paths)
    count = len(paths)
    if count < 2:
        raise AnsibleFilterError('A combined scratch LV needs at least two PVs')
    if count > 64:
        raise AnsibleFilterError('More than 64 scratch PVs is not supported')
    extent = int(float(extent_bytes))
    if extent <= 0 or extent % 1024:
        raise AnsibleFilterError('Unexpected LVM extent size: ' + str(extent_bytes))
    if reserve_mib < 0:
        raise AnsibleFilterError('RAID metadata reserve must not be negative')
    by_path = {pv['pv_name'].strip(): pv for pv in pvs}
    if redundancy:
        used_count = count - count % 2
        used = paths[:used_count]
        stripes = used_count // 2
        opts = ('--type raid1 --mirrors 1' if used_count == 2 else
                '--type raid10 --mirrors 1 --stripes ' + str(stripes))
        if nosync:
            opts += ' --nosync'
        reserve = max(1, (reserve_mib * 1024 * 1024 + extent - 1) // extent)
    else:
        used, stripes, reserve = paths, count, 0
        opts = '--type striped --stripes ' + str(stripes)
    free = []
    for path in used:
        pv = by_path.get(path)
        if pv is None or pv['vg_name'].strip() != 'vg_nvmes':
            raise AnsibleFilterError('Missing scratch PV: ' + path)
        free.append(int(pv['pv_pe_count']) - int(pv['pv_pe_alloc_count']))
    per_device_extents = min(free) - reserve
    if per_device_extents <= 0:
        raise AnsibleFilterError('Insufficient free extents for scratch and RAID metadata')
    return {'opts': opts, 'pvs': used, 'unallocated_pvs': paths[len(used):],
            'size_kib': per_device_extents * stripes * extent // 1024}


class FilterModule:
    def filters(self):
        return {'localdisk_merge_probes': localdisk_merge_probes,
                'localdisk_select': localdisk_select,
                'localdisk_plan_lvm': localdisk_plan_lvm}
