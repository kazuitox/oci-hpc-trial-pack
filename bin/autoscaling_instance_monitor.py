#!/usr/bin/env python3
"""Read-only OCI/Slurm reconciliation; Python 3.6 compatible."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import yaml
from autoscaling_failure_notify import clean, oci_executable, publish

DEFAULT_CONFIG = '/opt/oci-hpc/conf/autoscaling_instance_monitor.json'
OWNER_TAG = 'oci_hpc_autoscaling_deployment'
TERMINAL_JOBS = {'COMPLETED', 'CANCELLED', 'FAILED', 'TIMEOUT', 'NODE_FAIL',
                 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'OUT_OF_MEMORY'}


class CommandError(RuntimeError):
    """A bounded command diagnostic safe to show locally and in health alerts."""


def command_label(command):
    name = Path(command[0]).name
    if name == 'oci':
        return 'oci ' + ' '.join(command[1:4])
    if name == 'scontrol' and 'show' in command:
        index = command.index('show')
        return 'scontrol ' + ' '.join(command[index:index + 2])
    return name


def stderr_detail(value):
    # A private key spans multiple lines, so line-based keyword removal alone
    # would leave the encoded body behind. Remove the entire PEM block first.
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|\Z)',
                   '[private key omitted]', str(value), flags=re.I)
    return clean(value, 1200).strip() or '(no diagnostic on stderr)'


def error_detail(error):
    # Do not expose arbitrary exceptions, configuration values or CLI argv.
    return '{}: {}'.format(type(error).__name__, error) if isinstance(error, CommandError) else type(error).__name__


def run(command):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('SQUEUE_', 'SINFO_')) and k != 'SLURM_CLUSTERS'}
    env.update(LC_ALL='C.UTF-8', LANG='C.UTF-8', SLURM_TIME_FORMAT='%Y-%m-%dT%H:%M:%S')
    label = command_label(command)
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, encoding='utf-8', errors='replace', timeout=90, env=env)
    except subprocess.TimeoutExpired:
        raise CommandError('{} timed out after 90 seconds'.format(label)) from None
    except OSError as error:
        raise CommandError('{} could not start: {}'.format(label, error.strerror or type(error).__name__)) from None
    if result.returncode:
        raise CommandError('{} failed (exit {}): {}'.format(
            label, result.returncode, stderr_detail(result.stderr)))
    return result.stdout


def oci(config, region, args):
    command = [oci_executable()] + args + ['--region', region, '--output', 'json',
               '--max-retries', '0', '--connection-timeout', '5', '--read-timeout', '20']
    if config['instance_principal']:
        command += ['--auth', 'instance_principal']
    result = json.loads(run(command))
    if not isinstance(result, dict) or not isinstance(result.get('data'), list):
        raise ValueError('OCI list response is invalid')
    return result['data']


def slurm_snapshot():
    nodes = []
    output = run(['scontrol', '--local', '--oneliner', 'show', 'nodes'])
    for line in output.splitlines():
        if not line.strip() or line.strip() == 'No nodes in the system':
            continue
        node = dict(re.findall(r'(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=(\S+)', line))
        if not node.get('NodeName') or not node.get('State') or not node.get('CPUAlloc', '').isdigit():
            raise ValueError('Slurm node response is invalid')
        nodes.append(node)
    busy = set()
    output = run(['squeue', '--local', '--all', '--noheader', '--array',
                  '--states=all', '--format=%i|%T|%N'])
    expressions = set()
    for line in output.splitlines():
        fields = [v.strip() for v in line.split('|')]
        if len(fields) != 3 or not fields[0] or not fields[1]:
            raise ValueError('Slurm job response is invalid')
        if fields[1] not in TERMINAL_JOBS and fields[2] not in ('', '(null)', 'None', 'N/A'):
            expressions.add(fields[2])
    for expression in sorted(expressions):
        expanded = run(['scontrol', 'show', 'hostnames', expression]).split()
        if not expanded:
            raise ValueError('Slurm job hostlist is empty')
        busy.update(expanded)
    return nodes, busy


def local_clusters(path):
    """Read only compute inventory sections. Never load/log Terraform state."""
    clusters = {}
    for folder in sorted(Path(path).iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        inventory = folder / 'inventory'
        members = {}
        if inventory.exists():
            section = ''
            for line in inventory.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.startswith('['):
                    section = line
                elif section in ('[compute]', '[compute_to_add]', '[compute_configured]', '[compute_to_destroy]'):
                    fields = line.split()
                    if fields and not line.startswith('#'):
                        attrs = dict(re.findall(r'(\w+)=([^\s]+)', line))
                        instance_id = attrs.get('oci_instance_id')
                        if instance_id:
                            members[instance_id] = [fields[0], attrs.get('ansible_host', '')]
        markers = {}
        for marker in ('currently_building', 'currently_destroying'):
            file = folder / marker
            try:
                markers[marker] = file.stat().st_mtime
            except FileNotFoundError:
                pass
        clusters[folder.name] = {'members': members, 'markers': markers}
    return clusters


def queue_type(queues, cluster):
    matches = []
    for queue in queues:
        prefix = queue['name'] + '-'
        if not cluster.startswith(prefix):
            continue
        index, separator, keyword = cluster[len(prefix):].partition('-')
        if separator and index.isdigit():
            matches.extend(item for item in queue['instance_types'] if item['instance_keyword'] == keyword)
    if len(matches) > 1:
        raise ValueError('Ambiguous queues.conf cluster mapping')
    return matches[0] if matches else None


def collect(config):
    config['_stage'] = 'LOCAL_CONFIGURATION'
    queues = yaml.safe_load(Path(config['queues_conf']).read_text(encoding='utf-8'))['queues']
    if not isinstance(queues, list) or not queues:
        raise ValueError('queues.conf has no queues')
    clusters = local_clusters(config['clusters_path'])
    known = {oid: name for name, data in clusters.items() for oid in data['members']}
    scopes = {(item['region'], item['targetCompartment'])
              for queue in queues for item in queue['instance_types']}
    scopes.add((config['region'], config['compartment_id']))
    scopes.update((item['region'], item['compartment_id']) for item in config.get('additional_scopes', []))
    instances = []
    for region, compartment in sorted(scopes):
        config['_stage'] = 'OCI_INSTANCE_LIST'
        for instance in oci(config, region, ['compute', 'instance', 'list', '--all', '--compartment-id', compartment]):
            if not isinstance(instance, dict) or not all(instance.get(k) for k in ('id', 'lifecycle-state')):
                raise ValueError('OCI instance response is incomplete')
            if instance['lifecycle-state'] == 'TERMINATED' or instance['id'] in config.get('excluded_instance_ids', []):
                continue
            tags = instance.get('freeform-tags') or {}
            if not isinstance(tags, dict):
                raise ValueError('Invalid ownership tags')
            owner = tags.get(OWNER_TAG)
            # A conflicting explicit owner is never overridden by local inventory.
            legacy = config.get('legacy_instance_clusters', {})
            owned = owner == config['deployment_id'] if owner else instance['id'] in known or instance['id'] in legacy
            if not owned:
                continue
            cluster = tags.get('parent_cluster') or tags.get('cluster_name') or known.get(instance['id']) or legacy.get(instance['id'])
            if cluster in config.get('excluded_cluster_names', []):
                continue
            if not cluster:
                raise ValueError('Owned instance has no cluster identity')
            instance = dict(instance, cluster=cluster, region=region)
            aliases = set(clusters.get(cluster, {}).get('members', {}).get(instance['id'], []))
            # Inventory may be gone and OCI display names may differ from Slurm
            # names (Compute Cluster). Match primary private IP/hostname too.
            config['_stage'] = 'OCI_VNIC_LIST'
            vnics = oci(config, region, ['compute', 'instance', 'list-vnics',
                        '--instance-id', instance['id'], '--compartment-id', compartment, '--all'])
            if not vnics and instance['lifecycle-state'] == 'RUNNING':
                raise ValueError('Owned instance has no VNIC evidence')
            for vnic in vnics:
                if not isinstance(vnic, dict) or not vnic.get('private-ip'):
                    raise ValueError('OCI VNIC response is invalid')
                aliases.update([vnic['private-ip'], vnic.get('hostname-label', '')])
            instance['aliases'] = aliases - {''}
            instances.append(instance)
    config['_stage'] = 'SLURM_SNAPSHOT'
    nodes, busy = slurm_snapshot()
    return instances, clusters, queues, nodes, busy


def findings(instances, clusters, queues, nodes, busy, now, config):
    result = {}
    by_cluster = {}
    for instance in instances:
        by_cluster.setdefault(instance['cluster'], []).append(instance)
    used_nodes = {}
    for cluster, members in by_cluster.items():
        local = clusters.get(cluster)
        item = queue_type(queues, cluster)
        markers = local['markers'] if local else {}
        phase = None
        for marker, threshold, reason in (
                ('currently_destroying', 'delete_timeout_seconds', 'DELETE_STALLED'),
                ('currently_building', 'build_timeout_seconds', 'BUILD_STALLED')):
            if marker in markers:
                phase = 'TRANSITION'
                if now - markers[marker] >= config[threshold]:
                    phase = reason
                break
        mapped = {}
        all_idle = True
        for instance in members:
            matched = [node for node in nodes if instance['aliases'].intersection(
                {node.get('NodeName'), node.get('NodeAddr'), node.get('NodeHostName')})]
            if len(matched) > 1:
                raise ValueError('Ambiguous Slurm identity')
            node = matched[0] if matched else None
            if node:
                previous_id = used_nodes.setdefault(node['NodeName'], instance['id'])
                if previous_id != instance['id']:
                    raise ValueError('Slurm node matched multiple OCI instances')
            mapped[instance['id']] = node
            if (not node or node['State'] != 'IDLE' or int(node['CPUAlloc']) != 0
                    or node['NodeName'] in busy):
                all_idle = False
        for instance in members:
            node = mapped[instance['id']]
            state = instance['lifecycle-state']
            reason = phase
            if reason == 'TRANSITION':
                continue
            if reason is None:
                if not local:
                    reason = 'LOCAL_RECORD_MISSING'
                elif state != 'RUNNING':
                    reason = 'OCI_NOT_RUNNING'
                elif not node:
                    reason = 'SLURM_NODE_MISSING'
                elif '*' in node['State'] or set(node['State'].split('+')).intersection(
                        {'DOWN', 'DRAIN', 'FAIL', 'FAILING', 'UNKNOWN', 'INVALID_REG', 'NOT_RESPONDING'}):
                    reason = 'SLURM_UNHEALTHY'
                elif item is None:
                    reason = 'QUEUE_CONFIG_MISSING'
                elif all_idle and item.get('permanent') is False:
                    reason = 'IDLE_CLUSTER_RETAINED'
            if reason:
                key = instance['id'] + ':' + reason
                result[key] = {'reason': reason, 'instance_id': instance['id'], 'cluster': cluster,
                               'region': instance['region'], 'oci_state': state,
                               'shape': instance.get('shape', 'unknown'),
                               'slurm_state': node['State'] if node else 'MISSING'}
    return result


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.monitor-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reconcile(previous, current, now, config, send):
    """Only successful snapshots resolve alerts. Delivery failure retains retry eligibility."""
    records = previous.get('records', {})
    updated = {}
    for key, detail in sorted(current.items()):
        record = dict(records.get(key, {'first_seen': now, 'samples': 0, 'last_sent': None}))
        # Long gaps and clock rollback cannot prove continuous observation.
        if now < record['first_seen'] or now - record.get('last_seen', now) > config['max_sample_gap_seconds']:
            record.update(first_seen=now, samples=0)
        record.update(last_seen=now, samples=record['samples'] + 1, detail=detail)
        mature = record['samples'] >= 2 and now - record['first_seen'] >= config['grace_seconds']
        due = record['last_sent'] is None or now - record['last_sent'] >= config['repeat_seconds']
        if mature and due:
            body = ('想定外の計算インスタンス残存、または監視不能を検出しました。\n'
                    '実際の請求額を確認した通知ではありません。自動削除は行いません。\n'
                    'First observed (UTC): {}\n{}\n'
                    '管理者は OCI / Slurm / 作成・削除ログと実行中ジョブを照合してください。').format(
                        datetime.datetime.fromtimestamp(record['first_seen'], datetime.timezone.utc).isoformat(),
                        json.dumps(detail, ensure_ascii=False, sort_keys=True))
            if send({'title': 'HPC instance monitor: ' + detail['reason'], 'body': body}):
                record['last_sent'] = now
        updated[key] = record
    for key, record in sorted(records.items()):
        if key not in current and record.get('last_sent') is not None:
            if not send({'title': 'HPC instance monitor: RECOVERED',
                         'body': '照合上の異常が解消しました。削除完了や課金停止を保証する通知ではありません。\n' +
                                 json.dumps(record['detail'], ensure_ascii=False, sort_keys=True)}):
                updated[key] = record
    return {'version': 1, 'last_success': now, 'records': updated}


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('Invalid configuration object')
    if config.get('enabled') is False:
        return config
    for name in ('region', 'compartment_id', 'deployment_id', 'topic_id', 'state_dir',
                 'clusters_path', 'queues_conf'):
        if not isinstance(config.get(name), str) or not config[name].strip():
            raise ValueError('Missing configuration: ' + name)
    config.setdefault('instance_principal', True)
    if type(config['instance_principal']) is not bool:
        raise ValueError('Invalid authentication setting')
    legacy = config.get('legacy_instance_clusters', {})
    if not isinstance(legacy, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in legacy.items()):
        raise ValueError('Invalid legacy instance mapping')
    scopes = config.get('additional_scopes', [])
    if not isinstance(scopes, list) or any(not isinstance(v, dict) or not isinstance(v.get('region'), str) or not v['region'] or not isinstance(v.get('compartment_id'), str) or not v['compartment_id'] for v in scopes):
        raise ValueError('Invalid additional scopes')
    for name, default in (('grace_seconds', 900), ('repeat_seconds', 21600),
                          ('build_timeout_seconds', 10800), ('delete_timeout_seconds', 3600),
                          ('max_sample_gap_seconds', 1800)):
        config.setdefault(name, default)
        if type(config[name]) is not int or config[name] <= 0:
            raise ValueError('Invalid threshold: ' + name)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--dry-run', action='store_true', help='Read and report only; do not notify or update state')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if config.get('enabled') is False:
            return 0
        state_dir = Path(config['state_dir'])
        if args.dry_run:
            snapshot = collect(config)
            current = findings(*snapshot, now=time.time(), config=config)
            print(json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(str(state_dir / '.lock'), 'a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            state_file = state_dir / 'state.json'
            previous = json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {'version': 1, 'records': {}}
            if not isinstance(previous, dict) or previous.get('version') != 1 or not isinstance(previous.get('records'), dict):
                raise ValueError('Invalid monitor state')
            if not isinstance(previous.get('health', {}), dict):
                raise ValueError('Invalid health state')
            for record in previous['records'].values():
                if not isinstance(record, dict) or not all(k in record for k in ('first_seen', 'last_seen', 'samples', 'last_sent', 'detail')):
                    raise ValueError('Invalid incident record')
            now = time.time()
            try:
                snapshot = collect(config)
                config['_stage'] = 'CLASSIFICATION'
                current = findings(*snapshot, now=now, config=config)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, yaml.YAMLError, subprocess.SubprocessError) as error:
                print('Monitor collection failed [{}]: {}'.format(config.get('_stage', 'UNKNOWN'), error_detail(error)), file=sys.stderr)
                # Freeze existing incident/recovery state; use a separate health stream.
                health = {'MONITOR_UNAVAILABLE': {'reason': 'MONITOR_UNAVAILABLE',
                          'error_type': type(error).__name__, 'stage': config.get('_stage', 'CLASSIFICATION')}}
                if isinstance(error, CommandError):
                    health['MONITOR_UNAVAILABLE']['diagnostic'] = str(error)
                health_state = reconcile(previous.get('health', {}), health, now, config,
                                         lambda msg: publish(dict(config, spool=str(state_dir)), msg))
                previous['health'] = health_state
                previous['last_attempt'] = now
                atomic_json(state_file, previous)
                return 2
            send = lambda msg: publish(dict(config, spool=str(state_dir)), msg)
            updated = reconcile(previous, current, now, config, send)
            updated['health'] = reconcile(previous.get('health', {}), {}, now, config, send)
            atomic_json(state_file, updated)
            return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, yaml.YAMLError, subprocess.SubprocessError) as error:
        print('Monitor failed [{}]: {}'.format(
            config.get('_stage', 'CONFIGURATION_OR_STATE') if 'config' in locals() else 'CONFIGURATION',
            error_detail(error)), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
