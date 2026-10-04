#!/usr/bin/python
"""Move nodes between exact topology switches in one validated transaction."""
import fcntl
import os
import re
import subprocess
import tempfile

DOCUMENTATION = r'''
---
module: slurm_topology
short_description: Move nodes between exact Slurm topology switches
options:
  path:
    type: path
    required: true
  nodes:
    type: list
    elements: str
    required: true
  action:
    type: str
    choices: [add, remove, reclaim]
    required: true
  cluster:
    type: str
  inactive:
    type: str
  racks:
    type: dict
    default: {}
'''


def identifier(value):
    if not isinstance(value, str) or not value or re.search(r'[\s,#=\[\]]', value):
        raise ValueError('Invalid topology identifier: {!r}'.format(value))
    return value


def plan_update(source, nodes, action, cluster, inactive, racks, expand, condense):
    """Build the entire replacement before writing; Slurm errors propagate."""
    records = {}
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        tokens = line.split('#', 1)[0].split()
        names = [token[11:] for token in tokens if token.startswith('SwitchName=')]
        if not names:
            continue
        if len(names) != 1:
            raise ValueError('Multiple SwitchName fields on one line')
        name = identifier(names[0])
        if name in records:
            raise ValueError('Duplicate SwitchName: ' + name)
        fields = {}
        for token in tokens:
            if '=' not in token:
                raise ValueError('Malformed switch line: ' + name)
            key, value = token.split('=', 1)
            if key in fields:
                raise ValueError('Duplicate field on switch: ' + name)
            fields[key] = value
        if ('Nodes' in fields) == ('Switches' in fields):
            raise ValueError('Expected Nodes or Switches on switch: ' + name)
        records[name] = (index, fields)

    nodes = set(identifier(node) for node in nodes)
    if not nodes:
        raise ValueError('No nodes supplied')
    if action != 'reclaim':
        identifier(cluster)
        identifier(inactive)
        if cluster == inactive or cluster.startswith('inactive-'):
            raise ValueError('Invalid cluster/inactive switch pair')
    for node, rack in racks.items():
        identifier(node)
        identifier(rack)
        if node not in nodes or not rack.startswith(cluster + ':') or rack == cluster + ':':
            raise ValueError('Rack must belong to the exact cluster and supplied nodes')
    if action == 'add' and racks and set(racks) != nodes:
        raise ValueError('Every node requires a rack')

    cache = {}
    replacements = {}

    def members(name, required=False):
        if name not in records:
            if required:
                raise ValueError('Missing switch: ' + name)
            return set()
        fields = records[name][1]
        if 'Nodes' not in fields:
            raise ValueError('Expected a Nodes switch: ' + name)
        if name not in cache:
            value = fields['Nodes']
            cache[name] = set(expand(value)) if value else set()
            if value and not cache[name]:
                raise ValueError('Hostlist expansion returned no nodes: ' + name)
            for node in cache[name]:
                identifier(node)
        return cache[name]

    def store(name, field, values, keep_empty=False):
        values = sorted(values)
        if not values and not keep_empty:
            replacements[name] = None
            return
        value = condense(values) if values and field == 'Nodes' else ','.join(values)
        if values and (not value or re.search(r'\s', value)):
            raise ValueError('Invalid condensed hostlist: ' + name)
        if values and field == 'Nodes' and set(expand(value)) != set(values):
            raise ValueError('Condensed hostlist does not preserve nodes: ' + name)
        if name in records:
            index, fields = records[name]
            if field not in fields:
                raise ValueError('Unexpected switch type: ' + name)
            # Keep all other fields, whitespace and comments on existing lines.
            replacements[name] = re.sub(
                r'(?<!\S)' + field + r'=\S*',
                lambda match: field + '=' + value,
                lines[index], count=1,
            )
        else:
            replacements[name] = 'SwitchName={} {}={}\n'.format(name, field, value)

    inactive_moves = {}
    if action == 'reclaim':
        for node in nodes:
            prefix, separator, index = node.rpartition('-node-')
            if not separator or not index.isdigit():
                raise ValueError('Cannot identify inactive switch for node: ' + node)
            name = 'inactive-' + prefix
            members(name, required=True)
            inactive_moves.setdefault(name, set()).add(node)
    else:
        members(inactive, required=True)
        inactive_moves[inactive] = nodes

    affected_parents = set()
    if action == 'add':
        targets = {}
        for node in nodes:
            targets.setdefault(racks.get(node, cluster), set()).add(node)
        for name, adding in targets.items():
            store(name, 'Nodes', members(name) | adding)
        if racks:
            affected_parents.add(cluster)
        store(inactive, 'Nodes', members(inactive) - nodes, keep_empty=True)
    else:
        for name, (_, fields) in records.items():
            if 'Nodes' not in fields or name.startswith('inactive-'):
                continue
            if action != 'reclaim' and name != cluster and not name.startswith(cluster + ':'):
                continue
            existing = members(name)
            removing = existing & nodes
            if not removing:
                continue
            store(name, 'Nodes', existing - nodes)
            if ':' in name:
                affected_parents.add(name.rsplit(':', 1)[0])
        for name, returning in inactive_moves.items():
            store(name, 'Nodes', members(name) | returning, keep_empty=True)
        if cluster and cluster in records and 'Switches' in records[cluster][1]:
            affected_parents.add(cluster)

    for parent in affected_parents:
        children = {name for name, (_, fields) in records.items()
                    if name.startswith(parent + ':') and 'Nodes' in fields
                    and replacements.get(name, '') is not None}
        children.update(name for name, line in replacements.items()
                        if name.startswith(parent + ':') and line is not None)
        store(parent, 'Switches', children)

    for name, replacement in replacements.items():
        if name in records:
            lines[records[name][0]] = replacement or ''
    additions = [replacement for name, replacement in replacements.items()
                 if name not in records and replacement]
    output = ''.join(lines)
    if additions and output and not output.endswith('\n'):
        output += '\n'
    return output + ''.join(additions)


