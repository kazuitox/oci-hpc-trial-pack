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
            try:
                for potential_vnic_attachment in oci.pagination.list_call_get_all_results(computeClient.list_vnic_attachments,compartment_id=comp_ocid,instance_id=instance.id).data:
                    if potential_vnic_attachment.display_name is None:
                        vnic_attachment = potential_vnic_attachment
                vnic = virtualNetworkClient.get_vnic(vnic_attachment.vnic_id).data
            except:
                continue
            cn_instances.append({'display_name':instance.display_name,'ip':vnic.private_ip,'ocid':instance.id})   
    else:
        if CN == "CN":
            instance_summaries = oci.pagination.list_call_get_all_results(computeManagementClient.list_cluster_network_instances,comp_ocid,cn_ocid).data
        else:
            instance_summaries = oci.pagination.list_call_get_all_results(computeManagementClient.list_instance_pool_instances,comp_ocid,cn_ocid).data
        for instance_summary in instance_summaries:
            try:
                instance=computeClient.get_instance(instance_summary.id).data
                for potential_vnic_attachment in oci.pagination.list_call_get_all_results(computeClient.list_vnic_attachments,compartment_id=comp_ocid,instance_id=instance.id).data:
                    if potential_vnic_attachment.display_name is None:
                        vnic_attachment = potential_vnic_attachment
                vnic = virtualNetworkClient.get_vnic(vnic_attachment.vnic_id).data
            except:
                continue
            cn_instances.append({'display_name':instance_summary.display_name,'ip':vnic.private_ip,'ocid':instance_summary.id})   
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
            if instance.display_name != host_name:
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
            if host in line:
                inventory_dict['compute_configured'].remove(line)
                ips_to_remove.append(line.split("ansible_host=")[1].split("ansible_user=")[0].strip())
                hostRemoved=True
        for line in inventory_dict['compute_to_add']:
            if host in line:
                inventory_dict['compute_to_add'].remove(line)
                ips_to_remove.append(line.split("ansible_host=")[1].split("ansible_user=")[0].strip())
                hostRemoved=True
        for line in inventory_dict['nfs']:
            if host in line:
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
    if not len(ips_to_remove):
        print("No hostname found, trying anyway with "+" ".join(nodes_to_remove))
        for node in nodes_to_remove: # Temporary fix while the playbook is changed to be able to run multiple at the time
            update_flag = update_cluster(tmp_inventory_destroy,playbook,add_vars={"unreachable_node_list":node})
            time.sleep(10)
    else:
        for ip in ips_to_remove: # Temporary fix while the playbook is changed to be able to run multiple at the time
            update_flag = update_cluster(tmp_inventory_destroy,playbook,add_vars={"unreachable_node_list":ip})
            time.sleep(10)
    if update_flag == 0:
        os.remove(tmp_inventory_destroy)
        inventory_dict['compute_to_destroy']=[]
        tmp_inventory="/tmp/"+inventory.replace('/','_')
        write_inventory(inventory_dict,tmp_inventory)
        os.system('sudo mv '+tmp_inventory+' '+inventory)
        os.system('')
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
            if host in line:
                if host in [node['display_name'] for node in reachable_node_to_remove ]:
                    inventory_dict['compute_to_destroy'].append(line)
                compute_to_remove.append(line)
        for line in inventory_dict['compute_to_add']:
            if host in line:
                if host in [node['display_name'] for node in reachable_node_to_remove ]:
                    inventory_dict['compute_to_destroy'].append(line)
                compute_to_remove.append(line)
        for line in inventory_dict['nfs']:
            if host in line:
                if host in [node['display_name'] for node in reachable_node_to_remove ]:
                    nfs_to_remove.append(line)
        for line in compute_to_remove:
            inventory_dict['compute_configured'].remove(line)
        for line in nfs_to_remove:
            inventory_dict['nfs'].remove(line)
    for instance in unreachable_instances:
        for line in inventory_dict['compute_configured']:
            if instance['display_name'] in line:
                inventory_dict['compute_configured'].remove(line)
        for line in inventory_dict['compute_to_add']:
           if instance['display_name'] in line:
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
    if not os.path.isfile(inventory):
        print("There is no inventory file, are you on the controller? The cluster has been resized but not reconfigured")
        exit()
    host_to_wait_for=[]
    for node in reachable_instances:
        name=node['display_name']
        ip=node['ip']
        configured=False
        for line in inventory_dict['compute_configured']:
            if name in line and ip in line:
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
        os.system('sudo mv '+tmp_inventory+' '+inventory)
    else:
        print("The reconfiguration to add the node(s) had an error")
        print("Try rerunning this command: ansible-playbook -i "+tmp_inventory_add+' '+playbooks_dir+"resize_add.yml" )
    return update_flag

