#!/bin/python3
import subprocess
import datetime
import time
import sys, os
import traceback
import json
import copy
import yaml
import re
import uuid
import shutil

lockfile = "/tmp/autoscaling_lock"
queues_conf_file = "/opt/oci-hpc/conf/queues.conf"
idle_time = 600
script_path = '/opt/oci-hpc/bin'
slurm_command_timeout = 20
delete_acceptance_timeout = 10

class SafetyCheckError(RuntimeError):
    """A deletion precondition could not be proved."""

    def __init__(self, message, outcome_unknown=False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


def runSlurm(args):
    # Do not let inherited output filters hide jobs or make timestamps relative.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('SQUEUE_', 'SINFO_'))}
    env['SLURM_TIME_FORMAT'] = '%Y-%m-%dT%H:%M:%S'
    env['LC_ALL'] = 'C'
    command = args
    if args[:2] == ['scontrol', 'update'] and os.geteuid() != 0:
        executable = shutil.which('scontrol')
        if executable is None:
            raise SafetyCheckError('scontrol executable was not found')
        command = ['sudo', '-n', executable] + args[1:]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True,
                                timeout=slurm_command_timeout, env=env)
    except subprocess.TimeoutExpired as error:
        # The controller may have applied the update before the client timed out.
        raise SafetyCheckError(str(error), outcome_unknown=True)
    except OSError as error:
        raise SafetyCheckError(str(error))
    if result.returncode != 0:
        raise SafetyCheckError('{} failed ({}): {}'.format(
            args[0], result.returncode, result.stderr.strip()))
    return result.stdout


def stateTokens(snapshot):
    return {part.strip('*').upper() for part in snapshot['State'].split('+') if part}


def getNodeSnapshot(node):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', node):
        raise SafetyCheckError('Invalid node name: ' + node)
    output = runSlurm(['scontrol', '--local', '--oneliner', 'show', 'node', node])
    fields = dict(re.findall(r'(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=(\S+)', output))
    if (fields.get('NodeName') != node or not fields.get('State') or
            not fields.get('CPUAlloc', '').isdigit()):
        raise SafetyCheckError('Incomplete node information: ' + node)
    reason = re.search(r'(?:^|\s)Reason=(.*)', output)
    fields['Reason'] = re.split(r'\s+\[', reason.group(1), maxsplit=1)[0].strip() if reason else ''
    return fields


def nodeIsQuiescent(snapshot):
    # CPUAlloc is an extra guard, never the proof of idleness: squeue is checked
    # separately because SUSPENDED jobs can report zero allocated CPUs.
    states = stateTokens(snapshot)
    active = {'ALLOCATED', 'MIXED', 'COMPLETING', 'POWERING_UP',
              'POWERING_DOWN', 'REBOOT_ISSUED'}
    return int(snapshot['CPUAlloc']) == 0 and not states.intersection(active) and bool(
        states.intersection({'IDLE', 'DOWN', 'FAIL', 'FAILING', 'UNKNOWN'}))


def nodeHasFailureState(snapshot):
    return bool(stateTokens(snapshot).intersection(
        {'DOWN', 'FAIL', 'FAILING', 'INVALID_REG', 'NOT_RESPONDING', 'DRAIN'}))


def parseSlurmTime(value):
    if not value or value.upper() in ('UNKNOWN', 'NONE', 'N/A'):
        return None
    try:
        return datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None


def getIdleTime(node, snapshot=None, now=None):
    snapshot = getNodeSnapshot(node) if snapshot is None else snapshot
    if not nodeIsQuiescent(snapshot):
        return None
    # LastBusyTime is updated only after Slurm has finished the job's cleanup.
    # A newly registered node must also age from SlurmdStartTime. Either field
    # can legitimately be Unknown on a failed node, so do not invent a date.
    timestamps = [parseSlurmTime(snapshot.get(field))
                  for field in ('LastBusyTime', 'SlurmdStartTime')]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    if not timestamps:
        return None
    duration = ((now or datetime.datetime.now()) - max(timestamps)).total_seconds()
    return duration if duration >= 0 else None