def scontrol_hostlist(subcommand, value):
    # Unset the inherited cluster selector. An empty value is still a selector
    # and makes real Slurm look up a cluster named ''. Keep SLURM_CONF intact.
    environment = dict(os.environ)
    environment.pop('SLURM_CLUSTERS', None)
    result = subprocess.run(
        ['scontrol', 'show', subcommand, value],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, env=environment,
    )
    if result.returncode != 0:
        raise ValueError('scontrol {} failed: {}'.format(
            subcommand, result.stderr or result.stdout))
    return result.stdout.strip()


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(argument_spec=dict(
        path=dict(type='path', required=True),
        nodes=dict(type='list', elements='str', required=True),
        action=dict(type='str', choices=['add', 'remove', 'reclaim'], required=True),
        cluster=dict(type='str'), inactive=dict(type='str'),
        racks=dict(type='dict', default={}),
    ), supports_check_mode=True)
    params = module.params
    path = params['path']
    temporary = None
    try:
        # A stable lock inode is needed because atomic_move replaces topology.conf.
        with open(path + '.oci-hpc.lock', 'a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with open(path, encoding='utf-8') as stream:
                source = stream.read()

            result = plan_update(source, params['nodes'], params['action'],
                                 params['cluster'], params['inactive'], params['racks'],
                                 lambda value: scontrol_hostlist('hostname', value).splitlines(),
                                 lambda values: scontrol_hostlist('hostlistsorted', ','.join(values)))
            changed = result != source
            if changed and not module.check_mode:
                fd, temporary = tempfile.mkstemp(prefix='.topology-', dir=os.path.dirname(path))
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(result)
                    stream.flush()
                    os.fsync(stream.fileno())
                module.atomic_move(temporary, path)
                temporary = None
        module.exit_json(changed=changed)
    except (OSError, ValueError) as error:
        module.fail_json(msg=str(error), changed=False)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    main()
