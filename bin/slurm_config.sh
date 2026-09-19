#!/bin/bash
#
# Regenerate Slurm Config
#
# Add --initial as argument if you need to restart slurm from scratch (Removes the current topology file)


scripts=`realpath $0`
folder=`dirname $scripts`
autoscaling_folder=$folder/../autoscaling/
conf_folder=$folder/../conf/
playbooks_path=$folder/../playbooks/

# Validate every queue before --initial can delete topology.conf or restart
# slurmctld, and before Ansible can publish any generated configuration.
python3 "$folder/validate_queues.py" "$conf_folder/queues.conf" || exit $?

source /etc/os-release

if [[ ${@: -1} == "--INITIAL" || ${@: -1} == "--initial" || ${@: -1} == "-INITIAL" || ${@: -1} == "-initial" ]]
then
   sudo rm /etc/slurm/topology.conf
   case "$ID" in
      ubuntu)
         slurmctld_path=/usr/local/sbin/slurmctld
         ;;
      ol|centos)
         slurmctld_path=/usr/sbin/slurmctld
         ;;
      *)
         echo "Unsupported OS: $ID" >&2
         exit 1
         ;;
   esac
   sudo "$slurmctld_path" -c
fi
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook "$playbooks_path/slurm_config.yml" || exit $?
if [[ ${@: -1} == "--INITIAL" || ${@: -1} == "--initial" || ${@: -1} == "-INITIAL" || ${@: -1} == "-initial" ]]
then
   for inventory in /opt/oci-hpc/autoscaling/clusters/*/inventory ;
   do
      if [ -f $(dirname $inventory)/currently* ]
      then
         echo "Cluster is not in running state"
      else
         ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook $playbooks_path/slurm_config_as.yml -i $inventory
      fi
   done
fi