def nodeMayBeDeleted(node, snapshot):
    duration = getIdleTime(node, snapshot)
    if duration is not None and duration >= idle_time:
        return True
    # A failed, unused node with missing timestamps is reclaimed only after the
    # same two squeue checks and DRAIN fence used for every deletion. This is a
    # failure-recovery path, not a fabricated "old idle" timestamp.
    return duration is None and nodeIsQuiescent(snapshot) and nodeHasFailureState(snapshot)


def ensureNoJobs(nodes):
    output = runSlurm(['squeue', '--local', '--all', '--noheader', '--array',
                      '--states=all', '--nodes=' + ','.join(nodes),
                      '--format=%i|%T|%N'])
    terminal = {'COMPLETED', 'CANCELLED', 'FAILED', 'TIMEOUT', 'NODE_FAIL',
                'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'OUT_OF_MEMORY'}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = [value.strip() for value in line.split('|')]
        if len(fields) != 3 or not fields[0] or not fields[1]:
            raise SafetyCheckError('Unrecognized squeue result: ' + line)
        if fields[1].upper() not in terminal:
            raise SafetyCheckError('Job {} is {} on {}'.format(*fields))


def waitForDeleteAcceptance(cluster_name, process):
    marker = os.path.join(clusters_path, cluster_name, 'currently_destroying')
    deadline = time.monotonic() + delete_acceptance_timeout
    while time.monotonic() < deadline:
        if os.path.isfile(marker):
            return True
        status = process.poll()
        if status is not None:
            raise SafetyCheckError('delete_cluster.sh exited {} before accepting deletion'.format(status))
        time.sleep(.1)
    # The process may own the resize lock or be between lock and marker. Keeping
    # DRAIN is safer than allowing a new allocation during an unknown handoff.
    raise SafetyCheckError('delete_cluster.sh acceptance is unknown', outcome_unknown=True)


def deleteClusterSafely(cluster_name):
    owned_drains = []
    handoff = 'not_started'
    reason = 'autoscale-delete-' + uuid.uuid4().hex
    try:
        cluster_dir = os.path.join(clusters_path, cluster_name)
        for marker in ('currently_building', 'currently_destroying'):
            if os.path.isfile(os.path.join(cluster_dir, marker)):
                raise SafetyCheckError('Cluster has ' + marker)
        nodes = getTopology(cluster_name)
        snapshots = {node: getNodeSnapshot(node) for node in nodes}
        for node in nodes:
            if not nodeMayBeDeleted(node, snapshots[node]):
                raise SafetyCheckError('Node is busy or has not been idle long enough: ' + node)
        ensureNoJobs(nodes)
        for node in nodes:
            if 'DRAIN' not in stateTokens(snapshots[node]):
                runSlurm(['scontrol', 'update', 'NodeName=' + node,
                          'State=DRAIN', 'Reason=' + reason])
                owned_drains.append((node, snapshots[node]['Reason']))

        if set(getTopology(cluster_name)) != set(nodes):
            raise SafetyCheckError('Cluster membership changed during deletion check')
        for node in nodes:
            snapshot = getNodeSnapshot(node)
            if 'DRAIN' not in stateTokens(snapshot) or not nodeMayBeDeleted(node, snapshot):
                raise SafetyCheckError('Node became busy or recently active: ' + node)
        ensureNoJobs(nodes)
        process = subprocess.Popen([script_path + '/delete_cluster.sh', cluster_name])
        handoff = 'unknown'
        waitForDeleteAcceptance(cluster_name, process)
        handoff = 'accepted'
        print('Deleting cluster ' + cluster_name + ' after safety checks', flush=True)
        return True
    except (SafetyCheckError, OSError) as error:
        if handoff == 'unknown' and not getattr(error, 'outcome_unknown', False):
            handoff = 'rejected'
        print('Skipping deletion of {}: {}'.format(cluster_name, error), flush=True)
        return False
    finally:
        if handoff in ('not_started', 'rejected'):
            for node, previous_reason in owned_drains:
                try:
                    snapshot = getNodeSnapshot(node)
                    if snapshot['Reason'] == reason and 'DRAIN' in stateTokens(snapshot):
                        runSlurm(['scontrol', 'update', 'NodeName=' + node,
                                  'State=UNDRAIN', 'Reason=' + previous_reason])
                except SafetyCheckError as error:
                    print('Could not undo autoscaling DRAIN on {}: {}'.format(node, error), flush=True)


def israckaware():
    rackware = False
    if os.path.isfile("/opt/oci-hpc/conf/variables.tf"):
        variablefile = open("/opt/oci-hpc/conf/variables.tf", 'r')
        for line in variablefile:
            if "\"rack_aware\"" in line and ("true" in line or "True" in line or "yes" in line or "Yes" in line):
                rackware = True
                break
    return rackware


def getTopology(clusterName):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', clusterName):
        raise SafetyCheckError('Invalid cluster name: ' + clusterName)
    output = runSlurm(['scontrol', '--local', 'show', 'topology', clusterName])
    matches = []
    for line in output.splitlines():
        fields = dict(item.split('=', 1) for item in line.split() if '=' in item)
        if fields.get('SwitchName') == clusterName and fields.get('Nodes'):
            matches.append(fields['Nodes'])
    if len(matches) != 1:
        raise SafetyCheckError('Cannot determine all cluster nodes: ' + clusterName)
    nodes = runSlurm(['scontrol', 'show', 'hostnames', matches[0]]).split()
    if not nodes or len(set(nodes)) != len(nodes):
        raise SafetyCheckError('Empty or duplicate cluster nodes: ' + clusterName)
    return nodes


def getJobs():
    out = subprocess.Popen(
        ['squeue', '-r', '-O', 'STATE,JOBID,FEATURE:100,NUMNODES,Partition,UserName,Dependency'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    stdout, stderr = out.communicate()
    return stdout.split("\n")[1:]


def getClusters():
    out = subprocess.Popen(['sinfo', '-hN', '-o', '\"%T %E %D %N\"'],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    stdout, stderr = out.communicate()
    return stdout.split("\n")


def getNodeDetails(node):
    out = subprocess.Popen(['sinfo', '-h', '-n', node, '-o', '"%f %R"'],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    stdout, stderr = out.communicate()
    for pot_output in stdout.split("\n"):
        if not "(null)" in pot_output and pot_output.strip() != '':
            output = pot_output
            if output[0] == '"':
                output = output[1:]
            if output[-1] == '"':
                output = output[:-1]
        else:
            continue
    return output


def getQueueConf(queue_file):
    with open(queue_file) as file:
        try:
            data = yaml.load(file, Loader=yaml.FullLoader)
        except:
            data = yaml.load(file)
        return data["queues"]


def getQueue(config, queue_name):
    for queue in config:
        if queue["name"] == queue_name:
            return queue
    return None


def getDefaultsConfig(config, queue_name):
    for partition in config:
        if queue_name == partition["name"]:
            for instance_type in partition["instance_types"]:
                if "default" in instance_type.keys():
                    if instance_type["default"]:
                        return {"queue": partition["name"], "instance_type": instance_type["name"],
                                "shape": instance_type["shape"], "cluster_network": instance_type["cluster_network"],
                                "instance_keyword": instance_type["instance_keyword"]}
            if len(partition["instance_types"]) > 0:
                instance_type = partition["instance_types"][0]
                return {"queue": partition["name"], "instance_type": instance_type["name"],
                        "shape": instance_type["shape"], "cluster_network": instance_type["cluster_network"],
                        "instance_keyword": instance_type["instance_keyword"]}
    return None


def getJobConfig(config, queue_name, instance_type_name):
    for partition in config:
        if queue_name == partition["name"]:
            for instance_type in partition["instance_types"]:
                if instance_type_name == instance_type["name"]:
                    return {"queue": partition["name"], "instance_type": instance_type["name"],
                            "shape": instance_type["shape"], "cluster_network": instance_type["cluster_network"],
                            "instance_keyword": instance_type["instance_keyword"]}
    return None


def getQueueLimits(config, queue_name, instance_type_name):
    for partition in config:
        if queue_name == partition["name"]:
            for instance_type in partition["instance_types"]:
                if instance_type_name == instance_type["name"]:
                    return {"max_number_nodes": int(instance_type["max_number_nodes"]),
                            "max_cluster_size": int(instance_type["max_cluster_size"]),
                            "max_cluster_count": int(instance_type["max_cluster_count"])}
    return {"max_number_nodes": 0, "max_cluster_size": 0, "max_cluster_count": 0}


def getInstanceType(config, queue_name, instance_keyword):
    for partition in config:
        if queue_name == partition["name"]:
            for instance_type in partition["instance_types"]:
                if instance_keyword == instance_type["instance_keyword"]:
                    return instance_type["name"]
    return None


def parseClusterName(config, cluster_name):
    # Cluster directories use <queue>-<index>-<instance_keyword>. Resolve the
    # queue from queues.conf so queue names containing '-' remain unambiguous.
    for partition in sorted(config, key=lambda item: len(item["name"]), reverse=True):
        queue_name = partition["name"]
        prefix = queue_name + '-'
        if not cluster_name.startswith(prefix):
            continue
        remainder = cluster_name[len(prefix):]
        cluster_number, separator, instance_keyword = remainder.partition('-')
        if not separator or not cluster_number.isdigit():
            continue
        if any(instance_keyword == item["instance_keyword"] for item in partition["instance_types"]):
            return queue_name, int(cluster_number), instance_keyword
    return None


def isPermanent(config, queue_name, instance_type_name):
    for partition in config:
        if queue_name == partition["name"]:
            for instance_type in partition["instance_types"]:
                if instance_type_name == instance_type["name"]:
                    return instance_type["permanent"]
    return None


def getClusterName(node):
    out = subprocess.Popen(['scontrol', 'show', 'topology', node],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    stdout, stderr = out.communicate()
    clusterName = None
    try:
        if len(stdout.split('\n')) > 2:
            for output in stdout.split('\n')[:-1]:
                if "Switches=" in output:
                    clusterName = output.split()[0].split('SwitchName=')[1]
                    break
                elif "SwitchName=inactive-" in output:
                    continue
                else:
                    clusterName = output.split()[0].split('SwitchName=')[1]
        elif len(stdout.split('\n')) == 2:
            clusterName = stdout.split('\n')[0].split()[0].split('SwitchName=')[1]
        if clusterName.startswith("inactive-"):
            return "NOCLUSTERFOUND"
    except:
        return "NOCLUSTERFOUND"
    return clusterName


def getstatus_slurm():
    cluster_to_build = []
    clusters_data = {}
    current_nodes = {}
    building_nodes = {}
    cluster_building = []
    cluster_destroying = []
    used_index = {}

    for line in getJobs():
        if len(line.split()) > 3:
            new_line = re.split(r"\s{1,}", line)
            if new_line[0] == 'PENDING' and ('null' in new_line[6] or len(new_line[6]) == 0):
                queue = new_line[4]
                user = new_line[5]
                features = new_line[2].split('&')
                instanceType = None
                possible_types = [inst_type["name"]
                                  for inst_type in getQueue(config, queue)["instance_types"]]
                default_config = getDefaultsConfig(config, queue)
                if instanceType is None:
                    instanceType = default_config["instance_type"]
                    for feature in features:
                        if feature in possible_types:
                            instanceType = feature
                            break
                nodes = int(new_line[3])
                jobID = int(new_line[1])
                cluster_to_build.append([nodes, instanceType, queue, jobID, user])

    for line in getClusters():
        if len(line.split()) == 0:
            break
        old_nodes = line.split()[-1].split(',')
        broken = False
        nodes = []
        for node in old_nodes:
            if broken:
                if ']' in node:
                    broken = False
                    nodes.append(currentNode + ',' + node)
                else:
                    currentNode = currentNode + ',' + node
            elif '[' in node and not ']' in node:
                broken = True
                currentNode = node
            else:
                nodes.append(node)
        for node in nodes:
            if node.endswith('"'):
                node = node[:-1]
            if node.startswith('"'):
                node = node[1:]
            details = getNodeDetails(node).split(' ')
            features = details[0].split(',')
            queue = details[-1]
            clustername = getClusterName(node)
            if clustername is None:
                continue
            instanceType = features[-1]
            if queue in current_nodes:
                current_nodes[queue][instanceType] = current_nodes[queue].get(
                    instanceType, 0) + 1
            else:
                current_nodes[queue] = {instanceType: 1}
            if clustername not in clusters_data:
                clusters_data[clustername] = {"nodes": [], "min_idle": None,
                                              "running": False, "queue": queue,
                                              "instance_type": instanceType}
            clusters_data[clustername]["nodes"].append(node)
            try:
                snapshot = getNodeSnapshot(node)
                node_idle = getIdleTime(node, snapshot)
                # A failed, unused node whose Slurm timestamps are Unknown is
                # eligible for the fenced recovery path; it is not assigned an
                # invented idle age.
                if node_idle is None:
                    if nodeIsQuiescent(snapshot) and nodeHasFailureState(snapshot):
                        node_idle = idle_time
                    else:
                        clusters_data[clustername]["running"] = True
                        continue
                if clusters_data[clustername]["min_idle"] is None or node_idle < clusters_data[clustername]["min_idle"]:
                    clusters_data[clustername]["min_idle"] = node_idle
            except SafetyCheckError as error:
                print('Deletion check unavailable for {}: {}'.format(node, error), flush=True)
                clusters_data[clustername]["running"] = True

    for clusterName in os.listdir(clusters_path):
        cluster_details = parseClusterName(config, clusterName)
        if cluster_details is None:
            continue
        queue, clusterNumber, instance_keyword = cluster_details
        instanceType = getInstanceType(config, queue, instance_keyword)
        if queue not in used_index:
            used_index[queue] = {}
        if instanceType not in used_index[queue]:
            used_index[queue][instanceType] = []
        used_index[queue][instanceType].append(clusterNumber)
        if os.path.isfile(os.path.join(clusters_path, clusterName, 'currently_building')):
            with open(os.path.join(clusters_path, clusterName, 'currently_building'), 'r') as f:
                parts = f.read().strip().split()
                if len(parts) >= 3:
                    try:
                        nodes = int(parts[0])
                        instance_type = parts[1]
                        queue_name = parts[2]
                        cluster_building.append([nodes, instance_type, queue_name])
                        if queue_name in building_nodes:
                            building_nodes[queue_name][instance_type] = building_nodes[queue_name].get(
                                instance_type, 0) + nodes
                        else:
                            building_nodes[queue_name] = {instance_type: nodes}
                    except:
                        pass
        if os.path.isfile(os.path.join(clusters_path, clusterName, 'currently_destroying')):
            cluster_destroying.append(clusterName)

    cluster_to_destroy = []
    for clustername, info in clusters_data.items():
        if clustername == "NOCLUSTERFOUND":
            continue
        if info["running"]:
            continue
        if isPermanent(config, info["queue"], info["instance_type"]):
            continue
        if info["min_idle"] is None:
            continue
        if info["min_idle"] >= idle_time:
            cluster_to_destroy.append([clustername])
        else:
            print(f"{clustername}[{','.join(info['nodes'])}] is too young to die : {int(info['min_idle'])} sec")

    nodes_to_destroy = {}
    return cluster_to_build, cluster_to_destroy, nodes_to_destroy, cluster_building, cluster_destroying, used_index, current_nodes, building_nodes


def getAutoscaling():
    out = subprocess.Popen(
        ["cat /etc/ansible/hosts | grep 'autoscaling =' | awk -F  '= ' '{print $2}'"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True, universal_newlines=True)
    stdout, stderr = out.communicate()
    output = stdout.split("\n")
    autoscaling_value = False
    for i in range(0, len(output) - 1):
        autoscaling_value = output[i]
    return autoscaling_value



autoscaling = getAutoscaling()

if autoscaling == "true":
    if os.path.isfile(lockfile):
        print("Lockfile " + lockfile + " is present, exiting")
        exit()
    open(lockfile, 'w').close()
    try:
        path = os.path.dirname(os.path.dirname(os.path.realpath(sys.argv[0])))
        clusters_path = os.path.join(path, 'clusters')
        config = getQueueConf(queues_conf_file)
        cluster_to_build, cluster_to_destroy, nodes_to_destroy, cluster_building, cluster_destroying, used_index, current_nodes, building_nodes = getstatus_slurm()
        print(time.strftime("%Y-%m-%d %H:%M:%S"))
        print(cluster_to_build, 'cluster_to_build')
        print(cluster_to_destroy, 'cluster_to_destroy')
        print(nodes_to_destroy, 'nodes_to_destroy')
        print(cluster_building, 'cluster_building')
        print(cluster_destroying, 'cluster_destroying')
        print(current_nodes, 'current_nodes')
        print(building_nodes, 'building_nodes')
        for i in cluster_building:
            for j in cluster_to_build:
                if i[0] == j[0] and i[1] == j[1] and i[2] == j[2]:
                    cluster_to_build.remove(j)
                    break
        for cluster in cluster_to_destroy:
            cluster_name = cluster[0]
            if deleteClusterSafely(cluster_name):
                time.sleep(5)
        for index, cluster in enumerate(cluster_to_build):
            nodes = cluster[0]
            instance_type = cluster[1]
            queue = cluster[2]
            jobID = str(cluster[3])
            user = str(cluster[4])
            jobconfig = getJobConfig(config, queue, instance_type)
            limits = getQueueLimits(config, queue, instance_type)
            try:
                clusterCount = len(used_index[queue][instance_type])
            except:
                clusterCount = 0
            if clusterCount >= limits["max_cluster_count"]:
                print("This would go over the number of running clusters, you have reached the max number of clusters")
                continue
            nextIndex = None
            if clusterCount == 0:
                if queue in used_index:
                    used_index[queue][instance_type] = [1]
                else:
                    used_index[queue] = {instance_type: [1]}
                nextIndex = 1
            else:
                for i in range(1, 10000):
                    if i not in used_index[queue][instance_type]:
                        nextIndex = i
                        used_index[queue][instance_type].append(i)
                        break
            clusterName = queue + '-' + str(nextIndex) + '-' + \
                jobconfig["instance_keyword"]
            if queue not in current_nodes:
                current_nodes[queue] = {instance_type: 0}
            else:
                if instance_type not in current_nodes[queue]:
                    current_nodes[queue][instance_type] = 0
            if queue not in building_nodes:
                building_nodes[queue] = {instance_type: 0}
            else:
                if instance_type not in building_nodes[queue]:
                    building_nodes[queue][instance_type] = 0
            if nodes > limits["max_cluster_size"]:
                print("Cluster " + clusterName +
                      " won't be created, it would go over the total number of nodes per cluster limit")
            elif current_nodes[queue][instance_type] + building_nodes[queue][instance_type] + nodes > limits["max_number_nodes"]:
                print("Cluster " + clusterName +
                      " won't be created, it would go over the total number of nodes limit")
            else:
                current_nodes[queue][instance_type] += nodes
                clusterCount += 1
                print("Creating cluster " + clusterName +
                      " with " + str(nodes) + " nodes")
                subprocess.Popen([script_path + '/create_cluster.sh', str(nodes),
                                 clusterName, instance_type, queue, jobID, ""])
                time.sleep(5)
    except Exception:
        traceback.print_exc()
    os.remove(lockfile)
else:
    print("Autoscaling is false")
    exit()
