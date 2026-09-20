import sys
import oci
import subprocess
import json
import time
import requests
import argparse
import shutil
import os
import copy
import ipaddress
import re
import stat
import tempfile
import uuid
import fcntl
from datetime import datetime

LOCAL_BLOCK_VOLUME_DEVICE = "/dev/oracleoci/oraclevdc"
LOCAL_BLOCK_VOLUME_TAG_ENABLED = "oci_hpc_local_block_volume"
LOCAL_BLOCK_VOLUME_TAG_SIZE = "oci_hpc_local_block_volume_size"
LOCAL_BLOCK_VOLUME_TAG_VPUS = "oci_hpc_local_block_volume_vpus"
LOCAL_BLOCK_VOLUME_TAG_MOUNT = "oci_hpc_local_block_volume_mount"
PENDING_LOCAL_BLOCK_VOLUME_DELETIONS_FILENAME = ".pending-local-block-volume-deletions.json"
INSTANCE_POOL_HOSTNAME_SYNC_PLAN_FILENAME = ".instance-pool-hostname-sync.json"
PENDING_INSTANCE_POOL_NODE_REMOVALS_FILENAME = ".pending-instance-pool-node-removals.json"
PENDING_COMPUTE_CLUSTER_NODE_REMOVALS_FILENAME = ".pending-compute-cluster-node-removals.json"
INSTANCE_POOL_OCI_DNS_RESOURCE_ADDRESS = "oci_dns_rrset.rrset-cluster-network-OCI"
INSTANCE_POOL_DNS_OWNERSHIP_MARKER_FILENAME = ".instance-pool-python-dns-v1"
MANAGED_POOL_DNS_OWNERSHIP_MARKER_FILENAME = ".managed-pool-python-dns-v2"
INSTANCE_POOL_NAME_DNS_OWNERSHIP_FILENAME = ".instance-pool-name-dns.json"
INSTANCE_POOL_POST_RESIZE_RECOVERY_FILENAME = ".instance-pool-post-resize-recovery.json"
RESIZE_LOCK_FILENAME = ".oci-hpc-resize.lock"
DESTROY_MARKER_FILENAME = "currently_destroying"
ALLOW_DURING_DESTROY_ENVIRONMENT_VARIABLE = "OCI_HPC_ALLOW_DURING_DESTROY"
LOCAL_BLOCK_VOLUME_TAG_KEYS = {
    LOCAL_BLOCK_VOLUME_TAG_ENABLED,
    LOCAL_BLOCK_VOLUME_TAG_SIZE,
    LOCAL_BLOCK_VOLUME_TAG_VPUS,
    LOCAL_BLOCK_VOLUME_TAG_MOUNT,
}

def acquire_resize_lock(inventory_path):
    lock_path = os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        RESIZE_LOCK_FILENAME,
    )
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    lock_file = os.fdopen(lock_fd, "a+")
    try:
        os.chmod(lock_path, 0o600)
        if os.path.exists(inventory_path):
            inventory_stat = os.stat(inventory_path)
            try:
                os.fchown(lock_file.fileno(), inventory_stat.st_uid, inventory_stat.st_gid)
            except PermissionError:
                if inventory_stat.st_uid != os.getuid() or inventory_stat.st_gid != os.getgid():
                    raise
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another resize operation is already using inventory "+inventory_path)
        return lock_file
    except Exception:
        lock_file.close()
        raise

def ensure_cluster_is_not_being_destroyed(inventory_path):
    destroy_marker_path = os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        DESTROY_MARKER_FILENAME,
    )
    if (
        os.path.isfile(destroy_marker_path)
        and os.environ.get(ALLOW_DURING_DESTROY_ENVIRONMENT_VARIABLE) != "1"
    ):
        raise RuntimeError("Cluster deletion is already in progress for inventory "+inventory_path)

def parse_bool(value):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ["true", "yes", "1", "on"]:
        return True
    if normalized in ["false", "no", "0", "off", ""]:
        return False
    raise ValueError("Invalid boolean value: "+str(value))

def get_inventory_variable(inventory_dict, name, default=None):
    for inv_var in inventory_dict.get("all:vars", []):
        key, separator, value = inv_var.partition("=")
        if separator and key.strip() == name:
            return value.strip()
    return default

def validate_local_block_volume_mount_point(mount_point, inventory_dict):
    normalized_mount_point = mount_point.rstrip("/")
    path_components = normalized_mount_point.split(os.sep)[1:]
    if (
        not re.fullmatch(r"/[A-Za-z0-9._/-]+", mount_point)
        or len(mount_point) > 200
        or normalized_mount_point == ""
        or normalized_mount_point == "/"
        or any(component in ["", ".", ".."] for component in path_components)
        or os.path.normpath(normalized_mount_point) != normalized_mount_point
    ):
        raise ValueError("local_block_volume_mount_point must be a canonical absolute path other than /")
    reserved_mount_points = [
        (get_inventory_variable(inventory_dict, "nvme_path", "/mnt/localdisk") or "/mnt/localdisk").rstrip("/"),
        (get_inventory_variable(inventory_dict, "scratch_nfs_path", "/nfs/scratch") or "/nfs/scratch").rstrip("/"),
        (get_inventory_variable(inventory_dict, "cluster_nfs_path", "/nfs/cluster") or "/nfs/cluster").rstrip("/"),
        "/mnt/localdisk/nfs",
        "/home",
        (get_inventory_variable(inventory_dict, "nfs_target_path", "/share") or "/share").rstrip("/"),
    ]
    for reserved_mount_point in reserved_mount_points:
        if (
            normalized_mount_point == reserved_mount_point
            or normalized_mount_point.startswith(reserved_mount_point+"/")
            or reserved_mount_point.startswith(normalized_mount_point+"/")
        ):
            raise ValueError("local_block_volume_mount_point conflicts with reserved path "+reserved_mount_point)
    return normalized_mount_point

def get_local_block_volume_config(inventory_dict):
    enabled = parse_bool(get_inventory_variable(inventory_dict, "use_local_block_volume", "false"))
    size_in_gbs = int(get_inventory_variable(inventory_dict, "local_block_volume_size", "1000"))
    performance = get_inventory_variable(inventory_dict, "local_block_volume_performance", "10. Balanced performance")
    mount_point = get_inventory_variable(inventory_dict, "local_block_volume_mount_point", "/scratch")
    performance_values = {
        "0.  Lower performance": 0,
        "10. Balanced performance": 10,
        "20. High Performance": 20,
    }
    if performance not in performance_values:
        raise ValueError("Invalid local_block_volume_performance: "+str(performance))
    vpus_per_gb = performance_values[performance]
    if size_in_gbs < 50:
        raise ValueError("local_block_volume_size must be at least 50 GB")
    validate_local_block_volume_mount_point(mount_point, inventory_dict)
    return {
        "enabled": enabled,
        "size_in_gbs": size_in_gbs,
        "vpus_per_gb": vpus_per_gb,
        "mount_point": mount_point,
    }

def get_metadata():
    """ Make a request to metadata endpoint """
    headers = { 'Authorization' : 'Bearer Oracle' }
    metadata_url = "http://169.254.169.254/opc/"
    metadata_ver = "2"
    request_url = metadata_url + "v" + metadata_ver + "/instance/"
    return requests.get(request_url, headers=headers).json()

def wait_for_running_status(cluster_name,comp_ocid,cn_ocid,CN,expected_size=None,max_wait_seconds=3600):
    deadline = time.time()+max_wait_seconds
    while True:
        if CN == "CC": 
            break
        elif CN == "CN":
            state = computeManagementClient.get_cluster_network(cn_ocid).data.lifecycle_state
            instances=oci.pagination.list_call_get_all_results(
                computeManagementClient.list_cluster_network_instances,
                comp_ocid,
                cn_ocid,
            ).data
        else:
            state = computeManagementClient.get_instance_pool(cn_ocid).data.lifecycle_state
            instances=oci.pagination.list_call_get_all_results(
                computeManagementClient.list_instance_pool_instances,
                comp_ocid,
                cn_ocid,
            ).data
        if state != 'RUNNING':
            print("Cluster state is "+state+", cannot add or remove nodes")
            print ("Waiting...")
            time.sleep(30)
        elif not expected_size is None:
            if expected_size == len(instances):
                break
            else:
                print("The instance list does not match the expected size")
                time.sleep(30)
        else:
            break
        if time.time() >= deadline:
            raise RuntimeError("Timed out waiting for cluster "+cluster_name+" to reach the expected running state")
    return True

def get_instances(comp_ocid,cn_ocid,CN):
    cn_instances=[]
    if CN == "CC":
        instances = oci.pagination.list_call_get_all_results(
            computeClient.list_instances,
            compartment_id=comp_ocid,
            compute_cluster_id=cn_ocid,
        ).data
        for instance in instances:
            if instance.lifecycle_state == "TERMINATED":
                continue
            private_ip = get_instance_primary_private_ip(
                comp_ocid,
                instance.id,
                require_explicit_primary=True,
            )
            cn_instances.append({
                'display_name': instance.display_name,
                'ip': private_ip,
                'ocid': instance.id,
            })
    else:
        if CN == "CN":
            instance_summaries = oci.pagination.list_call_get_all_results(computeManagementClient.list_cluster_network_instances,comp_ocid,cn_ocid).data
        else:
            instance_summaries = oci.pagination.list_call_get_all_results(computeManagementClient.list_instance_pool_instances,comp_ocid,cn_ocid).data
        for instance_summary in instance_summaries:
            if CN in ["IP", "CN"]:
                # A VNIC attachment display name is optional metadata and does
                # not identify the primary VNIC.  Resolve the full VNIC and use
                # its is_primary flag so named primary attachments are not
                # silently dropped from managed-pool membership.
                instance = computeClient.get_instance(instance_summary.id).data
                private_ip = get_instance_primary_private_ip(
                    comp_ocid,
                    instance.id,
                )
                cn_instances.append({
                    'display_name': instance.display_name,
                    'ip': private_ip,
                    'ocid': instance_summary.id,
                })
                continue
    return cn_instances

def get_active_instance_identities(compartment_id, cluster_id, cluster_type, expected_size):
    active_instances = get_instances(compartment_id, cluster_id, cluster_type)
    if len(active_instances) != expected_size:
        raise RuntimeError(
            "The active instance list does not match the current instance pool size"
        )
    display_names = {instance["display_name"] for instance in active_instances}
    private_ips = {str(ipaddress.ip_address(instance["ip"])) for instance in active_instances}
    if len(display_names) != len(active_instances) or len(private_ips) != len(active_instances):
        raise RuntimeError("The active instance list contains duplicate hostnames or private IPs")
    return display_names, private_ips

def get_instance_local_block_volume_config(instance, inventory_dict, expected_cluster_name):
    tags = instance.freeform_tags or {}
    present_tag_keys = LOCAL_BLOCK_VOLUME_TAG_KEYS.intersection(tags.keys())
    if not present_tag_keys:
        return {"enabled": False}
    if LOCAL_BLOCK_VOLUME_TAG_ENABLED not in tags:
        raise RuntimeError("Instance "+instance.id+" has incomplete local Block Volume tags")
    enabled_value = str(tags[LOCAL_BLOCK_VOLUME_TAG_ENABLED]).strip().lower()
    if enabled_value not in ["true", "false"]:
        raise RuntimeError("Instance "+instance.id+" has an invalid "+LOCAL_BLOCK_VOLUME_TAG_ENABLED+" tag")
    if enabled_value == "false":
        return {"enabled": False}
    missing_tag_keys = LOCAL_BLOCK_VOLUME_TAG_KEYS.difference(tags.keys())
    if missing_tag_keys:
        raise RuntimeError(
            "Instance "+instance.id+" is missing local Block Volume tags: "+", ".join(sorted(missing_tag_keys))
        )
    if (tags.get("parent_cluster") or tags.get("cluster_name")) != expected_cluster_name:
        raise RuntimeError("Instance "+instance.id+" does not belong to cluster "+expected_cluster_name)
    size_value = str(tags[LOCAL_BLOCK_VOLUME_TAG_SIZE]).strip()
    vpus_value = str(tags[LOCAL_BLOCK_VOLUME_TAG_VPUS]).strip()
    mount_point = str(tags[LOCAL_BLOCK_VOLUME_TAG_MOUNT]).strip()
    if not re.fullmatch(r"[1-9][0-9]*", size_value) or int(size_value) < 50:
        raise RuntimeError("Instance "+instance.id+" has an invalid local Block Volume size tag")
    if vpus_value not in ["0", "10", "20"]:
        raise RuntimeError("Instance "+instance.id+" has an invalid local Block Volume VPUs tag")
    try:
        validate_local_block_volume_mount_point(mount_point, inventory_dict)
    except ValueError as error:
        raise RuntimeError("Instance "+instance.id+" has an invalid local Block Volume mount tag: "+str(error))
    return {
        "enabled": True,
        "size_in_gbs": int(size_value),
        "vpus_per_gb": int(vpus_value),
        "mount_point": mount_point,
    }

def get_local_block_volume_attachment(
    compartment_id,
    instance_id,
    max_wait_seconds=300,
    expected_config=None,
    expected_cluster_name=None,
    instance=None,
):
    if instance is None:
        instance = computeClient.get_instance(instance_id).data
    if expected_cluster_name is None:
        expected_cluster_name = cluster_name
    if expected_config is None:
        expected_config = get_instance_local_block_volume_config(instance, inventory_dict, expected_cluster_name)
    if not expected_config.get("enabled", False):
        raise RuntimeError("Instance "+instance_id+" does not expect a local Block Volume")
    deadline = time.time()+max_wait_seconds
    while True:
        attachments = oci.pagination.list_call_get_all_results(
            computeClient.list_volume_attachments,
            compartment_id=compartment_id,
            instance_id=instance_id,
        ).data
        matches = [
            attachment for attachment in attachments
            if attachment.device == LOCAL_BLOCK_VOLUME_DEVICE
            and attachment.lifecycle_state != "DETACHED"
        ]
        if len(matches) > 1:
            raise RuntimeError("Multiple local Block Volume attachments were found for instance "+instance_id)
        if len(matches) == 1:
            attachment = matches[0]
            if (attachment.attachment_type or "").lower() != "iscsi":
                raise RuntimeError("The vdc attachment is not iSCSI for instance "+instance_id)
            if getattr(attachment, "instance_id", instance_id) != instance_id:
                raise RuntimeError("The local Block Volume attachment belongs to another instance")
            cluster_attachment_name = expected_cluster_name+"-local-scratch-attachment"
            instance_attachment_name = instance.display_name+"-local-scratch-attachment"
            if attachment.display_name == cluster_attachment_name:
                attachment_scope = "cluster"
                expected_volume_name = expected_cluster_name+"-local-scratch"
            elif attachment.display_name == instance_attachment_name:
                attachment_scope = "instance"
                expected_volume_name = instance.display_name+"-local-scratch"
            elif (
                isinstance(attachment.display_name, str)
                and attachment.display_name.endswith("-local-scratch-attachment")
            ):
                # Compute Cluster instance and VNIC display names are synchronized
                # after launch, but launch-created volume attachment names are not
                # updateable as part of that operation.  Authenticate the exact
                # attachment by instance/device/state below and retain its original
                # launch-time base name for the corresponding volume check.
                attachment_scope = "instance"
                expected_volume_name = attachment.display_name[:-len("-attachment")]
            else:
                raise RuntimeError("The vdc attachment has an unexpected display name for instance "+instance_id)
            if attachment.lifecycle_state == "ATTACHED":
                # Instance Configuration block_volumes are created and owned by the
                # Instance Pool/Cluster Network, but OCI does not mark those
                # attachments as simplified-launch volumes.  The cluster-scoped
                # path is therefore authenticated by its strict volume ownership
                # tags below.  Direct LaunchInstance volumes must retain the launch
                # marker because their volume tags are optional in older clusters.
                if (
                    attachment_scope == "instance"
                    and getattr(attachment, "is_volume_created_during_launch", None) is not True
                ):
                    raise RuntimeError("The local scratch volume was not created with instance "+instance_id)
                try:
                    volume = blockstorageClient.get_volume(attachment.volume_id).data
                except oci.exceptions.ServiceError as error:
                    if error.status != 404:
                        raise
                    volume = None
                if volume is not None:
                    volume_tags = volume.freeform_tags or {}
                    volume_parent_cluster = volume_tags.get("parent_cluster", volume_tags.get("cluster_name"))
                    if volume.display_name != expected_volume_name:
                        raise RuntimeError("The local scratch volume has an unexpected display name for instance "+instance_id)
                    if attachment_scope == "cluster":
                        if volume_parent_cluster != expected_cluster_name or volume_tags.get("oci_hpc_local_scratch") != "true":
                            raise RuntimeError("The cluster-managed local scratch volume has invalid ownership tags")
                    else:
                        if volume_parent_cluster is not None and volume_parent_cluster != expected_cluster_name:
                            raise RuntimeError("The local scratch volume parent tag does not match cluster "+expected_cluster_name)
                        if "oci_hpc_local_scratch" in volume_tags and volume_tags["oci_hpc_local_scratch"] != "true":
                            raise RuntimeError("The local scratch volume purpose tag is invalid")
                    if (
                        volume.size_in_gbs != expected_config["size_in_gbs"]
                        or volume.vpus_per_gb != expected_config["vpus_per_gb"]
                    ):
                        raise RuntimeError("The local scratch volume does not match its instance tags for instance "+instance_id)
                    if (
                        not attachment.ipv4
                        or not attachment.port
                        or int(attachment.port) <= 0
                        or not attachment.iqn
                    ):
                        volume = None
                    if volume is not None:
                        return attachment
        if time.time() >= deadline:
            raise RuntimeError("The local Block Volume attachment did not become ready for instance "+instance_id)
        time.sleep(5)

def compute_inventory_line(node, username):
    return (
        node['display_name']+" ansible_host="+node['ip']+" ansible_user="+username+
        " role=compute oci_instance_id="+node['ocid']+"\n"
    )

def parse_inventory(inventory):
    try:
        inv = open(inventory,"r")
    except:
        return None
    inventory_dict = {}
    current_section = None
    for line in inv:
        if line.strip().startswith("[") and line.strip().endswith("]"):
            current_section=line.split('[')[1].split(']')[0]
            if not current_section in inventory_dict.keys():
                inventory_dict[current_section]=[]
        else:
            if not current_section is None:
                inventory_dict[current_section].append(line)
    inv.close()
    return inventory_dict

def write_inventory(dict,inventory):
    inv = open(inventory,"w")
    for section in dict.keys():
        inv.write("["+section+"]\n")
        for line in dict[section]:
            inv.write(line)
    inv.close()

def write_inventory_atomic(inventory_dict, inventory_path):
    inventory_directory = os.path.dirname(os.path.abspath(inventory_path))
    inventory_stat = os.stat(inventory_path)
    temp_fd, temp_path = tempfile.mkstemp(prefix=".oci-hpc-inventory-", dir=inventory_directory, text=True)
    try:
        with os.fdopen(temp_fd, "w") as temp_inventory:
            for section in inventory_dict.keys():
                temp_inventory.write("["+section+"]\n")
                for line in inventory_dict[section]:
                    temp_inventory.write(line)
            temp_inventory.flush()
            os.fsync(temp_inventory.fileno())
        os.chmod(temp_path, stat.S_IMODE(inventory_stat.st_mode))
        try:
            os.chown(temp_path, inventory_stat.st_uid, inventory_stat.st_gid)
        except PermissionError:
            if inventory_stat.st_uid != os.getuid() or inventory_stat.st_gid != os.getgid():
                raise
        os.replace(temp_path, inventory_path)
        temp_path = None
        fsync_directory(inventory_directory)
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            os.unlink(temp_path)

def split_inventory_host_line(line):
    line_without_newline = line.rstrip("\r\n")
    leading_whitespace = line_without_newline[:len(line_without_newline)-len(line_without_newline.lstrip())]
    stripped_line = line_without_newline.strip()
    if not stripped_line or stripped_line.startswith("#") or stripped_line.startswith(";"):
        return None
    comment = ""
    comment_match = re.search(r"\s[#;]", stripped_line)
    if comment_match:
        comment = stripped_line[comment_match.start()+1:]
        stripped_line = stripped_line[:comment_match.start()].rstrip()
    return leading_whitespace, stripped_line.split(), comment

def rewrite_compute_inventory_line(line, local_block_volume_values):
    parsed_line = split_inventory_host_line(line)
    if parsed_line is None:
        return line
    leading_whitespace, tokens, comment = parsed_line
    rewritten_tokens = []
    for token in tokens:
        key = token.split("=", 1)[0] if "=" in token else None
        if key == "use_local_block_volume" or (key is not None and key.startswith("local_block_volume_")):
            continue
        rewritten_tokens.append(token)
    for key, value in local_block_volume_values.items():
        rewritten_tokens.append(key+"="+str(value))
    rewritten_line = leading_whitespace+" ".join(rewritten_tokens)
    if comment:
        rewritten_line += " "+comment
    return rewritten_line+"\n"

def get_inventory_instance_id(line):
    parsed_line = split_inventory_host_line(line)
    if parsed_line is None:
        return None, None
    _, tokens, _ = parsed_line
    instance_id_values = [
        token.split("=", 1)[1]
        for token in tokens
        if token.startswith("oci_instance_id=")
    ]
    if len(instance_id_values) != 1 or not instance_id_values[0]:
        host_name = tokens[0] if tokens else "<unknown>"
        raise RuntimeError("Inventory host "+host_name+" must have exactly one oci_instance_id")
    return tokens[0], instance_id_values[0]

def get_inventory_token(line, key):
    parsed_line = split_inventory_host_line(line)
    if parsed_line is None:
        return None
    _, tokens, _ = parsed_line
    values = [
        token.split("=", 1)[1]
        for token in tokens
        if token.startswith(key+"=")
    ]
    if len(values) > 1:
        raise RuntimeError("Inventory host line contains duplicate "+key+" values")
    return values[0] if values else None

def inventory_host_line_matches(line, hostname, private_ip=None):
    parsed_line = split_inventory_host_line(line)
    if parsed_line is None or not parsed_line[1] or parsed_line[1][0] != hostname:
        return False
    return private_ip is None or get_inventory_token(line, "ansible_host") == private_ip

def get_compute_inventory_instance_ids(inventory_dict):
    instance_ids = set()
    for section in ["compute_configured", "compute_to_add"]:
        for line in inventory_dict.get(section, []):
            _, instance_id = get_inventory_instance_id(line)
            if instance_id is None:
                continue
            if instance_id in instance_ids:
                raise RuntimeError("Instance "+instance_id+" occurs more than once in compute inventory")
            instance_ids.add(instance_id)
    return instance_ids

def get_instance_pool_inventory_hosts(inventory_dict):
    hosts_by_instance_id = {}
    inventory_aliases = set()
    private_ips = set()
    for section in ["compute_configured", "compute_to_add"]:
        if section not in inventory_dict:
            raise RuntimeError("Inventory does not contain ["+section+"]")
        for line in inventory_dict[section]:
            inventory_hostname, instance_id = get_inventory_instance_id(line)
            if instance_id is None:
                continue
            private_ip = get_inventory_token(line, "ansible_host")
            if not private_ip:
                raise RuntimeError(
                    "Inventory host "+inventory_hostname+" does not define ansible_host"
                )
            try:
                private_ip = str(ipaddress.ip_address(private_ip))
            except (TypeError, ValueError):
                raise RuntimeError(
                    "Inventory host "+inventory_hostname+" has an invalid ansible_host"
                )
            if instance_id in hosts_by_instance_id:
                raise RuntimeError("Instance "+instance_id+" occurs more than once in compute inventory")
            if inventory_hostname in inventory_aliases:
                raise RuntimeError("Inventory hostname "+inventory_hostname+" occurs more than once")
            if private_ip in private_ips:
                raise RuntimeError("Private IP "+private_ip+" occurs more than once in compute inventory")
            if (
                not inventory_hostname
                or inventory_hostname in [".", ".."]
                or "/" in inventory_hostname
                or "\\" in inventory_hostname
                or "\x00" in inventory_hostname
            ):
                raise RuntimeError("Inventory contains an unsafe compute hostname")
            hosts_by_instance_id[instance_id] = {
                "inventory_hostname": inventory_hostname,
                "private_ip": private_ip,
            }
            inventory_aliases.add(inventory_hostname)
            private_ips.add(private_ip)
    return hosts_by_instance_id

def validate_instance_pool_removal_inventory_plan(
    inventory_dict,
    current_instances,
    hostnames_to_remove,
):
    current_pool_ids = {instance["ocid"] for instance in current_instances}
    if len(current_pool_ids) != len(current_instances):
        raise RuntimeError("The Instance Pool member list contains duplicate OCIDs")
    inventory_ids_after_removal = set()
    pool_ids_to_remove = {
        instance["ocid"]
        for instance in current_instances
        if instance["display_name"] in hostnames_to_remove
    }
    requested_names = set(hostnames_to_remove)
    for section in ["compute_configured", "compute_to_add"]:
        for line in inventory_dict.get(section, []):
            inventory_hostname, instance_id = get_inventory_instance_id(line)
            if instance_id is None:
                continue
            if inventory_hostname in requested_names:
                if instance_id in current_pool_ids:
                    pool_ids_to_remove.add(instance_id)
            else:
                inventory_ids_after_removal.add(instance_id)
    expected_pool_ids_after_removal = current_pool_ids.difference(
        pool_ids_to_remove
    )
    if inventory_ids_after_removal != expected_pool_ids_after_removal:
        raise RuntimeError(
            "The requested removal would leave the Autoscaling Instance Pool and compute "
            "inventory inconsistent. Run reconfigure before removing nodes"
        )
    return pool_ids_to_remove

def validate_os_hostname(hostname):
    if (
        not isinstance(hostname, str)
        or hostname != hostname.strip()
        or len(hostname) > 63
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", hostname) is None
    ):
        raise RuntimeError(
            "Ansible returned an OS hostname that is not a valid DNS hostname label: "+
            repr(hostname)
        )
    return hostname

def get_instance_pool_hostname_sync_plan_path(inventory_path):
    return os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        INSTANCE_POOL_HOSTNAME_SYNC_PLAN_FILENAME,
    )

def validate_instance_pool_hostname_sync_plan(plan):
    plan_version = plan.get("version") if isinstance(plan, dict) else None
    if (
        not isinstance(plan, dict)
        or plan_version not in [1, 2, 3]
        or plan.get("status") != "pending"
        or not isinstance(plan.get("cluster_name"), str)
        or not plan["cluster_name"]
        or not isinstance(plan.get("members"), dict)
    ):
        raise RuntimeError("The pending Instance Pool hostname sync plan has an invalid format")
    if plan_version in [1, 2] and (
        not isinstance(plan.get("instance_pool_id"), str)
        or not plan["instance_pool_id"]
    ):
        raise RuntimeError("The pending Instance Pool hostname sync plan has an invalid format")
    if plan_version == 2:
        deployment_type = plan.get("deployment_type")
        if deployment_type not in ["IP", "CN"]:
            raise RuntimeError(
                "The pending hostname sync plan has an invalid deployment_type"
            )
        cluster_network_id = plan.get("cluster_network_id")
        if deployment_type == "CN" and (
            not isinstance(cluster_network_id, str)
            or not cluster_network_id
        ):
            raise RuntimeError(
                "The pending Cluster Network hostname sync plan has an invalid cluster_network_id"
            )
        if deployment_type == "IP" and cluster_network_id not in [None, ""]:
            raise RuntimeError(
                "An Instance Pool hostname sync plan cannot reference a Cluster Network"
            )
    if plan_version == 3 and (
        plan.get("deployment_type") != "CC"
        or not isinstance(plan.get("compute_cluster_id"), str)
        or not plan["compute_cluster_id"]
        or plan.get("instance_pool_id") not in [None, ""]
        or plan.get("cluster_network_id") not in [None, ""]
    ):
        raise RuntimeError(
            "The pending Compute Cluster hostname sync plan has an invalid identity"
        )
    normalized_hostnames = set()
    for instance_id, member in plan["members"].items():
        if not isinstance(instance_id, str) or not instance_id or not isinstance(member, dict):
            raise RuntimeError("The pending Instance Pool hostname sync plan has an invalid member")
        for key in [
            "hostname",
            "private_ip",
            "inventory_hostname",
            "previous_display_name",
        ]:
            if not isinstance(member.get(key), str) or not member[key]:
                raise RuntimeError(
                    "The pending Instance Pool hostname sync plan has an invalid "+key
                )
        validate_os_hostname(member["hostname"])
        try:
            member["private_ip"] = str(ipaddress.ip_address(member["private_ip"]))
        except (TypeError, ValueError):
            raise RuntimeError(
                "The pending Instance Pool hostname sync plan has an invalid private IP"
            )
        normalized_hostname = member["hostname"].lower()
        if normalized_hostname in normalized_hostnames:
            raise RuntimeError(
                "The pending Instance Pool hostname sync plan contains duplicate hostnames"
            )
        normalized_hostnames.add(normalized_hostname)
    return plan

def load_instance_pool_hostname_sync_plan(inventory_path):
    plan_path = get_instance_pool_hostname_sync_plan_path(inventory_path)
    if not os.path.exists(plan_path):
        return None
    if os.path.islink(plan_path) or not os.path.isfile(plan_path):
        raise RuntimeError("The pending Instance Pool hostname sync plan is not a regular file")
    plan_stat = os.stat(plan_path)
    inventory_stat = os.stat(inventory_path)
    if (
        plan_stat.st_uid != inventory_stat.st_uid
        or plan_stat.st_gid != inventory_stat.st_gid
        or stat.S_IMODE(plan_stat.st_mode) & 0o077
    ):
        raise RuntimeError(
            "The pending Instance Pool hostname sync plan has unsafe ownership or permissions"
        )
    try:
        with open(plan_path, "r", encoding="utf-8") as plan_file:
            plan = json.load(plan_file)
    except (OSError, ValueError) as error:
        raise RuntimeError("Failed to read pending Instance Pool hostname sync plan: "+str(error))
    return validate_instance_pool_hostname_sync_plan(plan)

def write_instance_pool_hostname_sync_plan(inventory_path, plan):
    validate_instance_pool_hostname_sync_plan(plan)
    plan_path = get_instance_pool_hostname_sync_plan_path(inventory_path)
    plan_directory = os.path.dirname(plan_path)
    temp_fd, temp_path = tempfile.mkstemp(
        prefix=".oci-hpc-hostname-sync-",
        dir=plan_directory,
        text=True,
    )
    try:
        with os.fdopen(temp_fd, "w") as plan_file:
            json.dump(plan, plan_file, indent=2, sort_keys=True)
            plan_file.write("\n")
            plan_file.flush()
            os.fsync(plan_file.fileno())
        os.chmod(temp_path, 0o600)
        inventory_stat = os.stat(inventory_path)
        try:
            os.chown(temp_path, inventory_stat.st_uid, inventory_stat.st_gid)
        except PermissionError:
            if inventory_stat.st_uid != os.getuid() or inventory_stat.st_gid != os.getgid():
                raise
        os.replace(temp_path, plan_path)
        temp_path = None
        fsync_directory(plan_directory)
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            os.unlink(temp_path)

def clear_instance_pool_hostname_sync_plan(inventory_path):
    plan_path = get_instance_pool_hostname_sync_plan_path(inventory_path)
    try:
        os.unlink(plan_path)
    except FileNotFoundError:
        return
    fsync_directory(os.path.dirname(plan_path))

def get_pending_instance_pool_node_removals_path(inventory_path):
    return os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        PENDING_INSTANCE_POOL_NODE_REMOVALS_FILENAME,
    )

def validate_pending_instance_pool_node_removal(record):
    required_strings = [
        "cluster_name",
        "compartment_id",
        "instance_pool_id",
        "instance_id",
        "instance_display_name",
        "private_ip",
        "zone_name",
    ]
    if not isinstance(record, dict):
        raise RuntimeError("A pending Instance Pool node removal is malformed")
    for key in required_strings:
        if not isinstance(record.get(key), str) or not record[key]:
            raise RuntimeError("A pending Instance Pool node removal has an invalid "+key)
    try:
        record["private_ip"] = str(ipaddress.ip_address(record["private_ip"]))
    except ValueError:
        raise RuntimeError("A pending Instance Pool node removal has an invalid private IP")
    for key in ["instance_names", "dns_domains"]:
        values = record.get(key)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))
        ):
            raise RuntimeError("A pending Instance Pool node removal has invalid "+key)
    if record["instance_display_name"] not in record["instance_names"]:
        raise RuntimeError("A pending Instance Pool node removal is missing its OCI display name")
    for instance_name in record["instance_names"]:
        validate_os_hostname(instance_name)
    expected_domain_suffix = "."+record["zone_name"].lower()
    for domain in record["dns_domains"]:
        if (
            len(domain) > 253
            or domain.lower().endswith(expected_domain_suffix) is False
            or re.fullmatch(r"[A-Za-z0-9.-]+", domain) is None
            or ".." in domain
        ):
            raise RuntimeError("A pending Instance Pool node removal has an invalid DNS domain")
    return record

def load_pending_instance_pool_node_removals(inventory_path):
    pending_path = get_pending_instance_pool_node_removals_path(inventory_path)
    if not os.path.exists(pending_path):
        return []
    if os.path.islink(pending_path) or not os.path.isfile(pending_path):
        raise RuntimeError("The pending Instance Pool node removal journal is not a regular file")
    pending_stat = os.stat(pending_path)
    inventory_stat = os.stat(inventory_path)
    if (
        pending_stat.st_uid != inventory_stat.st_uid
        or pending_stat.st_gid != inventory_stat.st_gid
        or stat.S_IMODE(pending_stat.st_mode) & 0o077
    ):
        raise RuntimeError("The pending Instance Pool node removal journal has unsafe permissions")
    try:
        with open(pending_path, "r", encoding="utf-8") as pending_file:
            document = json.load(pending_file)
    except (OSError, ValueError) as error:
        raise RuntimeError("Failed to read pending Instance Pool node removals: "+str(error))
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or not isinstance(document.get("removals"), list)
    ):
        raise RuntimeError("The pending Instance Pool node removal journal is malformed")
    records = []
    instance_ids = set()
    for record in document["removals"]:
        validate_pending_instance_pool_node_removal(record)
        if record["instance_id"] in instance_ids:
            raise RuntimeError("The pending Instance Pool node removal journal has duplicate instances")
        instance_ids.add(record["instance_id"])
        records.append(record)
    return records

def write_pending_instance_pool_node_removals(inventory_path, records):
    pending_path = get_pending_instance_pool_node_removals_path(inventory_path)
    if not records:
        try:
            os.unlink(pending_path)
        except FileNotFoundError:
            return
        fsync_directory(os.path.dirname(pending_path))
        return
    for record in records:
        validate_pending_instance_pool_node_removal(record)
    pending_directory = os.path.dirname(pending_path)
    temporary_fd, temporary_path = tempfile.mkstemp(
        prefix=".oci-hpc-node-removal-",
        dir=pending_directory,
        text=True,
    )
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as temporary_file:
            json.dump(
                {"version": 1, "removals": records},
                temporary_file,
                indent=2,
                sort_keys=True,
            )
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, 0o600)
        inventory_stat = os.stat(inventory_path)
        try:
            os.chown(temporary_path, inventory_stat.st_uid, inventory_stat.st_gid)
        except PermissionError:
            if inventory_stat.st_uid != os.getuid() or inventory_stat.st_gid != os.getgid():
                raise
        os.replace(temporary_path, pending_path)
        temporary_path = None
        fsync_directory(pending_directory)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)

def remember_pending_instance_pool_node_removal(inventory_path, record):
    validate_pending_instance_pool_node_removal(record)
    records = load_pending_instance_pool_node_removals(inventory_path)
    for existing_record in records:
        if existing_record["instance_id"] == record["instance_id"]:
            if existing_record != record:
                raise RuntimeError("The pending Instance Pool node removal changed unexpectedly")
            return
    records.append(record)
    write_pending_instance_pool_node_removals(inventory_path, records)

def forget_pending_instance_pool_node_removal(inventory_path, instance_id):
    records = load_pending_instance_pool_node_removals(inventory_path)
    write_pending_instance_pool_node_removals(
        inventory_path,
        [record for record in records if record["instance_id"] != instance_id],
    )

def get_compute_cluster_node_removal_journal_path(inventory_path):
    return os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        PENDING_COMPUTE_CLUSTER_NODE_REMOVALS_FILENAME,
    )

def validate_compute_cluster_node_removal_record(record):
    if not isinstance(record, dict):
        raise RuntimeError("A pending Compute Cluster node removal is malformed")
    for key in [
        "instance_id",
        "instance_display_name",
        "private_ip",
    ]:
        if not isinstance(record.get(key), str) or not record[key]:
            raise RuntimeError(
                "A pending Compute Cluster node removal has an invalid "+key
            )
    instance_names = record.get("instance_names")
    if (
        not isinstance(instance_names, list)
        or not instance_names
        or any(not isinstance(name, str) or not name for name in instance_names)
        or len({name.lower() for name in instance_names}) != len(instance_names)
    ):
        raise RuntimeError(
            "A pending Compute Cluster node removal has invalid instance_names"
        )
    if record["instance_display_name"].lower() not in {
        name.lower() for name in instance_names
    }:
        raise RuntimeError(
            "A pending Compute Cluster node removal is missing its OCI display name"
        )
    for instance_name in instance_names:
        validate_os_hostname(instance_name)
    try:
        record["private_ip"] = str(ipaddress.ip_address(record["private_ip"]))
    except (TypeError, ValueError):
        raise RuntimeError(
            "A pending Compute Cluster node removal has an invalid private IP"
        )
    if not isinstance(record.get("delete_launch_created_data_volumes"), bool):
        raise RuntimeError(
            "A pending Compute Cluster node removal has an invalid local volume flag"
        )
    return record

def validate_compute_cluster_node_removal_journal(document):
    if not isinstance(document, dict):
        raise RuntimeError("The pending Compute Cluster node removal journal is malformed")
    for key in ["cluster_name", "compartment_id", "compute_cluster_id"]:
        if not isinstance(document.get(key), str) or not document[key]:
            raise RuntimeError(
                "The pending Compute Cluster node removal journal has an invalid "+key
            )
    if (
        document.get("version") != 1
        or document.get("status") != "pending"
        or not isinstance(document.get("no_reconfigure"), bool)
        or not isinstance(document.get("source_instance_ids"), list)
        or not isinstance(document.get("removals"), list)
        or not document["removals"]
    ):
        raise RuntimeError("The pending Compute Cluster node removal journal is malformed")
    source_instance_ids = document["source_instance_ids"]
    if (
        any(not isinstance(instance_id, str) or not instance_id for instance_id in source_instance_ids)
        or len(source_instance_ids) != len(set(source_instance_ids))
    ):
        raise RuntimeError(
            "The pending Compute Cluster node removal journal has invalid source members"
        )
    removal_ids = set()
    private_ips = set()
    instance_names = set()
    for record in document["removals"]:
        validate_compute_cluster_node_removal_record(record)
        if record["instance_id"] in removal_ids:
            raise RuntimeError(
                "The pending Compute Cluster node removal journal has duplicate instances"
            )
        if record["private_ip"] in private_ips:
            raise RuntimeError(
                "The pending Compute Cluster node removal journal has duplicate private IPs"
            )
        normalized_names = {name.lower() for name in record["instance_names"]}
        if instance_names.intersection(normalized_names):
            raise RuntimeError(
                "The pending Compute Cluster node removal journal has duplicate names"
            )
        removal_ids.add(record["instance_id"])
        private_ips.add(record["private_ip"])
        instance_names.update(normalized_names)
    if not removal_ids.issubset(set(source_instance_ids)):
        raise RuntimeError(
            "The pending Compute Cluster node removals are not source members"
        )
    return document

def load_compute_cluster_node_removal_journal(inventory_path):
    journal_path = get_compute_cluster_node_removal_journal_path(inventory_path)
    if not os.path.exists(journal_path):
        return None
    if os.path.islink(journal_path) or not os.path.isfile(journal_path):
        raise RuntimeError(
            "The pending Compute Cluster node removal journal is not a regular file"
        )
    journal_stat = os.stat(journal_path)
    inventory_stat = os.stat(inventory_path)
    if (
        journal_stat.st_uid != inventory_stat.st_uid
        or journal_stat.st_gid != inventory_stat.st_gid
        or stat.S_IMODE(journal_stat.st_mode) & 0o077
    ):
        raise RuntimeError(
            "The pending Compute Cluster node removal journal has unsafe permissions"
        )
    try:
        with open(journal_path, "r", encoding="utf-8") as journal_file:
            document = json.load(journal_file)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Failed to read pending Compute Cluster node removal journal: "+str(error)
        )
    return validate_compute_cluster_node_removal_journal(document)

def write_compute_cluster_node_removal_journal(inventory_path, document):
    document = validate_compute_cluster_node_removal_journal(document)
    journal_path = get_compute_cluster_node_removal_journal_path(inventory_path)
    journal_directory = os.path.dirname(journal_path)
    temporary_fd, temporary_path = tempfile.mkstemp(
        prefix=".oci-hpc-compute-cluster-node-removal-",
        dir=journal_directory,
        text=True,
    )
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as journal_file:
            json.dump(document, journal_file, indent=2, sort_keys=True)
            journal_file.write("\n")
            journal_file.flush()
            os.fsync(journal_file.fileno())
        os.chmod(temporary_path, 0o600)
        inventory_stat = os.stat(inventory_path)
        try:
            os.chown(
                temporary_path,
                inventory_stat.st_uid,
                inventory_stat.st_gid,
            )
        except PermissionError:
            if (
                inventory_stat.st_uid != os.getuid()
                or inventory_stat.st_gid != os.getgid()
            ):
                raise
        os.replace(temporary_path, journal_path)
        temporary_path = None
        fsync_directory(journal_directory)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)

def clear_compute_cluster_node_removal_journal(inventory_path):
    journal_path = get_compute_cluster_node_removal_journal_path(inventory_path)
    try:
        os.unlink(journal_path)
    except FileNotFoundError:
        return
    fsync_directory(os.path.dirname(journal_path))

def get_instance_pool_name_dns_ownership_path(inventory_path):
    return os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        INSTANCE_POOL_NAME_DNS_OWNERSHIP_FILENAME,
    )

def validate_instance_pool_name_dns_ownership(document):
    if (
        not isinstance(document, dict)
        or document.get("version") not in [1, 2]
        or not isinstance(document.get("cluster_name"), str)
        or not document["cluster_name"]
        or not isinstance(document.get("rrsets"), list)
    ):
        raise RuntimeError("The Instance Pool DNS ownership file is malformed")
    if document["version"] == 1 and (
        not isinstance(document.get("instance_pool_id"), str)
        or not document["instance_pool_id"]
    ):
        raise RuntimeError("The Instance Pool DNS ownership file is malformed")
    if document["version"] == 2 and (
        document.get("deployment_type") != "CC"
        or not isinstance(document.get("compute_cluster_id"), str)
        or not document["compute_cluster_id"]
        or document.get("instance_pool_id") not in [None, ""]
    ):
        raise RuntimeError("The Compute Cluster DNS ownership file is malformed")
    terraform_state_released = document.get("terraform_state_released", True)
    if not isinstance(terraform_state_released, bool):
        raise RuntimeError(
            "The Instance Pool DNS ownership file has an invalid Terraform ownership state"
        )
    seen_rrsets = set()
    for rrset in document["rrsets"]:
        if not isinstance(rrset, dict):
            raise RuntimeError("The Instance Pool DNS ownership file has an invalid RRset")
        for key in ["zone_id", "zone_name", "domain"]:
            if not isinstance(rrset.get(key), str) or not rrset[key]:
                raise RuntimeError(
                    "The Instance Pool DNS ownership file has an invalid "+key
                )
        private_ips = rrset.get("private_ips")
        if (
            not isinstance(private_ips, list)
            or not private_ips
            or len(private_ips) != len(set(private_ips))
        ):
            raise RuntimeError(
                "The Instance Pool DNS ownership file has invalid private IPs"
            )
        normalized_private_ips = []
        for private_ip in private_ips:
            try:
                normalized_private_ips.append(
                    str(ipaddress.ip_address(private_ip))
                )
            except (TypeError, ValueError):
                raise RuntimeError(
                    "The Instance Pool DNS ownership file has an invalid private IP"
                )
        rrset["private_ips"] = normalized_private_ips
        if (
            len(rrset["domain"]) > 253
            or not rrset["domain"].lower().endswith(
                "."+rrset["zone_name"].lower()
            )
            or re.fullmatch(r"[A-Za-z0-9.-]+", rrset["domain"]) is None
            or ".." in rrset["domain"]
        ):
            raise RuntimeError(
                "The Instance Pool DNS ownership file has an invalid domain"
            )
        rrset_key = (rrset["zone_id"], rrset["domain"].lower())
        if rrset_key in seen_rrsets:
            raise RuntimeError(
                "The Instance Pool DNS ownership file contains a duplicate RRset"
            )
        seen_rrsets.add(rrset_key)
    return document

def load_instance_pool_name_dns_ownership(inventory_path):
    ownership_path = get_instance_pool_name_dns_ownership_path(inventory_path)
    if not os.path.exists(ownership_path):
        return None
    if os.path.islink(ownership_path) or not os.path.isfile(ownership_path):
        raise RuntimeError("The Instance Pool DNS ownership file is not a regular file")
    ownership_stat = os.stat(ownership_path)
    inventory_stat = os.stat(inventory_path)
    if (
        ownership_stat.st_uid != inventory_stat.st_uid
        or ownership_stat.st_gid != inventory_stat.st_gid
        or stat.S_IMODE(ownership_stat.st_mode) & 0o077
    ):
        raise RuntimeError(
            "The Instance Pool DNS ownership file has unsafe ownership or permissions"
        )
    try:
        with open(ownership_path, "r", encoding="utf-8") as ownership_file:
            document = json.load(ownership_file)
    except (OSError, ValueError) as error:
        raise RuntimeError("Failed to read Instance Pool DNS ownership: "+str(error))
    return validate_instance_pool_name_dns_ownership(document)

def write_instance_pool_name_dns_ownership(inventory_path, document):
    ownership_path = get_instance_pool_name_dns_ownership_path(inventory_path)
    if document is None or not document.get("rrsets"):
        try:
            os.unlink(ownership_path)
        except FileNotFoundError:
            return
        fsync_directory(os.path.dirname(ownership_path))
        return
    validate_instance_pool_name_dns_ownership(document)
    ownership_directory = os.path.dirname(ownership_path)
    temporary_fd, temporary_path = tempfile.mkstemp(
        prefix=".oci-hpc-name-dns-",
        dir=ownership_directory,
        text=True,
    )
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as ownership_file:
            json.dump(document, ownership_file, indent=2, sort_keys=True)
            ownership_file.write("\n")
            ownership_file.flush()
            os.fsync(ownership_file.fileno())
        os.chmod(temporary_path, 0o600)
        inventory_stat = os.stat(inventory_path)
        try:
            os.chown(
                temporary_path,
                inventory_stat.st_uid,
                inventory_stat.st_gid,
            )
        except PermissionError:
            if (
                inventory_stat.st_uid != os.getuid()
                or inventory_stat.st_gid != os.getgid()
            ):
                raise
        os.replace(temporary_path, ownership_path)
        temporary_path = None
        fsync_directory(ownership_directory)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)

def validate_instance_pool_dns_ownership_identity(
    document,
    expected_cluster_name,
    instance_pool_id=None,
    require_terraform_state_released=True,
    compute_cluster_id=None,
):
    if document is None:
        return
    if document["cluster_name"] != expected_cluster_name:
        raise RuntimeError(
            "The Instance Pool DNS ownership file belongs to another cluster or pool"
        )
    if document.get("version") == 2:
        if (
            instance_pool_id is not None
            or compute_cluster_id is None
            or document.get("deployment_type") != "CC"
            or document.get("compute_cluster_id") != compute_cluster_id
        ):
            raise RuntimeError(
                "The Compute Cluster DNS ownership file belongs to another cluster"
            )
    elif compute_cluster_id is not None or document["instance_pool_id"] != instance_pool_id:
        raise RuntimeError(
            "The Instance Pool DNS ownership file belongs to another cluster or pool"
        )
    if (
        require_terraform_state_released
        and document.get("terraform_state_released", True) is not True
    ):
        raise RuntimeError(
            "The managed pool DNS ownership transfer from Terraform is incomplete"
        )

def build_name_dns_ownership_document(
    expected_cluster_name,
    rrsets,
    instance_pool_id=None,
    compute_cluster_id=None,
    terraform_state_released=None,
):
    if (instance_pool_id is None) == (compute_cluster_id is None):
        raise RuntimeError("Exactly one compute deployment identity is required for DNS ownership")
    if compute_cluster_id is not None:
        document = {
            "version": 2,
            "deployment_type": "CC",
            "cluster_name": expected_cluster_name,
            "compute_cluster_id": compute_cluster_id,
            "rrsets": list(rrsets),
        }
    else:
        document = {
            "version": 1,
            "cluster_name": expected_cluster_name,
            "instance_pool_id": instance_pool_id,
            "rrsets": list(rrsets),
        }
    if terraform_state_released is not None:
        document["terraform_state_released"] = terraform_state_released
    return validate_instance_pool_name_dns_ownership(document)

def get_instance_pool_post_resize_recovery_path(inventory_path):
    return os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        INSTANCE_POOL_POST_RESIZE_RECOVERY_FILENAME,
    )

def validate_instance_pool_post_resize_recovery(document):
    if not isinstance(document, dict):
        raise RuntimeError("The Instance Pool post-resize recovery marker is malformed")
    common_fields_are_valid = (
        isinstance(document.get("cluster_name"), str)
        and bool(document["cluster_name"])
        and isinstance(document.get("instance_pool_id"), str)
        and bool(document["instance_pool_id"])
    )
    if document.get("version") == 1:
        if (
            not common_fields_are_valid
            or document.get("status") != "reconfigure_and_sync"
        ):
            raise RuntimeError(
                "The Instance Pool post-resize recovery marker is malformed"
            )
        return document
    if document.get("version") != 2 or not common_fields_are_valid:
        raise RuntimeError("The Instance Pool post-resize recovery marker is malformed")
    if document.get("status") not in ["reconfigure_and_sync", "state_only"]:
        raise RuntimeError("The Instance Pool post-resize recovery marker is malformed")
    if document.get("action") not in ["add", "remove"]:
        raise RuntimeError("The Instance Pool post-resize recovery marker is malformed")
    for size_key in ["source_size", "target_size"]:
        size = document.get(size_key)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError(
                "The Instance Pool post-resize recovery marker is malformed"
            )
    if (
        document["action"] == "add"
        and document["target_size"] < document["source_size"]
    ) or (
        document["action"] == "remove"
        and document["target_size"] > document["source_size"]
    ):
        raise RuntimeError("The Instance Pool post-resize recovery marker is malformed")
    return document

def load_instance_pool_post_resize_recovery(inventory_path):
    recovery_path = get_instance_pool_post_resize_recovery_path(inventory_path)
    if not os.path.exists(recovery_path):
        return None
    if os.path.islink(recovery_path) or not os.path.isfile(recovery_path):
        raise RuntimeError(
            "The Instance Pool post-resize recovery marker is not a regular file"
        )
    recovery_stat = os.stat(recovery_path)
    inventory_stat = os.stat(inventory_path)
    if (
        recovery_stat.st_uid != inventory_stat.st_uid
        or recovery_stat.st_gid != inventory_stat.st_gid
        or stat.S_IMODE(recovery_stat.st_mode) & 0o077
    ):
        raise RuntimeError(
            "The Instance Pool post-resize recovery marker has unsafe permissions"
        )
    try:
        with open(recovery_path, "r", encoding="utf-8") as recovery_file:
            document = json.load(recovery_file)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Failed to read Instance Pool post-resize recovery marker: "+str(error)
        )
    return validate_instance_pool_post_resize_recovery(document)

def write_instance_pool_post_resize_recovery(
    inventory_path,
    expected_cluster_name,
    instance_pool_id,
    status="reconfigure_and_sync",
    action=None,
    source_size=None,
    target_size=None,
):
    document = validate_instance_pool_post_resize_recovery({
        "version": 2,
        "status": status,
        "cluster_name": expected_cluster_name,
        "instance_pool_id": instance_pool_id,
        "action": action,
        "source_size": source_size,
        "target_size": target_size,
    })
    recovery_path = get_instance_pool_post_resize_recovery_path(inventory_path)
    recovery_directory = os.path.dirname(recovery_path)
    temporary_fd, temporary_path = tempfile.mkstemp(
        prefix=".oci-hpc-post-resize-",
        dir=recovery_directory,
        text=True,
    )
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as recovery_file:
            json.dump(document, recovery_file, indent=2, sort_keys=True)
            recovery_file.write("\n")
            recovery_file.flush()
            os.fsync(recovery_file.fileno())
        os.chmod(temporary_path, 0o600)
        inventory_stat = os.stat(inventory_path)
        try:
            os.chown(
                temporary_path,
                inventory_stat.st_uid,
                inventory_stat.st_gid,
            )
        except PermissionError:
            if (
                inventory_stat.st_uid != os.getuid()
                or inventory_stat.st_gid != os.getgid()
            ):
                raise
        os.replace(temporary_path, recovery_path)
        temporary_path = None
        fsync_directory(recovery_directory)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)

def clear_instance_pool_post_resize_recovery(inventory_path):
    recovery_path = get_instance_pool_post_resize_recovery_path(inventory_path)
    try:
        os.unlink(recovery_path)
    except FileNotFoundError:
        return
    fsync_directory(os.path.dirname(recovery_path))

def write_text_atomic_preserving_metadata(path, contents):
    directory = os.path.dirname(os.path.abspath(path))
    original_stat = os.stat(path)
    temporary_fd, temporary_path = tempfile.mkstemp(
        prefix=".oci-hpc-config-",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(contents)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, stat.S_IMODE(original_stat.st_mode))
        try:
            os.chown(temporary_path, original_stat.st_uid, original_stat.st_gid)
        except PermissionError:
            if original_stat.st_uid != os.getuid() or original_stat.st_gid != os.getgid():
                raise
        os.replace(temporary_path, path)
        temporary_path = None
        fsync_directory(directory)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)

def find_hcl_block(configuration, header):
    block_start = configuration.find(header)
    if block_start < 0:
        raise RuntimeError("Terraform configuration does not contain "+header)
    opening_brace = configuration.find("{", block_start+len(header))
    if opening_brace < 0:
        raise RuntimeError("Terraform configuration has an incomplete "+header+" block")
    depth = 0
    for index in range(opening_brace, len(configuration)):
        if configuration[index] == "{":
            depth += 1
        elif configuration[index] == "}":
            depth -= 1
            if depth == 0:
                return block_start, index+1
    raise RuntimeError("Terraform configuration has an unterminated "+header+" block")

def migrate_compute_cluster_instance_display_name_management(inventory_path):
    """Keep Terraform from restoring pre-Ansible names on a later apply."""
    cluster_directory = os.path.dirname(os.path.abspath(inventory_path))
    compute_nodes_path = os.path.join(cluster_directory, "compute-nodes.tf")
    if not os.path.exists(compute_nodes_path):
        return False
    if os.path.islink(compute_nodes_path) or not os.path.isfile(compute_nodes_path):
        raise RuntimeError("The cluster compute-nodes.tf is not a regular file")
    with open(compute_nodes_path, "r", encoding="utf-8") as compute_nodes_file:
        configuration = compute_nodes_file.read()
    resource_header = 'resource "oci_core_instance" "compute_cluster_instances"'
    resource_start, resource_end = find_hcl_block(configuration, resource_header)
    resource_block = configuration[resource_start:resource_end]
    lifecycle_matches = list(re.finditer(r"(?m)^\s*lifecycle\s*\{", resource_block))
    if len(lifecycle_matches) != 1:
        raise RuntimeError(
            "The Compute Cluster instance lifecycle block has an unsupported format"
        )
    lifecycle_start = lifecycle_matches[0].start()
    ignore_match = re.search(
        r"(?m)^(?P<indent>\s*)ignore_changes\s*=\s*\[\s*$",
        resource_block[lifecycle_start:],
    )
    if ignore_match is None:
        raise RuntimeError(
            "The Compute Cluster instance ignore_changes list has an unsupported format"
        )
    ignore_line_end = (
        lifecycle_start+ignore_match.end()
    )
    closing_match = re.search(
        r"(?m)^\s*\]",
        resource_block[ignore_line_end:],
    )
    if closing_match is None:
        raise RuntimeError("The Compute Cluster instance ignore_changes list is incomplete")
    ignore_list_end = ignore_line_end+closing_match.start()
    ignore_body = resource_block[ignore_line_end:ignore_list_end]
    required_ignored_attributes = [
        "display_name",
        "create_vnic_details[0].display_name",
    ]
    missing_ignored_attributes = [
        attribute
        for attribute in required_ignored_attributes
        if re.search(
            r"(?m)^\s*"+re.escape(attribute)+r"\s*,?\s*$",
            ignore_body,
        ) is None
    ]
    if not missing_ignored_attributes:
        return False
    refuse_terraform_state_mutation_while_locked(cluster_directory)
    entry_indent = ignore_match.group("indent")+"  "
    migrated_resource = (
        resource_block[:ignore_line_end]
        +"".join(
            "\n"+entry_indent+attribute+","
            for attribute in missing_ignored_attributes
        )
        +resource_block[ignore_line_end:]
    )
    migrated_configuration = (
        configuration[:resource_start]
        +migrated_resource
        +configuration[resource_end:]
    )
    write_text_atomic_preserving_metadata(
        compute_nodes_path,
        migrated_configuration,
    )
    return True

def refuse_terraform_state_mutation_while_locked(cluster_directory):
    """Fail immediately instead of nesting state writes inside terraform apply."""
    lock_path = os.path.join(cluster_directory, ".terraform.tfstate.lock.info")
    if not os.path.lexists(lock_path):
        return
    if os.path.islink(lock_path) or not os.path.isfile(lock_path):
        raise RuntimeError("The Terraform state lock marker is unsafe")
    raise RuntimeError(
        "Terraform state is locked. Run /opt/oci-hpc/bin/resize.sh "
        "--cluster_name <cluster_name> reconfigure after terraform apply exits"
    )

def get_terraform_managed_oci_name_dns_state_addresses(inventory_path):
    """Read exact root OCI-name DNS addresses without acquiring a state lock."""
    state_path = os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        "terraform.tfstate",
    )
    if not os.path.exists(state_path):
        return []
    if os.path.islink(state_path) or not os.path.isfile(state_path):
        raise RuntimeError(
            "Terraform state is unsafe for OCI-name DNS ownership inspection"
        )
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Failed to read Terraform state for OCI-name DNS ownership: "+
            str(error)
        )
    addresses = []
    for resource in state.get("resources", []):
        if (
            resource.get("mode") != "managed"
            or resource.get("module") not in [None, ""]
            or resource.get("type") != "oci_dns_rrset"
            or resource.get("name") != "rrset-cluster-network-OCI"
        ):
            continue
        for resource_instance in resource.get("instances", []):
            address = INSTANCE_POOL_OCI_DNS_RESOURCE_ADDRESS
            if "index_key" in resource_instance:
                index_key = resource_instance["index_key"]
                if isinstance(index_key, str):
                    address += "["+json.dumps(
                        index_key,
                        separators=(",", ":"),
                    )+"]"
                elif isinstance(index_key, int) and not isinstance(index_key, bool):
                    address += "["+str(index_key)+"]"
                else:
                    raise RuntimeError(
                        "Terraform OCI-name DNS state has an invalid index key"
                    )
            addresses.append(address)
    if len(addresses) != len(set(addresses)):
        raise RuntimeError(
            "Terraform state contains duplicate OCI-name DNS resource addresses"
        )
    return addresses

def get_terraform_managed_oci_name_dns_rrsets(
    inventory_path,
    state_addresses,
    expected_zone_id,
    expected_zone_name,
):
    """Read every exact canonical-name RRset selected for state release."""
    if len(state_addresses) != len(set(state_addresses)):
        raise RuntimeError(
            "Terraform returned duplicate OCI-name DNS state addresses"
        )
    state_path = os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        "terraform.tfstate",
    )
    if os.path.islink(state_path) or not os.path.isfile(state_path):
        raise RuntimeError(
            "Terraform state was not found for OCI-name DNS ownership migration"
        )
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Failed to read Terraform DNS state for ownership migration: "+
            str(error)
        )
    rrsets_by_address = {}
    for resource in state.get("resources", []):
        if (
            resource.get("mode") != "managed"
            or resource.get("module") not in [None, ""]
            or resource.get("type") != "oci_dns_rrset"
            or resource.get("name") != "rrset-cluster-network-OCI"
        ):
            continue
        for resource_instance in resource.get("instances", []):
            address = INSTANCE_POOL_OCI_DNS_RESOURCE_ADDRESS
            if "index_key" in resource_instance:
                index_key = resource_instance["index_key"]
                if isinstance(index_key, str):
                    address += "["+json.dumps(index_key, separators=(",", ":"))+"]"
                elif isinstance(index_key, int) and not isinstance(index_key, bool):
                    address += "["+str(index_key)+"]"
                else:
                    raise RuntimeError(
                        "Terraform OCI-name DNS state has an invalid index key"
                    )
            if address in rrsets_by_address:
                raise RuntimeError(
                    "Terraform state contains duplicate OCI-name DNS resource addresses"
                )
            attributes = resource_instance.get("attributes")
            if not isinstance(attributes, dict):
                raise RuntimeError(
                    "Terraform OCI-name DNS state has invalid attributes"
                )
            domain = attributes.get("domain")
            zone_id = attributes.get("zone_name_or_id")
            items = attributes.get("items")
            if (
                not isinstance(domain, str)
                or not domain
                or zone_id != expected_zone_id
                or attributes.get("rtype") != "A"
                or attributes.get("scope") != "PRIVATE"
                or not isinstance(items, list)
                or not items
            ):
                raise RuntimeError(
                    "Terraform OCI-name DNS state is not the expected private A RRset"
                )
            private_ips = set()
            for item in items:
                if (
                    not isinstance(item, dict)
                    or item.get("rtype") != "A"
                    or not isinstance(item.get("domain"), str)
                    or item["domain"].rstrip(".").lower()
                    != domain.rstrip(".").lower()
                ):
                    raise RuntimeError(
                        "Terraform OCI-name DNS state has an invalid A record"
                    )
                try:
                    private_ips.add(str(ipaddress.ip_address(item.get("rdata"))))
                except (TypeError, ValueError):
                    raise RuntimeError(
                        "Terraform OCI-name DNS state has an invalid private IP"
                    )
            rrset = {
                "zone_id": zone_id,
                "zone_name": expected_zone_name,
                "domain": domain.rstrip("."),
                "private_ips": sorted(private_ips),
            }
            validate_instance_pool_name_dns_ownership({
                "version": 1,
                "cluster_name": "state-validation",
                "instance_pool_id": "state-validation",
                "rrsets": [rrset],
            })
            rrsets_by_address[address] = rrset
    if set(rrsets_by_address) != set(state_addresses):
        raise RuntimeError(
            "Terraform OCI-name DNS state addresses could not be recovered exactly"
        )
    return [rrsets_by_address[address] for address in state_addresses]

def get_terraform_managed_slurm_dns_rrsets(
    inventory_path,
    expected_zone_id,
):
    """Return exact Slurm RRsets that remain represented in Terraform state."""
    if inventory_path is None:
        return {}
    state_path = os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        "terraform.tfstate",
    )
    if not os.path.exists(state_path):
        return {}
    if os.path.islink(state_path) or not os.path.isfile(state_path):
        raise RuntimeError(
            "Terraform state is unsafe for Slurm DNS ownership inspection"
        )
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Failed to read Terraform state for Slurm DNS ownership: "+
            str(error)
        )
    rrsets_by_domain = {}
    for resource in state.get("resources", []):
        if (
            resource.get("mode") != "managed"
            or resource.get("module") not in [None, ""]
            or resource.get("type") != "oci_dns_rrset"
            or resource.get("name") != "rrset-cluster-network-SLURM"
        ):
            continue
        for resource_instance in resource.get("instances", []):
            attributes = resource_instance.get("attributes")
            if not isinstance(attributes, dict):
                raise RuntimeError(
                    "Terraform Slurm DNS state has invalid attributes"
                )
            domain = attributes.get("domain")
            items = attributes.get("items")
            zone_id = attributes.get("zone_name_or_id")
            if (
                not isinstance(domain, str)
                or not domain
                or not isinstance(zone_id, str)
                or not zone_id
                or (
                    expected_zone_id is not None
                    and zone_id != expected_zone_id
                )
                or attributes.get("rtype") != "A"
                or attributes.get("scope") != "PRIVATE"
                or not isinstance(items, list)
                or not items
            ):
                raise RuntimeError(
                    "Terraform Slurm DNS state is not the expected private A RRset"
                )
            private_ips = set()
            for item in items:
                if (
                    not isinstance(item, dict)
                    or item.get("rtype") != "A"
                    or not isinstance(item.get("domain"), str)
                    or item["domain"].rstrip(".").lower()
                    != domain.rstrip(".").lower()
                ):
                    raise RuntimeError(
                        "Terraform Slurm DNS state has an invalid A record"
                    )
                try:
                    private_ips.add(
                        str(ipaddress.ip_address(item.get("rdata")))
                    )
                except (TypeError, ValueError):
                    raise RuntimeError(
                        "Terraform Slurm DNS state has an invalid private IP"
                    )
            normalized_domain = domain.rstrip(".").lower()
            if normalized_domain in rrsets_by_domain:
                raise RuntimeError(
                    "Terraform state contains duplicate Slurm DNS domains"
                )
            rrsets_by_domain[normalized_domain] = {
                "zone_id": zone_id,
                "domain": domain.rstrip("."),
                "private_ips": sorted(private_ips),
            }
    return rrsets_by_domain

def get_terraform_managed_slurm_dns_domains(
    inventory_path,
    expected_zone_id,
):
    """Return exact Slurm RRset domains that remain owned by Terraform."""
    return set(
        get_terraform_managed_slurm_dns_rrsets(
            inventory_path,
            expected_zone_id,
        )
    )

def migrate_instance_pool_oci_dns_ownership(
    inventory_path,
    max_wait_seconds=120,
    compartment_id=None,
    instance_pool_id=None,
    compute_cluster_id=None,
    instances_by_id=None,
):
    """Move copied, pre-feature OCI-name RRsets out of Terraform."""
    if instance_pool_id is not None and compute_cluster_id is not None:
        raise RuntimeError("DNS ownership migration received multiple deployment identities")
    cluster_directory = os.path.dirname(os.path.abspath(inventory_path))
    network_path = os.path.join(cluster_directory, "network.tf")
    if not os.path.exists(network_path):
        return False
    if os.path.islink(network_path) or not os.path.isfile(network_path):
        raise RuntimeError("The cluster network.tf is not a regular file")
    with open(network_path, "r", encoding="utf-8") as network_file:
        network_configuration = network_file.read()
    resource_header = 'resource "oci_dns_rrset" "rrset-cluster-network-OCI"'
    resource_start = network_configuration.find(resource_header)
    if resource_start < 0:
        raise RuntimeError("The cluster network.tf does not contain the OCI-name DNS resource")
    next_resource = network_configuration.find("\nresource \"", resource_start+len(resource_header))
    resource_end = len(network_configuration) if next_resource < 0 else next_resource
    resource_block = network_configuration[resource_start:resource_end]
    for_each_matches = list(re.finditer(
        r"(?m)^(?P<prefix>\s*for_each\s*=\s*)(?P<value>[^\r\n]+)$",
        resource_block,
    ))
    if len(for_each_matches) != 1:
        raise RuntimeError("The OCI-name DNS resource has an unsupported for_each definition")
    version_one_template_value = (
        "var.dns_entries && (var.cluster_network || var.compute_cluster) ? "
        "toset([for v in range(var.node_count) : tostring(v)]) : []"
    )
    current_value = for_each_matches[0].group("value").strip()
    legacy_compatible_value = (
        "var.dns_entries && var.cluster_network ? "
        "toset([for v in range(var.node_count) : tostring(v)]) : []"
    )
    ownership_marker_path = os.path.join(
        cluster_directory,
        MANAGED_POOL_DNS_OWNERSHIP_MARKER_FILENAME,
    )
    legacy_value = (
        "var.dns_entries ? toset([for v in range(var.node_count) : "
        "tostring(v)]) : []"
    )
    compute_cluster_template_value = (
        "var.dns_entries && var.compute_cluster ? "
        "toset([for v in range(var.node_count) : tostring(v)]) : []"
    )
    # for_each only accepts a map or set.  Keep the historical literal [] as
    # an accepted migration input, but always write a correctly typed empty
    # set so a fresh Terraform plan remains valid.
    desired_value = "toset([])"
    supported_values = {
        legacy_value,
        version_one_template_value,
        legacy_compatible_value,
        compute_cluster_template_value,
        "[]",
        desired_value,
    }
    if current_value not in supported_values:
        raise RuntimeError(
            "The copied OCI-name DNS resource was customized; migrate its Terraform ownership manually"
        )
    prior_ownership = load_instance_pool_name_dns_ownership(inventory_path)
    ownership_transfer_is_pending = (
        prior_ownership is not None
        and prior_ownership.get("terraform_state_released", True) is not True
    )
    managed_marker_exists = os.path.lexists(ownership_marker_path)
    if managed_marker_exists:
        if os.path.islink(ownership_marker_path) or not os.path.isfile(
            ownership_marker_path
        ):
            raise RuntimeError(
                "The managed pool DNS ownership marker is not a regular file"
            )
        if current_value == desired_value and not ownership_transfer_is_pending:
            return False

    legacy_ownership_marker_path = os.path.join(
        cluster_directory,
        INSTANCE_POOL_DNS_OWNERSHIP_MARKER_FILENAME,
    )
    legacy_marker_exists = os.path.lexists(legacy_ownership_marker_path)
    if legacy_marker_exists and (
        os.path.islink(legacy_ownership_marker_path)
        or not os.path.isfile(legacy_ownership_marker_path)
    ):
        raise RuntimeError(
            "The legacy Instance Pool DNS ownership marker is not a regular file"
        )
    migration_inventory = parse_inventory(inventory_path)
    is_direct_instance_pool = not parse_bool(
        get_inventory_variable(
            migration_inventory or {},
            "cluster_network",
            "true",
        )
    )
    # Version 1 already disabled Terraform's canonical-name RRsets for direct
    # Instance Pools.  Upgrade those copied files and marker locally without
    # invoking Terraform inside a Terraform provisioner.  The same shortcut
    # is unsafe for Cluster Network, where version 1 still owned the RRsets.
    skip_state_inspection = (
        (
            legacy_marker_exists
            and is_direct_instance_pool
            and compute_cluster_id is None
            and not ownership_transfer_is_pending
            and current_value in {
                version_one_template_value,
                legacy_compatible_value,
                desired_value,
            }
        )
        or (
            managed_marker_exists
            and compute_cluster_id is None
            and not ownership_transfer_is_pending
            and current_value == compute_cluster_template_value
        )
    )

    state_lock_path = os.path.join(
        cluster_directory,
        ".terraform.tfstate.lock.info",
    )
    if not skip_state_inspection and os.path.lexists(state_lock_path):
        locked_state_addresses = (
            get_terraform_managed_oci_name_dns_state_addresses(
                inventory_path
            )
        )
        configuration_can_create_rrsets = (
            current_value == legacy_value
            or (
                current_value == version_one_template_value
                and (
                    not is_direct_instance_pool
                    or compute_cluster_id is not None
                )
            )
            or (
                current_value == legacy_compatible_value
                and not is_direct_instance_pool
            )
            or (
                current_value == compute_cluster_template_value
                and compute_cluster_id is not None
            )
        )
        if configuration_can_create_rrsets or locked_state_addresses:
            refuse_terraform_state_mutation_while_locked(cluster_directory)
        # The active plan cannot create this address and the state does not
        # contain it, so a fresh deployment can safely install the marker
        # without invoking Terraform recursively from local-exec.
        skip_state_inspection = True

    if skip_state_inspection:
        legacy_addresses = []
    else:
        try:
            listed_state = subprocess.run(
                ["terraform", "state", "list"],
                cwd=cluster_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=max_wait_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Failed to inspect Terraform DNS state: "+str(error))
        if listed_state.returncode != 0:
            detail = (listed_state.stderr or listed_state.stdout or "").strip()
            raise RuntimeError("Failed to inspect Terraform DNS state: "+detail)
        legacy_addresses = [
            address for address in listed_state.stdout.splitlines()
            if (
                address == INSTANCE_POOL_OCI_DNS_RESOURCE_ADDRESS
                or address.startswith(INSTANCE_POOL_OCI_DNS_RESOURCE_ADDRESS+"[")
            )
        ]
    if legacy_addresses:
        if (
            compartment_id is None
            or (instance_pool_id is None and compute_cluster_id is None)
            or instances_by_id is None
        ):
            raise RuntimeError(
                "Compute deployment identity is required to migrate legacy DNS ownership"
            )
        migration_cluster_name = get_inventory_variable(
            migration_inventory,
            "cluster_name",
        )
        migration_zone_name = get_inventory_variable(
            migration_inventory,
            "zone_name",
            migration_cluster_name+".local",
        )
        migration_zone_id = get_single_private_dns_zone_id(
            compartment_id,
            migration_zone_name,
        )
        if prior_ownership is not None:
            validate_instance_pool_dns_ownership_identity(
                prior_ownership,
                migration_cluster_name,
                instance_pool_id,
                require_terraform_state_released=False,
                compute_cluster_id=compute_cluster_id,
            )
        terraform_rrsets = get_terraform_managed_oci_name_dns_rrsets(
            inventory_path,
            legacy_addresses,
            migration_zone_id,
            migration_zone_name,
        )
        for rrset in terraform_rrsets:
            verify_private_dns_a_rrset_ownership(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )
        terraform_rrsets_by_domain = {}
        for rrset in terraform_rrsets:
            domain_key = rrset["domain"].lower()
            if domain_key in terraform_rrsets_by_domain:
                raise RuntimeError(
                    "Terraform OCI-name DNS state contains duplicate domains"
                )
            terraform_rrsets_by_domain[domain_key] = rrset
        live_derived_rrsets = []
        for instance in instances_by_id.values():
            display_name = instance.get("display_name")
            if not isinstance(display_name, str) or not display_name:
                raise RuntimeError(
                    "A managed pool member has an invalid display name during DNS migration"
                )
            expected_domain = (
                display_name+"."+migration_zone_name
            ).lower()
            try:
                private_ip = str(ipaddress.ip_address(instance.get("ip")))
            except (TypeError, ValueError):
                raise RuntimeError(
                    "A managed pool member has an invalid private IP during DNS migration"
                )
            state_rrset = terraform_rrsets_by_domain.get(expected_domain)
            if (
                state_rrset is not None
                and set(state_rrset["private_ips"]) != {private_ip}
            ):
                raise RuntimeError(
                    "Terraform OCI-name DNS state disagrees with the current managed pool membership"
                )
            if state_rrset is None:
                domain = display_name+"."+migration_zone_name
                existing_ips = verify_private_dns_a_rrset_ownership(
                    migration_zone_id,
                    domain,
                    {private_ip},
                )
                live_derived_rrsets.append({
                    "zone_id": migration_zone_id,
                    "zone_name": migration_zone_name,
                    "domain": domain,
                    "private_ips": sorted(existing_ips or {private_ip}),
                })
        migration_rrsets = {
            (rrset["zone_id"], rrset["domain"].lower()): rrset
            for rrset in (
                prior_ownership["rrsets"] if prior_ownership is not None else []
            )
        }
        for rrset in terraform_rrsets+live_derived_rrsets:
            rrset_key = (rrset["zone_id"], rrset["domain"].lower())
            if (
                rrset_key in migration_rrsets
                and migration_rrsets[rrset_key] != rrset
            ):
                raise RuntimeError(
                    "Terraform and Python DNS ownership records disagree"
                )
            migration_rrsets[rrset_key] = rrset
        # Seed exact domain/IP ownership before releasing Terraform state.  If
        # interrupted after state rm, cleanup and retry still know every
        # unmanaged legacy RRset.
        migration_ownership = build_name_dns_ownership_document(
            migration_cluster_name,
            list(migration_rrsets.values()),
            instance_pool_id=instance_pool_id,
            compute_cluster_id=compute_cluster_id,
            terraform_state_released=False,
        )
        write_instance_pool_name_dns_ownership(
            inventory_path,
            migration_ownership,
        )
        refuse_terraform_state_mutation_while_locked(cluster_directory)
        try:
            removed_state = subprocess.run(
                ["terraform", "state", "rm", "-lock-timeout=60s"]+legacy_addresses,
                cwd=cluster_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=max_wait_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Failed to release Terraform OCI-name DNS ownership: "+str(error))
        if removed_state.returncode != 0:
            detail = (removed_state.stderr or removed_state.stdout or "").strip()
            raise RuntimeError("Failed to release Terraform OCI-name DNS ownership: "+detail)
        migration_ownership["terraform_state_released"] = True
        write_instance_pool_name_dns_ownership(
            inventory_path,
            migration_ownership,
        )
    elif ownership_transfer_is_pending:
        if (
            compartment_id is None
            or (instance_pool_id is None and compute_cluster_id is None)
        ):
            raise RuntimeError(
                "Compute deployment identity is required to finish legacy DNS ownership migration"
            )
        migration_cluster_name = get_inventory_variable(
            migration_inventory,
            "cluster_name",
        )
        validate_instance_pool_dns_ownership_identity(
            prior_ownership,
            migration_cluster_name,
            instance_pool_id,
            require_terraform_state_released=False,
            compute_cluster_id=compute_cluster_id,
        )
        released_ownership = dict(prior_ownership)
        released_ownership["terraform_state_released"] = True
        write_instance_pool_name_dns_ownership(
            inventory_path,
            released_ownership,
        )

    if desired_value != current_value:
        match = for_each_matches[0]
        migrated_block = (
            resource_block[:match.start("value")]
            +desired_value
            +resource_block[match.end("value"):]
        )
        migrated_configuration = (
            network_configuration[:resource_start]
            +migrated_block
            +network_configuration[resource_end:]
        )
        write_text_atomic_preserving_metadata(network_path, migrated_configuration)
    marker_fd, temporary_marker_path = tempfile.mkstemp(
        prefix=".oci-hpc-dns-owner-",
        dir=cluster_directory,
        text=True,
    )
    try:
        with os.fdopen(marker_fd, "w", encoding="utf-8") as marker_file:
            marker_file.write("python-owned-canonical-dns-v2\n")
            marker_file.flush()
            os.fsync(marker_file.fileno())
        inventory_stat = os.stat(inventory_path)
        os.chmod(temporary_marker_path, 0o600)
        try:
            os.chown(
                temporary_marker_path,
                inventory_stat.st_uid,
                inventory_stat.st_gid,
            )
        except PermissionError:
            if (
                inventory_stat.st_uid != os.getuid()
                or inventory_stat.st_gid != os.getgid()
            ):
                raise
        os.replace(temporary_marker_path, ownership_marker_path)
        temporary_marker_path = None
        fsync_directory(cluster_directory)
    finally:
        if (
            temporary_marker_path is not None
            and os.path.exists(temporary_marker_path)
        ):
            os.unlink(temporary_marker_path)
    return True

def finish_pending_managed_pool_dns_ownership_transfer(
    inventory_path,
    ownership,
    expected_cluster_name,
    instance_pool_id=None,
    max_wait_seconds=120,
    compute_cluster_id=None,
):
    """Finish a journaled Terraform state release before DNS cleanup."""
    validate_instance_pool_dns_ownership_identity(
        ownership,
        expected_cluster_name,
        instance_pool_id,
        require_terraform_state_released=False,
        compute_cluster_id=compute_cluster_id,
    )
    if ownership.get("terraform_state_released", True) is True:
        return ownership
    cluster_directory = os.path.dirname(os.path.abspath(inventory_path))
    try:
        listed_state = subprocess.run(
            ["terraform", "state", "list"],
            cwd=cluster_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=max_wait_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(
            "Failed to inspect pending Terraform DNS ownership: "+str(error)
        )
    if listed_state.returncode != 0:
        detail = (listed_state.stderr or listed_state.stdout or "").strip()
        raise RuntimeError(
            "Failed to inspect pending Terraform DNS ownership: "+detail
        )
    managed_addresses = [
        address for address in listed_state.stdout.splitlines()
        if (
            address == INSTANCE_POOL_OCI_DNS_RESOURCE_ADDRESS
            or address.startswith(INSTANCE_POOL_OCI_DNS_RESOURCE_ADDRESS+"[")
        )
    ]
    if managed_addresses:
        ownership_zone_ids = {
            rrset["zone_id"] for rrset in ownership["rrsets"]
        }
        ownership_zone_names = {
            rrset["zone_name"] for rrset in ownership["rrsets"]
        }
        if len(ownership_zone_ids) != 1 or len(ownership_zone_names) != 1:
            raise RuntimeError(
                "Pending DNS ownership does not identify one private zone"
            )
        remaining_state_rrsets = get_terraform_managed_oci_name_dns_rrsets(
            inventory_path,
            managed_addresses,
            next(iter(ownership_zone_ids)),
            next(iter(ownership_zone_names)),
        )
        ownership_rrsets = {
            (rrset["zone_id"], rrset["domain"].lower()): rrset
            for rrset in ownership["rrsets"]
        }
        for rrset in remaining_state_rrsets:
            ownership_rrset = ownership_rrsets.get(
                (rrset["zone_id"], rrset["domain"].lower())
            )
            if ownership_rrset != rrset:
                raise RuntimeError(
                    "Pending DNS ownership no longer matches Terraform state"
                )
        refuse_terraform_state_mutation_while_locked(cluster_directory)
        try:
            removed_state = subprocess.run(
                ["terraform", "state", "rm", "-lock-timeout=60s"]+
                managed_addresses,
                cwd=cluster_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=max_wait_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                "Failed to finish Terraform DNS ownership release: "+str(error)
            )
        if removed_state.returncode != 0:
            detail = (removed_state.stderr or removed_state.stdout or "").strip()
            raise RuntimeError(
                "Failed to finish Terraform DNS ownership release: "+detail
            )
    released_ownership = dict(ownership)
    released_ownership["terraform_state_released"] = True
    write_instance_pool_name_dns_ownership(
        inventory_path,
        released_ownership,
    )
    return released_ownership

def collect_instance_pool_os_hostnames(inventory_path, max_wait_seconds=1800):
    inventory_dict = parse_inventory(inventory_path)
    if inventory_dict is None:
        raise RuntimeError("Inventory file "+inventory_path+" was not found")
    inventory_hosts = get_instance_pool_inventory_hosts(inventory_dict)
    if not inventory_hosts:
        return {}
    hosts_by_alias = {
        host["inventory_hostname"]: (instance_id, host)
        for instance_id, host in inventory_hosts.items()
    }
    my_env = os.environ.copy()
    my_env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    my_env["ANSIBLE_NOCOLOR"] = "True"
    with tempfile.TemporaryDirectory(prefix="oci-hpc-hostname-facts-") as facts_directory:
        os.chmod(facts_directory, 0o700)
        command = [
            "ansible",
            "compute",
            "-i",
            inventory_path,
            "-m",
            "setup",
            "-a",
            "filter=ansible_hostname",
            "--tree",
            facts_directory,
        ]
        try:
            completed = subprocess.run(
                command,
                env=my_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=max_wait_seconds,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Timed out while collecting final OS hostnames with Ansible")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            if len(detail) > 500:
                detail = detail[-500:]
            raise RuntimeError(
                "Ansible could not collect the final OS hostname from every Instance Pool member"+
                (": "+detail if detail else "")
            )
        fact_files = {
            entry.name: entry.path
            for entry in os.scandir(facts_directory)
            if entry.is_file(follow_symlinks=False)
        }
        expected_aliases = set(hosts_by_alias)
        if set(fact_files) != expected_aliases:
            missing = expected_aliases.difference(fact_files)
            unexpected = set(fact_files).difference(expected_aliases)
            details = []
            if missing:
                details.append("missing: "+", ".join(sorted(missing)))
            if unexpected:
                details.append("unexpected: "+", ".join(sorted(unexpected)))
            raise RuntimeError("Ansible hostname facts are incomplete ("+"; ".join(details)+")")

        observed_hostnames = {}
        for inventory_hostname, fact_path in fact_files.items():
            try:
                with open(fact_path, "r", encoding="utf-8") as fact_file:
                    result = json.load(fact_file)
            except (OSError, ValueError) as error:
                raise RuntimeError(
                    "Cannot read Ansible hostname facts for "+inventory_hostname+": "+str(error)
                )
            if not isinstance(result, dict):
                raise RuntimeError(
                    "Ansible returned malformed hostname facts for "+inventory_hostname
                )
            if result.get("failed") or result.get("unreachable"):
                raise RuntimeError(
                    "Ansible did not obtain the final OS hostname for "+inventory_hostname
                )
            ansible_facts = result.get("ansible_facts")
            hostname = validate_os_hostname(
                ansible_facts.get("ansible_hostname")
                if isinstance(ansible_facts, dict)
                else None
            )
            instance_id, inventory_host = hosts_by_alias[inventory_hostname]
            observed_hostnames[instance_id] = {
                "hostname": hostname,
                "private_ip": inventory_host["private_ip"],
                "inventory_hostname": inventory_hostname,
            }
    return observed_hostnames

def replace_inventory_hostname(line, hostname):
    parsed_line = split_inventory_host_line(line)
    if parsed_line is None:
        return line
    leading_whitespace, tokens, comment = parsed_line
    tokens[0] = hostname
    tokens = [token for token in tokens if not token.startswith("desired_hostname=")]
    rewritten = leading_whitespace+" ".join(tokens)
    if comment:
        rewritten += " "+comment
    return rewritten+("\n" if line.endswith("\n") else "")

def validate_instance_pool_name_plan(
    inventory_dict,
    desired_names_by_instance_id,
    private_ips_by_instance_id=None,
):
    desired_names = list(desired_names_by_instance_id.values())
    for desired_name in desired_names:
        validate_os_hostname(desired_name)
    normalized_desired_names = {name.lower() for name in desired_names}
    if len(normalized_desired_names) != len(desired_names):
        raise RuntimeError("Observed Instance Pool OS hostnames are not unique")
    target_instance_ids = set(desired_names_by_instance_id)
    target_compute_aliases = set()
    for section in ["compute_configured", "compute_to_add"]:
        for line in inventory_dict.get(section, []):
            parsed_line = split_inventory_host_line(line)
            if parsed_line is None or not parsed_line[1]:
                continue
            if get_inventory_token(line, "oci_instance_id") in target_instance_ids:
                target_compute_aliases.add(parsed_line[1][0].lower())
    reserved_aliases = set()
    for section in [
        "controller",
        "slurm_backup",
        "login",
        "compute_configured",
        "compute_to_add",
        "compute_to_destroy",
        "nfs",
    ]:
        for line in inventory_dict.get(section, []):
            parsed_line = split_inventory_host_line(line)
            if parsed_line is None or not parsed_line[1]:
                continue
            instance_id = get_inventory_token(line, "oci_instance_id")
            if instance_id in target_instance_ids:
                continue
            normalized_alias = parsed_line[1][0].lower()
            # [nfs] intentionally repeats one compute alias but does not carry
            # oci_instance_id.  It is the same host, not a reserved external
            # name, and must remain valid on repeated/no-op synchronizations.
            if section == "nfs" and normalized_alias in target_compute_aliases:
                continue
            reserved_aliases.add(normalized_alias)
    collisions = normalized_desired_names.intersection(reserved_aliases)
    if collisions:
        raise RuntimeError("Observed Instance Pool OS hostname conflicts with inventory: "+", ".join(sorted(collisions)))
    if private_ips_by_instance_id is not None:
        slurm_alias_owners = {}
        for instance_id, private_ip in private_ips_by_instance_id.items():
            slurm_domain = get_instance_pool_slurm_dns_domain(
                inventory_dict,
                private_ip,
            )
            if slurm_domain is not None:
                slurm_alias_owners[slurm_domain.split(".", 1)[0].lower()] = (
                    instance_id,
                    str(ipaddress.ip_address(private_ip)),
                )
        for instance_id, desired_name in desired_names_by_instance_id.items():
            slurm_owner = slurm_alias_owners.get(desired_name.lower())
            if (
                slurm_owner is not None
                and slurm_owner[1] != str(
                    ipaddress.ip_address(private_ips_by_instance_id[instance_id])
                )
            ):
                raise RuntimeError(
                    "Observed Instance Pool OS hostname conflicts with another node's Slurm DNS alias"
                )

def get_oci_retry_kwargs():
    retry_module = getattr(oci, "retry", None)
    retry_strategy = getattr(retry_module, "DEFAULT_RETRY_STRATEGY", None)
    return {"retry_strategy": retry_strategy} if retry_strategy is not None else {}

def normalize_oci_state(value):
    if not isinstance(value, str):
        return None
    return value.upper()

def get_complete_instance_pool_instances(
    compartment_id,
    instance_pool_id,
    max_wait_seconds=60,
):
    deadline = time.time()+max_wait_seconds
    retry_kwargs = get_oci_retry_kwargs()
    last_observation = "no Instance Pool response was received"
    while True:
        instance_pool = computeManagementClient.get_instance_pool(
            instance_pool_id,
            **retry_kwargs,
        ).data
        lifecycle_state = getattr(instance_pool, "lifecycle_state", "RUNNING")
        normalized_pool_state = normalize_oci_state(lifecycle_state)
        desired_size = getattr(instance_pool, "size", None)
        current_size = getattr(instance_pool, "current_size", None)
        member_summaries = oci.pagination.list_call_get_all_results(
            computeManagementClient.list_instance_pool_instances,
            compartment_id=compartment_id,
            instance_pool_id=instance_pool_id,
            **retry_kwargs,
        ).data
        member_ids = {member.id for member in member_summaries}
        member_lifecycle_states = [
            str(getattr(member, "lifecycle_state", None))
            for member in member_summaries
        ]
        member_instance_states = [
            str(getattr(member, "state", None))
            for member in member_summaries
        ]
        members_are_ready = (
            isinstance(desired_size, int)
            and not isinstance(desired_size, bool)
            and desired_size >= 0
            and (
                current_size is None
                or current_size == desired_size
            )
            and len(member_ids) == len(member_summaries)
            and len(member_summaries) == desired_size
            and all(
                (
                    # list_instance_pool_instances returns InstanceSummary,
                    # which normally has state but no lifecycle_state.  Some
                    # compatible responses expose the attachment state; when
                    # they do, it must already be ACTIVE.
                    normalize_oci_state(
                        getattr(member, "lifecycle_state", None)
                    ) in [None, "ACTIVE"]
                    and normalize_oci_state(
                        getattr(member, "state", None)
                    ) == "RUNNING"
                )
                for member in member_summaries
            )
        )
        instances = []
        vnic_error = None
        full_instance_states = []
        if members_are_ready:
            for member in member_summaries:
                full_instance = computeClient.get_instance(
                    member.id,
                    **retry_kwargs,
                ).data
                full_instance_states.append(
                    str(getattr(full_instance, "lifecycle_state", None))
                )
                if (
                    getattr(full_instance, "id", member.id) != member.id
                    or normalize_oci_state(
                        getattr(full_instance, "lifecycle_state", None)
                    ) != "RUNNING"
                ):
                    members_are_ready = False
                    break
                try:
                    private_ip = get_instance_primary_private_ip(
                        compartment_id,
                        member.id,
                    )
                except RuntimeError as error:
                    members_are_ready = False
                    vnic_error = str(error)
                    break
                instances.append({
                    "display_name": full_instance.display_name,
                    "ip": private_ip,
                    "ocid": member.id,
                })
        instances_by_id = {instance["ocid"]: instance for instance in instances}
        if len(instances_by_id) != len(instances):
            raise RuntimeError("The Instance Pool member list contains duplicate OCIDs")
        private_ips = {instance["ip"] for instance in instances}
        if len(private_ips) != len(instances):
            raise RuntimeError("The Instance Pool member list contains duplicate private IPs")
        if (
            normalized_pool_state == "RUNNING"
            and members_are_ready
            and set(instances_by_id) == member_ids
        ):
            return instances, instances_by_id
        last_observation = (
            "pool_state="+str(lifecycle_state)+
            ", desired_size="+str(desired_size)+
            ", current_size="+str(current_size)+
            ", member_count="+str(len(member_summaries))+
            ", member_lifecycle_states="+
            repr(sorted(member_lifecycle_states))+
            ", member_instance_states="+
            repr(sorted(member_instance_states))+
            ", full_instance_states="+
            repr(sorted(full_instance_states))+
            (", vnic="+vnic_error if vnic_error else "")
        )
        if time.time() >= deadline:
            raise RuntimeError(
                "The Instance Pool member list is incomplete ("+
                last_observation+")"
            )
        time.sleep(min(2, max(0, deadline-time.time())))

def get_complete_cluster_network_instances(
    compartment_id,
    cluster_network_id,
    instance_pool_id,
    expected_cluster_name,
    max_wait_seconds=60,
):
    """Return one stable snapshot shared by the parent CN and embedded pool."""
    deadline = time.time()+max_wait_seconds
    retry_kwargs = get_oci_retry_kwargs()
    last_observation = "no Cluster Network response was received"
    while True:
        cluster_network = computeManagementClient.get_cluster_network(
            cluster_network_id,
            **retry_kwargs,
        ).data
        embedded_pools = list(
            getattr(cluster_network, "instance_pools", None) or []
        )
        if (
            getattr(cluster_network, "id", cluster_network_id)
            != cluster_network_id
            or getattr(cluster_network, "compartment_id", None)
            != compartment_id
            or getattr(cluster_network, "display_name", None)
            != expected_cluster_name
            or len(embedded_pools) != 1
            or getattr(embedded_pools[0], "id", None) != instance_pool_id
        ):
            raise RuntimeError(
                "The Cluster Network identity or embedded Instance Pool does not match this cluster"
            )
        network_state = normalize_oci_state(
            getattr(cluster_network, "lifecycle_state", None)
        )
        embedded_pool = embedded_pools[0]
        embedded_size = getattr(embedded_pool, "size", None)
        instance_pool = computeManagementClient.get_instance_pool(
            instance_pool_id,
            **retry_kwargs,
        ).data
        pool_state = normalize_oci_state(
            getattr(instance_pool, "lifecycle_state", None)
        )
        pool_size = getattr(instance_pool, "size", None)
        pool_current_size = getattr(instance_pool, "current_size", None)
        if (
            getattr(instance_pool, "id", instance_pool_id) != instance_pool_id
            or getattr(instance_pool, "compartment_id", None) != compartment_id
            or getattr(instance_pool, "display_name", None)
            != expected_cluster_name
        ):
            raise RuntimeError(
                "The Cluster Network embedded Instance Pool identity does not match this cluster"
            )
        snapshots_are_ready = (
            network_state == "RUNNING"
            and pool_state == "RUNNING"
            and isinstance(pool_size, int)
            and not isinstance(pool_size, bool)
            and pool_size >= 0
            and (
                pool_current_size is None
                or pool_current_size == pool_size
            )
            and (
                embedded_size is None
                or (
                    isinstance(embedded_size, int)
                    and not isinstance(embedded_size, bool)
                    and embedded_size == pool_size
                )
            )
        )
        if snapshots_are_ready:
            remaining_seconds = max(0, deadline-time.time())
            try:
                instances, instances_by_id = (
                    get_complete_instance_pool_instances(
                        compartment_id,
                        instance_pool_id,
                        max_wait_seconds=remaining_seconds,
                    )
                )
            except RuntimeError as error:
                raise RuntimeError(
                    "The Cluster Network embedded Instance Pool is incomplete: "+
                    str(error)
                )
        else:
            instances = []
            instances_by_id = {}

        cluster_members = oci.pagination.list_call_get_all_results(
            computeManagementClient.list_cluster_network_instances,
            compartment_id=compartment_id,
            cluster_network_id=cluster_network_id,
            **retry_kwargs,
        ).data
        cluster_member_ids = {
            getattr(member, "id", None) for member in cluster_members
        }
        cluster_member_states = [
            str(getattr(member, "state", None)) for member in cluster_members
        ]
        cluster_members_are_ready = (
            None not in cluster_member_ids
            and len(cluster_member_ids) == len(cluster_members)
            and len(cluster_members) == pool_size
            and all(
                normalize_oci_state(getattr(member, "state", None))
                == "RUNNING"
                and normalize_oci_state(
                    getattr(member, "lifecycle_state", None)
                ) in [None, "ACTIVE"]
                and getattr(member, "compartment_id", compartment_id)
                == compartment_id
                for member in cluster_members
            )
        )
        if (
            snapshots_are_ready
            and cluster_members_are_ready
            and cluster_member_ids == set(instances_by_id)
        ):
            return instances, instances_by_id
        last_observation = (
            "network_state="+str(getattr(cluster_network, "lifecycle_state", None))+
            ", pool_state="+str(getattr(instance_pool, "lifecycle_state", None))+
            ", pool_size="+str(pool_size)+
            ", pool_current_size="+str(pool_current_size)+
            ", embedded_size="+str(embedded_size)+
            ", cluster_member_count="+str(len(cluster_members))+
            ", cluster_member_states="+repr(sorted(cluster_member_states))+
            ", pool_member_count="+str(len(instances_by_id))
        )
        if time.time() >= deadline:
            raise RuntimeError(
                "The Cluster Network member list is incomplete ("+
                last_observation+")"
            )
        time.sleep(min(2, max(0, deadline-time.time())))

def validate_compute_cluster_identity(
    compute_cluster,
    compute_cluster_id,
    compartment_id,
    expected_cluster_name,
):
    if (
        getattr(compute_cluster, "id", None) != compute_cluster_id
        or getattr(compute_cluster, "compartment_id", None) != compartment_id
        or getattr(compute_cluster, "display_name", None) != expected_cluster_name
        or normalize_oci_state(
            getattr(compute_cluster, "lifecycle_state", None)
        ) != "ACTIVE"
    ):
        raise RuntimeError(
            "The Terraform-tracked Compute Cluster identity is not an ACTIVE match for this cluster"
        )
    return compute_cluster

def validate_compute_cluster_cleanup_identity(
    compute_cluster,
    compute_cluster_id,
    compartment_id,
    expected_cluster_name,
):
    lifecycle_state = normalize_oci_state(
        getattr(compute_cluster, "lifecycle_state", None)
    )
    if (
        getattr(compute_cluster, "id", None) != compute_cluster_id
        or getattr(compute_cluster, "compartment_id", None) != compartment_id
        or getattr(compute_cluster, "display_name", None) != expected_cluster_name
        or lifecycle_state not in [
            "CREATING",
            "ACTIVE",
            "DELETING",
            "DELETED",
            "FAILED",
        ]
    ):
        raise RuntimeError(
            "The Terraform-tracked Compute Cluster identity is not a safe cleanup match"
        )
    return compute_cluster

def get_compute_cluster_instances_for_cleanup(
    compartment_id,
    compute_cluster_id,
    expected_cluster_name,
):
    """Resolve any available member IPs without requiring RUNNING state."""
    retry_kwargs = get_oci_retry_kwargs()
    compute_cluster = computeClient.get_compute_cluster(
        compute_cluster_id,
        **retry_kwargs,
    ).data
    validate_compute_cluster_cleanup_identity(
        compute_cluster,
        compute_cluster_id,
        compartment_id,
        expected_cluster_name,
    )
    parent_availability_domain = getattr(
        compute_cluster,
        "availability_domain",
        None,
    )
    listed_instances = oci.pagination.list_call_get_all_results(
        computeClient.list_instances,
        compartment_id=compartment_id,
        compute_cluster_id=compute_cluster_id,
        **retry_kwargs,
    ).data
    member_ids = [
        getattr(instance, "id", None)
        for instance in listed_instances
        if normalize_oci_state(
            getattr(instance, "lifecycle_state", None)
        ) != "TERMINATED"
    ]
    if None in member_ids or len(member_ids) != len(set(member_ids)):
        raise RuntimeError(
            "The Compute Cluster cleanup member list contains invalid or duplicate OCIDs"
        )
    resolved_instances = []
    for instance_id in member_ids:
        try:
            instance = computeClient.get_instance(
                instance_id,
                **retry_kwargs,
            ).data
        except oci.exceptions.ServiceError as error:
            if error.status == 404:
                continue
            raise
        if normalize_oci_state(
            getattr(instance, "lifecycle_state", None)
        ) == "TERMINATED":
            continue
        tags = getattr(instance, "freeform_tags", None) or {}
        parent_cluster = tags.get("parent_cluster") or tags.get("cluster_name")
        instance_availability_domain = getattr(
            instance,
            "availability_domain",
            None,
        )
        if (
            getattr(instance, "id", None) != instance_id
            or getattr(instance, "compartment_id", None) != compartment_id
            or (
                parent_cluster is not None
                and parent_cluster != expected_cluster_name
            )
            or (
                parent_availability_domain is not None
                and instance_availability_domain is not None
                and instance_availability_domain != parent_availability_domain
            )
        ):
            raise RuntimeError(
                "A Compute Cluster cleanup member does not match its exact parent"
            )
        try:
            private_ip = get_instance_primary_private_ip(
                compartment_id,
                instance_id,
                require_explicit_primary=True,
            )
        except oci.exceptions.ServiceError as error:
            if error.status not in [404, 409]:
                raise
            continue
        except RuntimeError:
            # A STOPPING/TERMINATING instance may already have detached its
            # VNIC. Inventory, pending plan, and DNS ledger still provide the
            # durable identities; do not make cluster destroy wait forever.
            continue
        resolved_instances.append({
            "display_name": instance.display_name,
            "ip": private_ip,
            "ocid": instance_id,
        })
    private_ips = [instance["ip"] for instance in resolved_instances]
    if len(private_ips) != len(set(private_ips)):
        raise RuntimeError(
            "The Compute Cluster cleanup member list contains duplicate private IPs"
        )
    return resolved_instances

def get_complete_compute_cluster_instances(
    compartment_id,
    compute_cluster_id,
    expected_cluster_name,
    expected_instance_ids=None,
    max_wait_seconds=60,
):
    """Return every live member of one exact Compute Cluster with Primary VNICs."""
    if expected_instance_ids is not None:
        expected_instance_ids = set(expected_instance_ids)
    deadline = time.time()+max_wait_seconds
    retry_kwargs = get_oci_retry_kwargs()
    last_observation = "no Compute Cluster response was received"
    while True:
        compute_cluster = computeClient.get_compute_cluster(
            compute_cluster_id,
            **retry_kwargs,
        ).data
        validate_compute_cluster_identity(
            compute_cluster,
            compute_cluster_id,
            compartment_id,
            expected_cluster_name,
        )
        parent_availability_domain = getattr(
            compute_cluster,
            "availability_domain",
            None,
        )
        listed_instances = oci.pagination.list_call_get_all_results(
            computeClient.list_instances,
            compartment_id=compartment_id,
            compute_cluster_id=compute_cluster_id,
            **retry_kwargs,
        ).data
        live_summaries = [
            instance for instance in listed_instances
            if normalize_oci_state(
                getattr(instance, "lifecycle_state", None)
            ) != "TERMINATED"
        ]
        member_ids = {
            getattr(instance, "id", None) for instance in live_summaries
        }
        members_are_ready = (
            None not in member_ids
            and len(member_ids) == len(live_summaries)
            and all(
                normalize_oci_state(
                    getattr(instance, "lifecycle_state", None)
                ) == "RUNNING"
                for instance in live_summaries
            )
            and (
                expected_instance_ids is None
                or member_ids == expected_instance_ids
            )
        )
        instances = []
        vnic_error = None
        if members_are_ready:
            for summary in live_summaries:
                instance_id = summary.id
                instance = computeClient.get_instance(
                    instance_id,
                    **retry_kwargs,
                ).data
                tags = getattr(instance, "freeform_tags", None) or {}
                parent_cluster = tags.get("parent_cluster") or tags.get("cluster_name")
                instance_availability_domain = getattr(
                    instance,
                    "availability_domain",
                    None,
                )
                if (
                    getattr(instance, "id", None) != instance_id
                    or getattr(instance, "compartment_id", None) != compartment_id
                    or normalize_oci_state(
                        getattr(instance, "lifecycle_state", None)
                    ) != "RUNNING"
                    or (
                        parent_cluster is not None
                        and parent_cluster != expected_cluster_name
                    )
                    or (
                        parent_availability_domain is not None
                        and instance_availability_domain is not None
                        and instance_availability_domain
                        != parent_availability_domain
                    )
                ):
                    raise RuntimeError(
                        "Compute Cluster member "+instance_id+
                        " does not match its exact parent cluster"
                    )
                try:
                    private_ip = get_instance_primary_private_ip(
                        compartment_id,
                        instance_id,
                        require_explicit_primary=True,
                    )
                except RuntimeError as error:
                    members_are_ready = False
                    vnic_error = str(error)
                    break
                instances.append({
                    "display_name": instance.display_name,
                    "ip": private_ip,
                    "ocid": instance_id,
                })
        instances_by_id = {
            instance["ocid"]: instance for instance in instances
        }
        private_ips = {instance["ip"] for instance in instances}
        if len(instances_by_id) != len(instances):
            raise RuntimeError("The Compute Cluster member list contains duplicate OCIDs")
        if len(private_ips) != len(instances):
            raise RuntimeError("The Compute Cluster member list contains duplicate private IPs")
        if members_are_ready and set(instances_by_id) == member_ids:
            return instances, instances_by_id
        last_observation = (
            "member_count="+str(len(live_summaries))+
            ", member_ids="+repr(sorted(member_ids, key=lambda value: str(value)))+
            ", member_states="+repr(sorted(
                str(getattr(instance, "lifecycle_state", None))
                for instance in live_summaries
            ))+
            (
                ", expected_instance_ids="+repr(sorted(expected_instance_ids))
                if expected_instance_ids is not None
                else ""
            )+
            (", vnic="+vnic_error if vnic_error else "")
        )
        if time.time() >= deadline:
            raise RuntimeError(
                "The Compute Cluster member list is incomplete ("+
                last_observation+")"
            )
        time.sleep(min(2, max(0, deadline-time.time())))

def update_instance_display_name_with_conflict_retry(
    instance_id,
    desired_name,
    max_wait_seconds=60,
):
    # The SDK default strategy handles its own retryable errors, but not the
    # generic Conflict returned while another UpdateInstance is in progress.
    # Reuse the same request and retry token for this additional, narrow retry.
    details = oci.core.models.UpdateInstanceDetails(display_name=desired_name)
    update_kwargs = {
        "opc_retry_token": str(uuid.uuid4()),
        **get_oci_retry_kwargs(),
    }
    deadline = time.monotonic()+max_wait_seconds
    max_attempts = 8
    for attempt in range(1, max_attempts+1):
        try:
            return computeClient.update_instance(
                instance_id,
                details,
                **update_kwargs,
            )
        except oci.exceptions.ServiceError as error:
            if not (
                error.status == 409
                and getattr(error, "code", None) == "Conflict"
                and re.fullmatch(
                    r"instance\b.*\bis currently being modified,\s*try again later\.?",
                    str(getattr(error, "message", "")).strip(),
                    re.IGNORECASE,
                )
            ):
                raise
            remaining = deadline-time.monotonic()
            if attempt == max_attempts or remaining <= 0:
                raise
            delay = min(2**attempt, 10, remaining)
            print(
                "STDOUT: Instance display name update is temporarily busy for "+
                instance_id+"; retrying in "+str(delay)+" seconds"
            )
            time.sleep(delay)
            # This bounds additional attempts, including slow SDK calls and
            # oversleep.  It does not interrupt an SDK call already in flight.
            if time.monotonic() >= deadline:
                raise

def update_instance_pool_display_names(
    compartment_id,
    instance_pool_id,
    expected_cluster_name,
    desired_names_by_instance_id,
    max_wait_seconds=60,
    before_updates=None,
    expected_private_ips_by_instance_id=None,
    cluster_network_id=None,
    compute_cluster_id=None,
):
    if cluster_network_id is not None and compute_cluster_id is not None:
        raise RuntimeError("Display name update received multiple parent cluster identities")
    if not desired_names_by_instance_id:
        if before_updates is not None:
            before_updates({})
        return {}
    retry_kwargs = get_oci_retry_kwargs()
    if compute_cluster_id is not None:
        _, cluster_instances_by_id = get_complete_compute_cluster_instances(
            compartment_id,
            compute_cluster_id,
            expected_cluster_name,
            expected_instance_ids=desired_names_by_instance_id,
            max_wait_seconds=max_wait_seconds,
        )
        pool_member_ids = set(cluster_instances_by_id)
    elif cluster_network_id is not None:
        _, cluster_instances_by_id = get_complete_cluster_network_instances(
            compartment_id,
            cluster_network_id,
            instance_pool_id,
            expected_cluster_name,
            max_wait_seconds=max_wait_seconds,
        )
        pool_member_ids = set(cluster_instances_by_id)
    else:
        summaries = oci.pagination.list_call_get_all_results(
            computeManagementClient.list_instance_pool_instances,
            compartment_id=compartment_id,
            instance_pool_id=instance_pool_id,
            **retry_kwargs,
        ).data
        pool_member_ids = {summary.id for summary in summaries}
    if pool_member_ids != set(desired_names_by_instance_id):
        raise RuntimeError(
            "Inventory instances are not active members of the exact Instance Pool membership; "
            "membership changed before display name updates"
        )
    if (
        expected_private_ips_by_instance_id is not None
        and set(expected_private_ips_by_instance_id)
        != set(desired_names_by_instance_id)
    ):
        raise RuntimeError(
            "Expected Primary VNIC private IPs do not exactly match the Instance Pool membership"
        )

    current_instances = {}
    current_primary_vnics = {}
    for instance_id, desired_name in desired_names_by_instance_id.items():
        if compute_cluster_id is None:
            pool_member = computeManagementClient.get_instance_pool_instance(
                instance_pool_id,
                instance_id,
                **retry_kwargs,
            ).data
            membership_state = getattr(pool_member, "lifecycle_state", None)
            normalized_membership_state = normalize_oci_state(membership_state)
            pool_instance_state = getattr(pool_member, "state", None)
            normalized_pool_instance_state = normalize_oci_state(
                pool_instance_state
            )
            if not (
                normalized_membership_state == "ACTIVE"
                or (
                    normalized_membership_state is None
                    and normalized_pool_instance_state == "RUNNING"
                )
            ):
                raise RuntimeError(
                    "Instance "+instance_id+" is not an ACTIVE Instance Pool member ("+
                    str(membership_state)+")"
                )
            if (
                pool_instance_state is not None
                and normalized_pool_instance_state != "RUNNING"
            ):
                raise RuntimeError("Instance "+instance_id+" is not RUNNING")
        instance = computeClient.get_instance(instance_id, **retry_kwargs).data
        tags = instance.freeform_tags or {}
        parent_cluster = tags.get("parent_cluster") or tags.get("cluster_name")
        if (
            instance.compartment_id != compartment_id
            or (
                parent_cluster is not None
                and parent_cluster != expected_cluster_name
            )
        ):
            raise RuntimeError("Instance "+instance_id+" does not belong to cluster "+expected_cluster_name)
        lifecycle_state = getattr(instance, "lifecycle_state", "RUNNING")
        if normalize_oci_state(lifecycle_state) != "RUNNING":
            raise RuntimeError("Instance "+instance_id+" is not RUNNING")
        primary_vnic_id, primary_vnic, primary_vnic_etag = get_instance_primary_vnic(
            compartment_id,
            instance_id,
            require_explicit_primary=True,
        )
        # A VNIC belongs to its subnet's compartment, which can differ from
        # the instance compartment when an existing shared VCN is used.  The
        # ATTACHED attachment, exact VNIC OCID, is_primary flag, and private IP
        # checks above/below establish identity without rejecting that layout.
        vnic_lifecycle_state = normalize_oci_state(
            getattr(primary_vnic, "lifecycle_state", None)
        )
        if vnic_lifecycle_state != "AVAILABLE":
            raise RuntimeError(
                "Primary VNIC "+primary_vnic_id+" is not AVAILABLE ("+
                str(getattr(primary_vnic, "lifecycle_state", None))+")"
            )
        primary_private_ip = get_vnic_private_ip(primary_vnic, instance_id)
        if expected_private_ips_by_instance_id is not None:
            try:
                expected_private_ip = str(ipaddress.ip_address(
                    expected_private_ips_by_instance_id[instance_id]
                ))
            except (TypeError, ValueError):
                raise RuntimeError(
                    "Instance "+instance_id+
                    " has an invalid expected Primary VNIC private IP"
                )
            if primary_private_ip != expected_private_ip:
                raise RuntimeError(
                    "Instance "+instance_id+
                    " Primary VNIC private IP changed before display name updates"
                )
        current_instances[instance_id] = instance
        current_primary_vnics[instance_id] = (
            primary_vnic_id,
            primary_vnic,
            primary_vnic_etag,
        )

    if compute_cluster_id is not None:
        _, final_cluster_instances_by_id = get_complete_compute_cluster_instances(
            compartment_id,
            compute_cluster_id,
            expected_cluster_name,
            expected_instance_ids=desired_names_by_instance_id,
            max_wait_seconds=max_wait_seconds,
        )
        if set(final_cluster_instances_by_id) != set(desired_names_by_instance_id):
            raise RuntimeError(
                "Compute Cluster membership changed after display name preflight"
            )
        for instance_id, current_instance in current_instances.items():
            if (
                final_cluster_instances_by_id[instance_id]["display_name"]
                != current_instance.display_name
            ):
                raise RuntimeError(
                    "Compute Cluster instance names changed after display name preflight"
                )
        if expected_private_ips_by_instance_id is not None:
            for instance_id, expected_private_ip in (
                expected_private_ips_by_instance_id.items()
            ):
                if str(ipaddress.ip_address(
                    final_cluster_instances_by_id[instance_id]["ip"]
                )) != str(ipaddress.ip_address(expected_private_ip)):
                    raise RuntimeError(
                        "Compute Cluster Primary VNIC private IP changed after preflight"
                    )
    elif cluster_network_id is not None:
        _, final_cluster_instances_by_id = get_complete_cluster_network_instances(
            compartment_id,
            cluster_network_id,
            instance_pool_id,
            expected_cluster_name,
            max_wait_seconds=max_wait_seconds,
        )
        if set(final_cluster_instances_by_id) != set(desired_names_by_instance_id):
            raise RuntimeError(
                "Cluster Network membership changed after display name preflight"
            )
        for instance_id, current_instance in current_instances.items():
            if (
                final_cluster_instances_by_id[instance_id]["display_name"]
                != current_instance.display_name
            ):
                raise RuntimeError(
                    "Cluster Network instance names changed after display name preflight"
                )
        if expected_private_ips_by_instance_id is not None:
            for instance_id, expected_private_ip in (
                expected_private_ips_by_instance_id.items()
            ):
                if str(ipaddress.ip_address(
                    final_cluster_instances_by_id[instance_id]["ip"]
                )) != str(ipaddress.ip_address(expected_private_ip)):
                    raise RuntimeError(
                        "Cluster Network Primary VNIC private IP changed after preflight"
                    )

    previous_names = {
        instance_id: instance.display_name
        for instance_id, instance in current_instances.items()
    }
    if before_updates is not None:
        before_updates(previous_names)
    for instance_id, desired_name in desired_names_by_instance_id.items():
        (
            primary_vnic_id,
            primary_vnic,
            primary_vnic_etag,
        ) = current_primary_vnics[instance_id]
        if getattr(primary_vnic, "display_name", None) != desired_name:
            original_private_ip = get_vnic_private_ip(
                primary_vnic,
                instance_id,
            )
            original_hostname_label = getattr(
                primary_vnic,
                "hostname_label",
                None,
            )
            # display_name is management metadata.  Do not update
            # hostname_label: that separate field controls the VCN DNS name
            # for the primary private IP.
            update_vnic_kwargs = dict(retry_kwargs)
            if primary_vnic_etag is not None:
                update_vnic_kwargs["if_match"] = primary_vnic_etag
            virtualNetworkClient.update_vnic(
                primary_vnic_id,
                oci.core.models.UpdateVnicDetails(display_name=desired_name),
                **update_vnic_kwargs,
            )
            deadline = time.time()+max_wait_seconds
            while True:
                updated_vnic = virtualNetworkClient.get_vnic(
                    primary_vnic_id,
                    **retry_kwargs,
                ).data
                if (
                    getattr(updated_vnic, "id", primary_vnic_id)
                    != primary_vnic_id
                ):
                    raise RuntimeError(
                        "OCI returned a different Primary VNIC while updating "+
                        primary_vnic_id
                    )
                if (
                    get_vnic_private_ip(updated_vnic, instance_id)
                    != original_private_ip
                ):
                    raise RuntimeError(
                        "Primary VNIC private IP changed while updating display name for "+
                        instance_id
                    )
                if getattr(updated_vnic, "is_primary", None) is not True:
                    raise RuntimeError(
                        "Primary VNIC identity changed while updating display name for "+
                        instance_id
                    )
                if (
                    getattr(updated_vnic, "hostname_label", None)
                    != original_hostname_label
                ):
                    raise RuntimeError(
                        "Primary VNIC hostname_label changed while updating display name for "+
                        instance_id
                    )
                if getattr(updated_vnic, "display_name", None) == desired_name:
                    break
                if time.time() >= deadline:
                    raise RuntimeError(
                        "Timed out waiting for Primary VNIC display name update for "+
                        instance_id
                    )
                time.sleep(2)

        if current_instances[instance_id].display_name != desired_name:
            update_instance_display_name_with_conflict_retry(
                instance_id,
                desired_name,
                max_wait_seconds=max_wait_seconds,
            )
            deadline = time.time()+max_wait_seconds
            while True:
                updated_instance = computeClient.get_instance(
                    instance_id,
                    **retry_kwargs,
                ).data
                if updated_instance.display_name == desired_name:
                    break
                if time.time() >= deadline:
                    raise RuntimeError(
                        "Timed out waiting for Instance display name update for "+
                        instance_id
                    )
                time.sleep(2)
    if compute_cluster_id is not None:
        _, updated_cluster_instances_by_id = get_complete_compute_cluster_instances(
            compartment_id,
            compute_cluster_id,
            expected_cluster_name,
            expected_instance_ids=desired_names_by_instance_id,
            max_wait_seconds=max_wait_seconds,
        )
        if set(updated_cluster_instances_by_id) != set(desired_names_by_instance_id):
            raise RuntimeError(
                "Compute Cluster membership changed during display name updates"
            )
        for instance_id, desired_name in desired_names_by_instance_id.items():
            updated_member = updated_cluster_instances_by_id[instance_id]
            if updated_member["display_name"] != desired_name:
                raise RuntimeError(
                    "Compute Cluster Instance name did not remain stable after updates"
                )
            if expected_private_ips_by_instance_id is not None and (
                str(ipaddress.ip_address(updated_member["ip"]))
                != str(ipaddress.ip_address(
                    expected_private_ips_by_instance_id[instance_id]
                ))
            ):
                raise RuntimeError(
                    "Compute Cluster Primary VNIC private IP changed during display name updates"
                )
    elif cluster_network_id is not None:
        # Keep the durable plan until the parent CN and its embedded pool are
        # stable with the exact membership, IPs, and final Instance names.  A
        # scale started outside this process after the preflight must not let
        # the caller commit stale DNS or Inventory data.
        _, updated_cluster_instances_by_id = (
            get_complete_cluster_network_instances(
                compartment_id,
                cluster_network_id,
                instance_pool_id,
                expected_cluster_name,
                max_wait_seconds=max_wait_seconds,
            )
        )
        if set(updated_cluster_instances_by_id) != set(
            desired_names_by_instance_id
        ):
            raise RuntimeError(
                "Cluster Network membership changed during display name updates"
            )
        for instance_id, desired_name in desired_names_by_instance_id.items():
            updated_member = updated_cluster_instances_by_id[instance_id]
            if updated_member["display_name"] != desired_name:
                raise RuntimeError(
                    "Cluster Network Instance name did not remain stable after updates"
                )
            if expected_private_ips_by_instance_id is not None and (
                str(ipaddress.ip_address(updated_member["ip"]))
                != str(ipaddress.ip_address(
                    expected_private_ips_by_instance_id[instance_id]
                ))
            ):
                raise RuntimeError(
                    "Cluster Network Primary VNIC private IP changed during display name updates"
                )
    return previous_names

def rewrite_instance_pool_inventory_names(
    inventory_path,
    inventory_dict,
    desired_names_by_instance_id,
):
    rewritten_inventory = copy.deepcopy(inventory_dict)
    old_aliases = {}
    seen_instance_ids = set()
    for section in ["compute_configured", "compute_to_add"]:
        for index, line in enumerate(rewritten_inventory.get(section, [])):
            host_name, instance_id = get_inventory_instance_id(line)
            if instance_id is None or instance_id not in desired_names_by_instance_id:
                continue
            if instance_id in seen_instance_ids:
                raise RuntimeError("Instance "+instance_id+" occurs more than once in compute inventory")
            seen_instance_ids.add(instance_id)
            desired_name = desired_names_by_instance_id[instance_id]
            old_aliases[host_name] = desired_name
            rewritten_inventory[section][index] = replace_inventory_hostname(
                line,
                desired_name,
            )
    for index, line in enumerate(rewritten_inventory.get("nfs", [])):
        parsed_line = split_inventory_host_line(line)
        if parsed_line is None or not parsed_line[1]:
            continue
        desired_name = old_aliases.get(parsed_line[1][0])
        if desired_name:
            rewritten_inventory["nfs"][index] = replace_inventory_hostname(line, desired_name)
    if rewritten_inventory != inventory_dict:
        write_inventory_atomic(rewritten_inventory, inventory_path)
    return rewritten_inventory

def get_single_private_dns_zone_id(compartment_id, dns_zone_name):
    zones = dns_client.list_zones(
        compartment_id=compartment_id,
        name=dns_zone_name,
        zone_type="PRIMARY",
        scope="PRIVATE",
    ).data
    if len(zones) == 0:
        raise RuntimeError("Private DNS zone "+dns_zone_name+" was not found")
    if len(zones) != 1:
        raise RuntimeError(
            "Multiple private DNS zones named "+dns_zone_name+
            " were found; refusing an ambiguous RRset update"
        )
    return zones[0].id

def get_private_dns_a_records(zone_id, domain):
    try:
        rrset = dns_client.get_rr_set(
            zone_name_or_id=zone_id,
            domain=domain,
            rtype="A",
            scope="PRIVATE",
        ).data
    except oci.exceptions.ServiceError as error:
        if error.status == 404:
            return []
        raise
    return list(getattr(rrset, "items", []) or [])

def get_private_dns_a_record_ips(zone_id, domain):
    record_ips = set()
    for record in get_private_dns_a_records(zone_id, domain):
        rtype = getattr(record, "rtype", None)
        rdata = getattr(record, "rdata", None)
        try:
            record_ip = str(ipaddress.ip_address(rdata))
        except (TypeError, ValueError):
            raise RuntimeError(
                "Existing DNS RRset "+domain+" contains an invalid A record"
            )
        if rtype not in [None, "A"]:
            raise RuntimeError(
                "Existing DNS RRset "+domain+" contains an unexpected record type"
            )
        record_ips.add(record_ip)
    return record_ips

def delete_private_dns_a_rrset_if_owned(zone_id, domain, expected_private_ips):
    expected_private_ips = {
        str(ipaddress.ip_address(private_ip))
        for private_ip in expected_private_ips
    }
    record_ips = get_private_dns_a_record_ips(zone_id, domain)
    if not record_ips:
        return False
    if not expected_private_ips or not record_ips.issubset(expected_private_ips):
        raise RuntimeError(
            "Refusing to delete private DNS RRset "+domain+
            " because it no longer points to this Instance Pool"
        )
    delete_private_dns_rrset_if_present(zone_id, domain)
    return True

def verify_private_dns_a_rrset_ownership(zone_id, domain, expected_private_ips):
    expected_private_ips = {
        str(ipaddress.ip_address(private_ip))
        for private_ip in expected_private_ips
    }
    record_ips = get_private_dns_a_record_ips(zone_id, domain)
    if record_ips and (
        not expected_private_ips
        or not record_ips.issubset(expected_private_ips)
    ):
        raise RuntimeError(
            "Private DNS RRset "+domain+
            " no longer matches the Instance Pool DNS ownership file"
        )
    return record_ips

def preflight_instance_pool_name_dns(
    compartment_id,
    inventory_dict,
    instances_by_id,
    desired_names_by_instance_id,
    previous_names,
):
    """Refuse to overwrite an A record not attributable to this exact pool."""
    if not parse_bool(get_inventory_variable(inventory_dict, "dns_entries", "true")):
        return None
    dns_zone_name = get_inventory_variable(
        inventory_dict,
        "zone_name",
        get_inventory_variable(inventory_dict, "cluster_name")+".local",
    )
    zone_id = get_single_private_dns_zone_id(compartment_id, dns_zone_name)
    pool_private_ips = {
        str(ipaddress.ip_address(instance["ip"]))
        for instance in instances_by_id.values()
    }
    known_pool_aliases = {
        instance["display_name"].lower()
        for instance in instances_by_id.values()
        if instance.get("display_name")
    }
    known_pool_aliases.update(
        name.lower() for name in previous_names.values() if name
    )
    for section in ["compute_configured", "compute_to_add"]:
        for line in inventory_dict.get(section, []):
            parsed_line = split_inventory_host_line(line)
            if (
                parsed_line is not None
                and parsed_line[1]
                and get_inventory_token(line, "oci_instance_id") in instances_by_id
            ):
                known_pool_aliases.add(parsed_line[1][0].lower())

    for instance_id, desired_name in desired_names_by_instance_id.items():
        domain = desired_name+"."+dns_zone_name
        desired_ip = str(ipaddress.ip_address(instances_by_id[instance_id]["ip"]))
        record_ips = get_private_dns_a_record_ips(zone_id, domain)
        if not record_ips:
            continue
        same_target = record_ips == {desired_ip}
        known_pool_alias = desired_name.lower() in known_pool_aliases
        owned_pool_addresses = record_ips.issubset(pool_private_ips)
        if not same_target and not (known_pool_alias and owned_pool_addresses):
            raise RuntimeError(
                "Refusing to overwrite existing private DNS RRset "+domain+
                " because it is not owned by this Instance Pool"
            )
    return zone_id

def reconcile_instance_pool_name_dns(
    compartment_id,
    inventory_dict,
    instances_by_id,
    desired_names_by_instance_id,
    previous_names,
    protected_names=None,
    inventory_path=None,
    instance_pool_id=None,
    compute_cluster_id=None,
):
    expected_cluster_name = get_inventory_variable(inventory_dict, "cluster_name")
    ownership = (
        load_instance_pool_name_dns_ownership(inventory_path)
        if inventory_path is not None
        else None
    )
    if ownership is not None:
        if instance_pool_id is None and compute_cluster_id is None:
            raise RuntimeError(
                "Compute deployment OCID is required to reconcile persisted DNS ownership"
            )
        validate_instance_pool_dns_ownership_identity(
            ownership,
            expected_cluster_name,
            instance_pool_id,
            compute_cluster_id=compute_cluster_id,
        )
        # Validate the complete ledger before the first DNS mutation.
        for rrset in ownership["rrsets"]:
            verify_private_dns_a_rrset_ownership(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )

    if not parse_bool(get_inventory_variable(inventory_dict, "dns_entries", "true")):
        if ownership is not None:
            for rrset in ownership["rrsets"]:
                delete_private_dns_a_rrset_if_owned(
                    rrset["zone_id"],
                    rrset["domain"],
                    rrset["private_ips"],
                )
            write_instance_pool_name_dns_ownership(inventory_path, None)
        return
    dns_zone_name = get_inventory_variable(
        inventory_dict,
        "zone_name",
        expected_cluster_name+".local",
    )
    zone_id = preflight_instance_pool_name_dns(
        compartment_id,
        inventory_dict,
        instances_by_id,
        desired_names_by_instance_id,
        previous_names,
    )
    pool_private_ips = {
        str(ipaddress.ip_address(instance["ip"]))
        for instance in instances_by_id.values()
    }
    desired_rrsets = {}
    externally_owned_keys = set()
    for instance_id, desired_name in desired_names_by_instance_id.items():
        domain = desired_name+"."+dns_zone_name
        private_ip = str(ipaddress.ip_address(instances_by_id[instance_id]["ip"]))
        # When the final OS hostname intentionally equals this same node's
        # Terraform-owned Slurm alias, that existing RRset already has the
        # correct address.  Do not make the RRset dual-owned by updating it.
        slurm_domain = get_instance_pool_slurm_dns_domain(
            inventory_dict,
            private_ip,
            dns_zone_name,
        )
        if (
            parse_bool(get_inventory_variable(inventory_dict, "slurm", "false"))
            and slurm_domain is not None
            and domain.lower() == slurm_domain.lower()
        ):
            externally_owned_keys.add((zone_id, domain.lower()))
            continue
        desired_rrsets[(zone_id, domain.lower())] = {
            "zone_id": zone_id,
            "zone_name": dns_zone_name,
            "domain": domain,
            "private_ips": [private_ip],
        }
    protected_domains = {
        (name+"."+dns_zone_name).lower()
        for name in (
            protected_names
            if protected_names is not None
            else desired_names_by_instance_id.values()
        )
    }
    # Never remove a still-supported alias just because a previous OCI display
    # name happened to equal it.  This matters when a later Ansible run changes
    # an OS hostname that previously matched the Slurm DNS naming convention.
    for section in ["controller", "slurm_backup", "login"]:
        for line in inventory_dict.get(section, []):
            parsed_line = split_inventory_host_line(line)
            if parsed_line is not None and parsed_line[1]:
                protected_domains.add(
                    (parsed_line[1][0]+"."+dns_zone_name).lower()
                )
    if parse_bool(get_inventory_variable(inventory_dict, "slurm", "false")):
        queue_name = get_inventory_variable(inventory_dict, "queue")
        instance_type_name = get_inventory_variable(inventory_dict, "instance_type")
        private_subnet = get_inventory_variable(inventory_dict, "private_subnet")
        if not queue_name or not instance_type_name or not private_subnet:
            raise RuntimeError("Inventory is missing data required to protect Slurm DNS records")
        try:
            private_network = ipaddress.ip_network(private_subnet)
        except ValueError:
            raise RuntimeError("Inventory has an invalid private_subnet")
        for instance in instances_by_id.values():
            private_ip = ipaddress.ip_address(instance["ip"])
            if private_ip not in private_network:
                raise RuntimeError("An Instance Pool private IP is outside private_subnet")
            host_index = int(private_ip)-int(private_network.network_address)+1
            protected_domains.add(
                (
                    queue_name+"-"+instance_type_name+"-"+str(host_index)+"."+
                    dns_zone_name
                ).lower()
            )
    previous_name_candidates = set(previous_names.values())
    for section in ["compute_configured", "compute_to_add"]:
        for line in inventory_dict.get(section, []):
            host_name, instance_id = get_inventory_instance_id(line)
            if instance_id in desired_names_by_instance_id:
                previous_name_candidates.add(host_name)

    pending_rrsets = {}
    if ownership is not None:
        for rrset in ownership["rrsets"]:
            pending_rrsets[(rrset["zone_id"], rrset["domain"].lower())] = {
                **rrset,
                "private_ips": list(rrset["private_ips"]),
            }
    for previous_name in previous_name_candidates:
        previous_domain = previous_name+"."+dns_zone_name
        if previous_domain.lower() not in protected_domains:
            record_ips = verify_private_dns_a_rrset_ownership(
                zone_id,
                previous_domain,
                pool_private_ips,
            )
            if record_ips:
                key = (zone_id, previous_domain.lower())
                pending_rrsets[key] = {
                    "zone_id": zone_id,
                    "zone_name": dns_zone_name,
                    "domain": previous_domain,
                    "private_ips": sorted(record_ips),
                }

    # Persist both old and planned ownership before the first write.  A retry
    # can therefore clean up either side of a zone/hostname transition even if
    # the process stops between individual RRset calls.
    for key, desired_rrset in desired_rrsets.items():
        if key in pending_rrsets:
            pending_rrsets[key] = {
                **desired_rrset,
                "private_ips": sorted(
                    set(pending_rrsets[key]["private_ips"])
                    |set(desired_rrset["private_ips"])
                ),
            }
        else:
            pending_rrsets[key] = desired_rrset
    if inventory_path is not None:
        write_instance_pool_name_dns_ownership(
            inventory_path,
            build_name_dns_ownership_document(
                expected_cluster_name,
                list(pending_rrsets.values()),
                instance_pool_id=instance_pool_id,
                compute_cluster_id=compute_cluster_id,
            ),
        )

    for desired_rrset in desired_rrsets.values():
        private_ip = desired_rrset["private_ips"][0]
        domain = desired_rrset["domain"]
        dns_client.update_rr_set(
            zone_name_or_id=zone_id,
            domain=domain,
            rtype="A",
            scope="PRIVATE",
            update_rr_set_details=oci.dns.models.UpdateRRSetDetails(
                items=[oci.dns.models.RecordDetails(
                    domain=domain,
                    rdata=private_ip,
                    rtype="A",
                    ttl=3600,
                )]
            ),
        )

    for key, rrset in pending_rrsets.items():
        if key not in desired_rrsets and key not in externally_owned_keys:
            delete_private_dns_a_rrset_if_owned(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )
    if inventory_path is not None:
        write_instance_pool_name_dns_ownership(
            inventory_path,
            build_name_dns_ownership_document(
                expected_cluster_name,
                list(desired_rrsets.values()),
                instance_pool_id=instance_pool_id,
                compute_cluster_id=compute_cluster_id,
            ),
        )

def get_tracked_instance_pool_id(inventory_path):
    state_path = os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        "terraform.tfstate",
    )
    if not os.path.isfile(state_path):
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError) as error:
        raise RuntimeError("Failed to read Terraform state for Instance Pool identity: "+str(error))
    instance_pool_ids = set()
    for resource in state.get("resources", []):
        if (
            resource.get("mode") != "managed"
            or resource.get("module") not in [None, ""]
            or resource.get("type") != "oci_core_instance_pool"
            or resource.get("name") != "instance_pool"
        ):
            continue
        for resource_instance in resource.get("instances", []):
            instance_pool_id = resource_instance.get("attributes", {}).get("id")
            if instance_pool_id:
                instance_pool_ids.add(instance_pool_id)
    if len(instance_pool_ids) > 1:
        raise RuntimeError("Terraform state contains multiple Instance Pool identities")
    return next(iter(instance_pool_ids), None)

def get_tracked_cluster_network_state_identity(inventory_path):
    state_path = os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        "terraform.tfstate",
    )
    if not os.path.isfile(state_path):
        return None, None
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Failed to read Terraform state for Cluster Network identity: "+
            str(error)
        )
    cluster_network_ids = set()
    embedded_instance_pool_ids = set()
    for resource in state.get("resources", []):
        if (
            resource.get("mode") != "managed"
            or resource.get("module") not in [None, ""]
            or resource.get("type") != "oci_core_cluster_network"
            or resource.get("name") != "cluster_network"
        ):
            continue
        for resource_instance in resource.get("instances", []):
            attributes = resource_instance.get("attributes", {})
            cluster_network_id = attributes.get("id")
            if cluster_network_id:
                cluster_network_ids.add(cluster_network_id)
            for embedded_pool in attributes.get("instance_pools", []) or []:
                if isinstance(embedded_pool, dict) and embedded_pool.get("id"):
                    embedded_instance_pool_ids.add(embedded_pool["id"])
    if len(cluster_network_ids) > 1:
        raise RuntimeError(
            "Terraform state contains multiple Cluster Network identities"
        )
    if len(embedded_instance_pool_ids) > 1:
        raise RuntimeError(
            "Terraform state contains multiple embedded Instance Pool identities"
        )
    if not cluster_network_ids and embedded_instance_pool_ids:
        raise RuntimeError(
            "Terraform state contains an incomplete Cluster Network identity"
        )
    return (
        next(iter(cluster_network_ids), None),
        next(iter(embedded_instance_pool_ids), None),
    )

def get_tracked_cluster_network_id(inventory_path):
    cluster_network_id, _ = get_tracked_cluster_network_state_identity(
        inventory_path
    )
    return cluster_network_id

def has_tracked_compute_cluster_resources(tracked_resources):
    return (
        tracked_resources is not None
        and (
            tracked_resources[0] is not None
            or bool(tracked_resources[1])
        )
    )

def get_tracked_managed_instance_pool_id(inventory_path):
    direct_instance_pool_id = get_tracked_instance_pool_id(inventory_path)
    cluster_network_id, embedded_instance_pool_id = (
        get_tracked_cluster_network_state_identity(inventory_path)
    )
    compute_cluster_resources = get_tracked_compute_cluster_resources(
        inventory_path
    )
    compute_cluster_is_present = has_tracked_compute_cluster_resources(
        compute_cluster_resources
    )
    tracked_deployment_count = sum(
        deployment_is_present
        for deployment_is_present in [
            direct_instance_pool_id is not None,
            cluster_network_id is not None,
            compute_cluster_is_present,
        ]
    )
    if tracked_deployment_count > 1:
        raise RuntimeError(
            "Terraform state identifies multiple compute deployment types"
        )
    return direct_instance_pool_id or embedded_instance_pool_id

def get_tracked_instance_pool_for_hostname_sync(
    inventory_path,
    compartment_id,
    expected_display_name,
):
    instance_pool_id = get_tracked_instance_pool_id(inventory_path)
    if instance_pool_id is None:
        raise RuntimeError(
            "Terraform state does not identify the Instance Pool for hostname synchronization"
        )
    instance_pool = computeManagementClient.get_instance_pool(
        instance_pool_id,
        **get_oci_retry_kwargs(),
    ).data
    if (
        getattr(instance_pool, "id", instance_pool_id) != instance_pool_id
        or getattr(instance_pool, "compartment_id", None) != compartment_id
        or getattr(instance_pool, "display_name", None) != expected_display_name
        or normalize_oci_state(
            getattr(instance_pool, "lifecycle_state", None)
        ) in ["TERMINATING", "TERMINATED"]
    ):
        raise RuntimeError(
            "The Terraform-tracked Instance Pool identity does not match this cluster"
        )
    return instance_pool

def get_tracked_cluster_network_for_hostname_sync(
    inventory_path,
    compartment_id,
    expected_display_name,
    require_running=True,
):
    cluster_network_id, state_instance_pool_id = (
        get_tracked_cluster_network_state_identity(inventory_path)
    )
    if cluster_network_id is None or state_instance_pool_id is None:
        raise RuntimeError(
            "Terraform state does not identify the complete Cluster Network "
            "and embedded Instance Pool for hostname synchronization"
        )
    cluster_network = computeManagementClient.get_cluster_network(
        cluster_network_id,
        **get_oci_retry_kwargs(),
    ).data
    network_state = normalize_oci_state(
        getattr(cluster_network, "lifecycle_state", None)
    )
    embedded_pools = list(
        getattr(cluster_network, "instance_pools", None) or []
    )
    if (
        getattr(cluster_network, "id", cluster_network_id)
        != cluster_network_id
        or getattr(cluster_network, "compartment_id", None) != compartment_id
        or getattr(cluster_network, "display_name", None)
        != expected_display_name
        or len(embedded_pools) != 1
        or (
            require_running
            and network_state != "RUNNING"
        )
        or (
            not require_running
            and network_state not in ["RUNNING", "SCALING"]
        )
        or network_state in ["TERMINATING", "TERMINATED", "FAILED"]
    ):
        raise RuntimeError(
            "The Terraform-tracked Cluster Network identity is not a stable match for this cluster"
        )
    embedded_pool = embedded_pools[0]
    embedded_pool_id = getattr(embedded_pool, "id", None)
    embedded_pool_state = normalize_oci_state(
        getattr(embedded_pool, "lifecycle_state", None)
    )
    if (
        not embedded_pool_id
        or (
            state_instance_pool_id is not None
            and embedded_pool_id != state_instance_pool_id
        )
        or getattr(embedded_pool, "compartment_id", compartment_id)
        != compartment_id
        or getattr(embedded_pool, "display_name", expected_display_name)
        != expected_display_name
        or (
            require_running
            and embedded_pool_state not in [None, "RUNNING"]
        )
        or embedded_pool_state in ["TERMINATING", "TERMINATED", "FAILED"]
    ):
        raise RuntimeError(
            "The Terraform-tracked Cluster Network does not contain exactly the expected Instance Pool"
        )
    return cluster_network, embedded_pool

def get_tracked_compute_cluster_for_hostname_sync(
    inventory_path,
    compartment_id,
    expected_display_name,
):
    tracked_resources = get_tracked_compute_cluster_resources(inventory_path)
    if tracked_resources is None or tracked_resources[0] is None:
        if tracked_resources is not None and tracked_resources[1]:
            raise RuntimeError(
                "Terraform state identifies Compute Cluster instances without their exact parent"
            )
        raise RuntimeError(
            "Terraform state does not identify the Compute Cluster for hostname synchronization"
        )
    compute_cluster_id = tracked_resources[0]
    compute_cluster = computeClient.get_compute_cluster(
        compute_cluster_id,
        **get_oci_retry_kwargs(),
    ).data
    return validate_compute_cluster_identity(
        compute_cluster,
        compute_cluster_id,
        compartment_id,
        expected_display_name,
    )

def get_vnic_private_ip(vnic, instance_id):
    try:
        return str(ipaddress.ip_address(vnic.private_ip))
    except (AttributeError, ValueError):
        raise RuntimeError(
            "Instance "+instance_id+" has an invalid primary private IP"
        )

def get_instance_primary_vnic(
    compartment_id,
    instance_id,
    require_explicit_primary=False,
):
    retry_kwargs = get_oci_retry_kwargs()
    attachments = oci.pagination.list_call_get_all_results(
        computeClient.list_vnic_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id,
        **retry_kwargs,
    ).data
    active_attachments = []
    for attachment in attachments:
        attachment_state = getattr(attachment, "lifecycle_state", None)
        if (
            normalize_oci_state(attachment_state) == "ATTACHED"
            or (
                attachment_state is None
                and not require_explicit_primary
            )
        ):
            active_attachments.append(attachment)
    resolved_vnics = []
    for attachment in active_attachments:
        vnic_id = getattr(attachment, "vnic_id", None)
        if not vnic_id:
            continue
        vnic_response = virtualNetworkClient.get_vnic(
            vnic_id,
            **retry_kwargs,
        )
        response_headers = getattr(vnic_response, "headers", {}) or {}
        resolved_vnics.append((
            vnic_id,
            vnic_response.data,
            response_headers.get("etag") or response_headers.get("ETag"),
        ))
    primary_vnics = [
        (vnic_id, vnic, etag) for vnic_id, vnic, etag in resolved_vnics
        if getattr(vnic, "is_primary", None) is True
    ]
    # is_primary is the authoritative VNIC identity.  Older responses and
    # simple test doubles can omit it; a single active VNIC is still
    # unambiguous.  nic_index identifies the physical NIC, not whether a VNIC
    # is the instance's primary VNIC, so it must not be used for selection.
    if (
        not primary_vnics
        and not require_explicit_primary
        and len(resolved_vnics) == 1
        and getattr(resolved_vnics[0][1], "is_primary", None) is None
    ):
        primary_vnics = resolved_vnics
    if len(primary_vnics) != 1:
        raise RuntimeError("Instance "+instance_id+" does not have exactly one primary VNIC")
    primary_vnic_id, primary_vnic, primary_vnic_etag = primary_vnics[0]
    returned_vnic_id = getattr(primary_vnic, "id", None)
    if (
        returned_vnic_id not in [None, primary_vnic_id]
        or (
            require_explicit_primary
            and returned_vnic_id != primary_vnic_id
        )
    ):
        raise RuntimeError(
            "Instance "+instance_id+" returned an unexpected primary VNIC"
        )
    get_vnic_private_ip(primary_vnic, instance_id)
    return primary_vnic_id, primary_vnic, primary_vnic_etag

def get_instance_primary_private_ip(
    compartment_id,
    instance_id,
    require_explicit_primary=False,
):
    _, primary_vnic, _ = get_instance_primary_vnic(
        compartment_id,
        instance_id,
        require_explicit_primary=require_explicit_primary,
    )
    return get_vnic_private_ip(primary_vnic, instance_id)

def get_instance_pool_slurm_dns_domain(
    inventory_dict,
    private_ip,
    dns_zone_name=None,
):
    slurm_enabled_for_inventory = parse_bool(
        get_inventory_variable(inventory_dict, "slurm", "false")
    )
    if not slurm_enabled_for_inventory:
        return None
    queue_name = get_inventory_variable(inventory_dict, "queue")
    instance_type_name = get_inventory_variable(inventory_dict, "instance_type")
    private_subnet = get_inventory_variable(inventory_dict, "private_subnet")
    if not queue_name or not instance_type_name or not private_subnet:
        raise RuntimeError("Inventory is missing data required for Slurm DNS cleanup")
    try:
        private_network = ipaddress.ip_network(private_subnet)
        parsed_private_ip = ipaddress.ip_address(private_ip)
    except ValueError:
        raise RuntimeError("Inventory has invalid private subnet data for Slurm DNS cleanup")
    if parsed_private_ip not in private_network:
        raise RuntimeError("An Instance Pool private IP is outside private_subnet")
    if dns_zone_name is None:
        dns_zone_name = get_inventory_variable(
            inventory_dict,
            "zone_name",
            get_inventory_variable(inventory_dict, "cluster_name")+".local",
        )
    host_index = int(parsed_private_ip)-int(private_network.network_address)+1
    return (
        queue_name+"-"+instance_type_name+"-"+str(host_index)+"."+
        dns_zone_name
    )

def delete_instance_pool_node_dns_records(
    zone_id,
    inventory_dict,
    instance_names,
    private_ip,
):
    for domain in get_instance_pool_node_dns_domains(
        inventory_dict,
        instance_names,
        private_ip,
    ):
        delete_private_dns_rrset_if_present(zone_id, domain)

def get_instance_pool_node_dns_domains(
    inventory_dict,
    instance_names,
    private_ip,
):
    dns_zone_name = get_inventory_variable(
        inventory_dict,
        "zone_name",
        get_inventory_variable(inventory_dict, "cluster_name")+".local",
    )
    domains = {
        instance_name+"."+dns_zone_name
        for instance_name in instance_names
        if instance_name
    }
    slurm_domain = get_instance_pool_slurm_dns_domain(
        inventory_dict,
        private_ip,
        dns_zone_name,
    )
    if slurm_domain is not None:
        domains.add(slurm_domain)
    return sorted(domains)

def build_pending_instance_pool_node_removal(
    inventory_dict,
    compartment_id,
    instance_pool_id,
    instance_id,
    instance_display_name,
    instance_names,
    private_ip,
):
    cluster_name_for_record = get_inventory_variable(inventory_dict, "cluster_name")
    zone_name_for_record = get_inventory_variable(
        inventory_dict,
        "zone_name",
        cluster_name_for_record+".local",
    )
    names = sorted({name for name in instance_names if name})
    if instance_display_name not in names:
        names.append(instance_display_name)
        names.sort()
    record = {
        "cluster_name": cluster_name_for_record,
        "compartment_id": compartment_id,
        "instance_pool_id": instance_pool_id,
        "instance_id": instance_id,
        "instance_display_name": instance_display_name,
        "instance_names": names,
        "private_ip": str(ipaddress.ip_address(private_ip)),
        "zone_name": zone_name_for_record,
        "dns_domains": (
            get_instance_pool_node_dns_domains(
                inventory_dict,
                names,
                private_ip,
            )
            if parse_bool(get_inventory_variable(inventory_dict, "dns_entries", "true"))
            else []
        ),
    }
    return validate_pending_instance_pool_node_removal(record)

def build_instance_pool_removal_journal_plan(
    inventory_dict,
    compartment_id,
    instance_pool_id,
    current_instances,
    hostnames_to_remove,
    selected_instance_ids,
    existing_records=None,
):
    """Preflight and freeze every selected Instance Pool removal by OCID."""
    expected_cluster_name = get_inventory_variable(inventory_dict, "cluster_name")
    current_by_id = {}
    for instance in current_instances:
        instance_id = instance.get("ocid")
        if not instance_id or instance_id in current_by_id:
            raise RuntimeError(
                "The Instance Pool removal plan contains an invalid or duplicate OCID"
            )
        current_by_id[instance_id] = instance
    selected_instance_ids = set(selected_instance_ids)
    if not selected_instance_ids.issubset(set(current_by_id)):
        raise RuntimeError(
            "The Instance Pool removal plan contains a non-member instance"
        )
    inventory_hosts = get_instance_pool_inventory_hosts(inventory_dict)
    requested_names = set(hostnames_to_remove)
    if existing_records is None:
        existing_records = []
    existing_by_id = {
        record["instance_id"]: record for record in existing_records
    }
    if len(existing_by_id) != len(existing_records):
        raise RuntimeError(
            "The pending Instance Pool node removal contains duplicate instances"
        )
    planned_records = []
    seen_private_ips = set()
    seen_instance_names = set()
    for instance_id in sorted(selected_instance_ids):
        summary = current_by_id[instance_id]
        instance = computeClient.get_instance(instance_id).data
        instance_tags = instance.freeform_tags or {}
        parent_cluster = instance_tags.get(
            "parent_cluster",
            instance_tags.get("cluster_name"),
        )
        if (
            getattr(instance, "id", instance_id) != instance_id
            or instance.compartment_id != compartment_id
            or instance.lifecycle_state == "TERMINATED"
            or instance.display_name != summary.get("display_name")
            or parent_cluster not in [None, expected_cluster_name]
        ):
            raise RuntimeError(
                "An Instance Pool removal target has an invalid OCI identity"
            )
        try:
            summary_private_ip = str(ipaddress.ip_address(summary.get("ip")))
        except (TypeError, ValueError):
            raise RuntimeError(
                "An Instance Pool removal target has an invalid private IP"
            )
        live_private_ip = get_instance_primary_private_ip(
            compartment_id,
            instance_id,
        )
        if live_private_ip != summary_private_ip:
            raise RuntimeError(
                "An Instance Pool removal target changed private IP"
            )
        if summary_private_ip in seen_private_ips:
            raise RuntimeError(
                "The Instance Pool removal plan contains a duplicate private IP"
            )
        seen_private_ips.add(summary_private_ip)
        instance_names = {instance.display_name}
        inventory_host = inventory_hosts.get(instance_id)
        if inventory_host is not None:
            if inventory_host["private_ip"] != summary_private_ip:
                raise RuntimeError(
                    "An Instance Pool removal target does not match inventory IP"
                )
            instance_names.add(inventory_host["inventory_hostname"])
        if instance_id in existing_by_id:
            instance_names.update(existing_by_id[instance_id]["instance_names"])
        instance_names.update(
            name for name in requested_names
            if name in instance_names or name == summary.get("display_name")
        )
        if seen_instance_names.intersection(instance_names):
            raise RuntimeError(
                "The Instance Pool removal plan contains duplicate instance names"
            )
        seen_instance_names.update(instance_names)
        planned_records.append(
            build_pending_instance_pool_node_removal(
                inventory_dict,
                compartment_id,
                instance_pool_id,
                instance_id,
                instance.display_name,
                instance_names,
                summary_private_ip,
            )
        )

    planned_by_id = {
        record["instance_id"]: record for record in planned_records
    }
    if existing_records and (
        existing_by_id != planned_by_id
    ):
        raise RuntimeError(
            "The pending Instance Pool node removal changed unexpectedly"
        )
    return planned_records

def build_compute_cluster_removal_plan(
    inventory_dict,
    compartment_id,
    compute_cluster_id,
    expected_cluster_name,
    current_instances,
    selected_instance_ids,
    inventory_path,
):
    """Freeze exact CC removal identities before DNS or instance mutation."""
    compute_cluster = computeClient.get_compute_cluster(
        compute_cluster_id,
        **get_oci_retry_kwargs(),
    ).data
    validate_compute_cluster_identity(
        compute_cluster,
        compute_cluster_id,
        compartment_id,
        expected_cluster_name,
    )
    parent_availability_domain = getattr(
        compute_cluster,
        "availability_domain",
        None,
    )
    current_by_id = {}
    for summary in current_instances:
        instance_id = summary.get("ocid")
        if not instance_id or instance_id in current_by_id:
            raise RuntimeError(
                "The Compute Cluster removal plan contains an invalid or duplicate OCID"
            )
        current_by_id[instance_id] = summary
    selected_instance_ids = set(selected_instance_ids)
    if not selected_instance_ids.issubset(set(current_by_id)):
        raise RuntimeError(
            "The Compute Cluster removal plan contains a non-member instance"
        )
    state_external_instance_ids = (
        get_compute_cluster_state_external_member_ids(
            inventory_path,
            compute_cluster_id,
            current_instances,
        )
    )
    tracked_instance_ids = set(current_by_id)-state_external_instance_ids
    tracked_removal_ids = selected_instance_ids.intersection(
        tracked_instance_ids
    )
    if tracked_removal_ids:
        raise RuntimeError(
            "Routine Compute Cluster resize cannot remove Terraform-managed "
            "initial nodes safely; delete and recreate the cluster instead"
        )
    inventory_hosts = get_instance_pool_inventory_hosts(inventory_dict)
    planned_records = []
    seen_private_ips = set()
    seen_names = set()
    for instance_id in sorted(selected_instance_ids):
        summary = current_by_id[instance_id]
        instance = computeClient.get_instance(
            instance_id,
            **get_oci_retry_kwargs(),
        ).data
        tags = getattr(instance, "freeform_tags", None) or {}
        parent_cluster = tags.get("parent_cluster") or tags.get("cluster_name")
        instance_availability_domain = getattr(
            instance,
            "availability_domain",
            None,
        )
        if (
            getattr(instance, "id", None) != instance_id
            or getattr(instance, "compartment_id", None) != compartment_id
            or normalize_oci_state(
                getattr(instance, "lifecycle_state", None)
            ) == "TERMINATED"
            or instance.display_name != summary.get("display_name")
            or (
                parent_cluster is not None
                and parent_cluster != expected_cluster_name
            )
            or (
                parent_availability_domain is not None
                and instance_availability_domain is not None
                and instance_availability_domain != parent_availability_domain
            )
        ):
            raise RuntimeError(
                "A Compute Cluster removal target has an invalid OCI identity"
            )
        try:
            summary_private_ip = str(ipaddress.ip_address(summary.get("ip")))
        except (TypeError, ValueError):
            raise RuntimeError(
                "A Compute Cluster removal target has an invalid private IP"
            )
        live_private_ip = get_instance_primary_private_ip(
            compartment_id,
            instance_id,
            require_explicit_primary=True,
        )
        if live_private_ip != summary_private_ip:
            raise RuntimeError(
                "A Compute Cluster removal target changed private IP"
            )
        if summary_private_ip in seen_private_ips:
            raise RuntimeError(
                "The Compute Cluster removal plan contains a duplicate private IP"
            )
        seen_private_ips.add(summary_private_ip)
        instance_names = {instance.display_name}
        inventory_host = inventory_hosts.get(instance_id)
        if inventory_host is not None:
            if inventory_host["private_ip"] != summary_private_ip:
                raise RuntimeError(
                    "A Compute Cluster removal target does not match inventory IP"
                )
            instance_names.add(inventory_host["inventory_hostname"])
        if seen_names.intersection(instance_names):
            raise RuntimeError(
                "The Compute Cluster removal plan contains duplicate instance names"
            )
        seen_names.update(instance_names)
        planned_records.append({
            "instance_id": instance_id,
            "instance_display_name": instance.display_name,
            "instance_names": sorted(instance_names),
            "private_ip": summary_private_ip,
            "delete_launch_created_data_volumes": (
                instance_has_exclusive_managed_local_block_volume(
                    compartment_id,
                    instance_id,
                )
            ),
        })
    return planned_records

def get_compute_cluster_state_external_member_ids(
    inventory_path,
    compute_cluster_id,
    current_instances,
):
    """Prove which live CC members are outside Terraform instance state."""
    cluster_directory = os.path.dirname(os.path.abspath(inventory_path))
    refuse_terraform_state_mutation_while_locked(cluster_directory)
    current_instance_ids = []
    for instance in current_instances:
        instance_id = instance.get("ocid")
        if not isinstance(instance_id, str) or not instance_id:
            raise RuntimeError(
                "A Compute Cluster member has an invalid OCID"
            )
        current_instance_ids.append(instance_id)
    if len(current_instance_ids) != len(set(current_instance_ids)):
        raise RuntimeError(
            "The Compute Cluster member list contains duplicate OCIDs"
        )
    tracked_resources = get_tracked_compute_cluster_resources(inventory_path)
    if (
        tracked_resources is None
        or tracked_resources[0] != compute_cluster_id
        or not tracked_resources[1]
    ):
        raise RuntimeError(
            "Terraform state does not prove the exact Compute Cluster members"
        )
    tracked_instance_ids = set(tracked_resources[1])
    current_instance_ids = set(current_instance_ids)
    if not tracked_instance_ids.issubset(current_instance_ids):
        raise RuntimeError(
            "A Terraform-managed Compute Cluster instance is no longer a live member"
        )
    return current_instance_ids-tracked_instance_ids

def build_compute_cluster_node_removal_journal(
    expected_cluster_name,
    compartment_id,
    compute_cluster_id,
    current_instances,
    planned_records,
    no_reconfigure,
):
    source_instance_ids = []
    for instance in current_instances:
        instance_id = instance.get("ocid")
        if not isinstance(instance_id, str) or not instance_id:
            raise RuntimeError(
                "A Compute Cluster source member has an invalid OCID"
            )
        source_instance_ids.append(instance_id)
    return validate_compute_cluster_node_removal_journal({
        "version": 1,
        "status": "pending",
        "cluster_name": expected_cluster_name,
        "compartment_id": compartment_id,
        "compute_cluster_id": compute_cluster_id,
        "no_reconfigure": bool(no_reconfigure),
        "source_instance_ids": sorted(source_instance_ids),
        "removals": list(planned_records),
    })

def inspect_compute_cluster_node_removal_journal(
    journal,
    expected_cluster_name,
    compartment_id,
    compute_cluster_id,
    require_source_boundary=True,
):
    """Validate the exact live boundary for an initial or resumed CC removal."""
    journal = validate_compute_cluster_node_removal_journal(journal)
    if (
        journal["cluster_name"] != expected_cluster_name
        or journal["compartment_id"] != compartment_id
        or journal["compute_cluster_id"] != compute_cluster_id
    ):
        raise RuntimeError(
            "The pending Compute Cluster node removal belongs to another cluster"
        )
    compute_cluster = computeClient.get_compute_cluster(
        compute_cluster_id,
        **get_oci_retry_kwargs(),
    ).data
    validate_compute_cluster_identity(
        compute_cluster,
        compute_cluster_id,
        compartment_id,
        expected_cluster_name,
    )
    parent_availability_domain = getattr(
        compute_cluster,
        "availability_domain",
        None,
    )
    members = oci.pagination.list_call_get_all_results(
        computeClient.list_instances,
        compartment_id=compartment_id,
        compute_cluster_id=compute_cluster_id,
    ).data
    members_by_id = {}
    nonterminated_member_ids = set()
    for member in members:
        member_id = getattr(member, "id", None)
        member_state = normalize_oci_state(
            getattr(member, "lifecycle_state", None)
        )
        tags = getattr(member, "freeform_tags", None) or {}
        parent_cluster = tags.get("parent_cluster") or tags.get("cluster_name")
        member_availability_domain = getattr(
            member,
            "availability_domain",
            None,
        )
        if (
            not isinstance(member_id, str)
            or not member_id
            or member_id in members_by_id
            or getattr(member, "compartment_id", None) != compartment_id
            or (
                parent_cluster is not None
                and parent_cluster != expected_cluster_name
            )
            or (
                parent_availability_domain is not None
                and member_availability_domain is not None
                and member_availability_domain != parent_availability_domain
            )
        ):
            raise RuntimeError(
                "The Compute Cluster returned an invalid removal member"
            )
        members_by_id[member_id] = member
        if member_state != "TERMINATED":
            nonterminated_member_ids.add(member_id)

    source_instance_ids = set(journal["source_instance_ids"])
    removal_ids = {
        record["instance_id"] for record in journal["removals"]
    }
    survivor_ids = source_instance_ids-removal_ids
    if require_source_boundary:
        if nonterminated_member_ids-source_instance_ids:
            raise RuntimeError(
                "The Compute Cluster membership grew outside the pending removal boundary"
            )
        if not survivor_ids.issubset(nonterminated_member_ids):
            raise RuntimeError(
                "A Compute Cluster survivor disappeared outside the pending removal"
            )

    active_removals = {}
    for record in journal["removals"]:
        instance_id = record["instance_id"]
        member = members_by_id.get(instance_id)
        if member is None:
            try:
                candidate = computeClient.get_instance(
                    instance_id,
                    **get_oci_retry_kwargs(),
                ).data
            except oci.exceptions.ServiceError as error:
                if error.status == 404:
                    continue
                raise
            if normalize_oci_state(
                getattr(candidate, "lifecycle_state", None)
            ) != "TERMINATED":
                raise RuntimeError(
                    "A pending Compute Cluster removal target is no longer an exact member"
                )
            continue
        member_state = normalize_oci_state(
            getattr(member, "lifecycle_state", None)
        )
        if member_state == "TERMINATED":
            continue
        if (
            getattr(member, "display_name", None)
            not in record["instance_names"]
        ):
            raise RuntimeError(
                "A pending Compute Cluster removal target changed display name"
            )
        if member_state != "TERMINATING":
            live_private_ip = get_instance_primary_private_ip(
                compartment_id,
                instance_id,
                require_explicit_primary=True,
            )
            if live_private_ip != record["private_ip"]:
                raise RuntimeError(
                    "A pending Compute Cluster removal target changed private IP"
                )
        active_removals[instance_id] = member
    return active_removals

def preflight_compute_cluster_node_removal_dns(
    journal,
    inventory_dict,
    inventory_path,
):
    for record in journal["removals"]:
        delete_compute_cluster_node_name_dns_records(
            journal["compartment_id"],
            inventory_dict,
            inventory_path,
            journal["compute_cluster_id"],
            record["instance_names"],
            record["private_ip"],
            preflight_only=True,
        )

def complete_compute_cluster_node_removals(
    journal,
    inventory_dict,
    inventory_path,
):
    """Complete only the exact CC OCIDs committed by one durable journal."""
    state_external_instance_ids = (
        get_compute_cluster_state_external_member_ids(
            inventory_path,
            journal["compute_cluster_id"],
            [
                {"ocid": instance_id}
                for instance_id in journal["source_instance_ids"]
            ],
        )
    )
    if not {
        record["instance_id"] for record in journal["removals"]
    }.issubset(state_external_instance_ids):
        raise RuntimeError(
            "A pending Compute Cluster removal target became Terraform-managed"
        )
    try:
        active_removals = inspect_compute_cluster_node_removal_journal(
            journal,
            journal["cluster_name"],
            journal["compartment_id"],
            journal["compute_cluster_id"],
        )
    except RuntimeError:
        # Once every exact target is already gone, unrelated survivor drift is
        # no reason to strand this journal forever.  Active targets still keep
        # the strict original membership boundary.
        active_removals = inspect_compute_cluster_node_removal_journal(
            journal,
            journal["cluster_name"],
            journal["compartment_id"],
            journal["compute_cluster_id"],
            require_source_boundary=False,
        )
        if active_removals:
            raise
    preflight_compute_cluster_node_removal_dns(
        journal,
        inventory_dict,
        inventory_path,
    )
    cluster_directory = os.path.dirname(os.path.abspath(inventory_path))
    refuse_terraform_state_mutation_while_locked(cluster_directory)
    # Re-read the tracked boundary immediately before the first irreversible
    # DNS/instance mutation.  A Terraform apply must not turn a target into a
    # managed instance between selection and completion.
    current_state_external_instance_ids = (
        get_compute_cluster_state_external_member_ids(
            inventory_path,
            journal["compute_cluster_id"],
            [
                {"ocid": instance_id}
                for instance_id in journal["source_instance_ids"]
            ],
        )
    )
    if not {
        record["instance_id"] for record in journal["removals"]
    }.issubset(current_state_external_instance_ids):
        raise RuntimeError(
            "A pending Compute Cluster removal target changed Terraform ownership"
        )
    for record in journal["removals"]:
        refuse_terraform_state_mutation_while_locked(cluster_directory)
        instance_id = record["instance_id"]
        delete_compute_cluster_node_name_dns_records(
            journal["compartment_id"],
            inventory_dict,
            inventory_path,
            journal["compute_cluster_id"],
            record["instance_names"],
            record["private_ip"],
        )
        if instance_id in active_removals:
            terminate_instance_and_delete_launch_volumes(
                instance_id,
                delete_launch_created_data_volumes=record[
                    "delete_launch_created_data_volumes"
                ],
            )
            print(
                "STDOUT: The instance "+record["instance_display_name"]+
                " is terminating"
            )
        else:
            print(
                "STDOUT: The instance "+record["instance_display_name"]+
                " is already terminated"
            )
    remaining_removals = inspect_compute_cluster_node_removal_journal(
        journal,
        journal["cluster_name"],
        journal["compartment_id"],
        journal["compute_cluster_id"],
        require_source_boundary=False,
    )
    if remaining_removals:
        raise RuntimeError(
            "One or more pending Compute Cluster removal targets are still active"
        )
    return len(journal["removals"])

def get_pending_compute_cluster_inventory_names(inventory_dict, journal):
    records_by_id = {
        record["instance_id"]: record for record in journal["removals"]
    }
    pending_names = []
    seen_names = set()
    for section in ["compute_configured", "compute_to_add", "compute_to_destroy"]:
        for line in inventory_dict.get(section, []):
            parsed_line = split_inventory_host_line(line)
            if parsed_line is None or not parsed_line[1]:
                continue
            inventory_name = parsed_line[1][0]
            instance_id = get_inventory_token(line, "oci_instance_id")
            record = records_by_id.get(instance_id)
            if record is None and not instance_id:
                record = next(
                    (
                        candidate for candidate in journal["removals"]
                        if inventory_name in candidate["instance_names"]
                    ),
                    None,
                )
            if record is not None and inventory_name not in seen_names:
                pending_names.append(inventory_name)
                seen_names.add(inventory_name)
    return pending_names

def resume_pending_compute_cluster_node_removals(
    inventory_path,
    expected_cluster_name,
    compartment_id,
    compute_cluster_id,
    force=False,
):
    journal = load_compute_cluster_node_removal_journal(inventory_path)
    if journal is None:
        return 0
    inventory_dict = parse_inventory(inventory_path)
    try:
        active_removals = inspect_compute_cluster_node_removal_journal(
            journal,
            expected_cluster_name,
            compartment_id,
            compute_cluster_id,
        )
    except RuntimeError:
        active_removals = inspect_compute_cluster_node_removal_journal(
            journal,
            expected_cluster_name,
            compartment_id,
            compute_cluster_id,
            require_source_boundary=False,
        )
        if active_removals:
            raise
    preflight_compute_cluster_node_removal_dns(
        journal,
        inventory_dict,
        inventory_path,
    )
    pending_inventory_names = get_pending_compute_cluster_inventory_names(
        inventory_dict,
        journal,
    )
    if pending_inventory_names and not journal["no_reconfigure"]:
        refuse_terraform_state_mutation_while_locked(
            os.path.dirname(os.path.abspath(inventory_path))
        )
        configured_playbooks_directory = globals().get("playbooks_dir")
        if configured_playbooks_directory:
            playbook = os.path.join(
                configured_playbooks_directory,
                "resize_remove_unreachable.yml",
            )
        else:
            source_path = globals().get(
                "__file__",
                "/opt/oci-hpc/bin/resize.py",
            )
            playbook = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(source_path))),
                "playbooks",
                "resize_remove_unreachable.yml",
            )
        error_code = destroy_unreachable_reconfigure(
            inventory_path,
            pending_inventory_names,
            playbook,
        )
        if error_code != 0:
            if not force:
                raise RuntimeError(
                    "The pending Compute Cluster nodes could not be removed by Ansible; retry with Force"
                )
            print("STDOUT: Force deleting the pending Compute Cluster nodes")
        inventory_dict = parse_inventory(inventory_path)
    completed = complete_compute_cluster_node_removals(
        journal,
        inventory_dict,
        inventory_path,
    )
    clear_compute_cluster_node_removal_journal(inventory_path)
    return completed

def delete_pending_instance_pool_node_dns_records(record, inventory_path=None):
    validate_pending_instance_pool_node_removal(record)
    ownership = (
        load_instance_pool_name_dns_ownership(inventory_path)
        if inventory_path is not None
        else None
    )
    owned_domain_keys = {
        domain.lower() for domain in record["dns_domains"]
    }
    instance_name_prefixes = {
        (instance_name+".").lower()
        for instance_name in record["instance_names"]
    }
    if ownership is not None:
        for rrset in ownership["rrsets"]:
            if (
                set(rrset["private_ips"]) == {record["private_ip"]}
                and any(
                    rrset["domain"].lower().startswith(prefix)
                    for prefix in instance_name_prefixes
                )
            ):
                owned_domain_keys.add(rrset["domain"].lower())
    if not owned_domain_keys:
        return
    handled_domains = set()
    if ownership is not None:
        validate_instance_pool_dns_ownership_identity(
            ownership,
            record["cluster_name"],
            record["instance_pool_id"],
        )
        owned_rrsets = [
            rrset for rrset in ownership["rrsets"]
            if (
                rrset["domain"].lower() in owned_domain_keys
                and set(rrset["private_ips"]) == {record["private_ip"]}
            )
        ]
        for rrset in owned_rrsets:
            verify_private_dns_a_rrset_ownership(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )
        for rrset in owned_rrsets:
            delete_private_dns_a_rrset_if_owned(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )
            handled_domains.add(rrset["domain"].lower())

    remaining_domains = [
        domain for domain in record["dns_domains"]
        if domain.lower() not in handled_domains
    ]
    if remaining_domains:
        zones = dns_client.list_zones(
            compartment_id=record["compartment_id"],
            name=record["zone_name"],
            zone_type="PRIMARY",
            scope="PRIVATE",
        ).data
        if len(zones) > 1:
            raise RuntimeError(
                "Multiple private DNS zones matched a pending node removal"
            )
        if len(zones) == 1:
            for domain in remaining_domains:
                delete_private_dns_a_rrset_if_owned(
                    zones[0].id,
                    domain,
                    {record["private_ip"]},
                )

    if ownership is not None:
        write_instance_pool_name_dns_ownership(
            inventory_path,
            {
                **ownership,
                "rrsets": [
                    rrset for rrset in ownership["rrsets"]
                    if not (
                        rrset["domain"].lower() in handled_domains
                        and set(rrset["private_ips"]) == {record["private_ip"]}
                    )
                ],
            },
        )

def cleanup_instance_pool_name_dns_records(
    compartment_id,
    inventory_dict,
    inventory_path=None,
):
    expected_cluster_name = get_inventory_variable(inventory_dict, "cluster_name")
    tracked_cluster_network_id = (
        get_tracked_cluster_network_id(inventory_path)
        if inventory_path is not None
        else None
    )
    tracked_pool_id = (
        get_tracked_managed_instance_pool_id(inventory_path)
        if inventory_path is not None
        else None
    )
    ownership = (
        load_instance_pool_name_dns_ownership(inventory_path)
        if inventory_path is not None
        else None
    )
    hostnames = set()
    private_ips = set()
    private_ips_by_instance_id = {}
    for section in ["compute_configured", "compute_to_add", "compute_to_destroy"]:
        for line in inventory_dict.get(section, []):
            parsed_line = split_inventory_host_line(line)
            if parsed_line is not None and parsed_line[1]:
                hostnames.add(parsed_line[1][0])
                private_ip = get_inventory_token(line, "ansible_host")
                if private_ip:
                    try:
                        private_ip = str(ipaddress.ip_address(private_ip))
                    except ValueError:
                        raise RuntimeError("Inventory contains an invalid compute private IP")
                    private_ips.add(private_ip)
                    instance_id = get_inventory_token(line, "oci_instance_id")
                    if instance_id:
                        private_ips_by_instance_id[instance_id] = private_ip
    pending_plan = None
    pending_removal_pool_ids = set()
    if inventory_path is not None:
        pending_plan = load_instance_pool_hostname_sync_plan(inventory_path)
        if (
            pending_plan is not None
            and pending_plan["cluster_name"] != expected_cluster_name
        ):
            raise RuntimeError(
                "The pending Instance Pool hostname sync plan belongs to another cluster"
            )
        if pending_plan is not None:
            if (
                pending_plan.get("version") == 2
                and pending_plan.get("deployment_type") == "CN"
                and tracked_cluster_network_id is not None
                and pending_plan.get("cluster_network_id")
                != tracked_cluster_network_id
            ):
                raise RuntimeError(
                    "The pending hostname sync plan belongs to another Cluster Network"
                )
            if (
                pending_plan.get("version") == 2
                and pending_plan.get("deployment_type") == "CN"
                and tracked_cluster_network_id is None
            ):
                pending_cluster_network_id = pending_plan["cluster_network_id"]
                try:
                    pending_cluster_network = (
                        computeManagementClient.get_cluster_network(
                            pending_cluster_network_id,
                            **get_oci_retry_kwargs(),
                        ).data
                    )
                except oci.exceptions.ServiceError as error:
                    if error.status != 404:
                        raise
                else:
                    if (
                        getattr(
                            pending_cluster_network,
                            "id",
                            pending_cluster_network_id,
                        ) != pending_cluster_network_id
                        or getattr(
                            pending_cluster_network,
                            "compartment_id",
                            None,
                        ) != compartment_id
                        or getattr(
                            pending_cluster_network,
                            "display_name",
                            None,
                        ) != expected_cluster_name
                    ):
                        raise RuntimeError(
                            "The pending hostname sync plan has invalid Cluster Network ownership"
                        )
            for instance_id, member in pending_plan["members"].items():
                hostnames.update({
                    member["hostname"],
                    member["previous_display_name"],
                    member["inventory_hostname"],
                })
                private_ips.add(member["private_ip"])
                private_ips_by_instance_id[instance_id] = member["private_ip"]
        for removal in load_pending_instance_pool_node_removals(inventory_path):
            if (
                removal["cluster_name"] != expected_cluster_name
                or removal["compartment_id"] != compartment_id
            ):
                raise RuntimeError(
                    "A pending Instance Pool node removal belongs to another cluster"
                )
            hostnames.update(removal["instance_names"])
            private_ips.add(removal["private_ip"])
            private_ips_by_instance_id[removal["instance_id"]] = removal["private_ip"]
            pending_removal_pool_ids.add(removal["instance_pool_id"])
    if len(pending_removal_pool_ids) > 1:
        raise RuntimeError("Pending node removals belong to multiple Instance Pools")
    journal_pool_ids = set(pending_removal_pool_ids)
    if pending_plan is not None:
        journal_pool_ids.add(pending_plan["instance_pool_id"])
    ownership_pool_id = None
    if ownership is not None:
        ownership_pool_id = tracked_pool_id or ownership["instance_pool_id"]
        validate_instance_pool_dns_ownership_identity(
            ownership,
            expected_cluster_name,
            ownership_pool_id,
            require_terraform_state_released=False,
        )
    authoritative_pool_id = tracked_pool_id or ownership_pool_id
    if len(journal_pool_ids) > 1 or (
        authoritative_pool_id is not None
        and journal_pool_ids
        and authoritative_pool_id not in journal_pool_ids
    ):
        raise RuntimeError(
            "Pending Instance Pool work belongs to another pool"
        )
    exact_pool_id = authoritative_pool_id or (
        next(iter(journal_pool_ids)) if journal_pool_ids else None
    )
    active_pool = None
    if exact_pool_id is not None:
        try:
            candidate_pool = computeManagementClient.get_instance_pool(
                exact_pool_id
            ).data
        except oci.exceptions.ServiceError as error:
            if error.status != 404:
                raise
        else:
            if (
                candidate_pool.display_name != expected_cluster_name
                or candidate_pool.compartment_id != compartment_id
            ):
                raise RuntimeError("The Terraform-tracked Instance Pool has invalid ownership")
            if candidate_pool.lifecycle_state not in ["TERMINATED", "FAILED"]:
                active_pool = candidate_pool
    dns_cleanup_enabled = parse_bool(
        get_inventory_variable(inventory_dict, "dns_entries", "true")
    )
    slurm_cleanup_enabled = (
        dns_cleanup_enabled
        and parse_bool(get_inventory_variable(inventory_dict, "slurm", "false"))
    )
    use_fallback_discovery = ownership is None and dns_cleanup_enabled
    if active_pool is not None and (
        use_fallback_discovery or slurm_cleanup_enabled
    ):
        member_summaries = oci.pagination.list_call_get_all_results(
            computeManagementClient.list_instance_pool_instances,
            compartment_id=compartment_id,
            instance_pool_id=active_pool.id,
        ).data
        for member_summary in member_summaries:
            instance = computeClient.get_instance(member_summary.id).data
            tags = instance.freeform_tags or {}
            parent_cluster = tags.get("parent_cluster", tags.get("cluster_name"))
            if (
                instance.compartment_id != compartment_id
                or (
                    parent_cluster is not None
                    and parent_cluster != expected_cluster_name
                )
            ):
                raise RuntimeError(
                    "Instance Pool member "+instance.id+" has invalid cluster ownership"
                )
            hostnames.add(instance.display_name)
            if member_summary.id not in private_ips_by_instance_id:
                private_ip = get_instance_primary_private_ip(
                    compartment_id,
                    member_summary.id,
                )
                private_ips.add(private_ip)
                private_ips_by_instance_id[member_summary.id] = private_ip

    terraform_slurm_domains = get_terraform_managed_slurm_dns_domains(
        inventory_path,
        None,
    )
    fallback_rrsets_by_key = {}
    if use_fallback_discovery or slurm_cleanup_enabled:
        dns_zone_name = get_inventory_variable(
            inventory_dict,
            "zone_name",
            get_inventory_variable(inventory_dict, "cluster_name")+".local",
        )
        zones = dns_client.list_zones(
            compartment_id=compartment_id,
            name=dns_zone_name,
            zone_type="PRIMARY",
            scope="PRIVATE",
        ).data
        if len(zones) > 1:
            raise RuntimeError(
                "Multiple private DNS zones named "+dns_zone_name+
                " were found; refusing ambiguous cleanup"
            )
        if len(zones) == 1:
            zone_id = zones[0].id
            if use_fallback_discovery:
                for hostname in sorted(hostnames):
                    domain = hostname+"."+dns_zone_name
                    if domain.lower() not in terraform_slurm_domains:
                        fallback_rrsets_by_key[(zone_id, domain.lower())] = (
                            zone_id,
                            domain,
                            set(private_ips),
                        )
            if slurm_cleanup_enabled:
                for private_ip in sorted(private_ips):
                    slurm_domain = get_instance_pool_slurm_dns_domain(
                        inventory_dict,
                        private_ip,
                        dns_zone_name,
                    )
                    if (
                        slurm_domain is not None
                        and slurm_domain.lower() not in terraform_slurm_domains
                    ):
                        rrset_key = (zone_id, slurm_domain.lower())
                        if rrset_key in fallback_rrsets_by_key:
                            fallback_rrsets_by_key[rrset_key][2].add(private_ip)
                        else:
                            fallback_rrsets_by_key[rrset_key] = (
                                zone_id,
                                slurm_domain,
                                {private_ip},
                            )
    ownership_keys = {
        (rrset["zone_id"], rrset["domain"].lower())
        for rrset in (ownership["rrsets"] if ownership is not None else [])
    }
    fallback_rrsets = [
        rrset for key, rrset in fallback_rrsets_by_key.items()
        if key not in ownership_keys
    ]

    # Validate every journal, exact pool identity, and candidate RRset before
    # the first DNS mutation.  A persisted ledger is authoritative; fallback
    # discovery is only for older deployments that never wrote one.  Slurm
    # aliases already present in Terraform state remain Terraform-owned; only
    # state-external aliases created by later pool additions are included.
    if ownership is not None:
        for rrset in ownership["rrsets"]:
            verify_private_dns_a_rrset_ownership(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )
    for zone_id, domain, expected_private_ips in fallback_rrsets:
        verify_private_dns_a_rrset_ownership(
            zone_id,
            domain,
            expected_private_ips,
        )

    if ownership is not None:
        ownership = finish_pending_managed_pool_dns_ownership_transfer(
            inventory_path,
            ownership,
            expected_cluster_name,
            ownership_pool_id,
        )
        for rrset in ownership["rrsets"]:
            if rrset["domain"].lower() in terraform_slurm_domains:
                continue
            delete_private_dns_a_rrset_if_owned(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )
    for zone_id, domain, expected_private_ips in fallback_rrsets:
        delete_private_dns_a_rrset_if_owned(
            zone_id,
            domain,
            expected_private_ips,
        )
    if ownership is not None:
        write_instance_pool_name_dns_ownership(inventory_path, None)

def get_compute_cluster_dns_cleanup_rrsets(
    compartment_id,
    inventory_dict,
    inventory_path,
    private_ips,
    excluded_keys=None,
    preserve_terraform_managed=True,
    reject_terraform_managed=False,
):
    """Return exact Slurm RRsets that are safe for the requested cleanup."""
    if not parse_bool(get_inventory_variable(inventory_dict, "dns_entries", "true")):
        return []
    if not parse_bool(get_inventory_variable(inventory_dict, "slurm", "false")):
        return []
    dns_zone_name = get_inventory_variable(
        inventory_dict,
        "zone_name",
        get_inventory_variable(inventory_dict, "cluster_name")+".local",
    )
    zones = dns_client.list_zones(
        compartment_id=compartment_id,
        name=dns_zone_name,
        zone_type="PRIMARY",
        scope="PRIVATE",
    ).data
    if len(zones) > 1:
        raise RuntimeError(
            "Multiple private DNS zones named "+dns_zone_name+
            " were found; refusing ambiguous Compute Cluster cleanup"
        )
    if not zones:
        return []
    zone_id = zones[0].id
    terraform_slurm_domains = get_terraform_managed_slurm_dns_domains(
        inventory_path,
        zone_id,
    )
    terraform_slurm_rrsets = (
        get_terraform_managed_slurm_dns_rrsets(inventory_path, zone_id)
        if not preserve_terraform_managed
        else {}
    )
    excluded_keys = set(excluded_keys or [])
    rrsets = []
    seen_keys = set()
    for private_ip in sorted({str(ipaddress.ip_address(ip)) for ip in private_ips}):
        domain = get_instance_pool_slurm_dns_domain(
            inventory_dict,
            private_ip,
            dns_zone_name,
        )
        if domain is None:
            continue
        key = (zone_id, domain.lower())
        if key in seen_keys or key in excluded_keys:
            continue
        normalized_domain = domain.rstrip(".").lower()
        if normalized_domain in terraform_slurm_domains:
            if reject_terraform_managed:
                raise RuntimeError(
                    "A state-external Compute Cluster node unexpectedly has "
                    "a Terraform-managed Slurm DNS record"
                )
            if preserve_terraform_managed:
                continue
            state_rrset = terraform_slurm_rrsets.get(normalized_domain)
            if (
                state_rrset is None
                or state_rrset["zone_id"] != zone_id
                or set(state_rrset["private_ips"]) != {private_ip}
            ):
                raise RuntimeError(
                    "Terraform Slurm DNS state does not match the Compute Cluster removal target"
                )
        seen_keys.add(key)
        rrsets.append({
            "zone_id": zone_id,
            "zone_name": dns_zone_name,
            "domain": domain,
            "private_ips": [private_ip],
        })
    return rrsets

def cleanup_compute_cluster_name_dns_records(
    compartment_id,
    inventory_dict,
    inventory_path,
    compute_cluster_id,
    live_instances=None,
):
    """Delete only DNS records proven to belong to one exact Compute Cluster."""
    expected_cluster_name = get_inventory_variable(inventory_dict, "cluster_name")
    if not expected_cluster_name:
        raise RuntimeError("Inventory does not define cluster_name")
    tracked_resources = get_tracked_compute_cluster_resources(inventory_path)
    if (
        get_tracked_instance_pool_id(inventory_path) is not None
        or get_tracked_cluster_network_id(inventory_path) is not None
    ):
        raise RuntimeError(
            "Terraform state identifies another compute deployment during "
            "Compute Cluster DNS cleanup"
        )
    if tracked_resources is not None and (
        tracked_resources[0] not in [None, compute_cluster_id]
        or (tracked_resources[0] is None and tracked_resources[1])
    ):
        raise RuntimeError(
            "Terraform state does not match the Compute Cluster DNS ownership"
        )
    ownership = load_instance_pool_name_dns_ownership(inventory_path)
    if ownership is not None:
        validate_instance_pool_dns_ownership_identity(
            ownership,
            expected_cluster_name,
            compute_cluster_id=compute_cluster_id,
            require_terraform_state_released=False,
        )

    private_ips = set()
    legacy_canonical_records = {}

    def remember_legacy_canonical_name(instance_name, private_ip):
        if not instance_name or not private_ip:
            return
        validate_os_hostname(instance_name)
        try:
            normalized_private_ip = str(ipaddress.ip_address(private_ip))
        except (TypeError, ValueError):
            raise RuntimeError(
                "A Compute Cluster DNS cleanup member has an invalid private IP"
            )
        normalized_name = instance_name.lower()
        previous = legacy_canonical_records.get(normalized_name)
        if previous is not None and previous[1] != normalized_private_ip:
            raise RuntimeError(
                "A Compute Cluster canonical DNS name maps to multiple private IPs"
            )
        legacy_canonical_records[normalized_name] = (
            instance_name,
            normalized_private_ip,
        )

    for section in ["compute_configured", "compute_to_add", "compute_to_destroy"]:
        for line in inventory_dict.get(section, []):
            private_ip = get_inventory_token(line, "ansible_host")
            if private_ip:
                try:
                    normalized_private_ip = str(ipaddress.ip_address(private_ip))
                    private_ips.add(normalized_private_ip)
                except ValueError:
                    raise RuntimeError(
                        "Inventory contains an invalid compute private IP"
                    )
                parsed_line = split_inventory_host_line(line)
                if parsed_line is not None and parsed_line[1]:
                    remember_legacy_canonical_name(
                        parsed_line[1][0],
                        normalized_private_ip,
                    )
    pending_plan = load_instance_pool_hostname_sync_plan(inventory_path)
    if pending_plan is not None:
        if (
            pending_plan.get("version") != 3
            or pending_plan.get("deployment_type") != "CC"
            or pending_plan.get("cluster_name") != expected_cluster_name
            or pending_plan.get("compute_cluster_id") != compute_cluster_id
        ):
            raise RuntimeError(
                "The pending hostname sync plan belongs to another Compute Cluster"
            )
        private_ips.update(
            member["private_ip"] for member in pending_plan["members"].values()
        )
        for member in pending_plan["members"].values():
            for instance_name in {
                member["hostname"],
                member["previous_display_name"],
                member["inventory_hostname"],
            }:
                remember_legacy_canonical_name(
                    instance_name,
                    member["private_ip"],
                )
    pending_removal_journal = load_compute_cluster_node_removal_journal(
        inventory_path
    )
    if pending_removal_journal is not None:
        if (
            pending_removal_journal["cluster_name"] != expected_cluster_name
            or pending_removal_journal["compartment_id"] != compartment_id
            or pending_removal_journal["compute_cluster_id"]
            != compute_cluster_id
        ):
            raise RuntimeError(
                "The pending node removal belongs to another Compute Cluster"
            )
        for record in pending_removal_journal["removals"]:
            private_ips.add(record["private_ip"])
            for instance_name in record["instance_names"]:
                remember_legacy_canonical_name(
                    instance_name,
                    record["private_ip"],
                )
    for instance in live_instances or []:
        try:
            normalized_private_ip = str(ipaddress.ip_address(instance["ip"]))
            private_ips.add(normalized_private_ip)
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                "A Compute Cluster DNS cleanup member has an invalid private IP"
            )
        remember_legacy_canonical_name(
            instance.get("display_name"),
            normalized_private_ip,
        )

    ownership_keys = {
        (rrset["zone_id"], rrset["domain"].lower())
        for rrset in (ownership["rrsets"] if ownership is not None else [])
    }
    legacy_canonical_rrsets = []
    if ownership is None and parse_bool(
        get_inventory_variable(inventory_dict, "dns_entries", "true")
    ):
        dns_zone_name = get_inventory_variable(
            inventory_dict,
            "zone_name",
            expected_cluster_name+".local",
        )
        zones = dns_client.list_zones(
            compartment_id=compartment_id,
            name=dns_zone_name,
            zone_type="PRIMARY",
            scope="PRIVATE",
        ).data
        if len(zones) > 1:
            raise RuntimeError(
                "Multiple private DNS zones named "+dns_zone_name+
                " were found; refusing ambiguous Compute Cluster cleanup"
            )
        if zones:
            zone_id = zones[0].id
            terraform_canonical_addresses = (
                get_terraform_managed_oci_name_dns_state_addresses(
                    inventory_path
                )
            )
            terraform_canonical_domains = {
                rrset["domain"].rstrip(".").lower()
                for rrset in get_terraform_managed_oci_name_dns_rrsets(
                    inventory_path,
                    terraform_canonical_addresses,
                    zone_id,
                    dns_zone_name,
                )
            }
            for instance_name, private_ip in legacy_canonical_records.values():
                domain = instance_name+"."+dns_zone_name
                if domain.rstrip(".").lower() in terraform_canonical_domains:
                    continue
                legacy_canonical_rrsets.append({
                    "zone_id": zone_id,
                    "zone_name": dns_zone_name,
                    "domain": domain,
                    "private_ips": [private_ip],
                })
        ownership_keys.update(
            (rrset["zone_id"], rrset["domain"].lower())
            for rrset in legacy_canonical_rrsets
        )
    additional_rrsets = get_compute_cluster_dns_cleanup_rrsets(
        compartment_id,
        inventory_dict,
        inventory_path,
        private_ips,
        excluded_keys=ownership_keys,
    )
    # Establish ownership for every candidate before the first DNS mutation.
    if ownership is not None:
        for rrset in ownership["rrsets"]:
            verify_private_dns_a_rrset_ownership(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )
    for rrset in legacy_canonical_rrsets+additional_rrsets:
        verify_private_dns_a_rrset_ownership(
            rrset["zone_id"],
            rrset["domain"],
            rrset["private_ips"],
        )

    if ownership is not None:
        ownership = finish_pending_managed_pool_dns_ownership_transfer(
            inventory_path,
            ownership,
            expected_cluster_name,
            compute_cluster_id=compute_cluster_id,
        )
        terraform_slurm_domains = get_terraform_managed_slurm_dns_domains(
            inventory_path,
            None,
        )
        for rrset in ownership["rrsets"]:
            if rrset["domain"].lower() in terraform_slurm_domains:
                continue
            delete_private_dns_a_rrset_if_owned(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )
    for rrset in legacy_canonical_rrsets+additional_rrsets:
        delete_private_dns_a_rrset_if_owned(
            rrset["zone_id"],
            rrset["domain"],
            rrset["private_ips"],
        )
    if ownership is not None:
        write_instance_pool_name_dns_ownership(inventory_path, None)

def delete_compute_cluster_node_name_dns_records(
    compartment_id,
    inventory_dict,
    inventory_path,
    compute_cluster_id,
    instance_names,
    private_ip,
    preflight_only=False,
):
    """Delete one CC member's Python-owned final-name and dynamic Slurm DNS."""
    expected_cluster_name = get_inventory_variable(inventory_dict, "cluster_name")
    compute_cluster = computeClient.get_compute_cluster(
        compute_cluster_id,
        **get_oci_retry_kwargs(),
    ).data
    validate_compute_cluster_identity(
        compute_cluster,
        compute_cluster_id,
        compartment_id,
        expected_cluster_name,
    )
    private_ip = str(ipaddress.ip_address(private_ip))
    ownership = load_instance_pool_name_dns_ownership(inventory_path)
    owned_rrsets = []
    if ownership is not None:
        validate_instance_pool_dns_ownership_identity(
            ownership,
            expected_cluster_name,
            compute_cluster_id=compute_cluster_id,
            require_terraform_state_released=False,
        )
        dns_zone_name = get_inventory_variable(
            inventory_dict,
            "zone_name",
            expected_cluster_name+".local",
        )
        member_domains = {
            (name+"."+dns_zone_name).lower()
            for name in instance_names
            if name
        }
        owned_rrsets = [
            rrset for rrset in ownership["rrsets"]
            if (
                set(rrset["private_ips"]) == {private_ip}
                and rrset["domain"].lower() in member_domains
            )
        ]
    owned_keys = {
        (rrset["zone_id"], rrset["domain"].lower()) for rrset in owned_rrsets
    }
    legacy_name_rrsets = []
    if ownership is None and parse_bool(
        get_inventory_variable(inventory_dict, "dns_entries", "true")
    ):
        dns_zone_name = get_inventory_variable(
            inventory_dict,
            "zone_name",
            expected_cluster_name+".local",
        )
        zone_id = get_single_private_dns_zone_id(
            compartment_id,
            dns_zone_name,
        )
        for instance_name in sorted({name for name in instance_names if name}):
            validate_os_hostname(instance_name)
            legacy_name_rrsets.append({
                "zone_id": zone_id,
                "zone_name": dns_zone_name,
                "domain": instance_name+"."+dns_zone_name,
                "private_ips": [private_ip],
            })
        owned_keys.update(
            (rrset["zone_id"], rrset["domain"].lower())
            for rrset in legacy_name_rrsets
        )
    additional_rrsets = get_compute_cluster_dns_cleanup_rrsets(
        compartment_id,
        inventory_dict,
        inventory_path,
        {private_ip},
        excluded_keys=owned_keys,
        reject_terraform_managed=True,
    )
    candidate_rrsets = owned_rrsets+legacy_name_rrsets+additional_rrsets
    for rrset in candidate_rrsets:
        verify_private_dns_a_rrset_ownership(
            rrset["zone_id"],
            rrset["domain"],
            rrset["private_ips"],
        )
    if preflight_only:
        if (
            ownership is not None
            and ownership.get("terraform_state_released", True) is not True
        ):
            raise RuntimeError(
                "The Compute Cluster DNS ownership transfer from Terraform is incomplete"
            )
        return bool(candidate_rrsets)
    if ownership is not None:
        ownership = finish_pending_managed_pool_dns_ownership_transfer(
            inventory_path,
            ownership,
            expected_cluster_name,
            compute_cluster_id=compute_cluster_id,
        )
    for rrset in candidate_rrsets:
        delete_private_dns_a_rrset_if_owned(
            rrset["zone_id"],
            rrset["domain"],
            rrset["private_ips"],
        )
    if ownership is not None:
        handled_keys = {
            (rrset["zone_id"], rrset["domain"].lower())
            for rrset in owned_rrsets
        }
        write_instance_pool_name_dns_ownership(
            inventory_path,
            {
                **ownership,
                "rrsets": [
                    rrset for rrset in ownership["rrsets"]
                    if (rrset["zone_id"], rrset["domain"].lower())
                    not in handled_keys
                ],
            },
        )
    return bool(candidate_rrsets)

def refresh_instance_pool_hosts(inventory_path, max_wait_seconds=1800):
    configured_playbooks_directory = globals().get("playbooks_dir")
    if configured_playbooks_directory:
        playbook_path = os.path.join(
            configured_playbooks_directory,
            "refresh_instance_pool_hosts.yml",
        )
    else:
        source_path = globals().get("__file__", "/opt/oci-hpc/bin/resize.py")
        playbook_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(source_path))),
            "playbooks",
            "refresh_instance_pool_hosts.yml",
        )
    my_env = os.environ.copy()
    my_env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    try:
        completed = subprocess.run(
            ["ansible-playbook", "-i", inventory_path, playbook_path],
            env=my_env,
            timeout=max_wait_seconds,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Timed out while refreshing /etc/hosts after Instance Pool name synchronization"
        )
    if completed.returncode != 0:
        raise RuntimeError(
            "Failed to refresh /etc/hosts after Instance Pool name synchronization"
        )

def synchronize_instance_pool_names(
    compartment_id,
    instance_pool_id,
    inventory_path,
    expected_cluster_name,
    observed_hostnames_by_instance_id=None,
    max_wait_seconds=60,
    cluster_network_id=None,
    compute_cluster_id=None,
):
    if compute_cluster_id is not None and (
        instance_pool_id is not None or cluster_network_id is not None
    ):
        raise RuntimeError("Hostname synchronization received multiple deployment identities")
    is_compute_cluster = compute_cluster_id is not None
    current_inventory = parse_inventory(inventory_path)
    if current_inventory is None:
        raise RuntimeError("Inventory file "+inventory_path+" was not found")
    inventory_cluster_name = get_inventory_variable(current_inventory, "cluster_name")
    if inventory_cluster_name != expected_cluster_name:
        raise RuntimeError("Requested cluster name does not match inventory cluster_name")
    pending_node_removals = load_pending_instance_pool_node_removals(
        inventory_path
    )
    if pending_node_removals:
        if is_compute_cluster:
            raise RuntimeError(
                "An Instance Pool node-removal journal cannot belong to a Compute Cluster"
            )
        for record in pending_node_removals:
            if (
                record["cluster_name"] != expected_cluster_name
                or record["compartment_id"] != compartment_id
                or record["instance_pool_id"] != instance_pool_id
            ):
                raise RuntimeError(
                    "A pending Instance Pool node removal belongs to another cluster or pool"
                )
        raise RuntimeError(
            "Cannot synchronize Instance Pool names while a node removal is pending; "
            "retry remove or remove_unreachable first"
        )
    pending_compute_cluster_removal = (
        load_compute_cluster_node_removal_journal(inventory_path)
    )
    if pending_compute_cluster_removal is not None:
        if not is_compute_cluster:
            raise RuntimeError(
                "A Compute Cluster node-removal journal cannot belong to a managed pool"
            )
        if (
            pending_compute_cluster_removal["cluster_name"]
            != expected_cluster_name
            or pending_compute_cluster_removal["compartment_id"]
            != compartment_id
            or pending_compute_cluster_removal["compute_cluster_id"]
            != compute_cluster_id
        ):
            raise RuntimeError(
                "The pending node removal belongs to another Compute Cluster"
            )
        raise RuntimeError(
            "Cannot synchronize Compute Cluster names while a node removal is "
            "pending; retry remove or remove_unreachable first"
        )
    post_resize_recovery = load_instance_pool_post_resize_recovery(
        inventory_path
    )
    if post_resize_recovery is not None:
        if is_compute_cluster:
            raise RuntimeError(
                "An Instance Pool post-resize marker cannot belong to a Compute Cluster"
            )
        if (
            post_resize_recovery["cluster_name"] != expected_cluster_name
            or post_resize_recovery["instance_pool_id"] != instance_pool_id
        ):
            raise RuntimeError(
                "The Instance Pool post-resize recovery marker belongs to another cluster or pool"
            )
    inventory_hosts = get_instance_pool_inventory_hosts(current_inventory)
    if is_compute_cluster:
        migrate_compute_cluster_instance_display_name_management(inventory_path)
        instances, instances_by_id = get_complete_compute_cluster_instances(
            compartment_id,
            compute_cluster_id,
            expected_cluster_name,
            expected_instance_ids=inventory_hosts,
            max_wait_seconds=max_wait_seconds,
        )
    elif cluster_network_id is not None:
        instances, instances_by_id = get_complete_cluster_network_instances(
            compartment_id,
            cluster_network_id,
            instance_pool_id,
            expected_cluster_name,
            max_wait_seconds=max_wait_seconds,
        )
    else:
        instances, instances_by_id = get_complete_instance_pool_instances(
            compartment_id,
            instance_pool_id,
            max_wait_seconds=max_wait_seconds,
        )
    if set(inventory_hosts) != set(instances_by_id):
        raise RuntimeError(
            "Compute inventory does not exactly match the current compute deployment membership"
        )
    # Each autoscaling cluster has a copied Terraform directory.  Older copies
    # still own the generated OCI-name RRsets, so release those state addresses
    # and gate that copied resource before Python starts owning final-hostname
    # records.  New clusters already contain the gated expression and no-op.
    migrate_instance_pool_oci_dns_ownership(
        inventory_path,
        compartment_id=compartment_id,
        instance_pool_id=instance_pool_id,
        compute_cluster_id=compute_cluster_id,
        instances_by_id=instances_by_id,
    )

    pending_plan = load_instance_pool_hostname_sync_plan(inventory_path)
    if pending_plan is not None:
        if pending_plan["cluster_name"] != expected_cluster_name:
            raise RuntimeError(
                "The pending hostname sync plan belongs to another cluster"
            )
        if is_compute_cluster and (
            pending_plan.get("version") != 3
            or pending_plan.get("deployment_type") != "CC"
            or pending_plan.get("compute_cluster_id") != compute_cluster_id
        ):
            raise RuntimeError(
                "The pending hostname sync plan belongs to another Compute Cluster"
            )
        if not is_compute_cluster and pending_plan.get("instance_pool_id") != instance_pool_id:
            raise RuntimeError(
                "The pending Instance Pool hostname sync plan belongs to another cluster or pool"
            )
        if not is_compute_cluster and cluster_network_id is not None and (
            pending_plan.get("version") != 2
            or pending_plan.get("deployment_type") != "CN"
            or pending_plan.get("cluster_network_id") != cluster_network_id
        ):
            raise RuntimeError(
                "The pending hostname sync plan belongs to another Cluster Network parent"
            )
        if not is_compute_cluster and cluster_network_id is None and (
            pending_plan.get("version") == 2
            and pending_plan.get("deployment_type") != "IP"
        ):
            raise RuntimeError(
                "The pending hostname sync plan belongs to another deployment type"
            )
        if set(pending_plan["members"]) != set(instances_by_id):
            raise RuntimeError(
                "The current compute membership changed while hostname synchronization was pending"
            )
        if observed_hostnames_by_instance_id is not None:
            raise RuntimeError(
                "Cannot replace OS hostname observations while a synchronization plan is pending"
            )
        observed_hostnames_by_instance_id = pending_plan["members"]
    elif observed_hostnames_by_instance_id is None:
        observed_hostnames_by_instance_id = collect_instance_pool_os_hostnames(
            inventory_path
        )
    if not isinstance(observed_hostnames_by_instance_id, dict):
        raise RuntimeError("Observed OS hostname data is malformed")
    if set(observed_hostnames_by_instance_id) != set(instances_by_id):
        raise RuntimeError(
            "Observed OS hostname Instance OCIDs do not exactly match the current compute membership"
        )
    desired_names_by_instance_id = {}
    for instance_id, inventory_host in inventory_hosts.items():
        observed = observed_hostnames_by_instance_id[instance_id]
        if not isinstance(observed, dict):
            raise RuntimeError("Observed OS hostname data is malformed for instance "+instance_id)
        try:
            observed_private_ip = str(ipaddress.ip_address(observed.get("private_ip")))
            current_private_ip = str(ipaddress.ip_address(instances_by_id[instance_id]["ip"]))
        except (TypeError, ValueError):
            raise RuntimeError("Observed OS hostname data has an invalid private IP")
        observed_inventory_hostname = observed.get("inventory_hostname")
        desired_hostname = validate_os_hostname(observed.get("hostname"))
        allowed_inventory_hostnames = {observed_inventory_hostname}
        if pending_plan is not None:
            allowed_inventory_hostnames.add(desired_hostname)
            current_display_name = instances_by_id[instance_id]["display_name"]
            if current_display_name not in {
                observed.get("previous_display_name"),
                desired_hostname,
            }:
                raise RuntimeError(
                    "Instance "+instance_id+
                    " changed outside the pending hostname synchronization plan"
                )
        if (
            inventory_host["inventory_hostname"] not in allowed_inventory_hostnames
            or observed_private_ip != inventory_host["private_ip"]
            or observed_private_ip != current_private_ip
        ):
            raise RuntimeError(
                "Observed OS hostname identity does not match inventory and OCI for instance "+
                instance_id
            )
        desired_names_by_instance_id[instance_id] = desired_hostname
    validate_instance_pool_name_plan(
        current_inventory,
        desired_names_by_instance_id,
        {
            instance_id: instance["ip"]
            for instance_id, instance in instances_by_id.items()
        },
    )

    for record in load_pending_local_block_volume_deletions(inventory_path):
        instance_id = record["instance_id"]
        if (
            instance_id in desired_names_by_instance_id
            and record["instance_display_name"] != desired_names_by_instance_id[instance_id]
        ):
            raise RuntimeError("Cannot rename an instance with a pending local Block Volume deletion")

    # DNS uses an update/replace API.  Inspect every target RRset before the
    # first OCI display-name change so an Ansible hostname cannot take over a
    # record owned by another cluster in a shared private zone.
    preflight_previous_names = {
        instance_id: (
            pending_plan["members"][instance_id]["previous_display_name"]
            if pending_plan is not None
            else instances_by_id[instance_id]["display_name"]
        )
        for instance_id in desired_names_by_instance_id
    }
    preflight_instance_pool_name_dns(
        compartment_id,
        current_inventory,
        instances_by_id,
        desired_names_by_instance_id,
        preflight_previous_names,
    )
    persisted_dns_ownership = load_instance_pool_name_dns_ownership(
        inventory_path
    )
    if persisted_dns_ownership is not None:
        validate_instance_pool_dns_ownership_identity(
            persisted_dns_ownership,
            expected_cluster_name,
            instance_pool_id,
            compute_cluster_id=compute_cluster_id,
        )
        for rrset in persisted_dns_ownership["rrsets"]:
            verify_private_dns_a_rrset_ownership(
                rrset["zone_id"],
                rrset["domain"],
                rrset["private_ips"],
            )

    def persist_plan_before_first_update(previous_names):
        nonlocal pending_plan
        if pending_plan is not None:
            return
        pending_plan = {
            "version": (
                3
                if is_compute_cluster
                else (2 if cluster_network_id is not None else 1)
            ),
            "status": "pending",
            "cluster_name": expected_cluster_name,
            "members": {
                instance_id: {
                    "hostname": desired_names_by_instance_id[instance_id],
                    "private_ip": inventory_hosts[instance_id]["private_ip"],
                    "inventory_hostname": inventory_hosts[instance_id]["inventory_hostname"],
                    "previous_display_name": previous_names[instance_id],
                }
                for instance_id in desired_names_by_instance_id
            },
        }
        if is_compute_cluster:
            pending_plan.update({
                "deployment_type": "CC",
                "compute_cluster_id": compute_cluster_id,
            })
        else:
            pending_plan["instance_pool_id"] = instance_pool_id
        if not is_compute_cluster and cluster_network_id is not None:
            pending_plan.update({
                "deployment_type": "CN",
                "cluster_network_id": cluster_network_id,
            })
        # The update helper calls this after it has validated the complete,
        # exact pool membership plus every instance and Primary VNIC, before
        # any possible OCI mutation.  A retry therefore never recollects facts
        # from a partly renamed cluster, while a failed preflight leaves no
        # stale journal.
        write_instance_pool_hostname_sync_plan(inventory_path, pending_plan)

    previous_names = update_instance_pool_display_names(
        compartment_id,
        instance_pool_id,
        expected_cluster_name,
        desired_names_by_instance_id,
        max_wait_seconds=max_wait_seconds,
        before_updates=persist_plan_before_first_update,
        expected_private_ips_by_instance_id={
            instance_id: instances_by_id[instance_id]["ip"]
            for instance_id in desired_names_by_instance_id
        },
        cluster_network_id=cluster_network_id,
        compute_cluster_id=compute_cluster_id,
    )
    previous_names_for_dns = dict(previous_names)
    previous_names_for_dns.update({
        "pending:"+instance_id: member["previous_display_name"]
        for instance_id, member in pending_plan["members"].items()
    })
    reconcile_instance_pool_name_dns(
        compartment_id,
        current_inventory,
        instances_by_id,
        desired_names_by_instance_id,
        previous_names_for_dns,
        protected_names=desired_names_by_instance_id.values(),
        inventory_path=inventory_path,
        instance_pool_id=instance_pool_id,
        compute_cluster_id=compute_cluster_id,
    )
    rewritten_inventory = rewrite_instance_pool_inventory_names(
        inventory_path,
        current_inventory,
        desired_names_by_instance_id,
    )
    # At this point OCI, DNS, and Inventory agree.  Keep the operation journal
    # while the derived /etc/hosts refresh is running so a process interruption
    # or unreachable host cannot leave the autoscaler resolving a stale alias.
    try:
        refresh_instance_pool_hosts(inventory_path)
    except Exception as refresh_error:
        raise RuntimeError(
            "Instance Pool names were synchronized, but /etc/hosts refresh did not "
            "complete: "+str(refresh_error)
        )
    clear_instance_pool_hostname_sync_plan(inventory_path)
    if not is_compute_cluster:
        clear_instance_pool_post_resize_recovery(inventory_path)
    for instance_id, desired_name in desired_names_by_instance_id.items():
        instances_by_id[instance_id]["display_name"] = desired_name
    return list(instances_by_id.values()), rewritten_inventory

def synchronize_autoscaling_managed_pool_names(
    compartment_id,
    inventory_path,
    expected_cluster_name,
    expected_instance_pool_id=None,
    expected_cluster_network_id=None,
    observed_hostnames_by_instance_id=None,
    max_wait_seconds=60,
):
    """Resolve Terraform's exact managed-pool identity, then synchronize it."""
    direct_instance_pool_id = get_tracked_instance_pool_id(inventory_path)
    tracked_cluster_network_id = get_tracked_cluster_network_id(inventory_path)
    tracked_compute_cluster_resources = get_tracked_compute_cluster_resources(
        inventory_path
    )
    tracked_compute_cluster_is_present = has_tracked_compute_cluster_resources(
        tracked_compute_cluster_resources
    )
    if sum(
        deployment_is_present
        for deployment_is_present in [
            direct_instance_pool_id is not None,
            tracked_cluster_network_id is not None,
            tracked_compute_cluster_is_present,
        ]
    ) > 1:
        raise RuntimeError(
            "Terraform state identifies multiple compute deployment types"
        )
    if tracked_compute_cluster_is_present:
        raise RuntimeError(
            "Terraform state identifies a Compute Cluster, not a managed pool"
        )
    if tracked_cluster_network_id is not None:
        cluster_network, embedded_pool = (
            get_tracked_cluster_network_for_hostname_sync(
                inventory_path,
                compartment_id,
                expected_cluster_name,
                # A detach work request can finish before the parent leaves
                # SCALING.  Resolve the exact identity now; the complete CN
                # snapshot below waits for RUNNING before any mutation.
                require_running=False,
            )
        )
        instance_pool_id = embedded_pool.id
        if (
            expected_cluster_network_id not in [None, cluster_network.id]
            or expected_instance_pool_id not in [None, instance_pool_id]
        ):
            raise RuntimeError(
                "The requested Cluster Network does not match Terraform state"
            )
        return synchronize_instance_pool_names(
            compartment_id,
            instance_pool_id,
            inventory_path,
            expected_cluster_name,
            observed_hostnames_by_instance_id=observed_hostnames_by_instance_id,
            max_wait_seconds=max_wait_seconds,
            cluster_network_id=cluster_network.id,
        )
    if direct_instance_pool_id is not None:
        instance_pool = get_tracked_instance_pool_for_hostname_sync(
            inventory_path,
            compartment_id,
            expected_cluster_name,
        )
        if (
            expected_cluster_network_id is not None
            or expected_instance_pool_id not in [None, instance_pool.id]
        ):
            raise RuntimeError(
                "The requested Instance Pool does not match Terraform state"
            )
        return synchronize_instance_pool_names(
            compartment_id,
            instance_pool.id,
            inventory_path,
            expected_cluster_name,
            observed_hostnames_by_instance_id=observed_hostnames_by_instance_id,
            max_wait_seconds=max_wait_seconds,
        )
    raise RuntimeError(
        "Terraform state does not identify an Instance Pool or Cluster Network for hostname synchronization"
    )

def synchronize_autoscaling_compute_names(
    compartment_id,
    inventory_path,
    expected_cluster_name,
    expected_instance_pool_id=None,
    expected_cluster_network_id=None,
    expected_compute_cluster_id=None,
    observed_hostnames_by_instance_id=None,
    max_wait_seconds=60,
):
    """Resolve one exact Autoscaling compute deployment, then synchronize it."""
    direct_instance_pool_id = get_tracked_instance_pool_id(inventory_path)
    tracked_cluster_network_id = get_tracked_cluster_network_id(inventory_path)
    tracked_compute_cluster_resources = get_tracked_compute_cluster_resources(
        inventory_path
    )
    tracked_compute_cluster_is_present = has_tracked_compute_cluster_resources(
        tracked_compute_cluster_resources
    )
    if sum(
        deployment_is_present
        for deployment_is_present in [
            direct_instance_pool_id is not None,
            tracked_cluster_network_id is not None,
            tracked_compute_cluster_is_present,
        ]
    ) > 1:
        raise RuntimeError(
            "Terraform state identifies multiple compute deployment types"
        )
    if tracked_compute_cluster_is_present:
        if (
            expected_instance_pool_id is not None
            or expected_cluster_network_id is not None
        ):
            raise RuntimeError(
                "The requested managed pool does not match the Terraform-tracked Compute Cluster"
            )
        compute_cluster = get_tracked_compute_cluster_for_hostname_sync(
            inventory_path,
            compartment_id,
            expected_cluster_name,
        )
        if expected_compute_cluster_id not in [None, compute_cluster.id]:
            raise RuntimeError(
                "The requested Compute Cluster does not match Terraform state"
            )
        return synchronize_instance_pool_names(
            compartment_id,
            None,
            inventory_path,
            expected_cluster_name,
            observed_hostnames_by_instance_id=observed_hostnames_by_instance_id,
            max_wait_seconds=max_wait_seconds,
            compute_cluster_id=compute_cluster.id,
        )
    if expected_compute_cluster_id is not None:
        raise RuntimeError(
            "The requested Compute Cluster does not match Terraform state"
        )
    return synchronize_autoscaling_managed_pool_names(
        compartment_id,
        inventory_path,
        expected_cluster_name,
        expected_instance_pool_id=expected_instance_pool_id,
        expected_cluster_network_id=expected_cluster_network_id,
        observed_hostnames_by_instance_id=observed_hostnames_by_instance_id,
        max_wait_seconds=max_wait_seconds,
    )

def prepare_local_block_volume_inventory(inventory_path, max_wait_seconds=300):
    prepared_inventory = parse_inventory(inventory_path)
    if prepared_inventory is None:
        raise RuntimeError("Inventory file "+inventory_path+" was not found")
    expected_cluster_name = get_inventory_variable(prepared_inventory, "cluster_name")
    if not expected_cluster_name:
        raise RuntimeError("Inventory does not define cluster_name")
    prepared_values_by_host = {}
    for section in ["compute_configured", "compute_to_add"]:
        if section not in prepared_inventory:
            raise RuntimeError("Inventory does not contain ["+section+"]")
        for index, line in enumerate(prepared_inventory[section]):
            host_name, instance_id = get_inventory_instance_id(line)
            if instance_id is None:
                continue
            instance = computeClient.get_instance(instance_id).data
            instance_parent_cluster = (
                (instance.freeform_tags or {}).get("parent_cluster")
                or (instance.freeform_tags or {}).get("cluster_name")
            )
            if (
                instance.display_name != host_name
                and instance_parent_cluster != expected_cluster_name
            ):
                raise RuntimeError(
                    "Inventory host "+host_name+" does not match OCI instance "+instance.display_name
                )
            expected_config = get_instance_local_block_volume_config(
                instance,
                prepared_inventory,
                expected_cluster_name,
            )
            local_block_volume_values = {"use_local_block_volume": "false"}
            if expected_config["enabled"]:
                attachment = get_local_block_volume_attachment(
                    instance.compartment_id,
                    instance_id,
                    max_wait_seconds=max_wait_seconds,
                    expected_config=expected_config,
                    expected_cluster_name=expected_cluster_name,
                    instance=instance,
                )
                local_block_volume_values = {
                    "use_local_block_volume": "true",
                    "local_block_volume_size": expected_config["size_in_gbs"],
                    "local_block_volume_mount_point": expected_config["mount_point"],
                    "local_block_volume_iscsi_ip": attachment.ipv4,
                    "local_block_volume_iscsi_port": attachment.port,
                    "local_block_volume_iqn": attachment.iqn,
                }
            prepared_values_by_host[host_name] = local_block_volume_values
            prepared_inventory[section][index] = rewrite_compute_inventory_line(
                line,
                local_block_volume_values,
            )
    for index, line in enumerate(prepared_inventory.get("nfs", [])):
        parsed_line = split_inventory_host_line(line)
        if parsed_line is None or not parsed_line[1]:
            continue
        host_name = parsed_line[1][0]
        if host_name in prepared_values_by_host:
            prepared_inventory["nfs"][index] = rewrite_compute_inventory_line(
                line,
                prepared_values_by_host[host_name],
            )
    write_inventory_atomic(prepared_inventory, inventory_path)

def remove_ip(filename,iplist):
    tmp_filename=os.path.join('/tmp',os.path.basename(filename))
    hostFile = open(filename,"r")
    hostFile_tmp = open(tmp_filename,"w")
    for line in hostFile:
        if not line.strip() in iplist:
            hostFile_tmp.write(line)
    hostFile.close()
    hostFile_tmp.close()
    os.system('mv '+tmp_filename+' '+filename)

def add_ip(filename,iplist):
    ip_to_add= copy.deepcopy(iplist)
    tmp_filename=os.path.join('/tmp',os.path.basename(filename))
    hostFile = open(filename,"r")
    hostFile_tmp = open(tmp_filename,"w")
    for line in hostFile:
        if line.strip() in iplist:
            ip_to_add.remove(line.strip())
        hostFile_tmp.write(line)
    for ip in ip_to_add:
        hostFile_tmp.write(ip+'\n')
    hostFile.close()
    hostFile_tmp.close()
    os.system('mv '+tmp_filename+' '+filename)

def backup_inventory(inventory):
    dateTimeObj = datetime.now()
    timestampStr = dateTimeObj.strftime("%d-%b-%Y-%H-%M-%S-%f")
    inventory.replace("/",'_')
    backup_ansible_hosts="/tmp/"+inventory.replace("/",'_')+"."+timestampStr
    shutil.copyfile(inventory,backup_ansible_hosts)
    tmp_file_do_not_edit="/tmp/"+inventory.replace("/",'_')+".do_not_edit"
    if os.path.isfile(tmp_file_do_not_edit):
        print("File "+tmp_file_do_not_edit+" exist, it means previous reconfigure had failed. Hence updating inventory to previous state")
        shutil.move(tmp_file_do_not_edit,inventory)

def destroy_unreachable_reconfigure(inventory,nodes_to_remove,playbook):
    if not os.path.isfile("/etc/ansible/hosts"):
        print("There is no inventory file, are you on the controller? The cluster has not been resized")
        exit()
    backup_inventory(inventory)
    inventory_dict = parse_inventory(inventory)
    tmp_inventory_destroy="/tmp/"+inventory.replace('/','_')+"_destroy"
    ips_to_remove = []
    for host in nodes_to_remove:
        hostRemoved=False
        for line in inventory_dict['compute_configured']:
            if inventory_host_line_matches(line, host):
                inventory_dict['compute_configured'].remove(line)
                ips_to_remove.append(line.split("ansible_host=")[1].split("ansible_user=")[0].strip())
                hostRemoved=True
        for line in inventory_dict['compute_to_add']:
            if inventory_host_line_matches(line, host):
                inventory_dict['compute_to_add'].remove(line)
                ips_to_remove.append(line.split("ansible_host=")[1].split("ansible_user=")[0].strip())
                hostRemoved=True
        for line in inventory_dict['nfs']:
            if inventory_host_line_matches(line, host):
                inventory_dict['nfs'].remove(line)
    if len(ips_to_remove) != len(nodes_to_remove):
        instances = get_instances(comp_ocid,cn_ocid,CN)
        for instance in instances:
            if instance['display_name'] in nodes_to_remove and not instance['ip'] in ips_to_remove:
                ips_to_remove.append(instance['ip'])
        if len(ips_to_remove) != len(nodes_to_remove):
            print("Some nodes are removed in OCI and removed from the inventory")
            print("Try rerunning with the --nodes option and a list of IPs or Slurm Hostnames to cleanup the controller")
    write_inventory(inventory_dict,tmp_inventory_destroy)
    update_flag = 0
    if not len(ips_to_remove):
        print("No hostname found, trying anyway with "+" ".join(nodes_to_remove))
        for node in nodes_to_remove: # Temporary fix while the playbook is changed to be able to run multiple at the time
            node_update_flag = update_cluster(tmp_inventory_destroy,playbook,add_vars={"unreachable_node_list":node})
            if node_update_flag != 0 and update_flag == 0:
                update_flag = node_update_flag
            time.sleep(10)
    else:
        for ip in ips_to_remove: # Temporary fix while the playbook is changed to be able to run multiple at the time
            node_update_flag = update_cluster(tmp_inventory_destroy,playbook,add_vars={"unreachable_node_list":ip})
            if node_update_flag != 0 and update_flag == 0:
                update_flag = node_update_flag
            time.sleep(10)
    if update_flag == 0:
        os.remove(tmp_inventory_destroy)
        inventory_dict['compute_to_destroy']=[]
        tmp_inventory="/tmp/"+inventory.replace('/','_')
        write_inventory(inventory_dict,tmp_inventory)
        subprocess.run(
            ["sudo", "mv", tmp_inventory, inventory],
            check=True,
        )
    return update_flag

def destroy_reconfigure(inventory,nodes_to_remove,playbook):
    if not os.path.isfile("/etc/ansible/hosts"):
        print("There is no inventory file, are you on the controller? The cluster has not been resized")
        exit()
    backup_inventory(inventory)
    inventory_dict = parse_inventory(inventory)
    inventory_dict['compute_to_destroy']=[]
    instances = get_instances(comp_ocid,cn_ocid,CN)
    nodes_to_remove_instances = [{'ip':node,'display_name':node} for node in nodes_to_remove ]
    username="opc"
    for inv_vars in inventory_dict["all:vars"]:
        if inv_vars.startswith("compute_username"):
            username=inv_vars.split("compute_username=")[1].strip()
            break
    if remove_unreachable:
        reachable_instances,unreachable_instances = getreachable(instances,username)
        reachable_node_to_remove,unreachable_node_to_remove = getreachable(nodes_to_remove_instances,username)
    else:
        reachable_instances=instances
        unreachable_instances=[]
        reachable_node_to_remove=nodes_to_remove_instances
        unreachable_node_to_remove=[]
    for host in nodes_to_remove:
        compute_to_remove=[]
        nfs_to_remove=[]
        for line in inventory_dict['compute_configured']:
            if inventory_host_line_matches(line, host):
                if host in [node['display_name'] for node in reachable_node_to_remove ]:
                    inventory_dict['compute_to_destroy'].append(line)
                compute_to_remove.append(line)
        for line in inventory_dict['compute_to_add']:
            if inventory_host_line_matches(line, host):
                if host in [node['display_name'] for node in reachable_node_to_remove ]:
                    inventory_dict['compute_to_destroy'].append(line)
                compute_to_remove.append(line)
        for line in inventory_dict['nfs']:
            if inventory_host_line_matches(line, host):
                if host in [node['display_name'] for node in reachable_node_to_remove ]:
                    nfs_to_remove.append(line)
        for line in compute_to_remove:
            inventory_dict['compute_configured'].remove(line)
        for line in nfs_to_remove:
            inventory_dict['nfs'].remove(line)
    for instance in unreachable_instances:
        for line in inventory_dict['compute_configured']:
            if inventory_host_line_matches(line, instance['display_name']):
                inventory_dict['compute_configured'].remove(line)
        for line in inventory_dict['compute_to_add']:
           if inventory_host_line_matches(line, instance['display_name']):
                inventory_dict['compute_to_add'].remove(line)
    tmp_inventory_destroy="/tmp/"+inventory.replace('/','_')+"_destroy"
    write_inventory(inventory_dict,tmp_inventory_destroy)
    update_flag = update_cluster(tmp_inventory_destroy,playbook)
    if update_flag == 0:
        os.remove(tmp_inventory_destroy)
        inventory_dict['compute_to_destroy']=[]
        tmp_inventory="/tmp/"+inventory.replace('/','_')
        write_inventory(inventory_dict,tmp_inventory)
        os.system('sudo mv '+tmp_inventory+' '+inventory)
        os.system('')
    return update_flag

def add_reconfigure(comp_ocid,cn_ocid,inventory,CN,specific_hosts=None):
    if not os.path.isfile(inventory):
        print("There is no inventory file, are you on the controller? The cluster has been resized but not reconfigured")
        exit()
    instances = get_instances(comp_ocid,cn_ocid,CN)
    backup_inventory(inventory)
    inventory_dict = parse_inventory(inventory)
    username="opc"
    for inv_vars in inventory_dict["all:vars"]:
        if inv_vars.startswith("compute_username"):
            username=inv_vars.split("compute_username=")[1].strip()
            break
    reachable_instances=instances
    unreachable_instances=[]
    host_to_wait_for=[]
    for node in reachable_instances:
        name=node['display_name']
        ip=node['ip']
        configured=False
        for line in inventory_dict['compute_configured']:
            if inventory_host_line_matches(line, name, ip):
                configured = True
                break
        if not configured:
            nodeline=compute_inventory_line(node,username)
            if not specific_hosts is None:
                if name in specific_hosts:
                    inventory_dict['compute_to_add'].append(nodeline)
                else:
                    inventory_dict['compute_configured'].append(nodeline)
            else:
                inventory_dict['compute_to_add'].append(nodeline)
            host_to_wait_for.append(ip)
    if len(inventory_dict['nfs'])==0:
        if len(inventory_dict['compute_to_add']) > 0:
            inventory_dict['nfs'].append(inventory_dict['compute_to_add'][0])
        elif len(inventory_dict['compute_configured']) > 0:
            inventory_dict['nfs'].append(inventory_dict['compute_configured'][0])
    hostfile=open("/tmp/hosts_"+cluster_name,'w')
    hostfile.write("\n".join(host_to_wait_for))
    hostfile.close()
    tmp_inventory_add="/tmp/"+inventory.replace('/','_')+"_add"
    write_inventory(inventory_dict,tmp_inventory_add)
    prepare_local_block_volume_inventory(tmp_inventory_add)
    inventory_dict = parse_inventory(tmp_inventory_add)
    update_flag = update_cluster(tmp_inventory_add,playbooks_dir+"resize_add.yml",hostfile="/tmp/hosts_"+cluster_name)
    if update_flag == 0:
        os.remove(tmp_inventory_add)
        for line in inventory_dict['compute_to_add']:
            inventory_dict['compute_configured'].append(line)
        inventory_dict['compute_to_add']=[]
        tmp_inventory="/tmp/"+inventory.replace('/','_')
        write_inventory(inventory_dict,tmp_inventory)
        move_status = subprocess.run(["sudo", "mv", tmp_inventory, inventory]).returncode
        if move_status != 0:
            print("Failed to install the updated inventory")
            return 1
        if autoscaling:
            try:
                synchronize_autoscaling_compute_names(
                    comp_ocid,
                    inventory,
                    cluster_name,
                    expected_instance_pool_id=(cn_ocid if CN == "IP" else None),
                    expected_cluster_network_id=(cn_ocid if CN == "CN" else None),
                    expected_compute_cluster_id=(cn_ocid if CN == "CC" else None),
                )
            except Exception as error:
                print("Compute OS hostname synchronization failed: "+str(error))
                return 1
    else:
        print("The reconfiguration to add the node(s) had an error")
        print("Try rerunning this command: ansible-playbook -i "+tmp_inventory_add+' '+playbooks_dir+"resize_add.yml" )
    return update_flag

def reconfigure(comp_ocid,cn_ocid,inventory,CN, crucial=False):
    if not os.path.isfile(inventory):
        print("There is no inventory file, are you on the controller? Reconfigure did not happen")
        exit()
    instances = get_instances(comp_ocid,cn_ocid,CN)
    backup_inventory(inventory)
    inventory_dict = parse_inventory(inventory)
    host_to_wait_for=[]
    inventory_dict['compute_configured']=[]
    inventory_dict['compute_to_add']=[]
    username="opc"
    for inv_vars in inventory_dict["all:vars"]:
        if inv_vars.startswith("compute_username"):
            username=inv_vars.split("compute_username=")[1].strip()
            break
    for node in instances:
        name=node['display_name']
        ip=node['ip']
        nodeline=compute_inventory_line(node,username)
        inventory_dict['compute_configured'].append(nodeline)
        host_to_wait_for.append(ip)
    if len(inventory_dict['nfs'])==0:
        if len(inventory_dict['compute_to_add']) > 0:
            inventory_dict['nfs'].append(inventory_dict['compute_to_add'][0])
        elif len(inventory_dict['compute_configured']) > 0:
            inventory_dict['nfs'].append(inventory_dict['compute_configured'][0])
    hostfile=open("/tmp/hosts_"+cluster_name,'w')
    hostfile.write("\n".join(host_to_wait_for))
    hostfile.close()
    tmp_inventory_reconfig="/tmp/"+inventory.replace('/','_')+"_reconfig"
    write_inventory(inventory_dict,tmp_inventory_reconfig)
    prepare_local_block_volume_inventory(tmp_inventory_reconfig)
    if autoscaling:
        playbook=playbooks_dir+"new_nodes.yml"
    else:
        playbook=playbooks_dir+"site.yml"
    if crucial:
        playbook=playbooks_dir+"resize_remove.yml"
    update_flag = update_cluster(tmp_inventory_reconfig,playbook,hostfile="/tmp/hosts_"+cluster_name)
    if update_flag == 0:
        move_status = subprocess.run(
            ["sudo", "mv", tmp_inventory_reconfig, inventory]
        ).returncode
        if move_status != 0:
            print("Failed to install the reconfigured inventory")
            return 1
        if autoscaling:
            try:
                synchronize_autoscaling_compute_names(
                    comp_ocid,
                    inventory,
                    cluster_name,
                    expected_instance_pool_id=(cn_ocid if CN == "IP" else None),
                    expected_cluster_network_id=(cn_ocid if CN == "CN" else None),
                    expected_compute_cluster_id=(cn_ocid if CN == "CC" else None),
                )
            except Exception as error:
                print("Compute OS hostname synchronization failed: "+str(error))
                return 1
    else:
        print("The reconfiguration had an error")
        print("Try rerunning this command: ansible-playbook -i "+tmp_inventory_reconfig+' '+playbook )
    return update_flag

def getreachable(instances,username,delay=0):
    if delay == 0 :
        delays=[0]
    else:
        delays=range(0,delay,int(delay/1))#change 1 back to 10
    
    reachable_ips=[]
    for i in delays:
        input_file=open('/tmp/input_hosts_to_check_'+cluster_name,'w')
        for node in instances:
            if not node['ip'] in reachable_ips:
                input_file.write(node['ip']+"\n")
        input_file.close()
        my_env = os.environ.copy()
        my_env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
        p = subprocess.Popen(["/opt/oci-hpc/bin/find_reachable_hosts.sh","/tmp/input_hosts_to_check_"+cluster_name,"/tmp/reachable_hosts_"+cluster_name,username,"0"],env=my_env,stderr = subprocess.PIPE, stdout=subprocess.PIPE)
        while True:
            output = p.stdout.readline().decode()
            if output == '' and p.poll() is not None:
                break
            if output:
                print(output.strip())
        output_file=open('/tmp/reachable_hosts_'+cluster_name,'r')
        for line in output_file:
            reachable_ips.append(line.strip())
        output_file.close()
        if len(instances)==len(reachable_ips):
            break
        if i != delays[-1]:
            time.sleep(int(delay/10))
    reachable_instances=[]
    unreachable_instances=[]
    for ip in reachable_ips:
        added=False
        for node in instances:
            if node['ip']==ip:
                reachable_instances.append(node)
                added=True
    for node in instances:
        if not node in reachable_instances:
            unreachable_instances.append(node)
    return reachable_instances,unreachable_instances

def update_cluster(inventory,playbook,hostfile=None,add_vars={}):
    my_env = os.environ.copy()
    my_env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    rc = 0
    inventory_dict = parse_inventory(inventory)
    username="opc"
    for inv_vars in inventory_dict["all:vars"]:
        if inv_vars.startswith("compute_username"):
            username=inv_vars.split("compute_username=")[1].strip()
            break
    if not hostfile is None:
        p = subprocess.Popen(["/opt/oci-hpc/bin/wait_for_hosts.sh",hostfile,username],env=my_env,stderr=subprocess.STDOUT,stdout=subprocess.PIPE)
        while True:
            output = p.stdout.readline().decode()
            if output == '' and p.poll() is not None:
                break
            if output:
                print(output.strip())
        rc = p.wait()
        if (rc != 0):
            print("The hosts did not come up for SSH, not reconfiguring")
            return 2
    for add_var in add_vars.keys():
        my_env[add_var] = add_vars[add_var]
    p = subprocess.Popen(["ansible-playbook","-i",inventory,playbook],env=my_env,stderr=subprocess.STDOUT,stdout=subprocess.PIPE)
    while True:
        output = p.stdout.readline().decode()
        if output == '' and p.poll() is not None:
            break
        if output:
            print(output.strip())
    rc = p.wait()
    tmp_file_do_not_edit="/tmp/"+inventory.replace("/",'_')+".do_not_edit"
    if (rc == 0):
        print("success")
        if os.path.isfile(tmp_file_do_not_edit):
            os.remove(tmp_file_do_not_edit)
        return 0
    else:
        print("return code from ansible playbook job was non-zero, review what failed during ansible tasks run : "+str(rc))
        if os.path.isfile(tmp_file_do_not_edit):
            shutil.move(tmp_file_do_not_edit, "/tmp/etc_ansible_hosts.do_not_edit.old")
        print("Resolve the issue which caused ansible playbook to fail (hint: look for word fatal in above output). Then run the below command to only run the reconfigure step (ansible playbook) without again adding or removing node from HPC/GPU cluster.")
        return 1
        #if mode == 'add':
        #    print("Command:  python3 playbooks/resize.py reconfigure --nodes newly_added_node1_hostname newly_added_node2_hostname ")
        #if mode == 'remove':
        #    print("Command:  python3 playbooks/resize.py reconfigure --slurm_only_update true ")

def getNFSnode(inventory):
    dict = parse_inventory(inventory)
    if dict is None:
        return ''
    if len(dict['nfs']) == 0:
        return ''
    if dict['nfs'][0] == '\n':
        return ''
    else:
        return dict['nfs'][0].split()[0]

def get_summary(comp_ocid,cluster_name):
    CN = "CN"
    cn_summaries = computeManagementClient.list_cluster_networks(comp_ocid,display_name=cluster_name).data
    running_clusters = 0
    scaling_clusters = 0
    cn_summary=None
    for cn_summary_tmp in cn_summaries:
        if cn_summary_tmp.lifecycle_state == "RUNNING":
            cn_summary = cn_summary_tmp
            running_clusters = running_clusters + 1
        elif cn_summary_tmp.lifecycle_state == "SCALING":
            scaling_clusters = scaling_clusters + 1
    if running_clusters == 0:
        try: 
            cn_summaries = computeClient.list_compute_clusters(comp_ocid,display_name=cluster_name).data.items
        except:
            print("The list_compute_clusters call returned an error, considering no Compute CLusters are present")
            cn_summaries = []
        if len(cn_summaries) > 0:
            CN = "CC"
            for cn_summary_tmp in cn_summaries:
                if cn_summary_tmp.lifecycle_state == "ACTIVE" and cn_summary_tmp.display_name == cluster_name :
                    cn_summary = cn_summary_tmp
                    running_clusters = running_clusters + 1 
        if running_clusters == 0:
            cn_summaries = computeManagementClient.list_instance_pools(comp_ocid,display_name=cluster_name).data
            if len(cn_summaries) > 0:
                CN = "IP"
                for cn_summary_tmp in cn_summaries:
                    if cn_summary_tmp.lifecycle_state == "RUNNING":
                        cn_summary = cn_summary_tmp
                        running_clusters = running_clusters + 1 
                    elif cn_summary_tmp.lifecycle_state == "SCALING":
                        scaling_clusters = scaling_clusters + 1
            if running_clusters == 0:
                if scaling_clusters:
                    print("No running cluster was found but there is a cluster in SCALING mode, try rerunning in a moment")
                else:
                    print("The cluster was not found")
                return None,None,True
    if running_clusters > 1:
        print("There were multiple running clusters with this name, we selected the one with OCID:"+cn_summary.id)
    if CN == "CN":
        ip_summary=cn_summary.instance_pools[0]
    elif CN == "CC":
        ip_summary=None
    else:
        ip_summary=cn_summary
    return cn_summary,ip_summary,CN

def get_summary_for_operation(
    compartment_id,
    cluster_name,
    inventory_path,
    inventory_dict,
    mode,
    autoscaling,
    monitoring_output=False,
):
    requires_exact_managed_pool = autoscaling and (
        mode in [
            "add",
            "remove",
            "remove_unreachable",
            "reconfigure",
            "sync_instance_pool_names",
        ]
        or (mode == "list" and monitoring_output)
    )
    if autoscaling:
        direct_instance_pool_id = get_tracked_instance_pool_id(inventory_path)
        tracked_cluster_network_id = get_tracked_cluster_network_id(
            inventory_path
        )
        compute_cluster_resources = get_tracked_compute_cluster_resources(
            inventory_path
        )
        tracked_compute_cluster_is_present = has_tracked_compute_cluster_resources(
            compute_cluster_resources
        )
        if sum(
            deployment_is_present
            for deployment_is_present in [
                direct_instance_pool_id is not None,
                tracked_cluster_network_id is not None,
                tracked_compute_cluster_is_present,
            ]
        ) > 1:
            raise RuntimeError(
                "Terraform state identifies multiple compute deployment types"
            )
        if tracked_cluster_network_id is not None:
            cluster_network, embedded_pool = (
                get_tracked_cluster_network_for_hostname_sync(
                    inventory_path,
                    compartment_id,
                    cluster_name,
                    require_running=requires_exact_managed_pool,
                )
            )
            return cluster_network, embedded_pool, "CN"
        if direct_instance_pool_id is not None:
            instance_pool = get_tracked_instance_pool_for_hostname_sync(
                inventory_path,
                compartment_id,
                cluster_name,
            )
            return instance_pool, instance_pool, "IP"
        if requires_exact_managed_pool:
            if (
                compute_cluster_resources is not None
                and compute_cluster_resources[0] is not None
            ):
                tracked_compute_cluster_id = compute_cluster_resources[0]
                compute_cluster = computeClient.get_compute_cluster(
                    tracked_compute_cluster_id,
                    **get_oci_retry_kwargs(),
                ).data
                validate_compute_cluster_identity(
                    compute_cluster,
                    tracked_compute_cluster_id,
                    compartment_id,
                    cluster_name,
                )
                return compute_cluster, None, "CC"
            if tracked_compute_cluster_is_present:
                raise RuntimeError(
                    "Terraform state identifies Compute Cluster instances "
                    "without an exact Compute Cluster identity"
                )
            raise RuntimeError(
                "Terraform state does not identify an Autoscaling compute deployment"
            )
    return get_summary(compartment_id, cluster_name)

def get_exact_instance_pool_size(
    instance_pool_id,
    compartment_id,
    expected_display_name=None,
):
    """Return the live size of one exact Instance Pool after validating ownership."""
    instance_pool = computeManagementClient.get_instance_pool(instance_pool_id).data
    pool_size = getattr(instance_pool, "size", None)
    if (
        getattr(instance_pool, "id", instance_pool_id) != instance_pool_id
        or getattr(instance_pool, "compartment_id", None) != compartment_id
        or (
            expected_display_name is not None
            and getattr(instance_pool, "display_name", None)
            != expected_display_name
        )
        or isinstance(pool_size, bool)
        or not isinstance(pool_size, int)
        or pool_size < 0
    ):
        raise RuntimeError("The Instance Pool has an invalid identity or size")
    return pool_size

def reconcile_instance_pool_post_resize_state(
    inventory_path,
    compartment_id,
    recovery_document,
):
    recovery_document = validate_instance_pool_post_resize_recovery(
        recovery_document
    )
    live_size = get_exact_instance_pool_size(
        recovery_document["instance_pool_id"],
        compartment_id,
        expected_display_name=recovery_document["cluster_name"],
    )
    if recovery_document["version"] == 2:
        if recovery_document["action"] == "add":
            size_is_within_boundary = live_size in {
                recovery_document["source_size"],
                recovery_document["target_size"],
            }
        else:
            size_is_within_boundary = (
                recovery_document["target_size"]
                <= live_size
                <= recovery_document["source_size"]
            )
        if not size_is_within_boundary:
            raise RuntimeError(
                "The Instance Pool size no longer matches the pending resize boundary"
            )
    updateTFState(
        inventory_path,
        recovery_document["cluster_name"],
        live_size,
    )
    return live_size

def updateTFState(inventory,cluster_name,size):
    inventory_path = os.path.abspath(inventory)
    if inventory_path == "/etc/ansible/hosts":
        return False
    cluster_directory = os.path.dirname(inventory_path)
    state_path = os.path.join(cluster_directory, "terraform.tfstate")
    variables_path = os.path.join(cluster_directory, "variables.tf")
    if not os.path.isfile(state_path):
        raise RuntimeError("Terraform state was not found next to inventory "+inventory_path)
    if not os.path.isfile(variables_path):
        raise RuntimeError("Terraform variables file was not found next to inventory "+inventory_path)

    state_fd, temporary_state_path = tempfile.mkstemp(
        prefix=".oci-hpc-resize-state-",
        suffix=".tfstate",
        dir=cluster_directory,
        text=True,
    )
    variables_fd, temporary_variables_path = tempfile.mkstemp(
        prefix=".oci-hpc-resize-variables-",
        suffix=".tf",
        dir=cluster_directory,
        text=True,
    )
    try:
        found_serial = False
        found_pool_size = False
        with open(state_path, "r") as state_file, os.fdopen(state_fd, "w") as temporary_state:
            state_fd = None
            for line in state_file:
                stripped_line = line.strip()
                if stripped_line.startswith('"serial":'):
                    serial = int(stripped_line.split('"serial":', 1)[1].split(',', 1)[0])
                    temporary_state.write(line.replace(str(serial), str(serial+1), 1))
                    found_serial = True
                elif stripped_line.startswith('"size":'):
                    current_size = int(stripped_line.split('"size":', 1)[1].split(',', 1)[0])
                    temporary_state.write(line.replace(str(current_size), str(size), 1))
                    found_pool_size = True
                else:
                    temporary_state.write(line)
            temporary_state.flush()
            os.fsync(temporary_state.fileno())
        if not found_serial or not found_pool_size:
            raise RuntimeError("Terraform state does not contain the expected serial and instance pool size")

        found_node_count = False
        variables_stat = os.stat(variables_path)
        with open(variables_path, "r") as variables_file, os.fdopen(variables_fd, "w") as temporary_variables:
            variables_fd = None
            for line in variables_file:
                if line.strip().startswith('variable "node_count"'):
                    node_count_match = re.search(
                        r'\bdefault\s*=\s*(?P<quote>"?)(?P<value>[0-9]+)(?P=quote)',
                        line,
                    )
                    if node_count_match is None:
                        raise RuntimeError("Terraform node_count variable has an unsupported format")
                    temporary_variables.write(
                        line[:node_count_match.start("value")]
                        +str(size)
                        +line[node_count_match.end("value"):]
                    )
                    found_node_count = True
                else:
                    temporary_variables.write(line)
            temporary_variables.flush()
            os.fsync(temporary_variables.fileno())
        if not found_node_count:
            raise RuntimeError("Terraform variables do not contain node_count")
        os.chmod(temporary_variables_path, stat.S_IMODE(variables_stat.st_mode))
        try:
            os.chown(temporary_variables_path, variables_stat.st_uid, variables_stat.st_gid)
        except PermissionError:
            if variables_stat.st_uid != os.getuid() or variables_stat.st_gid != os.getgid():
                raise

        try:
            subprocess.run(
                ["terraform", "state", "push", temporary_state_path],
                cwd=cluster_directory,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError("Failed to update Terraform state: "+str(error))
        os.replace(temporary_variables_path, variables_path)
        temporary_variables_path = None
        fsync_directory(cluster_directory)
        return True
    finally:
        if state_fd is not None:
            os.close(state_fd)
        if variables_fd is not None:
            os.close(variables_fd)
        for temporary_path in [temporary_state_path, temporary_variables_path]:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)

def find_cluster_instance_by_name(
    compartment_id,
    instance_name,
    expected_cluster_name,
    inventory_for_resolution=None,
    allowed_instance_ids=None,
):
    if allowed_instance_ids is not None:
        allowed_instance_ids = set(allowed_instance_ids)
    if inventory_for_resolution is not None:
        inventory_matches = []
        for section in ["compute_configured", "compute_to_add", "compute_to_destroy"]:
            for line in inventory_for_resolution.get(section, []):
                parsed_line = split_inventory_host_line(line)
                if parsed_line is None or not parsed_line[1]:
                    continue
                inventory_hostname = parsed_line[1][0]
                instance_id = get_inventory_token(line, "oci_instance_id")
                if instance_id is not None and inventory_hostname == instance_name:
                    inventory_matches.append(instance_id)
        if len(set(inventory_matches)) > 1:
            raise RuntimeError(
                "Inventory hostname "+instance_name+" maps to multiple instances"
            )
        if inventory_matches:
            if (
                allowed_instance_ids is not None
                and inventory_matches[0] not in allowed_instance_ids
            ):
                raise RuntimeError("Inventory instance is not an active member of the Instance Pool")
            instance = computeClient.get_instance(inventory_matches[0]).data
            tags = instance.freeform_tags or {}
            parent_cluster = tags.get("parent_cluster", tags.get("cluster_name"))
            if (
                instance.compartment_id != compartment_id
                or (
                    parent_cluster is not None
                    and parent_cluster != expected_cluster_name
                )
            ):
                raise RuntimeError(
                    "Inventory instance "+instance.id+" does not belong to cluster "+
                    expected_cluster_name
                )
            return instance if instance.lifecycle_state != "TERMINATED" else None

    instances = oci.pagination.list_call_get_all_results(
        computeClient.list_instances,
        compartment_id=compartment_id,
        display_name=instance_name,
    ).data
    matches = []
    for instance in instances:
        tags = instance.freeform_tags or {}
        parent_cluster = tags.get("parent_cluster", tags.get("cluster_name"))
        exact_pool_member = (
            allowed_instance_ids is not None
            and instance.id in allowed_instance_ids
        )
        if (
            instance.display_name == instance_name
            and instance.lifecycle_state != "TERMINATED"
            and (
                (allowed_instance_ids is None and parent_cluster == expected_cluster_name)
                or (
                    exact_pool_member
                    and (
                        parent_cluster is None
                        or parent_cluster == expected_cluster_name
                    )
                )
            )
        ):
            matches.append(instance)
    if len(matches) > 1:
        raise RuntimeError("Multiple active instances named "+instance_name+" belong to cluster "+expected_cluster_name)
    return matches[0] if matches else None

def instance_is_pool_member(compartment_id, instance_pool_id, instance_id):
    instances = oci.pagination.list_call_get_all_results(
        computeManagementClient.list_instance_pool_instances,
        compartment_id,
        instance_pool_id,
    ).data
    return any(instance.id == instance_id for instance in instances)

def detach_instance_from_pool_if_needed(compartment_id, instance_pool_id, instance_id):
    if not instance_is_pool_member(compartment_id, instance_pool_id, instance_id):
        return
    instance_details = oci.core.models.DetachInstancePoolInstanceDetails(
        instance_id=instance_id,
        is_auto_terminate=False,
        is_decrement_size=True,
    )
    try:
        ComputeManagementClientCompositeOperations.detach_instance_pool_instance_and_wait_for_work_request(
            instance_pool_id,
            instance_details,
        )
    except Exception as detach_error:
        deadline = time.time()+120
        while time.time() < deadline:
            try:
                if not instance_is_pool_member(compartment_id, instance_pool_id, instance_id):
                    return
            except Exception:
                pass
            time.sleep(5)
        raise detach_error

def get_exclusive_managed_local_block_volume_attachment(compartment_id, instance_id):
    instance = computeClient.get_instance(instance_id).data
    expected_config = get_instance_local_block_volume_config(instance, inventory_dict, cluster_name)
    if not expected_config.get("enabled", False):
        return None
    attachments = oci.pagination.list_call_get_all_results(
        computeClient.list_volume_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id,
    ).data
    active_local_attachments = [
        attachment for attachment in attachments
        if attachment.device == LOCAL_BLOCK_VOLUME_DEVICE
        and attachment.lifecycle_state != "DETACHED"
    ]
    # A partially launched or already cleaned-up instance can carry the feature
    # tags without having a live vdc attachment.  There is no scratch volume to
    # delete in that case, so preserve any other data volumes and continue.
    if not active_local_attachments:
        return None
    managed_attachment = get_local_block_volume_attachment(
        compartment_id,
        instance_id,
        max_wait_seconds=0,
        expected_config=expected_config,
        expected_cluster_name=cluster_name,
        instance=instance,
    )
    launch_created_attachments = [
        attachment for attachment in attachments
        if attachment.lifecycle_state != "DETACHED"
        and getattr(attachment, "is_volume_created_during_launch", None) is True
    ]
    if (
        getattr(managed_attachment, "is_volume_created_during_launch", None) is True
        and len(launch_created_attachments) > 1
    ):
        raise RuntimeError(
            "Refusing to terminate instance "+instance_id+
            " because deleting its managed local scratch volume would also delete another launch-created data volume"
        )
    return managed_attachment

def instance_has_exclusive_managed_local_block_volume(compartment_id, instance_id):
    managed_attachment = get_exclusive_managed_local_block_volume_attachment(compartment_id, instance_id)
    return (
        managed_attachment is not None
        and getattr(managed_attachment, "is_volume_created_during_launch", None) is True
    )

def delete_managed_local_block_volume(volume_id, max_wait_seconds=1800):
    deadline = time.time()+max_wait_seconds
    delete_requested = False
    while True:
        try:
            volume = blockstorageClient.get_volume(volume_id).data
        except oci.exceptions.ServiceError as error:
            if error.status == 404:
                return
            raise
        if volume.lifecycle_state == "TERMINATED":
            return
        if not delete_requested:
            try:
                blockstorageClient.delete_volume(volume_id)
                delete_requested = True
            except oci.exceptions.ServiceError as error:
                if error.status == 404:
                    return
                # Instance termination and iSCSI detach are asynchronous.  Retry
                # only the expected incorrect-state conflict until the attachment
                # has finished detaching.
                if error.status != 409:
                    raise
        if time.time() >= deadline:
            raise RuntimeError("Timed out while deleting managed local scratch volume "+volume_id)
        time.sleep(5)

def terminate_instance_and_delete_launch_volumes(instance_id, max_wait_seconds=1800, delete_launch_created_data_volumes=None):
    try:
        state = computeClient.get_instance(instance_id).data.lifecycle_state
    except oci.exceptions.ServiceError as error:
        if error.status == 404:
            return
        raise
    if state == "TERMINATED":
        return
    if state != "TERMINATING":
        if delete_launch_created_data_volumes is None:
            delete_launch_created_data_volumes = instance_has_exclusive_managed_local_block_volume(comp_ocid,instance_id)
        try:
            computeClient.terminate_instance(
                instance_id,
                preserve_data_volumes_created_at_launch=not delete_launch_created_data_volumes,
            )
        except oci.exceptions.ServiceError as error:
            if error.status == 404:
                return
            raise
    deadline = time.time()+max_wait_seconds
    while True:
        try:
            state = computeClient.get_instance(instance_id).data.lifecycle_state
        except oci.exceptions.ServiceError as error:
            if error.status == 404:
                return
            raise
        if state == "TERMINATED":
            return
        if time.time() >= deadline:
            raise RuntimeError("Timed out while terminating instance "+instance_id)
        time.sleep(10)

def get_pending_local_block_volume_deletions_path(inventory_path):
    return os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        PENDING_LOCAL_BLOCK_VOLUME_DELETIONS_FILENAME,
    )

def fsync_directory(directory_path):
    directory_fd = os.open(directory_path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

def validate_pending_local_block_volume_deletion_record(record):
    required_string_keys = [
        "cluster_name",
        "compartment_id",
        "instance_id",
        "instance_display_name",
        "instance_private_ip",
        "instance_pool_id",
        "volume_id",
        "volume_display_name",
    ]
    if not isinstance(record, dict):
        raise RuntimeError("The pending local Block Volume deletion record is not an object")
    for key in required_string_keys:
        if not isinstance(record.get(key), str) or not record[key]:
            raise RuntimeError("The pending local Block Volume deletion record has an invalid "+key)
    if (
        isinstance(record.get("size_in_gbs"), bool)
        or not isinstance(record.get("size_in_gbs"), int)
        or record["size_in_gbs"] < 50
    ):
        raise RuntimeError("The pending local Block Volume deletion record has an invalid size_in_gbs")
    if record.get("vpus_per_gb") not in [0, 10, 20]:
        raise RuntimeError("The pending local Block Volume deletion record has an invalid vpus_per_gb")
    if (
        isinstance(record.get("instance_pool_size_before_removal"), bool)
        or not isinstance(record.get("instance_pool_size_before_removal"), int)
        or record["instance_pool_size_before_removal"] < 1
    ):
        raise RuntimeError(
            "The pending local Block Volume deletion record has an invalid instance_pool_size_before_removal"
        )
    return record

def load_pending_local_block_volume_deletions(inventory_path):
    pending_path = get_pending_local_block_volume_deletions_path(inventory_path)
    if not os.path.isfile(pending_path):
        return []
    try:
        with open(pending_path, "r") as pending_file:
            pending_document = json.load(pending_file)
    except (OSError, ValueError) as error:
        raise RuntimeError("Failed to read pending local Block Volume deletions: "+str(error))
    if (
        not isinstance(pending_document, dict)
        or pending_document.get("version") != 1
        or not isinstance(pending_document.get("deletions"), list)
    ):
        raise RuntimeError("The pending local Block Volume deletion file has an invalid format")
    pending_deletions = []
    seen_volume_ids = set()
    for record in pending_document["deletions"]:
        validate_pending_local_block_volume_deletion_record(record)
        if record["volume_id"] in seen_volume_ids:
            raise RuntimeError("The pending local Block Volume deletion file contains a duplicate volume OCID")
        seen_volume_ids.add(record["volume_id"])
        pending_deletions.append(record)
    return pending_deletions

def write_pending_local_block_volume_deletions(inventory_path, pending_deletions):
    pending_path = get_pending_local_block_volume_deletions_path(inventory_path)
    if not pending_deletions:
        deletion_changed_directory = False
        try:
            os.unlink(pending_path)
            deletion_changed_directory = True
        except FileNotFoundError:
            pass
        if deletion_changed_directory:
            fsync_directory(os.path.dirname(pending_path))
        return
    for record in pending_deletions:
        validate_pending_local_block_volume_deletion_record(record)
    pending_directory = os.path.dirname(pending_path)
    temp_fd, temp_path = tempfile.mkstemp(
        prefix=".oci-hpc-pending-volume-deletions-",
        dir=pending_directory,
        text=True,
    )
    try:
        with os.fdopen(temp_fd, "w") as pending_file:
            json.dump(
                {"version": 1, "deletions": pending_deletions},
                pending_file,
                indent=2,
                sort_keys=True,
            )
            pending_file.write("\n")
            pending_file.flush()
            os.fsync(pending_file.fileno())
        os.chmod(temp_path, 0o600)
        if os.path.exists(inventory_path):
            inventory_stat = os.stat(inventory_path)
            try:
                os.chown(temp_path, inventory_stat.st_uid, inventory_stat.st_gid)
            except PermissionError:
                if inventory_stat.st_uid != os.getuid() or inventory_stat.st_gid != os.getgid():
                    raise
        os.replace(temp_path, pending_path)
        temp_path = None
        fsync_directory(pending_directory)
    finally:
        if temp_path is not None and os.path.exists(temp_path):
            os.unlink(temp_path)

def remember_pending_local_block_volume_deletion(inventory_path, record):
    validate_pending_local_block_volume_deletion_record(record)
    pending_deletions = load_pending_local_block_volume_deletions(inventory_path)
    for existing_record in pending_deletions:
        if existing_record["volume_id"] == record["volume_id"]:
            if existing_record != record:
                raise RuntimeError("The pending local Block Volume deletion record changed unexpectedly")
            return
    pending_deletions.append(record)
    write_pending_local_block_volume_deletions(inventory_path, pending_deletions)

def validate_pending_local_block_volume(record, volume, expected_cluster_name, expected_compartment_id):
    if record["cluster_name"] != expected_cluster_name:
        raise RuntimeError("The pending local Block Volume belongs to another cluster")
    if record["compartment_id"] != expected_compartment_id:
        raise RuntimeError("The pending local Block Volume belongs to another compartment")
    volume_tags = volume.freeform_tags or {}
    volume_parent_cluster = volume_tags.get("parent_cluster", volume_tags.get("cluster_name"))
    if (
        record["volume_display_name"] != expected_cluster_name+"-local-scratch"
        or volume.display_name != record["volume_display_name"]
        or getattr(volume, "compartment_id", expected_compartment_id) != expected_compartment_id
        or volume_parent_cluster != expected_cluster_name
        or volume_tags.get("oci_hpc_local_scratch") != "true"
        or volume.size_in_gbs != record["size_in_gbs"]
        or volume.vpus_per_gb != record["vpus_per_gb"]
    ):
        raise RuntimeError("The pending local Block Volume no longer matches its ownership record")

def build_pending_local_block_volume_deletion_record(
    compartment_id,
    instance_pool_id,
    instance_id,
    instance_private_ip,
    volume_id,
):
    instance = computeClient.get_instance(instance_id).data
    instance_tags = instance.freeform_tags or {}
    if instance_tags.get("parent_cluster", instance_tags.get("cluster_name")) != cluster_name:
        raise RuntimeError("The local Block Volume instance belongs to another cluster")
    try:
        ipaddress.ip_address(instance_private_ip)
    except ValueError:
        raise RuntimeError("The local Block Volume instance has an invalid private IP address")
    instance_pool = computeManagementClient.get_instance_pool(instance_pool_id).data
    if (
        getattr(instance_pool, "id", instance_pool_id) != instance_pool_id
        or getattr(instance_pool, "compartment_id", compartment_id) != compartment_id
        or isinstance(instance_pool.size, bool)
        or not isinstance(instance_pool.size, int)
        or instance_pool.size < 1
    ):
        raise RuntimeError("The local Block Volume instance pool has an invalid identity or size")
    volume = blockstorageClient.get_volume(volume_id).data
    record = {
        "cluster_name": cluster_name,
        "compartment_id": compartment_id,
        "instance_id": instance_id,
        "instance_display_name": instance.display_name,
        "instance_private_ip": instance_private_ip,
        "instance_pool_id": instance_pool_id,
        "instance_pool_size_before_removal": instance_pool.size,
        "volume_id": volume_id,
        "volume_display_name": volume.display_name,
        "size_in_gbs": volume.size_in_gbs,
        "vpus_per_gb": volume.vpus_per_gb,
    }
    validate_pending_local_block_volume_deletion_record(record)
    validate_pending_local_block_volume(record, volume, cluster_name, compartment_id)
    return record

def inspect_pending_local_block_volume_deletion(
    record,
    expected_cluster_name,
    compartment_id,
):
    """Validate one journaled scratch volume and return its live state."""
    validate_pending_local_block_volume_deletion_record(record)
    if (
        record["cluster_name"] != expected_cluster_name
        or record["compartment_id"] != compartment_id
    ):
        raise RuntimeError(
            "A pending local Block Volume deletion does not match the requested cluster"
        )
    try:
        instance = computeClient.get_instance(record["instance_id"]).data
        instance_tags = instance.freeform_tags or {}
        if (
            instance_tags.get("parent_cluster", instance_tags.get("cluster_name"))
            != expected_cluster_name
        ):
            raise RuntimeError(
                "The pending local Block Volume instance belongs to another cluster"
            )
        if instance.display_name != record["instance_display_name"]:
            raise RuntimeError(
                "The pending local Block Volume instance name changed unexpectedly"
            )
        instance_state = instance.lifecycle_state
    except oci.exceptions.ServiceError as error:
        if error.status != 404:
            raise
        instance_state = "TERMINATED"

    # Validate the exact volume before detaching or terminating anything.  This
    # prevents a stale or tampered journal from turning a removal retry into an
    # arbitrary volume deletion.
    try:
        volume = blockstorageClient.get_volume(record["volume_id"]).data
    except oci.exceptions.ServiceError as error:
        if error.status != 404:
            raise
        volume = None
    if volume is not None:
        validate_pending_local_block_volume(
            record,
            volume,
            expected_cluster_name,
            compartment_id,
        )
    return instance_state, volume

def complete_pending_local_block_volume_deletion(
    record,
    expected_cluster_name,
    compartment_id,
    resume_pool_member=False,
):
    """Safely finish one journaled scratch-volume/node removal."""
    instance_state, volume = inspect_pending_local_block_volume_deletion(
        record,
        expected_cluster_name,
        compartment_id,
    )

    if instance_state != "TERMINATED":
        try:
            is_pool_member = instance_is_pool_member(
                compartment_id,
                record["instance_pool_id"],
                record["instance_id"],
            )
        except oci.exceptions.ServiceError as error:
            if error.status != 404:
                raise
            is_pool_member = False
        if is_pool_member:
            if not resume_pool_member:
                raise RuntimeError(
                    "Pending removal for "+record["instance_display_name"]+
                    " must be resumed explicitly with --nodes "+
                    record["instance_display_name"]
                )
            detach_instance_from_pool_if_needed(
                compartment_id,
                record["instance_pool_id"],
                record["instance_id"],
            )
        terminate_instance_and_delete_launch_volumes(
            record["instance_id"],
            delete_launch_created_data_volumes=False,
        )
    if volume is not None:
        delete_managed_local_block_volume(record["volume_id"])
    return record

def retry_pending_local_block_volume_deletions(
    inventory_path,
    expected_cluster_name,
    compartment_id,
    resume_pool_members=False,
    resume_pool_member_instance_ids=None,
    expected_instance_pool_id=None,
):
    pending_deletions = load_pending_local_block_volume_deletions(inventory_path)
    authorized_instance_ids = set(resume_pool_member_instance_ids or [])

    # Validate every pending member before the first detach, termination, or
    # volume deletion.  Already-detached records can still be completed while
    # only the exact requested OCIDs are authorized for a resumed detach.
    instance_ids = [record["instance_id"] for record in pending_deletions]
    instance_display_names = [
        record["instance_display_name"] for record in pending_deletions
    ]
    if len(instance_ids) != len(set(instance_ids)):
        raise RuntimeError(
            "Pending local Block Volume deletions contain duplicate instance OCIDs"
        )
    if len(instance_display_names) != len(set(instance_display_names)):
        raise RuntimeError(
            "Pending local Block Volume deletions contain duplicate instance names"
        )
    if (
        expected_instance_pool_id is not None
        and any(
            record["instance_pool_id"] != expected_instance_pool_id
            for record in pending_deletions
        )
    ):
        raise RuntimeError(
            "A pending local Block Volume deletion belongs to a different instance pool"
        )
    for record in pending_deletions:
        instance_state, _ = inspect_pending_local_block_volume_deletion(
            record,
            expected_cluster_name,
            compartment_id,
        )
        if instance_state == "TERMINATED":
            continue
        try:
            is_pool_member = instance_is_pool_member(
                compartment_id,
                record["instance_pool_id"],
                record["instance_id"],
            )
        except oci.exceptions.ServiceError as error:
            if error.status != 404:
                raise
            is_pool_member = False
        if (
            is_pool_member
            and not resume_pool_members
            and record["instance_id"] not in authorized_instance_ids
        ):
            raise RuntimeError(
                "Pending removal for "+record["instance_display_name"]+
                " must be resumed explicitly with --nodes "+
                record["instance_display_name"]
            )

    ready_deletions = []
    for record in list(pending_deletions):
        complete_pending_local_block_volume_deletion(
            record,
            expected_cluster_name,
            compartment_id,
            resume_pool_member=(
                resume_pool_members
                or record["instance_id"] in authorized_instance_ids
            ),
        )
        ready_deletions.append(record)
        print("STDOUT: Deleted pending local scratch volume "+record["volume_id"])
    return ready_deletions

def delete_private_dns_rrset_if_present(zone_id, domain):
    try:
        dns_client.delete_rr_set(
            zone_name_or_id=zone_id,
            domain=domain,
            rtype="A",
            scope="PRIVATE",
        )
    except oci.exceptions.ServiceError as error:
        if error.status != 404:
            raise

def get_pending_node_dns_domains(record):
    domains = [record["instance_display_name"]+"."+zone_name]
    if not slurm_enabled:
        return domains
    if queue is None or private_subnet_cidr is None:
        raise RuntimeError("Inventory does not contain queue and private_subnet values required for DNS cleanup")
    private_ip = ipaddress.ip_address(record["instance_private_ip"])
    try:
        host_index = list(private_subnet_cidr.hosts()).index(private_ip)+2
    except ValueError:
        raise RuntimeError(
            "The pending node private IP is outside private_subnet: "+record["instance_private_ip"]
        )
    domains.append(
        queue+"-"+instance_type+"-"+str(host_index)+"."+zone_name
    )
    return domains

def delete_pending_local_block_volume_dns_records_safely(
    records,
    compartment_id,
    allow_missing_zone=False,
):
    if not records:
        return 0
    zones = dns_client.list_zones(
        compartment_id=compartment_id,
        name=zone_name,
        zone_type="PRIMARY",
        scope="PRIVATE",
    ).data
    if len(zones) == 0:
        if allow_missing_zone:
            return len(records)
        raise RuntimeError("Private DNS zone "+zone_name+" was not found")
    if len(zones) != 1:
        raise RuntimeError(
            "Multiple private DNS zones matched pending local Block Volume cleanup"
        )
    zone_id = zones[0].id
    expected_dns_records = {}
    for record in records:
        expected_private_ip = str(
            ipaddress.ip_address(record["instance_private_ip"])
        )
        for domain in get_pending_node_dns_domains(record):
            domain_key = domain.lower()
            previous = expected_dns_records.get(domain_key)
            if previous is not None and previous[1] != expected_private_ip:
                raise RuntimeError(
                    "Pending local Block Volume removals claim the same DNS name"
                )
            expected_dns_records[domain_key] = (domain, expected_private_ip)

    # Verify every RRset before deleting the first one.  Empty RRsets are an
    # idempotent success when the generic node-removal journal deleted them.
    for domain, expected_private_ip in expected_dns_records.values():
        verify_private_dns_a_rrset_ownership(
            zone_id,
            domain,
            {expected_private_ip},
        )
    for domain, expected_private_ip in expected_dns_records.values():
        delete_private_dns_a_rrset_if_owned(
            zone_id,
            domain,
            {expected_private_ip},
        )
    return len(records)

def cleanup_pending_local_block_volume_dns_records(
    inventory_path,
    ready_deletions,
    expected_cluster_name,
    compartment_id,
):
    if not ready_deletions or not dns_entries:
        return 0
    pending_by_volume_id = {
        record["volume_id"]: record
        for record in load_pending_local_block_volume_deletions(inventory_path)
    }
    for record in ready_deletions:
        validate_pending_local_block_volume_deletion_record(record)
        if (
            record["cluster_name"] != expected_cluster_name
            or record["compartment_id"] != compartment_id
            or pending_by_volume_id.get(record["volume_id"]) != record
        ):
            raise RuntimeError("A pending DNS cleanup does not match the requested cluster")
    return delete_pending_local_block_volume_dns_records_safely(
        ready_deletions,
        compartment_id,
        allow_missing_zone=True,
    )

def finalize_pending_local_block_volume_deletions(
    inventory_path,
    ready_deletions,
    expected_cluster_name,
    compartment_id,
    current_pool_size=None,
    current_instance_pool_id=None,
    active_instance_display_names=None,
    active_instance_private_ips=None,
):
    if not ready_deletions:
        return 0
    pending_deletions = load_pending_local_block_volume_deletions(inventory_path)
    pending_by_volume_id = {
        record["volume_id"]: record for record in pending_deletions
    }
    for record in ready_deletions:
        validate_pending_local_block_volume_deletion_record(record)
        if (
            record["cluster_name"] != expected_cluster_name
            or record["compartment_id"] != compartment_id
        ):
            raise RuntimeError("A completed local Block Volume deletion does not match the requested cluster")
        if pending_by_volume_id.get(record["volume_id"]) != record:
            raise RuntimeError("The pending local Block Volume deletion record changed before finalization")

    if current_pool_size is None:
        raise RuntimeError("The current instance pool size is required to finalize node removal")
    if not current_instance_pool_id:
        raise RuntimeError("The current instance pool OCID is required to finalize node removal")
    if any(
        record["instance_pool_id"] != current_instance_pool_id
        for record in ready_deletions
    ):
        raise RuntimeError("The pending node removal belongs to a different instance pool")
    if any(
        current_pool_size >= record["instance_pool_size_before_removal"]
        for record in ready_deletions
    ):
        raise RuntimeError(
            "The instance pool size was not decremented for every pending node removal"
        )
    if dns_entries:
        if active_instance_display_names is None or active_instance_private_ips is None:
            raise RuntimeError("Active instance identities are required for DNS cleanup")
        if queue is None or private_subnet_cidr is None:
            raise RuntimeError("Inventory does not contain queue and private_subnet values required for DNS cleanup")
        normalized_active_display_names = {
            name.lower() for name in active_instance_display_names
        }
        for record in ready_deletions:
            private_ip = ipaddress.ip_address(record["instance_private_ip"])
            if (
                record["instance_display_name"].lower()
                in normalized_active_display_names
                or str(private_ip) in active_instance_private_ips
            ):
                raise RuntimeError(
                    "A pending node hostname or private IP is in use by an active instance"
                )
        delete_pending_local_block_volume_dns_records_safely(
            ready_deletions,
            compartment_id,
        )
    updateTFState(inventory_path, expected_cluster_name, current_pool_size)

    completed_volume_ids = {
        record["volume_id"] for record in ready_deletions
    }
    write_pending_local_block_volume_deletions(
        inventory_path,
        [
            record for record in pending_deletions
            if record["volume_id"] not in completed_volume_ids
        ],
    )
    return len(ready_deletions)

def process_pending_local_block_volume_deletions_for_operation(
    inventory_path,
    expected_cluster_name,
    compartment_id,
    mode,
    requested_hostnames,
    authorized_instance_ids=None,
    expected_instance_pool_id=None,
):
    pending_deletions = load_pending_local_block_volume_deletions(inventory_path)
    pending_instance_ids = {
        record["instance_id"] for record in pending_deletions
    }
    if authorized_instance_ids is None:
        # Backward-compatible recovery for a legacy local-volume journal that
        # predates the generic node-removal journal.  The retry preflight below
        # rejects duplicate names and enforces the current exact pool ID.
        requested_hostname_set = set(requested_hostnames or [])
        authorized_instance_ids = {
            record["instance_id"]
            for record in pending_deletions
            if (
                mode in ["remove", "remove_unreachable"]
                and record["instance_display_name"] in requested_hostname_set
            )
        }
    else:
        authorized_instance_ids = (
            set(authorized_instance_ids) & pending_instance_ids
        )
    return retry_pending_local_block_volume_deletions(
        inventory_path,
        expected_cluster_name,
        compartment_id,
        resume_pool_members=mode == "cleanup_compute_cluster",
        resume_pool_member_instance_ids=authorized_instance_ids,
        expected_instance_pool_id=expected_instance_pool_id,
    )

def remove_instance_pool_member_and_managed_local_block_volume(
    compartment_id,
    instance_pool_id,
    instance_id,
    instance_private_ip,
    inventory_path,
):
    managed_local_attachment = get_exclusive_managed_local_block_volume_attachment(
        compartment_id,
        instance_id,
    )
    delete_launch_created_data_volumes = (
        managed_local_attachment is not None
        and getattr(
            managed_local_attachment,
            "is_volume_created_during_launch",
            None,
        ) is True
    )
    explicitly_deleted_volume_id = (
        managed_local_attachment.volume_id
        if managed_local_attachment is not None
        and not delete_launch_created_data_volumes
        else None
    )
    pending_record = None
    if explicitly_deleted_volume_id is not None:
        pending_record = build_pending_local_block_volume_deletion_record(
            compartment_id,
            instance_pool_id,
            instance_id,
            instance_private_ip,
            explicitly_deleted_volume_id,
        )
        remember_pending_local_block_volume_deletion(inventory_path, pending_record)
    detach_instance_from_pool_if_needed(compartment_id, instance_pool_id, instance_id)
    terminate_instance_and_delete_launch_volumes(
        instance_id,
        delete_launch_created_data_volumes=delete_launch_created_data_volumes,
    )
    if pending_record is not None:
        delete_managed_local_block_volume(pending_record["volume_id"])
    return pending_record

def resume_pending_instance_pool_node_removals(
    inventory_path,
    expected_cluster_name,
    compartment_id,
    instance_pool_id,
    mode,
):
    records = load_pending_instance_pool_node_removals(inventory_path)
    if not records:
        return [], set(), 0, False

    # Establish the exact Terraform/pool boundary before any detach or
    # termination.  cleanup_compute_cluster may legitimately see a pool that
    # Terraform has already deleted, but it must never accept a different live
    # pool merely because a journal names it.
    tracked_pool_id = get_tracked_managed_instance_pool_id(inventory_path)
    if tracked_pool_id is not None and tracked_pool_id != instance_pool_id:
        raise RuntimeError("The pending removal does not match Terraform's Instance Pool")
    try:
        instance_pool = computeManagementClient.get_instance_pool(instance_pool_id).data
    except oci.exceptions.ServiceError as error:
        if error.status != 404 or mode != "cleanup_compute_cluster":
            raise
        instance_pool = None
    if instance_pool is not None and (
        instance_pool.compartment_id != compartment_id
        or instance_pool.display_name != expected_cluster_name
    ):
        raise RuntimeError("The pending removal Instance Pool has invalid ownership")

    pending_volume_deletions = load_pending_local_block_volume_deletions(
        inventory_path
    )
    pending_volume_records = {
        record["instance_id"]: record for record in pending_volume_deletions
    }
    if len(pending_volume_records) != len(pending_volume_deletions):
        raise RuntimeError(
            "Pending local Block Volume deletions contain duplicate instance OCIDs"
        )

    # Validate every journal member, live instance identity, private IP, and
    # scratch volume before changing the first member.  This keeps a mixed or
    # stale multi-node journal from being applied partially.
    preflight = {}
    for record in records:
        if (
            record["cluster_name"] != expected_cluster_name
            or record["compartment_id"] != compartment_id
            or record["instance_pool_id"] != instance_pool_id
        ):
            raise RuntimeError("A pending Instance Pool node removal belongs to another cluster")
        try:
            is_pool_member = instance_is_pool_member(
                compartment_id,
                instance_pool_id,
                record["instance_id"],
            )
        except oci.exceptions.ServiceError as error:
            if error.status != 404 or mode != "cleanup_compute_cluster":
                raise
            is_pool_member = False
        try:
            instance = computeClient.get_instance(record["instance_id"]).data
        except oci.exceptions.ServiceError as error:
            if error.status != 404:
                raise
            instance = None
            instance_state = "TERMINATED"
        else:
            instance_state = instance.lifecycle_state
            instance_tags = instance.freeform_tags or {}
            parent_cluster = instance_tags.get(
                "parent_cluster",
                instance_tags.get("cluster_name"),
            )
            if (
                instance.compartment_id != compartment_id
                or instance.display_name != record["instance_display_name"]
                or (
                    parent_cluster is not None
                    and parent_cluster != expected_cluster_name
                )
                or (
                    parent_cluster is None
                    and not is_pool_member
                    and tracked_pool_id != instance_pool_id
                )
            ):
                raise RuntimeError(
                    "A pending Instance Pool node removal has invalid instance ownership"
                )
            if instance_state != "TERMINATED":
                current_private_ip = get_instance_primary_private_ip(
                    compartment_id,
                    record["instance_id"],
                )
                if current_private_ip != record["private_ip"]:
                    raise RuntimeError(
                        "A pending Instance Pool node removal has a changed private IP"
                    )
        pending_volume_record = pending_volume_records.get(record["instance_id"])
        if pending_volume_record is not None:
            if (
                pending_volume_record["cluster_name"] != expected_cluster_name
                or pending_volume_record["compartment_id"] != compartment_id
                or pending_volume_record["instance_pool_id"] != instance_pool_id
                or pending_volume_record["instance_private_ip"] != record["private_ip"]
                or pending_volume_record["instance_display_name"]
                != record["instance_display_name"]
            ):
                raise RuntimeError("The pending local volume removal does not match its node")
            inspect_pending_local_block_volume_deletion(
                pending_volume_record,
                expected_cluster_name,
                compartment_id,
            )
        preflight[record["instance_id"]] = {
            "is_pool_member": is_pool_member,
            "instance_state": instance_state,
            "pending_volume_record": pending_volume_record,
        }

    active_names = []
    active_instance_ids = set()
    completed_instance_ids = []
    for record in records:
        member = preflight[record["instance_id"]]
        if member["is_pool_member"] and mode in ["remove", "remove_unreachable"]:
            active_names.append(record["instance_display_name"])
            active_instance_ids.add(record["instance_id"])
            continue
        if member["is_pool_member"] and mode != "cleanup_compute_cluster":
            raise RuntimeError(
                "An Instance Pool node removal is pending; retry the removal before another operation"
            )
        pending_volume_record = member["pending_volume_record"]
        if pending_volume_record is not None:
            complete_pending_local_block_volume_deletion(
                pending_volume_record,
                expected_cluster_name,
                compartment_id,
                resume_pool_member=True,
            )
        elif member["instance_state"] != "TERMINATED":
            # No scratch-volume journal exists, so discover and persist any
            # managed volume before making the instance removal irreversible.
            remove_instance_pool_member_and_managed_local_block_volume(
                compartment_id,
                instance_pool_id,
                record["instance_id"],
                record["private_ip"],
                inventory_path,
            )
        delete_pending_instance_pool_node_dns_records(record, inventory_path)
        completed_instance_ids.append(record["instance_id"])
    completed = len(completed_instance_ids)
    if completed:
        # Refresh size after all detach operations; the preflight snapshot is
        # deliberately not used for Terraform state reconciliation.
        try:
            instance_pool = computeManagementClient.get_instance_pool(instance_pool_id).data
        except oci.exceptions.ServiceError as error:
            if error.status != 404 or mode != "cleanup_compute_cluster":
                raise
            instance_pool = None
        if instance_pool is not None:
            if (
                instance_pool.compartment_id != compartment_id
                or instance_pool.display_name != expected_cluster_name
            ):
                raise RuntimeError("The pending removal Instance Pool has invalid ownership")
            if tracked_pool_id is not None or mode != "cleanup_compute_cluster":
                updateTFState(inventory_path, expected_cluster_name, instance_pool.size)
        for instance_id in completed_instance_ids:
            forget_pending_instance_pool_node_removal(
                inventory_path,
                instance_id,
            )
    return active_names, active_instance_ids, completed, True

def resume_pending_instance_pool_node_removals_for_cleanup(
    inventory_path,
    expected_cluster_name,
    compartment_id,
):
    records = load_pending_instance_pool_node_removals(inventory_path)
    if not records:
        return [], set(), 0, False
    instance_pool_ids = {record["instance_pool_id"] for record in records}
    if len(instance_pool_ids) != 1:
        raise RuntimeError(
            "Pending Instance Pool node removals reference multiple pools"
        )
    instance_pool_id = next(iter(instance_pool_ids))
    tracked_pool_id = get_tracked_managed_instance_pool_id(inventory_path)
    if tracked_pool_id is not None and tracked_pool_id != instance_pool_id:
        raise RuntimeError(
            "Pending Instance Pool node removals do not match Terraform state"
        )
    return resume_pending_instance_pool_node_removals(
        inventory_path,
        expected_cluster_name,
        compartment_id,
        instance_pool_id,
        "cleanup_compute_cluster",
    )

def get_tracked_compute_cluster_resources(inventory_path):
    state_path = os.path.join(
        os.path.dirname(os.path.abspath(inventory_path)),
        "terraform.tfstate",
    )
    if not os.path.exists(state_path):
        return None
    if os.path.islink(state_path) or not os.path.isfile(state_path):
        raise RuntimeError(
            "Terraform state is unsafe for Compute Cluster identity resolution"
        )
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Failed to read Terraform state for Compute Cluster identity: "+
            str(error)
        )
    tracked_compute_cluster_ids = set()
    tracked_ids = set()
    for resource in state.get("resources", []):
        if (
            resource.get("mode") != "managed"
            or resource.get("module") not in [None, ""]
        ):
            continue
        for resource_instance in resource.get("instances", []):
            resource_id = resource_instance.get("attributes", {}).get("id")
            if not resource_id:
                continue
            if resource.get("type") == "oci_core_compute_cluster" and resource.get("name") == "compute_cluster":
                tracked_compute_cluster_ids.add(resource_id)
            elif resource.get("type") == "oci_core_instance" and resource.get("name") == "compute_cluster_instances":
                tracked_ids.add(resource_id)
    if len(tracked_compute_cluster_ids) > 1:
        raise RuntimeError("Terraform state contains multiple managed Compute Clusters")
    tracked_compute_cluster_id = next(iter(tracked_compute_cluster_ids), None)
    return tracked_compute_cluster_id, tracked_ids

def terminate_compute_cluster_instances(compartment_id, compute_cluster_id, excluded_instance_ids=None):
    excluded_instance_ids = excluded_instance_ids or set()
    instances = oci.pagination.list_call_get_all_results(
        computeClient.list_instances,
        compartment_id=compartment_id,
        compute_cluster_id=compute_cluster_id,
    ).data
    candidates = [
        instance for instance in instances
        if instance.id not in excluded_instance_ids and instance.lifecycle_state != "TERMINATED"
    ]
    cleanup_errors = []
    for instance in candidates:
        try:
            print("STDOUT: Terminating compute cluster instance "+instance.display_name)
            terminate_instance_and_delete_launch_volumes(instance.id)
        except Exception as error:
            cleanup_errors.append(instance.display_name+": "+str(error))
    remaining_instances = oci.pagination.list_call_get_all_results(
        computeClient.list_instances,
        compartment_id=compartment_id,
        compute_cluster_id=compute_cluster_id,
    ).data
    remaining_ids = {
        instance.id for instance in remaining_instances
        if instance.id not in excluded_instance_ids and instance.lifecycle_state != "TERMINATED"
    }
    if remaining_ids:
        detail = "; ".join(cleanup_errors) if cleanup_errors else ", ".join(sorted(remaining_ids))
        raise RuntimeError("Failed to terminate all state-external Compute Cluster instances: "+detail)

def find_compute_cluster_launch_instance_by_name(
    compartment_id,
    compute_cluster_id,
    expected_cluster_name,
    instance_name,
    max_wait_seconds=60,
):
    """Bounded exact-parent lookup for a launch whose POST response was lost."""
    deadline = time.time()+max_wait_seconds
    while True:
        compute_cluster = computeClient.get_compute_cluster(
            compute_cluster_id,
            **get_oci_retry_kwargs(),
        ).data
        validate_compute_cluster_identity(
            compute_cluster,
            compute_cluster_id,
            compartment_id,
            expected_cluster_name,
        )
        candidates = [
            instance
            for instance in oci.pagination.list_call_get_all_results(
                computeClient.list_instances,
                compartment_id=compartment_id,
                compute_cluster_id=compute_cluster_id,
                **get_oci_retry_kwargs(),
            ).data
            if (
                getattr(instance, "display_name", None) == instance_name
                and normalize_oci_state(
                    getattr(instance, "lifecycle_state", None)
                ) != "TERMINATED"
            )
        ]
        if len(candidates) > 1:
            raise RuntimeError(
                "Multiple Compute Cluster launch instances have the same pending name"
            )
        if candidates:
            instance = candidates[0]
            tags = getattr(instance, "freeform_tags", None) or {}
            parent_cluster = tags.get("parent_cluster") or tags.get(
                "cluster_name"
            )
            if (
                not isinstance(getattr(instance, "id", None), str)
                or not instance.id
                or getattr(instance, "compartment_id", None) != compartment_id
                or parent_cluster != expected_cluster_name
            ):
                raise RuntimeError(
                    "The pending-name Compute Cluster instance has invalid ownership"
                )
            return instance
        if time.time() >= deadline:
            return None
        time.sleep(min(2, max(0, deadline-time.time())))

def rollback_compute_cluster_instances(launched_instances):
    rollback_errors = []
    for launched_instance in launched_instances:
        if isinstance(launched_instance, dict):
            instance_id = launched_instance.get("ocid")
            instance_name = launched_instance.get("display_name")
        else:
            # Backward-compatible input for callers created before exact launch
            # response OCIDs were recorded.
            instance_id = None
            instance_name = launched_instance
        try:
            instance = (
                computeClient.get_instance(instance_id).data
                if instance_id
                else find_compute_cluster_launch_instance_by_name(
                    comp_ocid,
                    cn_ocid,
                    cluster_name,
                    instance_name,
                )
            )
            if instance is not None:
                tags = instance.freeform_tags or {}
                parent_cluster = tags.get("parent_cluster") or tags.get("cluster_name")
                if (
                    instance.compartment_id != comp_ocid
                    or parent_cluster != cluster_name
                ):
                    raise RuntimeError("launched instance ownership changed")
                terminate_instance_and_delete_launch_volumes(instance.id)
        except Exception as error:
            rollback_errors.append(str(instance_name or instance_id)+": "+str(error))
    if rollback_errors:
        raise RuntimeError(
            "Compute Cluster rollback had errors: "+"; ".join(rollback_errors)
        )

def wait_for_compute_cluster_instance_running(
    instance_id,
    expected_display_name,
    expected_cluster_name,
    compartment_id,
    max_wait_seconds=3600,
):
    deadline = time.time()+max_wait_seconds
    while True:
        try:
            instance = computeClient.get_instance(
                instance_id,
                **get_oci_retry_kwargs(),
            ).data
        except oci.exceptions.ServiceError as error:
            if error.status != 404 or time.time() >= deadline:
                raise
            instance = None
        if instance is not None:
            tags = getattr(instance, "freeform_tags", None) or {}
            parent_cluster = tags.get("parent_cluster") or tags.get(
                "cluster_name"
            )
            state = normalize_oci_state(
                getattr(instance, "lifecycle_state", None)
            )
            if (
                getattr(instance, "id", None) != instance_id
                or getattr(instance, "compartment_id", None) != compartment_id
                or getattr(instance, "display_name", None)
                != expected_display_name
                or parent_cluster != expected_cluster_name
            ):
                raise RuntimeError(
                    "The launched Compute Cluster instance has invalid ownership"
                )
            if state == "RUNNING":
                return instance
            if state in [
                "STOPPING",
                "STOPPED",
                "TERMINATING",
                "TERMINATED",
                "FAILED",
            ]:
                raise RuntimeError(
                    "The launched Compute Cluster instance entered state "+state
                )
        if time.time() >= deadline:
            raise RuntimeError(
                "Timed out while waiting for Compute Cluster instance "+
                instance_id+" to run"
            )
        time.sleep(min(5, max(0, deadline-time.time())))

def rollback_instance_pool_size(instance_pool_id, size):
    try:
        update_size = oci.core.models.UpdateInstancePoolDetails(size=size)
        ComputeManagementClientCompositeOperations.update_instance_pool_and_wait_for_state(
            instance_pool_id,
            update_size,
            ['RUNNING'],
            waiter_kwargs={'max_wait_seconds':3600},
        )
    except Exception as error:
        print("STDOUT: Instance Pool rollback had an error: "+str(error))
    
def generate_compute_cluster_launch_display_name(cluster_name):
    # This temporary name is also inspected as a possible legacy DNS label
    # during the first hostname sync.  A fixed prefix keeps it within the
    # 63-character DNS label limit regardless of the user-facing cluster name.
    return "cc-node-pending-"+uuid.uuid4().hex

def getLaunchInstanceDetails(instance,comp_ocid,cn_ocid,new_display_name,local_block_volume_config):

    agent_config=instance.agent_config
    agent_config.__class__ = oci.core.models.LaunchInstanceAgentConfigDetails

    _, primary_vnic, _ = get_instance_primary_vnic(
        comp_ocid,
        instance.id,
        require_explicit_primary=True,
    )
    create_vnic_details=oci.core.models.CreateVnicDetails(
        assign_public_ip=False,
        subnet_id=primary_vnic.subnet_id,
    )

    shape_config=instance.shape_config
    try: 
        nvmes=shape_config.local_disks
        launchInstanceShapeConfigDetails = oci.core.models.LaunchInstanceShapeConfigDetails(baseline_ocpu_utilization=shape_config.baseline_ocpu_utilization,memory_in_gbs=shape_config.memory_in_gbs,nvmes=nvmes,ocpus=shape_config.ocpus)
    except:
        launchInstanceShapeConfigDetails = oci.core.models.LaunchInstanceShapeConfigDetails(baseline_ocpu_utilization=shape_config.baseline_ocpu_utilization,memory_in_gbs=shape_config.memory_in_gbs,ocpus=shape_config.ocpus)

    launch_freeform_tags = dict(instance.freeform_tags or {})
    # The source node can be running another user's job. A new node has no
    # allocation yet and must not inherit that user's runtime cost ownership.
    launch_freeform_tags.pop("user", None)
    launch_defined_tags = copy.deepcopy(instance.defined_tags or {})
    launch_defined_tags.setdefault("hpc-cost", {})["User"] = "Management"
    launch_freeform_tags.update({
        LOCAL_BLOCK_VOLUME_TAG_ENABLED: "true" if local_block_volume_config["enabled"] else "false",
        LOCAL_BLOCK_VOLUME_TAG_SIZE: str(local_block_volume_config["size_in_gbs"]),
        LOCAL_BLOCK_VOLUME_TAG_VPUS: str(local_block_volume_config["vpus_per_gb"]),
        LOCAL_BLOCK_VOLUME_TAG_MOUNT: local_block_volume_config["mount_point"],
    })
    launch_instance_kwargs = {
        "agent_config": agent_config,
        "availability_domain": instance.availability_domain,
        "compartment_id": comp_ocid,
        "compute_cluster_id": cn_ocid,
        "shape": instance.shape,
        "shape_config": launchInstanceShapeConfigDetails,
        "source_details": instance.source_details,
        "metadata": instance.metadata,
        "display_name": new_display_name,
        "freeform_tags": launch_freeform_tags,
        "defined_tags": launch_defined_tags,
        "create_vnic_details": create_vnic_details,
    }
    if local_block_volume_config["enabled"]:
        create_volume_details = oci.core.models.LaunchCreateVolumeFromAttributes(
            volume_creation_type="ATTRIBUTES",
            compartment_id=comp_ocid,
            display_name=new_display_name+"-local-scratch",
            size_in_gbs=local_block_volume_config["size_in_gbs"],
            vpus_per_gb=local_block_volume_config["vpus_per_gb"],
        )
        launch_instance_kwargs["launch_volume_attachments"] = [
            oci.core.models.LaunchAttachIScsiVolumeDetails(
                device=LOCAL_BLOCK_VOLUME_DEVICE,
                display_name=new_display_name+"-local-scratch-attachment",
                is_agent_auto_iscsi_login_enabled=True,
                is_read_only=False,
                is_shareable=False,
                use_chap=False,
                launch_create_volume_details=create_volume_details,
            )
        ]
    launch_instance_details=oci.core.models.LaunchInstanceDetails(**launch_instance_kwargs)
    return launch_instance_details      

batchsize=12
inventory="/etc/ansible/hosts"
playbooks_dir="/opt/oci-hpc/playbooks/"

parser = argparse.ArgumentParser(description='Script to resize the CN')
parser.add_argument('--compartment_ocid', help='OCID of the compartment, defaults to the Compartment OCID of the localhost')
parser.add_argument('--cluster_name', help='Name of the cluster to resize. Defaults to the name included in the controller')
parser.add_argument('--inventory', help='Inventory path. Defaults to the permanent or autoscaling cluster inventory selected by cluster_name')
parser.add_argument('mode', help='Mode type. add/remove node options, implicitly configures newly added nodes. Also implicitly reconfigure/restart services like Slurm to recognize new nodes. Similarly for remove option, terminates nodes and implicitly reconfigure/restart services like Slurm on rest of the cluster nodes to remove reference to deleted nodes.',choices=['add','remove','remove_unreachable','list','reconfigure','cleanup_compute_cluster','prepare_local_block_volume','sync_instance_pool_names'],default='list',nargs='?')
parser.add_argument('number', type=int, help="Number of nodes to add or delete if a list of hostnames is not defined",nargs='?')
parser.add_argument('--nodes', help="List of nodes to delete (Space Separated)",nargs='+')
parser.add_argument(
    '--no_reconfigure',
    help=(
        'If present, does not rerun the playbooks. Compute Cluster additions '
        'keep temporary OCI names until a later reconfigure.'
    ),
    action='store_true',
    default=False,
)
parser.add_argument('--user_logging', help='If present. Use the default settings in ~/.oci/config to connect to the API. Default is using instance_principal',action='store_true',default=False)
parser.add_argument('--force', help='If present. Nodes will be removed even if the destroy playbook failed',action='store_true',default=False)
parser.add_argument('--ansible_crucial', help='If present during reconfiguration, only crucial ansible playbooks will be executed on the live nodes. Non live nodes will be removed',action='store_true',default=False)
parser.add_argument('--remove_unreachable', help='If present, nodes that are not sshable will be terminated before running the action that was requested (Example Adding a node) ',action='store_true',default=False)
parser.add_argument('--quiet', help='If present, the script will not prompt for a response when removing nodes and will not give a reminder to save data from nodes that are being removed ',action='store_true',default=False)
parser.add_argument('--monitoring-output', help=argparse.SUPPRESS, action='store_true', default=False)

args = parser.parse_args()

metadata=get_metadata()
if args.compartment_ocid is None:
    comp_ocid=metadata['compartmentId']
else:
    comp_ocid=args.compartment_ocid

if args.cluster_name is None:
    cluster_name=metadata['displayName'].replace('-controller','')
else:
    cluster_name=args.cluster_name

if cluster_name == metadata['displayName'].replace('-controller',''):
    inventory="/etc/ansible/hosts"
    host_check_file="/tmp/hosts"
    autoscaling=False
else:
    inventory= "/opt/oci-hpc/autoscaling/clusters/"+cluster_name+'/inventory'
    host_check_file="/opt/oci-hpc/autoscaling/clusters/"+cluster_name+'/hosts_'+cluster_name
    autoscaling = True

if args.inventory is not None:
    inventory=os.path.abspath(args.inventory)

try:
    resize_lock_file = acquire_resize_lock(inventory)
    ensure_cluster_is_not_being_destroyed(inventory)
except Exception as error:
    print("STDOUT: Failed to acquire resize lock: "+str(error))
    exit(1)

inventory_dict = parse_inventory(inventory)
if inventory_dict is None:
    print("STDOUT: Inventory file "+inventory+" was not found")
    exit(1)
if args.mode == 'prepare_local_block_volume':
    inventory_cluster_name = get_inventory_variable(inventory_dict, "cluster_name")
    if not inventory_cluster_name:
        print("STDOUT: Inventory does not define cluster_name")
        exit(1)
    if args.cluster_name is not None and cluster_name != inventory_cluster_name:
        print("STDOUT: Requested cluster name does not match inventory cluster_name")
        exit(1)
    cluster_name = inventory_cluster_name
if args.mode != 'prepare_local_block_volume':
    try:
        local_block_volume_config = get_local_block_volume_config(inventory_dict)
    except (TypeError, ValueError) as error:
        print("STDOUT: Invalid local Block Volume configuration: "+str(error))
        exit(1)
username="opc"
for inv_vars in inventory_dict["all:vars"]:
    if inv_vars.startswith("compute_username"):
        username=inv_vars.split("compute_username=")[1].strip()
        break
zone_name=cluster_name+".local"
for inv_vars in inventory_dict["all:vars"]:
    if inv_vars.startswith("zone_name"):
        zone_name=inv_vars.split("zone_name=")[1].strip()
        break
dns_entries=parse_bool(get_inventory_variable(inventory_dict, "dns_entries", "true"))
slurm_enabled=parse_bool(get_inventory_variable(inventory_dict, "slurm", "false"))
queue=None
for inv_vars in inventory_dict["all:vars"]:
    if inv_vars.startswith("queue"):
        queue=inv_vars.split("queue=")[1].strip()
        break
instance_type=""
for inv_vars in inventory_dict["all:vars"]:
    if inv_vars.startswith("instance_type"):
        instance_type=inv_vars.split("instance_type=")[1].strip()
        break
private_subnet_cidr=None
for inv_vars in inventory_dict["all:vars"]:
    if inv_vars.startswith("private_subnet"):
        private_subnet_cidr=ipaddress.ip_network(inv_vars.split("private_subnet=")[1].strip())
        break

hostnames=args.nodes
if hostnames is None:
    hostnames=[]

if args.mode=='remove' and args.number is None and args.nodes is None:
    print("STDOUT: No Nodes to remove")
    exit()

if args.mode=='add' and args.number is None:
    print("STDOUT: No Nodes to add")
    exit()

if args.mode in ['add', 'remove'] and args.number is not None and args.number <= 0:
    print("STDOUT: The number of nodes must be greater than zero")
    exit(1)

if args.no_reconfigure is None:
    no_reconfigure=False
else:
    no_reconfigure=args.no_reconfigure

if args.user_logging is None:
    user_logging=False
else:
    user_logging=args.user_logging

if args.force is None:
    force=False
else:
    force=args.force

if args.ansible_crucial is None:
    ansible_crucial=False
else:
    ansible_crucial=args.ansible_crucial

if args.remove_unreachable is None:
    remove_unreachable=False
else:
    remove_unreachable=args.remove_unreachable

if user_logging:
    config_oci = oci.config.from_file()
    computeClient = oci.core.ComputeClient(config_oci)
    ComputeClientCompositeOperations = oci.core.ComputeClientCompositeOperations(computeClient)
    computeManagementClient = oci.core.ComputeManagementClient(config_oci)
    ComputeManagementClientCompositeOperations = oci.core.ComputeManagementClientCompositeOperations(computeManagementClient)
    blockstorageClient = oci.core.BlockstorageClient(config_oci)
    virtualNetworkClient = oci.core.VirtualNetworkClient(config_oci)
    dns_client = oci.dns.DnsClient(config_oci)
else:
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    computeClient = oci.core.ComputeClient(config={}, signer=signer)
    ComputeClientCompositeOperations= oci.core.ComputeClientCompositeOperations(computeClient)
    computeManagementClient = oci.core.ComputeManagementClient(config={}, signer=signer)
    ComputeManagementClientCompositeOperations = oci.core.ComputeManagementClientCompositeOperations(computeManagementClient)
    blockstorageClient = oci.core.BlockstorageClient(config={}, signer=signer)
    virtualNetworkClient = oci.core.VirtualNetworkClient(config={}, signer=signer)
    dns_client = oci.dns.DnsClient(config={}, signer=signer)

ready_pending_deletions = []
if args.mode == "cleanup_compute_cluster":
    cleanup_pending_node_removals = load_pending_instance_pool_node_removals(
        inventory
    )
    cleanup_expected_instance_pool_id = get_tracked_managed_instance_pool_id(
        inventory
    )
    if cleanup_expected_instance_pool_id is None and cleanup_pending_node_removals:
        cleanup_pending_pool_ids = {
            record["instance_pool_id"]
            for record in cleanup_pending_node_removals
        }
        if len(cleanup_pending_pool_ids) != 1:
            print(
                "STDOUT: Failed to establish one exact Instance Pool identity "
                "for pending cleanup"
            )
            exit(1)
        cleanup_expected_instance_pool_id = next(iter(cleanup_pending_pool_ids))
    cleanup_pending_local_volumes = load_pending_local_block_volume_deletions(
        inventory
    )
    cleanup_compute_cluster_resources = get_tracked_compute_cluster_resources(
        inventory
    )
    cleanup_has_tracked_compute_cluster = has_tracked_compute_cluster_resources(
        cleanup_compute_cluster_resources
    )
    if (
        cleanup_has_tracked_compute_cluster
        and cleanup_compute_cluster_resources[0] is None
    ):
        print(
            "STDOUT: Terraform state contains Compute Cluster instances but "
            "not their exact Compute Cluster identity; refusing cleanup"
        )
        exit(1)
    if (
        cleanup_pending_local_volumes
        and not cleanup_has_tracked_compute_cluster
        and cleanup_expected_instance_pool_id is None
    ):
        cleanup_local_pool_ids = {
            record["instance_pool_id"]
            for record in cleanup_pending_local_volumes
        }
        if len(cleanup_local_pool_ids) != 1:
            print(
                "STDOUT: Failed to establish one exact Instance Pool identity "
                "for pending local Block Volume cleanup"
            )
            exit(1)
        cleanup_journal_pool_id = next(iter(cleanup_local_pool_ids))
        try:
            deleted_pool = computeManagementClient.get_instance_pool(
                cleanup_journal_pool_id
            ).data
        except oci.exceptions.ServiceError as error:
            if error.status != 404:
                print(
                    "STDOUT: Failed to verify the deleted Instance Pool for "
                    "pending local Block Volume cleanup: "+str(error)
                )
                exit(1)
            # Terraform may already have deleted this exact pool while another
            # resource destroy failed.  Its single journaled OCID remains a
            # safe boundary because no live pool member can be detached.
            cleanup_expected_instance_pool_id = cleanup_journal_pool_id
        else:
            if (
                getattr(deleted_pool, "id", cleanup_journal_pool_id)
                == cleanup_journal_pool_id
                and getattr(deleted_pool, "compartment_id", comp_ocid)
                == comp_ocid
                and getattr(deleted_pool, "lifecycle_state", None)
                == "TERMINATED"
            ):
                cleanup_expected_instance_pool_id = cleanup_journal_pool_id
            else:
                print(
                    "STDOUT: Refusing pending local Block Volume cleanup because "
                    "its live Instance Pool is no longer proven by Terraform state"
                )
                exit(1)
    try:
        resume_pending_instance_pool_node_removals_for_cleanup(
            inventory,
            cluster_name,
            comp_ocid,
        )
    except Exception as error:
        print("STDOUT: Failed to retry pending Instance Pool node removal: "+str(error))
        exit(1)
    try:
        ready_pending_deletions = process_pending_local_block_volume_deletions_for_operation(
            inventory,
            cluster_name,
            comp_ocid,
            args.mode,
            hostnames,
            expected_instance_pool_id=cleanup_expected_instance_pool_id,
        )
    except Exception as error:
        print("STDOUT: Failed to retry pending local Block Volume deletion: "+str(error))
        exit(1)

if args.mode == 'prepare_local_block_volume':
    try:
        prepare_local_block_volume_inventory(inventory)
    except Exception as error:
        print("STDOUT: Failed to prepare local Block Volume inventory: "+str(error))
        exit(1)
    print("STDOUT: Prepared local Block Volume inventory "+inventory)
    exit(0)

if args.mode == 'cleanup_compute_cluster':
    # Keep completed removal journals until Terraform destroy succeeds and the
    # cluster directory is removed.  If destroy fails, a later normal resize can
    # still finish DNS and Terraform-state reconciliation from the journal.
    try:
        cleanup_pending_local_block_volume_dns_records(
            inventory,
            ready_pending_deletions,
            cluster_name,
            comp_ocid,
        )
    except Exception as error:
        print("STDOUT: Failed to clean up pending local Block Volume DNS records: "+str(error))
        exit(1)
    tracked_compute_cluster_resources = get_tracked_compute_cluster_resources(inventory)
    if tracked_compute_cluster_resources is None:
        raise RuntimeError("Terraform state was not found; refusing to clean up Compute Cluster instances before destroy")
    tracked_compute_cluster_id, tracked_instance_ids = tracked_compute_cluster_resources
    if tracked_compute_cluster_id is None:
        if tracked_instance_ids:
            raise RuntimeError(
                "Terraform state contains Compute Cluster instances without "
                "an exact Compute Cluster identity; refusing managed pool cleanup"
            )
        compute_dns_ownership = load_instance_pool_name_dns_ownership(
            inventory
        )
        if (
            compute_dns_ownership is not None
            and compute_dns_ownership.get("version") == 2
            and compute_dns_ownership.get("deployment_type") == "CC"
        ):
            try:
                cleanup_compute_cluster_name_dns_records(
                    comp_ocid,
                    inventory_dict,
                    inventory,
                    compute_dns_ownership["compute_cluster_id"],
                )
            except Exception as error:
                print(
                    "STDOUT: Failed to clean up Compute Cluster hostname DNS records: "+
                    str(error)
                )
                exit(1)
            print(
                "STDOUT: Terraform state no longer manages the Compute Cluster; "
                "cleaned up its Python-owned hostname DNS records"
            )
            exit(0)
        managed_dns_marker_path = os.path.join(
            os.path.dirname(os.path.abspath(inventory)),
            MANAGED_POOL_DNS_OWNERSHIP_MARKER_FILENAME,
        )
        managed_dns_ownership_path = get_instance_pool_name_dns_ownership_path(
            inventory
        )
        if os.path.lexists(managed_dns_marker_path) and (
            os.path.islink(managed_dns_marker_path)
            or not os.path.isfile(managed_dns_marker_path)
        ):
            print(
                "STDOUT: Failed to clean up managed pool hostname DNS records: "
                "the DNS ownership marker is unsafe"
            )
            exit(1)
        should_cleanup_managed_pool_dns = (
            not parse_bool(
                get_inventory_variable(
                    inventory_dict,
                    "cluster_network",
                    "false",
                )
            )
            or os.path.isfile(managed_dns_marker_path)
            or os.path.isfile(managed_dns_ownership_path)
        )
        if should_cleanup_managed_pool_dns:
            try:
                cleanup_instance_pool_name_dns_records(
                    comp_ocid,
                    inventory_dict,
                    inventory_path=inventory,
                )
            except Exception as error:
                print("STDOUT: Failed to clean up managed pool hostname DNS records: "+str(error))
                exit(1)
        print("STDOUT: Terraform state does not manage a Compute Cluster; no state-external instances need cleanup")
        exit(0)
    try:
        compute_cluster = computeClient.get_compute_cluster(tracked_compute_cluster_id).data
    except oci.exceptions.ServiceError as error:
        if error.status == 404:
            try:
                cleanup_compute_cluster_name_dns_records(
                    comp_ocid,
                    inventory_dict,
                    inventory,
                    tracked_compute_cluster_id,
                )
            except Exception as cleanup_error:
                print(
                    "STDOUT: Failed to clean up Compute Cluster hostname DNS records: "+
                    str(cleanup_error)
                )
                exit(1)
            print("STDOUT: The Terraform-managed Compute Cluster is already deleted")
            exit(0)
        raise
    validate_compute_cluster_cleanup_identity(
        compute_cluster,
        tracked_compute_cluster_id,
        comp_ocid,
        cluster_name,
    )
    try:
        live_compute_cluster_instances = get_compute_cluster_instances_for_cleanup(
            comp_ocid,
            tracked_compute_cluster_id,
            cluster_name,
        )
        cleanup_compute_cluster_name_dns_records(
            comp_ocid,
            inventory_dict,
            inventory,
            tracked_compute_cluster_id,
            live_instances=live_compute_cluster_instances,
        )
    except Exception as error:
        print(
            "STDOUT: Failed to clean up Compute Cluster hostname DNS records: "+
            str(error)
        )
        exit(1)
    compute_cluster_cleanup_state = normalize_oci_state(
        compute_cluster.lifecycle_state
    )
    if compute_cluster_cleanup_state != "DELETED":
        terminate_compute_cluster_instances(
            comp_ocid,
            tracked_compute_cluster_id,
            excluded_instance_ids=tracked_instance_ids,
        )
    else:
        print(
            "STDOUT: The Terraform-managed Compute Cluster is already deleted"
        )
    exit(0)

try:
    cn_summary,ip_summary,CN = get_summary_for_operation(
        comp_ocid,
        cluster_name,
        inventory,
        inventory_dict,
        args.mode,
        autoscaling,
        monitoring_output=args.monitoring_output,
    )
except Exception as error:
    print("STDOUT: Failed to resolve cluster identity: "+str(error))
    exit(1)
if cn_summary is None:
    exit(1)
cn_ocid =cn_summary.id
if CN == "CN":
    current_instance_pool_id = cn_summary.instance_pools[0].id
elif CN == "IP":
    current_instance_pool_id = cn_ocid
else:
    current_instance_pool_id = None
resuming_pending_node_removals = False
completed_pending_node_removals_to_report = 0
pending_local_volume_authorized_instance_ids = None
resuming_pending_node_removal_records = []

# A failed synchronization may have already renamed some OCI resources or
# rewritten Inventory.  Finish its persisted OCID/IP/hostname plan before any
# later resize or Ansible operation can change pool membership or aliases.
if autoscaling and args.mode in [
    "add",
    "remove",
    "remove_unreachable",
    "reconfigure",
]:
    try:
        pending_hostname_sync = load_instance_pool_hostname_sync_plan(inventory)
        if pending_hostname_sync is not None:
            synchronize_autoscaling_compute_names(
                comp_ocid,
                inventory,
                cluster_name,
                expected_instance_pool_id=(cn_ocid if CN == "IP" else None),
                expected_cluster_network_id=(cn_ocid if CN == "CN" else None),
                expected_compute_cluster_id=(cn_ocid if CN == "CC" else None),
            )
            print(
                "STDOUT: Completed the previously committed compute hostname "
                "synchronization; run the requested resize again if it is still needed"
            )
            # The journal is written only after a prior resize/reconfigure has
            # completed Ansible.  Continuing an add or count-based remove here
            # would apply that already committed request a second time.
            exit(0)
    except Exception as error:
        print(
            "STDOUT: Failed to resume pending compute hostname synchronization: "+
            str(error)
        )
        exit(1)

if CN == "CC" and autoscaling and args.mode in [
    "add",
    "remove",
    "remove_unreachable",
    "reconfigure",
]:
    try:
        completed_compute_cluster_removals = (
            resume_pending_compute_cluster_node_removals(
                inventory,
                cluster_name,
                comp_ocid,
                cn_ocid,
                force=force,
            )
        )
    except Exception as error:
        print(
            "STDOUT: Failed to resume pending Compute Cluster node removal: "+
            str(error)
        )
        exit(1)
    if completed_compute_cluster_removals:
        print(
            "STDOUT: Completed "+str(completed_compute_cluster_removals)+
            " pending Compute Cluster node removal(s); inspect the cluster and "
            "run the requested resize again if it is still needed"
        )
        exit(0)

if CN in ["IP", "CN"] and autoscaling and args.mode in [
    "add",
    "remove",
    "remove_unreachable",
    "reconfigure",
]:
    try:
        (
            pending_removal_names,
            pending_removal_instance_ids,
            completed_pending_node_removals,
            had_pending_node_removals,
        ) = resume_pending_instance_pool_node_removals(
            inventory,
            cluster_name,
            comp_ocid,
            current_instance_pool_id,
            args.mode,
        )
    except Exception as error:
        print("STDOUT: Failed to resume pending Instance Pool node removal: "+str(error))
        exit(1)
    if had_pending_node_removals:
        # The generic journal was already validated against this exact pool.
        # Carry its active OCIDs forward instead of re-authorizing by hostname.
        pending_local_volume_authorized_instance_ids = set(
            pending_removal_instance_ids
        )
    if had_pending_node_removals and args.mode in ["remove", "remove_unreachable"]:
        if pending_removal_names:
            # The earlier request already committed these exact OCIDs for
            # removal.  Finish only that work on this invocation.
            hostnames = pending_removal_names
            resuming_pending_node_removals = True
            pending_records_by_id = {
                record["instance_id"]: record
                for record in load_pending_instance_pool_node_removals(
                    inventory
                )
            }
            if set(pending_records_by_id) != set(pending_removal_instance_ids):
                raise RuntimeError(
                    "The pending Instance Pool removal journal changed during recovery"
                )
            resuming_pending_node_removal_records = [
                pending_records_by_id[instance_id]
                for instance_id in sorted(pending_removal_instance_ids)
            ]
        else:
            completed_pending_node_removals_to_report = (
                completed_pending_node_removals
            )

pending_post_resize_recovery = None
if CN in ["IP", "CN"] and autoscaling:
    try:
        pending_post_resize_recovery = load_instance_pool_post_resize_recovery(
            inventory
        )
        if pending_post_resize_recovery is not None and (
            pending_post_resize_recovery["cluster_name"] != cluster_name
            or pending_post_resize_recovery["instance_pool_id"]
            != current_instance_pool_id
        ):
            raise RuntimeError(
                "The post-resize recovery marker belongs to another cluster or pool"
            )
        if resuming_pending_node_removals and pending_post_resize_recovery is None:
            raise RuntimeError(
                "The pending Instance Pool node removal has no resize recovery marker"
            )
        if (
            pending_post_resize_recovery is not None
            and pending_post_resize_recovery["status"] == "state_only"
        ):
            # Preserve the original --no_reconfigure contract while finishing
            # an exact journaled removal later in this invocation.
            no_reconfigure = True
    except Exception as error:
        print("STDOUT: Failed to inspect Instance Pool resize recovery: "+str(error))
        exit(1)

if args.mode in ["add", "remove", "remove_unreachable", "reconfigure"]:
    try:
        ready_pending_deletions = process_pending_local_block_volume_deletions_for_operation(
            inventory,
            cluster_name,
            comp_ocid,
            args.mode,
            hostnames,
            authorized_instance_ids=(
                pending_local_volume_authorized_instance_ids
            ),
            expected_instance_pool_id=current_instance_pool_id,
        )
    except Exception as error:
        print("STDOUT: Failed to retry pending local Block Volume deletion: "+str(error))
        exit(1)

if args.mode == 'sync_instance_pool_names':
    if not autoscaling:
        print("STDOUT: Name synchronization is only enabled for Autoscaling compute deployments; no changes made")
        exit(0)
    try:
        synchronized_instances, _ = synchronize_autoscaling_compute_names(
            comp_ocid,
            inventory,
            cluster_name,
            expected_instance_pool_id=current_instance_pool_id,
            expected_cluster_network_id=(cn_ocid if CN == "CN" else None),
            expected_compute_cluster_id=(cn_ocid if CN == "CC" else None),
        )
    except Exception as error:
        print("STDOUT: Failed to synchronize compute names: "+str(error))
        exit(1)
    print("STDOUT: Synchronized "+str(len(synchronized_instances))+" compute name(s)")
    exit(0)

if CN != "CC":
    ipa_ocid = current_instance_pool_id
    if ready_pending_deletions:
        # A resumed scratch-volume deletion may just have detached a pool
        # member.  The summary above predates that mutation, so refresh the
        # exact pool by OCID before waiting or reconciling Terraform state.
        current_size = get_exact_instance_pool_size(
            ipa_ocid,
            comp_ocid,
            expected_display_name=cluster_name,
        )
    else:
        current_size=ip_summary.size

if ready_pending_deletions:
    if CN == "CC":
        raise RuntimeError("An Instance Pool local Block Volume deletion cannot be finalized as a Compute Cluster")
    try:
        active_instance_display_names = None
        active_instance_private_ips = None
        if dns_entries:
            wait_for_running_status(
                cluster_name,
                comp_ocid,
                cn_ocid,
                CN,
                expected_size=current_size,
            )
            (
                active_instance_display_names,
                active_instance_private_ips,
            ) = get_active_instance_identities(
                comp_ocid,
                cn_ocid,
                CN,
                current_size,
            )
        completed_pending_deletions = finalize_pending_local_block_volume_deletions(
            inventory,
            ready_pending_deletions,
            cluster_name,
            comp_ocid,
            current_pool_size=current_size,
            current_instance_pool_id=ipa_ocid,
            active_instance_display_names=active_instance_display_names,
            active_instance_private_ips=active_instance_private_ips,
        )
    except Exception as error:
        print("STDOUT: Failed to finalize pending local Block Volume deletion: "+str(error))
        exit(1)
    if (
        args.mode in ["remove", "remove_unreachable"]
        and pending_post_resize_recovery is None
    ):
        print(
            "STDOUT: Completed "+str(completed_pending_deletions)+
            " pending node removal(s); inspect the cluster before requesting additional removals"
        )
        exit(0)

if (
    pending_post_resize_recovery is not None
    and not resuming_pending_node_removals
    and args.mode in ["add", "remove", "remove_unreachable", "reconfigure"]
):
    try:
        recovered_pool_size = reconcile_instance_pool_post_resize_state(
            inventory,
            comp_ocid,
            pending_post_resize_recovery,
        )
        if pending_post_resize_recovery["status"] == "state_only":
            clear_instance_pool_post_resize_recovery(inventory)
            recovery_status = 0
        else:
            recovery_status = reconfigure(
                comp_ocid,
                cn_ocid,
                inventory,
                CN,
                crucial=ansible_crucial,
            )
    except Exception as error:
        print("STDOUT: Failed to complete Instance Pool post-resize recovery: "+str(error))
        exit(1)
    if recovery_status != 0:
        exit(recovery_status)
    print(
        "STDOUT: Recovered the pending Instance Pool resize at size "+
        str(recovered_pool_size)+"; "
        "run the requested resize again if it is still needed"
    )
    exit(0)

if completed_pending_node_removals_to_report:
    print(
        "STDOUT: Completed "+str(completed_pending_node_removals_to_report)+
        " pending Instance Pool node removal(s); inspect the cluster before removing more"
    )
    exit(0)

if args.mode == 'list':
    if args.monitoring_output:
        if not autoscaling:
            print("STDOUT: Validated monitoring output is only available for Autoscaling compute deployments")
            exit(1)
        try:
            if CN == "CN":
                cn_instances, _ = get_complete_cluster_network_instances(
                    comp_ocid,
                    cn_ocid,
                    current_instance_pool_id,
                    cluster_name,
                )
            elif CN == "CC":
                cn_instances, _ = get_complete_compute_cluster_instances(
                    comp_ocid,
                    cn_ocid,
                    cluster_name,
                    expected_instance_ids=get_compute_inventory_instance_ids(
                        inventory_dict
                    ),
                )
            else:
                cn_instances, _ = get_complete_instance_pool_instances(
                    comp_ocid,
                    current_instance_pool_id,
                )
        except Exception as error:
            print("STDOUT: Failed to obtain a complete compute member list: "+str(error))
            exit(1)
        print("EXPECTED_SIZE "+str(len(cn_instances)))
    else:
        state = cn_summary.lifecycle_state
        print("Cluster is in state:"+state )
        cn_instances = get_instances(comp_ocid,cn_ocid,CN)
    for cn_instance in cn_instances:
        print(cn_instance['display_name']+' '+cn_instance['ip']+' '+cn_instance['ocid'])
elif args.mode == 'reconfigure':
    if len(hostnames)>0:
        reconfigure_status = add_reconfigure(comp_ocid,cn_ocid,inventory,CN,specific_hosts=hostnames)
    else:
        reconfigure_status = reconfigure(comp_ocid,cn_ocid,inventory,CN,crucial=ansible_crucial)
    exit(reconfigure_status)

else:
    wait_for_running_status(cluster_name,comp_ocid,cn_ocid,CN)
    cn_instances = get_instances(comp_ocid,cn_ocid,CN)
    inventory_instances =[]
    only_inventory_instance=[]
    zone_id = None
    if dns_entries:
        if autoscaling:
            zone_id = get_single_private_dns_zone_id(comp_ocid, zone_name)
        else:
            zones = dns_client.list_zones(
                compartment_id=comp_ocid,
                name=zone_name,
                zone_type="PRIMARY",
                scope="PRIVATE",
            ).data
            if len(zones) == 0:
                raise RuntimeError("Private DNS zone "+zone_name+" was not found")
            zone_id = zones[0].id
    for line in inventory_dict['compute_configured']:
        host=line.split('ansible_host=')[0].strip()
        ip=line.split("ansible_host=")[1].split("ansible_user=")[0].strip()
        inventory_instances.append({'display_name':host,'ip':ip,'ocid':None})
        if not host in [i['display_name'] for i in cn_instances]:
            ip=line.split("ansible_host=")[1].split("ansible_user=")[0].strip()
            print("STDOUT: "+host+" with IP: "+ip+" is in the inventory but not in the cluster")
            only_inventory_instance.append({'display_name':host,'ip':ip,'ocid':None})
    for line in inventory_dict['compute_to_add']:
        host=line.split('ansible_host=')[0].strip()
        ip=line.split("ansible_host=")[1].split("ansible_user=")[0].strip()
        inventory_instances.append({'display_name':host,'ip':ip,'ocid':None})
        if not host in [i['display_name'] for i in cn_instances]:
            print("STDOUT: "+host+" with IP: "+ip+" is in the inventory but not in the cluster")
            only_inventory_instance.append({'display_name':host,'ip':ip,'ocid':None})
    if resuming_pending_node_removals:
        # The durable journal has already fixed the exact removal OCIDs.  Do
        # not widen this retry to newly unreachable nodes or the CLI count.
        hostnames_to_remove = list(hostnames)
    elif args.mode == 'remove_unreachable':
        if len(hostnames) == 0: 
            reachable_instances,unreachable_instances=getreachable(cn_instances+only_inventory_instance,username,delay=10)
            if len(unreachable_instances):
                hostnames_to_remove=[i['display_name'] for i in unreachable_instances]
            else:
                print("STDOUT: No list of nodes were specified and no unreachable nodes were found")
                exit(1)
        else:
            inventory_instances_to_test = []
            for instance_to_test in inventory_instances:
                if not instance_to_test['display_name'] in hostnames:
                    inventory_instances_to_test.append(instance_to_test)
            reachable_instances,unreachable_instances=getreachable(inventory_instances_to_test,username,delay=10)
            hostnames_to_remove=hostnames
            if len(unreachable_instances):
                print("STDOUT: At least one unreachable node is in the inventory and was not mentionned with OCI hostname to be removed. Trying anyway")
    else:
        reachable_instances,unreachable_instances=getreachable(inventory_instances,username,delay=10)
        if len(unreachable_instances):
            if not remove_unreachable:
                print("STDOUT: At least one unreachable node is in the inventory")
                print(unreachable_instances)
                print("STDOUT: Not doing anything")
                exit(1)
            else:
                hostnames_to_remove=[i['display_name'] for i in unreachable_instances]
        else:
            hostnames_to_remove=[]
    if args.mode == 'remove' and not resuming_pending_node_removals:
        if len(hostnames) == 0:
            nfsNode=getNFSnode(inventory)
            non_nfs=[i for i in cn_instances if i['display_name'] != nfsNode]
            additional_nodes_to_remove_number=args.number-len(hostnames_to_remove)
            if additional_nodes_to_remove_number > 0:
                if CN == "CC" and autoscaling:
                    state_external_instance_ids = (
                        get_compute_cluster_state_external_member_ids(
                            inventory,
                            cn_ocid,
                            cn_instances,
                        )
                    )
                    already_selected_names = set(hostnames_to_remove)
                    removable_candidates = [
                        instance for instance in non_nfs
                        if (
                            instance["ocid"] in state_external_instance_ids
                            and instance["display_name"]
                            not in already_selected_names
                        )
                    ]
                    if (
                        additional_nodes_to_remove_number
                        > len(removable_candidates)
                    ):
                        raise RuntimeError(
                            "The requested Compute Cluster reduction exceeds "
                            "the state-external nodes added by resize.sh"
                        )
                    hostnames_to_remove.extend(
                        instance["display_name"]
                        for instance in removable_candidates[
                            -additional_nodes_to_remove_number:
                        ]
                    )
                elif additional_nodes_to_remove_number < len(cn_instances):
                    hostnames_to_remove=hostnames_to_remove+[non_nfs[i]['display_name'] for i in range(len(non_nfs)-additional_nodes_to_remove_number,len(non_nfs))]
                else:
                    hostnames_to_remove=[cn_instances[i]['display_name'] for i in range(len(cn_instances))]
        else:
            hostnames_to_remove2 = list(hostnames)
            hostnames_to_remove2.extend(x for x in hostnames_to_remove if x not in hostnames_to_remove2)
            hostnames_to_remove=hostnames_to_remove2
    hostnames_to_remove_len=len(hostnames_to_remove)
    validated_removal_instance_ids = set()
    planned_instance_pool_removals = []
    planned_compute_cluster_removals = []
    compute_cluster_removal_journal = None
    if (
        hostnames_to_remove_len
        and autoscaling
    ):
        if CN == "CC":
            validated_removal_instance_ids = validate_instance_pool_removal_inventory_plan(
                inventory_dict,
                cn_instances,
                hostnames_to_remove,
            )
            planned_compute_cluster_removals = build_compute_cluster_removal_plan(
                inventory_dict,
                comp_ocid,
                cn_ocid,
                cluster_name,
                cn_instances,
                validated_removal_instance_ids,
                inventory_path=inventory,
            )
            compute_cluster_removal_journal = (
                build_compute_cluster_node_removal_journal(
                    cluster_name,
                    comp_ocid,
                    cn_ocid,
                    cn_instances,
                    planned_compute_cluster_removals,
                    no_reconfigure,
                )
            )
            # Check every candidate RRset before the removal playbook rewrites
            # Inventory or the first node/DNS record is changed.
            preflight_compute_cluster_node_removal_dns(
                compute_cluster_removal_journal,
                inventory_dict,
                inventory,
            )
        elif CN in ["IP", "CN"]:
            if resuming_pending_node_removals:
                # The earlier invocation already preflighted and atomically
                # froze every exact OCID before any Inventory or pool mutation.
                planned_instance_pool_removals = list(
                    resuming_pending_node_removal_records
                )
                validated_removal_instance_ids = {
                    record["instance_id"]
                    for record in planned_instance_pool_removals
                }
            else:
                validated_removal_instance_ids = validate_instance_pool_removal_inventory_plan(
                    inventory_dict,
                    cn_instances,
                    hostnames_to_remove,
                )
                planned_instance_pool_removals = build_instance_pool_removal_journal_plan(
                    inventory_dict,
                    comp_ocid,
                    ipa_ocid,
                    cn_instances,
                    hostnames_to_remove,
                    validated_removal_instance_ids,
                    existing_records=load_pending_instance_pool_node_removals(
                        inventory
                    ),
                )
    pool_members_to_remove_len = (
        len(planned_instance_pool_removals)
        if CN in ["IP", "CN"] and autoscaling
        else hostnames_to_remove_len
    )
    if (
        hostnames_to_remove_len
        and CN in ["IP", "CN"]
        and autoscaling
        and not resuming_pending_node_removals
    ):
        # Selection is now fixed, but the removal playbook itself rewrites the
        # permanent inventory.  Establish the retry boundary before that first
        # durable post-selection change.
        write_instance_pool_post_resize_recovery(
            inventory,
            cluster_name,
            ipa_ocid,
            status=(
                "state_only" if no_reconfigure else "reconfigure_and_sync"
            ),
            action="remove",
            source_size=current_size,
            target_size=(
                current_size-pool_members_to_remove_len
            ),
        )
        # Commit every exact target in one atomic journal write before the
        # removal playbook can rewrite Inventory or the first member can detach.
        write_pending_instance_pool_node_removals(
            inventory,
            planned_instance_pool_removals,
        )
    if compute_cluster_removal_journal is not None:
        # Freeze the whole exact-OCID selection before Ansible can rewrite
        # Inventory.  A retry therefore cannot select another node by count.
        refuse_terraform_state_mutation_while_locked(
            os.path.dirname(os.path.abspath(inventory))
        )
        write_compute_cluster_node_removal_journal(
            inventory,
            compute_cluster_removal_journal,
        )
    if hostnames_to_remove_len:
        if CN == "CC" and autoscaling:
            refuse_terraform_state_mutation_while_locked(
                os.path.dirname(os.path.abspath(inventory))
            )
        if not no_reconfigure:
            playbook = playbooks_dir+"resize_remove_unreachable.yml"
            error_code = destroy_unreachable_reconfigure(inventory,hostnames_to_remove,playbook)
            if error_code != 0:
                print("STDOUT: The nodes could not be removed. Try running this with Force")
                if not force:
                    exit(1)
                else:
                    print("STDOUT: Force deleting the nodes")
        terminated_instances=0
        completed_node_removal_ids=[]
        if CN != "CC":
            current_size = get_exact_instance_pool_size(
                ipa_ocid,
                comp_ocid,
                expected_display_name=cluster_name,
            )
        if CN == "CC" and autoscaling:
            completed_compute_cluster_removals = (
                complete_compute_cluster_node_removals(
                    compute_cluster_removal_journal,
                    parse_inventory(inventory),
                    inventory,
                )
            )
            terminated_instances += completed_compute_cluster_removals
            removal_targets = []
        else:
            removal_targets = (
            [
                (record["instance_display_name"], record)
                for record in planned_compute_cluster_removals
            ]
            if CN == "CC" and autoscaling
            else
            [
                (record["instance_display_name"], record)
                for record in planned_instance_pool_removals
            ]
            if CN in ["IP", "CN"] and autoscaling
            else [(instance_name, None) for instance_name in hostnames_to_remove]
            )
        for instanceName, frozen_node_removal in removal_targets:
            try:
                if frozen_node_removal is not None:
                    instance = computeClient.get_instance(
                        frozen_node_removal["instance_id"]
                    ).data
                else:
                    instance = find_cluster_instance_by_name(
                        comp_ocid,
                        instanceName,
                        cluster_name,
                    )
                if instance is None:
                    print("The instance "+instanceName+" does not exist")
                    continue
                instance_id = instance.id
                instance_private_ip = (
                    frozen_node_removal["private_ip"]
                    if frozen_node_removal is not None
                    and "private_ip" in frozen_node_removal
                    else next(
                        (
                            cluster_instance["ip"]
                            for cluster_instance in cn_instances
                            if cluster_instance["ocid"] == instance_id
                        ),
                        None,
                    )
                )
                if instance_private_ip is None:
                    raise RuntimeError(
                        "The private IP for instance "+instanceName+" was not found"
                    )
                pending_local_volume_deletion = None
                if CN != "CC":
                    instance_pool_dns_deleted = False
                    pending_node_removal = frozen_node_removal
                    pending_local_volume_deletion = remove_instance_pool_member_and_managed_local_block_volume(
                        comp_ocid,
                        ipa_ocid,
                        instance_id,
                        instance_private_ip,
                        inventory,
                    )
                    if pending_node_removal is not None:
                        delete_pending_instance_pool_node_dns_records(
                            pending_node_removal,
                            inventory,
                        )
                        completed_node_removal_ids.append(instance_id)
                        instance_pool_dns_deleted = True
                else:
                    instance_pool_dns_deleted = False
                    if autoscaling:
                        delete_compute_cluster_node_name_dns_records(
                            comp_ocid,
                            inventory_dict,
                            inventory,
                            cn_ocid,
                            {instanceName, instance.display_name},
                            instance_private_ip,
                        )
                        instance_pool_dns_deleted = True
                    terminate_instance_and_delete_launch_volumes(
                        instance_id,
                        delete_launch_created_data_volumes=(
                            frozen_node_removal[
                                "delete_launch_created_data_volumes"
                            ]
                            if frozen_node_removal is not None
                            and "delete_launch_created_data_volumes"
                            in frozen_node_removal
                            else None
                        ),
                    )
                if (
                    dns_entries
                    and pending_local_volume_deletion is None
                    and not instance_pool_dns_deleted
                ):
                    for dns_instance_name in {instanceName, instance.display_name}:
                        delete_private_dns_rrset_if_present(
                            zone_id,
                            dns_instance_name+"."+zone_name,
                        )
                    ip = ipaddress.ip_address(instance_private_ip)
                    if ip is not None:
                        index = list(private_subnet_cidr.hosts()).index(ip)+2
                        slurm_name=queue+"-"+instance_type+"-"+str(index)+"."+zone_name
                        delete_private_dns_rrset_if_present(zone_id,slurm_name)
                terminated_instances = terminated_instances + 1
                print("STDOUT: The instance "+instanceName+" is terminating")   
            except oci.exceptions.ServiceError as error:
                if error.status == 404:
                    print("The instance "+instanceName+" does not exist")
                else:
                    print("Failed to remove instance "+instanceName+": "+str(error))
                    raise
            except Exception as error:
                print("Failed to remove instance "+instanceName+": "+str(error))
                raise
        if CN == "CC":
            cn_instances = get_instances(comp_ocid,cn_ocid,CN)
            newsize=len(cn_instances)
            if autoscaling:
                remaining_instance_ids = {
                    instance["ocid"] for instance in cn_instances
                }
                planned_removal_ids = {
                    record["instance_id"]
                    for record in compute_cluster_removal_journal["removals"]
                }
                if remaining_instance_ids.intersection(planned_removal_ids):
                    raise RuntimeError(
                        "A Compute Cluster removal target remained after termination"
                    )
                clear_compute_cluster_node_removal_journal(inventory)
        else:
            current_cn_ocid = cn_ocid
            current_ipa_ocid = ipa_ocid
            newsize = get_exact_instance_pool_size(
                current_ipa_ocid,
                comp_ocid,
                expected_display_name=cluster_name,
            )
            ready_removed_nodes = retry_pending_local_block_volume_deletions(
                inventory,
                cluster_name,
                comp_ocid,
            )
            if ready_removed_nodes:
                active_instance_display_names = None
                active_instance_private_ips = None
                if dns_entries:
                    wait_for_running_status(
                        cluster_name,
                        comp_ocid,
                        current_cn_ocid,
                        CN,
                        expected_size=newsize,
                    )
                    (
                        active_instance_display_names,
                        active_instance_private_ips,
                    ) = get_active_instance_identities(
                        comp_ocid,
                        current_cn_ocid,
                        CN,
                        newsize,
                    )
                finalize_pending_local_block_volume_deletions(
                    inventory,
                    ready_removed_nodes,
                    cluster_name,
                    comp_ocid,
                    current_pool_size=newsize,
                    current_instance_pool_id=current_ipa_ocid,
                    active_instance_display_names=active_instance_display_names,
                    active_instance_private_ips=active_instance_private_ips,
                )
            else:
                updateTFState(inventory,cluster_name,newsize)
            for completed_instance_id in completed_node_removal_ids:
                forget_pending_instance_pool_node_removal(
                    inventory,
                    completed_instance_id,
                )
            if CN in ["IP", "CN"] and autoscaling and not no_reconfigure:
                try:
                    synchronize_autoscaling_managed_pool_names(
                        comp_ocid,
                        inventory,
                        cluster_name,
                        expected_instance_pool_id=current_ipa_ocid,
                        expected_cluster_network_id=(
                            current_cn_ocid if CN == "CN" else None
                        ),
                    )
                except Exception as name_sync_error:
                    raise RuntimeError(
                        "The managed pool reached size "+str(newsize)+
                        " but name synchronization failed. Run sync_instance_pool_names: "+
                        str(name_sync_error)
                    )
            elif CN in ["IP", "CN"] and autoscaling:
                clear_instance_pool_post_resize_recovery(inventory)
        print("STDOUT: Resized to "+str(newsize)+" instances")
#        if error_code != 0 and force:
#            print("STDOUT: The nodes were forced deleted, trying to reconfigure the left over nodes")
#            reconfigure(comp_ocid,cn_ocid,inventory,CN)

    if args.mode == 'add':
        cn_instances = get_instances(comp_ocid,cn_ocid,CN)
        previous_instance_ids = {instance['ocid'] for instance in cn_instances}
        if autoscaling:
            current_inventory = parse_inventory(inventory)
            inventory_instance_ids = get_compute_inventory_instance_ids(
                current_inventory
            )
            if inventory_instance_ids != previous_instance_ids:
                raise RuntimeError(
                    "The Autoscaling compute deployment and inventory do not match. "
                    "Run reconfigure before adding more instances"
                )
            if CN == "CC":
                # A copied Terraform state must still account for every initial
                # node.  Otherwise a missing tracked node could be mistaken for
                # a normal state-external resize member.
                get_compute_cluster_state_external_member_ids(
                    inventory,
                    cn_ocid,
                    cn_instances,
                )
        launched_compute_instances=[]
        pool_rollback_size=None
        if CN == "CC":
            current_size=len(cn_instances)
            expected_size=current_size+args.number
            if len(cn_instances) == 0:
                print("The resize script cannot work for a compute cluster if the size is there is no node in the cluster")
                exit(1)
            else:
                instance=computeClient.get_instance(cn_instances[0]['ocid']).data

                try:
                    for i in range(args.number):
                        launch_display_name = generate_compute_cluster_launch_display_name(
                            cluster_name
                        )
                        launched_instance = {
                            "display_name": launch_display_name,
                            "ocid": None,
                        }
                        launch_instance_details=getLaunchInstanceDetails(
                            instance,
                            comp_ocid,
                            cn_ocid,
                            launch_display_name,
                            local_block_volume_config,
                        )
                        refuse_terraform_state_mutation_while_locked(
                            os.path.dirname(os.path.abspath(inventory))
                        )
                        launched_compute_instances.append(launched_instance)
                        launch_response = computeClient.launch_instance(
                            launch_instance_details,
                            opc_retry_token=str(uuid.uuid4()),
                        )
                        launched_instance_id = getattr(
                            launch_response.data,
                            "id",
                            None,
                        )
                        if not isinstance(launched_instance_id, str) or not launched_instance_id:
                            raise RuntimeError(
                                "Compute Cluster launch did not return an instance OCID"
                            )
                        # Record the POST result before waiting so a waiter or
                        # VNIC/volume failure always rolls back the exact OCID.
                        launched_instance["ocid"] = launched_instance_id
                        wait_for_compute_cluster_instance_running(
                            launched_instance_id,
                            launch_display_name,
                            cluster_name,
                            comp_ocid,
                        )
                        if local_block_volume_config["enabled"]:
                            get_local_block_volume_attachment(
                                comp_ocid,
                                launched_instance_id,
                            )
                except Exception:
                    rollback_compute_cluster_instances(launched_compute_instances)
                    raise
        else:
            size = current_size - pool_members_to_remove_len + args.number
            expected_size=size
            pool_rollback_size=current_size-pool_members_to_remove_len
            if CN in ["IP", "CN"] and autoscaling:
                # Establish the recovery boundary before changing pool size.
                # A process interruption on either side of the OCI update can
                # therefore be recovered without applying the add count twice.
                write_instance_pool_post_resize_recovery(
                    inventory,
                    cluster_name,
                    ipa_ocid,
                    status=(
                        "state_only"
                        if no_reconfigure
                        else "reconfigure_and_sync"
                    ),
                    action="add",
                    source_size=pool_rollback_size,
                    target_size=size,
                )
            try:
                update_size = oci.core.models.UpdateInstancePoolDetails(size=size)
                ComputeManagementClientCompositeOperations.update_instance_pool_and_wait_for_state(ipa_ocid,update_size,['RUNNING'],waiter_kwargs={'max_wait_seconds':3600})
                wait_for_running_status(cluster_name,comp_ocid,cn_ocid,CN,expected_size=size)
            except Exception:
                rollback_instance_pool_size(ipa_ocid,pool_rollback_size)
                raise
        try:
            new_cn_instances = get_instances(comp_ocid,cn_ocid,CN)
            newsize=len(new_cn_instances)
            if newsize != expected_size:
                raise RuntimeError("Cluster has "+str(newsize)+" instances, expected "+str(expected_size))
            new_instances = [instance for instance in new_cn_instances if instance['ocid'] not in previous_instance_ids]
            if len(new_instances) != args.number:
                raise RuntimeError("Cluster added "+str(len(new_instances))+" instances, expected "+str(args.number))
            if local_block_volume_config["enabled"]:
                for new_instance in new_instances:
                    get_local_block_volume_attachment(comp_ocid,new_instance['ocid'])
        except Exception:
            if launched_compute_instances:
                rollback_compute_cluster_instances(launched_compute_instances)
            elif pool_rollback_size is not None:
                rollback_instance_pool_size(ipa_ocid,pool_rollback_size)
            raise
        if dns_entries:
            for new_instance in new_instances:
                instanceName=new_instance['display_name']
                ip = ipaddress.ip_address(new_instance['ip'])
                index = list(private_subnet_cidr.hosts()).index(ip)+2
                if (
                    slurm_enabled
                    or not autoscaling
                ):
                    slurm_name=queue+"-"+instance_type+"-"+str(index)+"."+zone_name
                    get_rr_set_response = dns_client.update_rr_set(zone_name_or_id=zone_id,domain=slurm_name,rtype="A",scope="PRIVATE",update_rr_set_details=oci.dns.models.UpdateRRSetDetails(items=[oci.dns.models.RecordDetails(domain=slurm_name,rdata=new_instance['ip'],rtype="A",ttl=3600,)]))
                if not autoscaling:
                    get_rr_set_response = dns_client.update_rr_set(zone_name_or_id=zone_id,domain=instanceName+"."+zone_name,rtype="A",scope="PRIVATE",update_rr_set_details=oci.dns.models.UpdateRRSetDetails(items=[oci.dns.models.RecordDetails(domain=instanceName+"."+zone_name,rdata=new_instance['ip'],rtype="A",ttl=3600)]))
        # Instance Pool / Cluster Network size lives in the managed pool state.
        # Compute Cluster additions are state-external API instances; applying
        # the pool-only state rewriter to that state can corrupt unrelated size
        # attributes and would make Terraform plan duplicate instances.
        if CN != "CC":
            updateTFState(inventory,cluster_name,newsize)
        if not no_reconfigure:
            reconfigure_status = add_reconfigure(comp_ocid,cn_ocid,inventory,CN)
            if reconfigure_status != 0:
                exit(reconfigure_status)
        elif CN in ["IP", "CN"] and autoscaling:
            clear_instance_pool_post_resize_recovery(inventory)
