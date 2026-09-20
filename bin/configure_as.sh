#!/bin/bash
#
# Cluster init configuration script
#

#
# wait for cloud-init completion on the controller host
#

scripts=`realpath $0`
folder=`dirname $scripts`
playbooks_path=$folder/../playbooks/
inventory_path=$folder/../autoscaling/clusters/$1
variables_path=$inventory_path/variables.tf
configure_stage_path=$inventory_path/.initial-configure-stage

is_autoscaling_compute_deployment()
{
  [ -f "$inventory_path/inventory" ] \
    && grep -Eq '^[[:space:]]*cluster_network[[:space:]]*=[[:space:]]*(true|false)[[:space:]]*$' "$inventory_path/inventory" \
    && [ -f "$variables_path" ]
}

synchronize_compute_names()
{
  if ! python3 "$folder/resize.py" --cluster_name "$1" --inventory "$inventory_path/inventory" sync_instance_pool_names
  then
    echo "Failed to synchronize compute names from final OS hostnames for $1" >&2
    return 1
  fi
}

reconcile_compute_monitoring()
{
  if ! bash "$folder/resize.sh" --cluster_name "$1" --reconcile-monitoring
  then
    echo "Failed to reconcile compute monitoring names for $1" >&2
    return 1
  fi
}

# Record the next unfinished phase before starting it.  In particular, a retry
# after an early name-sync failure must still configure the node before success.
save_configure_stage()
{
  if ! python3 "$folder/initial_configure_state.py" write --inventory "$inventory_path/inventory" --stage "$1"
  then
    echo "Failed to save initial configuration stage for $inventory_path" >&2
    return 1
  fi
  configure_stage=$1
}

autoscaling_compute=false
if is_autoscaling_compute_deployment; then
  autoscaling_compute=true
fi

configure_stage=""
if [ -e "$configure_stage_path" ] || [ -L "$configure_stage_path" ]; then
  if [ "$autoscaling_compute" != true ] \
    || [ ! -f "$configure_stage_path" ] || [ -L "$configure_stage_path" ]; then
    echo "Invalid initial configuration stage file: $configure_stage_path" >&2
    exit 1
  fi
  configure_stage=$(python3 "$folder/initial_configure_state.py" read --inventory "$inventory_path/inventory") || exit 1
  case "$configure_stage" in
    sync|configure|monitoring|legacy-sync) ;;
    *) echo "Unknown initial configuration stage in $configure_stage_path" >&2; exit 1 ;;
  esac
fi

# Releases before early synchronization wrote the name journal only AFTER the
# entire playbook had succeeded.  Preserve their sync-only recovery path.
if [ "$autoscaling_compute" = true ] \
  && [ -z "$configure_stage" ] \
  && [ -f "$inventory_path/.instance-pool-hostname-sync.json" ]
then
  save_configure_stage legacy-sync || exit 1
fi
if [ "$configure_stage" = legacy-sync ]; then
  synchronize_compute_names "$1" || exit 1
  save_configure_stage monitoring || exit 1
fi

# Monitoring retries must not restart Slurm or touch a running node's hostname.
if [ "$configure_stage" = monitoring ]; then
  reconcile_compute_monitoring "$1" || exit 1
  python3 "$folder/initial_configure_state.py" clear --inventory "$inventory_path/inventory"
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

if ! "$folder/wait_for_hosts.sh" "$inventory_path/hosts_$1" "$username"; then
  echo "SSH did not become ready on all autoscaling nodes" >&2
  exit 1
fi
#
# Ansible will take care of key exchange and learning the host fingerprints, but for the first time we need
# to disable host key checking. 
#

if [ "$autoscaling_compute" = true ]; then
  if [ -z "$configure_stage" ]; then
    if ! ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook "$playbooks_path/new_nodes_hostname.yml" -i "$inventory_path/inventory"; then
      echo "Failed to prepare autoscaling OS hostnames for $1" >&2
      exit 1
    fi
    save_configure_stage sync || exit 1
  fi
  if [ "$configure_stage" = sync ]; then
    synchronize_compute_names "$1" || exit 1
    save_configure_stage configure || exit 1
  fi

  # Start a new Ansible process to load the renamed inventory, and never change
  # the OS hostname again after the immutable name-sync plan has been applied.
  if ! ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook "$playbooks_path/new_nodes.yml" -i "$inventory_path/inventory" --extra-vars '{"autoscaling_names_prepared": true}'; then
    echo "Failed to configure autoscaling nodes for $1" >&2
    exit 1
  fi
  save_configure_stage monitoring || exit 1
  reconcile_compute_monitoring "$1" || exit 1
  python3 "$folder/initial_configure_state.py" clear --inventory "$inventory_path/inventory" || exit 1
elif ! ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook "$playbooks_path/new_nodes.yml" -i "$inventory_path/inventory"; then
  echo "Failed to configure nodes for $1" >&2
  exit 1
fi
