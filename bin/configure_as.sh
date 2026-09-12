#!/bin/bash
#
# Cluster init configuration script
#

#
# wait for cloud-init completion on the controller host
#

scripts=`realpath $0`
folder=`dirname $scripts`
execution=1
playbooks_path=$folder/../playbooks/
inventory_path=$folder/../autoscaling/clusters/$1
variables_path=$inventory_path/variables.tf

is_autoscaling_compute_deployment()
{
  [ -f "$inventory_path/inventory" ] \
    && grep -Eq '^[[:space:]]*cluster_network[[:space:]]*=[[:space:]]*(true|false)[[:space:]]*$' "$inventory_path/inventory" \
    && [ -f "$variables_path" ]
}

synchronize_compute_names_and_monitoring()
{
  if ! python3 "$folder/resize.py" --cluster_name "$1" --inventory "$inventory_path/inventory" sync_instance_pool_names
  then
    echo "Failed to synchronize compute names from final OS hostnames for $1" >&2
    return 1
  fi
  if ! bash "$folder/resize.sh" --cluster_name "$1" --reconcile-monitoring
  then
    echo "Failed to reconcile compute monitoring names for $1" >&2
    return 1
  fi
}

# A journal exists only after Ansible facts were collected and OCI mutation was
# about to begin.  On a Terraform/configure retry, finish that immutable plan
# before any playbook can change the final OS hostname it records.
if [ -f "$variables_path" ] \
  && is_autoscaling_compute_deployment \
  && [ -f "$inventory_path/.instance-pool-hostname-sync.json" ]
then
  synchronize_compute_names_and_monitoring "$1"
  exit $?
fi

if ! python3 "$folder/resize.py" --cluster_name "$1" --inventory "$inventory_path/inventory" prepare_local_block_volume; then
  echo "Failed to prepare local Block Volume inventory for $1" >&2
  exit 1
fi


username=`cat $inventory_path/inventory | grep compute_username= | tail -n 1| awk -F "=" '{print $2}'`
if [ "$username" == "" ]
then
username=$USER
fi

if ! /opt/oci-hpc/bin/wait_for_hosts.sh "$inventory_path/hosts_$1" "$username"; then
  echo "SSH did not become ready on all autoscaling nodes" >&2
  exit 1
fi
#
# Ansible will take care of key exchange and learning the host fingerprints, but for the first time we need
# to disable host key checking. 
#

if [[ $execution -eq 1 ]] ; then
  ANSIBLE_HOST_KEY_CHECKING=False ansible all -m setup --tree /tmp/ansible > /dev/null 2>&1
  if ! ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook $playbooks_path/new_nodes.yml -i $inventory_path/inventory; then
    echo "Failed to configure autoscaling nodes for $1" >&2
    exit 1
  fi
  if [ -f "$variables_path" ] && is_autoscaling_compute_deployment
  then
    if ! synchronize_compute_names_and_monitoring "$1"
    then
      exit 1
    fi
  fi
else

	cat <<- EOF > /tmp/motd
	At least one of the cluster nodes has been innacessible during installation. Please validate the hosts and re-run: 
    ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook $playbooks_path/new_nodes.yml -i $inventory_path/inventory
EOF
  exit 1
fi 