def reconfigure(comp_ocid,cn_ocid,inventory,CN, crucial=False):
    instances = get_instances(comp_ocid,cn_ocid,CN)
    if not os.path.isfile(inventory):
        print("There is no inventory file, are you on the controller? Reconfigure did not happen")
        exit()
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
        os.system('sudo mv '+tmp_inventory_reconfig+' '+inventory)
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

def find_cluster_instance_by_name(compartment_id, instance_name, expected_cluster_name):
    instances = oci.pagination.list_call_get_all_results(
        computeClient.list_instances,
        compartment_id=compartment_id,
        display_name=instance_name,
    ).data
    matches = []
    for instance in instances:
        tags = instance.freeform_tags or {}
        parent_cluster = tags.get("parent_cluster", tags.get("cluster_name"))
        if (
            instance.display_name == instance_name
            and instance.lifecycle_state != "TERMINATED"
            and parent_cluster == expected_cluster_name
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

def retry_pending_local_block_volume_deletions(
    inventory_path,
    expected_cluster_name,
    compartment_id,
    resume_pool_members=False,
):
    pending_deletions = load_pending_local_block_volume_deletions(inventory_path)
    ready_deletions = []
    for record in list(pending_deletions):
        if record["cluster_name"] != expected_cluster_name or record["compartment_id"] != compartment_id:
            raise RuntimeError("A pending local Block Volume deletion does not match the requested cluster")
        try:
            instance = computeClient.get_instance(record["instance_id"]).data
            instance_tags = instance.freeform_tags or {}
            if instance_tags.get("parent_cluster", instance_tags.get("cluster_name")) != expected_cluster_name:
                raise RuntimeError("The pending local Block Volume instance belongs to another cluster")
            if instance.display_name != record["instance_display_name"]:
                raise RuntimeError("The pending local Block Volume instance name changed unexpectedly")
            instance_state = instance.lifecycle_state
        except oci.exceptions.ServiceError as error:
            if error.status != 404:
                raise
            instance_state = "TERMINATED"
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
                if not resume_pool_members:
                    raise RuntimeError(
                        "Pending removal for "+record["instance_display_name"]+
                        " must be resumed explicitly with --nodes "+record["instance_display_name"]
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
        try:
            volume = blockstorageClient.get_volume(record["volume_id"]).data
        except oci.exceptions.ServiceError as error:
            if error.status == 404:
                ready_deletions.append(record)
                continue
            raise
        validate_pending_local_block_volume(
            record,
            volume,
            expected_cluster_name,
            compartment_id,
        )
        delete_managed_local_block_volume(record["volume_id"])
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
    if queue is None or private_subnet_cidr is None:
        raise RuntimeError("Inventory does not contain queue and private_subnet values required for DNS cleanup")
    private_ip = ipaddress.ip_address(record["instance_private_ip"])
    try:
        host_index = list(private_subnet_cidr.hosts()).index(private_ip)+2
    except ValueError:
        raise RuntimeError(
            "The pending node private IP is outside private_subnet: "+record["instance_private_ip"]
        )
    return [
        record["instance_display_name"]+"."+zone_name,
        queue+"-"+instance_type+"-"+str(host_index)+"."+zone_name,
    ]

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
    zones = dns_client.list_zones(
        compartment_id=compartment_id,
        name=zone_name,
        zone_type="PRIMARY",
        scope="PRIVATE",
    ).data
    if len(zones) == 0:
        # Terraform may already have removed the private zone on a destroy retry.
        return len(ready_deletions)
    domains_to_delete = []
    for record in ready_deletions:
        domains_to_delete.extend(get_pending_node_dns_domains(record))
    for domain in domains_to_delete:
        delete_private_dns_rrset_if_present(zones[0].id, domain)
    return len(ready_deletions)

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
        zones = dns_client.list_zones(
            compartment_id=compartment_id,
            name=zone_name,
            zone_type="PRIMARY",
            scope="PRIVATE",
        ).data
        if len(zones) == 0:
            raise RuntimeError("Private DNS zone "+zone_name+" was not found")
        if queue is None or private_subnet_cidr is None:
            raise RuntimeError("Inventory does not contain queue and private_subnet values required for DNS cleanup")
        zone_id = zones[0].id
        domains_to_delete = []
        for record in ready_deletions:
            private_ip = ipaddress.ip_address(record["instance_private_ip"])
            if (
                record["instance_display_name"] in active_instance_display_names
                or str(private_ip) in active_instance_private_ips
            ):
                raise RuntimeError(
                    "A pending node hostname or private IP is in use by an active instance"
                )
            domains_to_delete.extend(get_pending_node_dns_domains(record))
        for domain in domains_to_delete:
            delete_private_dns_rrset_if_present(zone_id, domain)
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
):
    pending_deletions = load_pending_local_block_volume_deletions(inventory_path)
    pending_instance_names = {
        record["instance_display_name"] for record in pending_deletions
    }
    resume_pool_members = (
        mode == "cleanup_compute_cluster"
        or (
            mode in ["remove", "remove_unreachable"]
            and pending_instance_names.issubset(set(requested_hostnames))
        )
    )
    return retry_pending_local_block_volume_deletions(
        inventory_path,
        expected_cluster_name,
        compartment_id,
        resume_pool_members=resume_pool_members,
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

def get_tracked_compute_cluster_resources(inventory_path):
    state_path = os.path.join(os.path.dirname(inventory_path), "terraform.tfstate")
    if not os.path.isfile(state_path):
        return None
    with open(state_path, "r") as state_file:
        state = json.load(state_file)
    tracked_compute_cluster_ids = set()
    tracked_ids = set()
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed":
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

def rollback_compute_cluster_instances(instance_names):
    rollback_errors = []
    for instance_name in instance_names:
        try:
            instance = find_cluster_instance_by_name(comp_ocid,instance_name,cluster_name)
            if instance is not None:
                terminate_instance_and_delete_launch_volumes(instance.id)
        except Exception as error:
            rollback_errors.append(instance_name+": "+str(error))
    if rollback_errors:
        print("STDOUT: Compute Cluster rollback had errors: "+"; ".join(rollback_errors))

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
    
def getLaunchInstanceDetails(instance,comp_ocid,cn_ocid,max_previous_index,index,local_block_volume_config):

    agent_config=instance.agent_config
    agent_config.__class__ = oci.core.models.LaunchInstanceAgentConfigDetails

    for potential_vnic_attachment in oci.pagination.list_call_get_all_results(computeClient.list_vnic_attachments,compartment_id=comp_ocid,instance_id=instance.id).data:
        if potential_vnic_attachment.display_name is None:
            vnic_attachment = potential_vnic_attachment
    splitted_name=instance.display_name.split('-')
    create_vnic_details=oci.core.models.CreateVnicDetails(assign_public_ip=False,subnet_id=vnic_attachment.subnet_id)

    shape_config=instance.shape_config
    try: 
        nvmes=shape_config.local_disks
        launchInstanceShapeConfigDetails = oci.core.models.LaunchInstanceShapeConfigDetails(baseline_ocpu_utilization=shape_config.baseline_ocpu_utilization,memory_in_gbs=shape_config.memory_in_gbs,nvmes=nvmes,ocpus=shape_config.ocpus)
    except:
        launchInstanceShapeConfigDetails = oci.core.models.LaunchInstanceShapeConfigDetails(baseline_ocpu_utilization=shape_config.baseline_ocpu_utilization,memory_in_gbs=shape_config.memory_in_gbs,ocpus=shape_config.ocpus)

    splitted_name[-1]=str(max_previous_index+1+index)
    new_display_name = '-'.join(splitted_name)
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
parser.add_argument('mode', help='Mode type. add/remove node options, implicitly configures newly added nodes. Also implicitly reconfigure/restart services like Slurm to recognize new nodes. Similarly for remove option, terminates nodes and implicitly reconfigure/restart services like Slurm on rest of the cluster nodes to remove reference to deleted nodes.',choices=['add','remove','remove_unreachable','list','reconfigure','cleanup_compute_cluster','prepare_local_block_volume'],default='list',nargs='?')
parser.add_argument('number', type=int, help="Number of nodes to add or delete if a list of hostnames is not defined",nargs='?')
parser.add_argument('--nodes', help="List of nodes to delete (Space Separated)",nargs='+')
parser.add_argument('--no_reconfigure', help='If present. Does not rerun the playbooks',action='store_true',default=False)
parser.add_argument('--user_logging', help='If present. Use the default settings in ~/.oci/config to connect to the API. Default is using instance_principal',action='store_true',default=False)
parser.add_argument('--force', help='If present. Nodes will be removed even if the destroy playbook failed',action='store_true',default=False)
parser.add_argument('--ansible_crucial', help='If present during reconfiguration, only crucial ansible playbooks will be executed on the live nodes. Non live nodes will be removed',action='store_true',default=False)
parser.add_argument('--remove_unreachable', help='If present, nodes that are not sshable will be terminated before running the action that was requested (Example Adding a node) ',action='store_true',default=False)
parser.add_argument('--quiet', help='If present, the script will not prompt for a response when removing nodes and will not give a reminder to save data from nodes that are being removed ',action='store_true',default=False)

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
if args.mode != "prepare_local_block_volume":
    try:
        ready_pending_deletions = process_pending_local_block_volume_deletions_for_operation(
            inventory,
            cluster_name,
            comp_ocid,
            args.mode,
            hostnames,
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
        print("STDOUT: Terraform state does not manage a Compute Cluster; no state-external instances need cleanup")
        exit(0)
    try:
        compute_cluster = computeClient.get_compute_cluster(tracked_compute_cluster_id).data
    except oci.exceptions.ServiceError as error:
        if error.status == 404:
            print("STDOUT: The Terraform-managed Compute Cluster is already deleted")
            exit(0)
        raise
    if compute_cluster.display_name != cluster_name or compute_cluster.compartment_id != comp_ocid:
        raise RuntimeError("Terraform state Compute Cluster identity does not match the requested cluster")
    terminate_compute_cluster_instances(comp_ocid,tracked_compute_cluster_id,excluded_instance_ids=tracked_instance_ids)
    exit(0)

cn_summary,ip_summary,CN = get_summary(comp_ocid,cluster_name)
if cn_summary is None:
    exit(1)
cn_ocid =cn_summary.id

if CN != "CC":
    current_size=ip_summary.size
    if CN == "CN":
        ipa_ocid = cn_summary.instance_pools[0].id
    else:
        ipa_ocid = cn_ocid

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
    if args.mode in ["remove", "remove_unreachable"]:
        print(
            "STDOUT: Completed "+str(completed_pending_deletions)+
            " pending node removal(s); inspect the cluster before requesting additional removals"
        )
        exit(0)

if args.mode == 'list':
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
        zones = dns_client.list_zones(compartment_id=comp_ocid,name=zone_name,zone_type="PRIMARY",scope="PRIVATE").data
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
    if args.mode == 'remove_unreachable':
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
    if args.mode == 'remove':
        if len(hostnames) == 0:
            nfsNode=getNFSnode(inventory)
            non_nfs=[i for i in cn_instances if i['display_name'] != nfsNode]
            additional_nodes_to_remove_number=args.number-len(hostnames_to_remove)
            if additional_nodes_to_remove_number > 0:
                if additional_nodes_to_remove_number < len(cn_instances):
                    hostnames_to_remove=hostnames_to_remove+[non_nfs[i]['display_name'] for i in range(len(non_nfs)-additional_nodes_to_remove_number,len(non_nfs))]
                else:
                    hostnames_to_remove=[cn_instances[i]['display_name'] for i in range(len(cn_instances))]
        else:
            hostnames_to_remove2 = list(hostnames)
            hostnames_to_remove2.extend(x for x in hostnames_to_remove if x not in hostnames_to_remove2)
            hostnames_to_remove=hostnames_to_remove2
    hostnames_to_remove_len=len(hostnames_to_remove)
    if hostnames_to_remove_len:
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
        cn_summary,ip_summary,CN = get_summary(comp_ocid,cluster_name)
        if CN != "CC": 
            current_size = ip_summary.size
        for instanceName in hostnames_to_remove:
            try:
                instance = find_cluster_instance_by_name(comp_ocid,instanceName,cluster_name)
                if instance is None:
                    print("The instance "+instanceName+" does not exist")
                    continue
                instance_id = instance.id
                pending_local_volume_deletion = None
                if CN != "CC":
                    instance_private_ip = next(
                        (
                            cluster_instance["ip"]
                            for cluster_instance in cn_instances
                            if cluster_instance["ocid"] == instance_id
                        ),
                        None,
                    )
                    if instance_private_ip is None:
                        raise RuntimeError("The private IP for instance "+instanceName+" was not found")
                    pending_local_volume_deletion = remove_instance_pool_member_and_managed_local_block_volume(
                        comp_ocid,
                        ipa_ocid,
                        instance_id,
                        instance_private_ip,
                        inventory,
                    )
                else:
                    terminate_instance_and_delete_launch_volumes(instance_id)
                if dns_entries and pending_local_volume_deletion is None:
                    delete_private_dns_rrset_if_present(zone_id,instanceName+"."+zone_name)
                    ip=None
                    for i in cn_instances: 
                        if i['display_name'] == instanceName:
                            ip = ipaddress.ip_address(i['ip'])
                    if not ip is None:
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
        cn_summary,ip_summary,CN = get_summary(comp_ocid,cluster_name)
        if CN == "CC":
            cn_instances = get_instances(comp_ocid,cn_ocid,CN)
            newsize=len(cn_instances)
        else:
            current_cn_ocid = cn_summary.id
            if CN == "CN":
                current_ipa_ocid = cn_summary.instance_pools[0].id
            else:
                current_ipa_ocid = current_cn_ocid
            newsize=ip_summary.size
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
        print("STDOUT: Resized to "+str(newsize)+" instances")
#        if error_code != 0 and force:
#            print("STDOUT: The nodes were forced deleted, trying to reconfigure the left over nodes")
#            reconfigure(comp_ocid,cn_ocid,inventory,CN)

    if args.mode == 'add':
        cn_instances = get_instances(comp_ocid,cn_ocid,CN)
        previous_instance_ids = {instance['ocid'] for instance in cn_instances}
        launched_instance_names=[]
        pool_rollback_size=None
        if CN == "CC":
            current_size=len(cn_instances)
            expected_size=current_size+args.number
            if len(cn_instances) == 0:
                print("The resize script cannot work for a compute cluster if the size is there is no node in the cluster")
                exit(1)
            else:
                max_index=-1
                for cn_instance in cn_instances:
                    if int(cn_instance['display_name'].split('-')[-1]) > max_index:
                        max_index=int(cn_instance['display_name'].split('-')[-1])
                instance=computeClient.get_instance(cn_instances[0]['ocid']).data

                try:
                    for i in range(args.number):
                        launch_instance_details=getLaunchInstanceDetails(instance,comp_ocid,cn_ocid,max_index,i,local_block_volume_config)
                        launched_instance_names.append(launch_instance_details.display_name)
                        launch_response = ComputeClientCompositeOperations.launch_instance_and_wait_for_state(
                            launch_instance_details,
                            wait_for_states=["RUNNING"],
                            operation_kwargs={"opc_retry_token": str(uuid.uuid4())},
                            waiter_kwargs={"max_wait_seconds": 3600},
                        )
                        if local_block_volume_config["enabled"]:
                            get_local_block_volume_attachment(comp_ocid,launch_response.data.id)
                except Exception:
                    rollback_compute_cluster_instances(launched_instance_names)
                    raise
        else:
            size = current_size - hostnames_to_remove_len + args.number
            expected_size=size
            pool_rollback_size=current_size-hostnames_to_remove_len
            try:
                update_size = oci.core.models.UpdateInstancePoolDetails(size=size)
                ComputeManagementClientCompositeOperations.update_instance_pool_and_wait_for_state(ipa_ocid,update_size,['RUNNING'],waiter_kwargs={'max_wait_seconds':3600})
                wait_for_running_status(cluster_name,comp_ocid,cn_ocid,CN,expected_size=size)
            except Exception:
                rollback_instance_pool_size(ipa_ocid,pool_rollback_size)
                raise
        try:
            cn_summary,ip_summary,CN = get_summary(comp_ocid,cluster_name)
            if cn_summary is None:
                raise RuntimeError("Cluster "+cluster_name+" was not found after adding instances")
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
            if launched_instance_names:
                rollback_compute_cluster_instances(launched_instance_names)
            elif pool_rollback_size is not None:
                rollback_instance_pool_size(ipa_ocid,pool_rollback_size)
            raise
        if dns_entries:
            for new_instance in new_instances:
                instanceName=new_instance['display_name']
                ip = ipaddress.ip_address(new_instance['ip'])
                index = list(private_subnet_cidr.hosts()).index(ip)+2
                slurm_name=queue+"-"+instance_type+"-"+str(index)+"."+zone_name
                get_rr_set_response = dns_client.update_rr_set(zone_name_or_id=zone_id,domain=slurm_name,rtype="A",scope="PRIVATE",update_rr_set_details=oci.dns.models.UpdateRRSetDetails(items=[oci.dns.models.RecordDetails(domain=slurm_name,rdata=new_instance['ip'],rtype="A",ttl=3600,)]))
                get_rr_set_response = dns_client.update_rr_set(zone_name_or_id=zone_id,domain=instanceName+"."+zone_name,rtype="A",scope="PRIVATE",update_rr_set_details=oci.dns.models.UpdateRRSetDetails(items=[oci.dns.models.RecordDetails(domain=instanceName+"."+zone_name,rdata=new_instance['ip'],rtype="A",ttl=3600)]))
        updateTFState(inventory,cluster_name,newsize)
        if not no_reconfigure:
            reconfigure_status = add_reconfigure(comp_ocid,cn_ocid,inventory,CN)
            if reconfigure_status != 0:
                exit(reconfigure_status)
