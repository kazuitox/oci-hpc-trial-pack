#!/bin/bash

date=`date -u '+%Y%m%d%H%M'`
start=`date -u +%s`
start_timestamp=`date -u +'%F %T'`
scripts=`realpath $0`
folder=`dirname $scripts`
autoscaling_folder=$folder/../autoscaling
monitoring_folder=$folder/../monitoring
logs_folder=$folder/../logs

is_autoscaling_compute_deployment()
{
  local cluster_directory=$autoscaling_folder/clusters/$1
  local variables_file=$cluster_directory/variables.tf
  local inventory_file=$cluster_directory/inventory
  [ -f "$variables_file" ] \
    && [ -f "$inventory_file" ] \
    && grep -Eq '^[[:space:]]*cluster_network[[:space:]]*=[[:space:]]*(true|false)[[:space:]]*$' "$inventory_file"
}

reconcile_compute_monitoring()
{
  local target_cluster_name=$1
  local cluster_directory=$autoscaling_folder/clusters/$target_cluster_name
  local inventory_file=$cluster_directory/inventory
  local cluster_id_file=$cluster_directory/cluster_id
  local monitoring_output
  local cluster_id
  local queue
  local shape
  local cluster_state
  local expected_size
  local header_marker
  local header_extra
  local max_index
  local existing_ocids
  local reconcile_timestamp
  local hostname
  local ip
  local ocid
  local extra
  local node_is_active
  local line_index
  local active_count
  local existing_ocid
  local normalized_hostname
  local reconcile_sql
  local -a monitoring_lines
  local -a active_ocids
  local -A seen_hostnames
  local -A seen_ocids

  if [ ! -f "$monitoring_folder/activated" ]
  then
    return 0
  fi
  if ! is_autoscaling_compute_deployment "$target_cluster_name"
  then
    return 0
  fi
  if [ ! -f "$inventory_file" ] || [ ! -f "$cluster_id_file" ]
  then
    echo "Cannot reconcile monitoring: compute inventory or cluster ID is missing" >&2
    return 1
  fi

  source "$monitoring_folder/env"
  cluster_id=`cat "$cluster_id_file"`
  queue=`awk -F= '/^[[:space:]]*queue[[:space:]]*=/{gsub(/[[:space:]]/, "", $2); print $2; exit}' "$inventory_file"`
  shape=`awk -F= '/^[[:space:]]*shape[[:space:]]*=/{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}' "$inventory_file"`
  monitoring_output=`mktemp`
  if [ -z "$monitoring_output" ]
  then
    echo "Cannot create temporary monitoring output" >&2
    return 1
  fi

  if ! cluster_state=`mysql --batch --skip-column-names -u "$ENV_MYSQL_USER" -p"$ENV_MYSQL_PASS" -e "use $ENV_MYSQL_DATABASE_NAME; select state from cluster_log.clusters where id='$cluster_id';" 2>> "$logs_folder/resize_${cluster_id}.log"`
  then
    rm -f "$monitoring_output"
    echo "Cannot read monitoring state for $target_cluster_name" >&2
    return 1
  fi
  if [ -z "$cluster_state" ] || [ "$cluster_state" == "creating" ]
  then
    rm -f "$monitoring_output"
    return 0
  fi

  if ! python3 "$folder/resize.py" --cluster_name "$target_cluster_name" --inventory "$inventory_file" list --monitoring-output > "$monitoring_output" 2>> "$logs_folder/resize_${cluster_id}.log"
  then
    rm -f "$monitoring_output"
    echo "Cannot obtain a complete compute member list; monitoring was not changed" >&2
    return 1
  fi
  mapfile -t monitoring_lines < "$monitoring_output"
  rm -f "$monitoring_output"
  if [ ${#monitoring_lines[@]} -eq 0 ]
  then
    echo "Validated compute member list is empty" >&2
    return 1
  fi
  read -r header_marker expected_size header_extra <<< "${monitoring_lines[0]}"
  if [ "$header_marker" != "EXPECTED_SIZE" ] || ! [[ "$expected_size" =~ ^[0-9]+$ ]] || [ -n "$header_extra" ]
  then
    echo "Validated compute member list has an invalid header" >&2
    return 1
  fi

  active_ocids=()
  active_count=0
  for (( line_index=1; line_index<${#monitoring_lines[@]}; line_index++ )); do
    hostname=
    ip=
    ocid=
    extra=
    read -r hostname ip ocid extra <<< "${monitoring_lines[$line_index]}"
    if ! [[ "$hostname" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]] \
      || ! [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
      || ! [[ "$ocid" =~ ^ocid1\.instance\.[A-Za-z0-9.-]+$ ]] \
      || [ -n "$extra" ]
    then
      echo "Validated compute member list contains a malformed row" >&2
      return 1
    fi
    normalized_hostname=${hostname,,}
    if [ -n "${seen_hostnames[$normalized_hostname]+present}" ] || [ -n "${seen_ocids[$ocid]+present}" ]
    then
      echo "Validated compute member list contains duplicate names or OCIDs" >&2
      return 1
    fi
    seen_hostnames[$normalized_hostname]=1
    seen_ocids[$ocid]=1
    active_ocids+=( "$ocid" )
    active_count=$((active_count+1))
  done
  if [ "$active_count" -ne "$expected_size" ]
  then
    echo "Validated compute member list does not match its expected size; monitoring was not changed" >&2
    return 1
  fi

  if ! existing_ocids=`mysql --batch --skip-column-names -u "$ENV_MYSQL_USER" -p"$ENV_MYSQL_PASS" -e "use $ENV_MYSQL_DATABASE_NAME; select node_OCID from nodes WHERE cluster_id='$cluster_id' and state <> 'deleted' and node_OCID is not null;" 2>> "$logs_folder/resize_${cluster_id}.log"`
  then
    echo "Cannot read existing monitoring members for $target_cluster_name" >&2
    return 1
  fi
  if ! max_index=`mysql --batch --skip-column-names -u "$ENV_MYSQL_USER" -p"$ENV_MYSQL_PASS" -e "use $ENV_MYSQL_DATABASE_NAME; select max(cluster_index) from nodes WHERE cluster_id='$cluster_id';" 2>> "$logs_folder/resize_${cluster_id}.log"`
  then
    echo "Cannot read monitoring node indexes for $target_cluster_name" >&2
    return 1
  fi
  if ! [[ "$max_index" =~ ^[0-9]+$ ]]
  then
    max_index=0
  fi
  reconcile_timestamp=`date -u +'%F %T'`
  reconcile_sql="START TRANSACTION;"

  # Retire absent rows and release every active row's hostname first.  Doing
  # this in the same transaction as the assignments supports replacement and
  # hostname swaps despite the global UNIQUE(hostname) constraint.
  for existing_ocid in $existing_ocids; do
    node_is_active=0
    for ocid in "${active_ocids[@]}"; do
      if [ "$ocid" = "$existing_ocid" ]
      then
        node_is_active=1
        break
      fi
    done
    if [ $node_is_active -eq 0 ]
    then
      reconcile_sql+=" UPDATE cluster_log.nodes SET hostname=NULL,started_deletion='$start_timestamp',deleted='$reconcile_timestamp',state='deleted' WHERE cluster_id='$cluster_id' AND node_OCID='$existing_ocid';"
    fi
  done
  for ocid in "${active_ocids[@]}"; do
    reconcile_sql+=" UPDATE cluster_log.nodes SET hostname=NULL WHERE node_OCID='$ocid';"
  done

  for (( line_index=1; line_index<${#monitoring_lines[@]}; line_index++ )); do
    read -r hostname ip ocid extra <<< "${monitoring_lines[$line_index]}"
    max_index=$((max_index+1))
    reconcile_sql+="
      UPDATE cluster_log.nodes SET hostname=NULL WHERE hostname='${hostname}' AND state='deleted' AND (node_OCID IS NULL OR node_OCID <> '${ocid}');
      SET @oci_hpc_node_exists = (SELECT COUNT(*) FROM cluster_log.nodes WHERE node_OCID='${ocid}');
      UPDATE cluster_log.nodes SET cluster_id='$cluster_id',hostname='${hostname}',ip='${ip}',state='running',started_deletion=NULL,deleted=NULL WHERE node_OCID='${ocid}';
      UPDATE cluster_log.nodes SET created=COALESCE(created,'$reconcile_timestamp'),hostname='${hostname}',ip='${ip}',node_OCID='${ocid}',state='running',started_deletion=NULL,deleted=NULL
      WHERE cluster_id='$cluster_id' AND node_OCID IS NULL AND state='provisioning' AND hostname='${hostname}' AND @oci_hpc_node_exists=0 ORDER BY cluster_index LIMIT 1;
      SET @oci_hpc_node_exists = (SELECT COUNT(*) FROM cluster_log.nodes WHERE node_OCID='${ocid}');
      UPDATE cluster_log.nodes SET created=COALESCE(created,'$reconcile_timestamp'),hostname='${hostname}',ip='${ip}',node_OCID='${ocid}',state='running',started_deletion=NULL,deleted=NULL
      WHERE cluster_id='$cluster_id' AND node_OCID IS NULL AND state='provisioning' AND @oci_hpc_node_exists=0 ORDER BY cluster_index LIMIT 1;
      SET @oci_hpc_node_exists = (SELECT COUNT(*) FROM cluster_log.nodes WHERE node_OCID='${ocid}');
      INSERT INTO cluster_log.nodes (cluster_id,cluster_index,cpus,created,state,class_name,shape,hostname,ip,node_OCID)
      SELECT '$cluster_id',$max_index,36,'$reconcile_timestamp','running','$queue','$shape','${hostname}','${ip}','${ocid}'
      WHERE @oci_hpc_node_exists=0;"
  done
  reconcile_sql+=" UPDATE cluster_log.clusters SET nodes=$expected_size,state='running',resize_log='$logs_folder/resize_${cluster_id}.log' WHERE id='$cluster_id'; COMMIT;"
  if ! mysql -u "$ENV_MYSQL_USER" -p"$ENV_MYSQL_PASS" -e "use $ENV_MYSQL_DATABASE_NAME; $reconcile_sql" >> "$logs_folder/resize_${cluster_id}.log" 2>&1
  then
    echo "Cannot atomically reconcile monitoring for $target_cluster_name" >&2
    return 1
  fi
}

if [ $EUID -eq 0 ]
then
  echo "Run this script as opc or ubuntu and not as root"
  exit
fi

if [ $USER != "ubuntu" ] && [ $USER != "opc" ]
then
  echo "Run this script as opc or ubuntu"
  exit
fi

if [ $# -eq 0 ]
then
  python3 $folder/resize.py --help
  exit
fi

resize_type=default
reconcile_monitoring_only=0
permanent=1
controllerName=`hostname`
cluster_name=${controllerName/-controller/}
nodes=NULL
quietMode=False
for (( i=1; i<=$#; i++)); do
    if [ ${!i} == "--cluster_name" ]
    then
        j=$((i+1))
        if [ $cluster_name != ${!j} ]
        then
          permanent=0
        fi
        cluster_name=${!j}
    elif [ ${!i} == "add" ]
    then
      resize_type=add
    elif [ ${!i} == "remove" ]
    then
      resize_type=remove
    elif [ ${!i} == "remove_unreachable" ]
    then
      resize_type=remove_unreachable
    elif [ ${!i} == "reconfigure" ] || [ ${!i} == "sync_instance_pool_names" ]
    then
      resize_type=reconcile
    elif [ ${!i} == "--reconcile-monitoring" ]
    then
      resize_type=reconcile
      reconcile_monitoring_only=1
    elif [ ${!i} == "--nodes" ]
    then
      j=$((i+1))
      nodes=${@:j}
    elif [ ${!i} == "--quiet" ]
    then
      quietMode=True
    fi
done

if { [ "$resize_type" = "remove" ] || [ "$resize_type" = "remove_unreachable" ]; } \
  && [ "$quietMode" = "False" ]
then
  echo "$(cat $folder/remove_nodes_prompt.txt)"
  echo "Do you confirm you have done all of the above steps and wish to proceed for the termination of the nodes? Enter 1 for Yes and 2 for No (to exit)."
  select yn in "Yes" "No"; do
    case $yn in
        Yes ) break;;
        No ) exit;;
    esac
  done
fi

if [ $resize_type != "default" ]
then
  if [ $permanent -eq 0 ]
  then
    cd $autoscaling_folder/clusters/$cluster_name
    cluster_id=`cat cluster_id`
    shape=`cat inventory | grep shape= | awk -F  "=" '{print $2}'`
    queue=`cat inventory | grep queue= | awk -F  "=" '{print $2}'`
    log=$logs_folder/resize_${cluster_id}.log
    if [ $reconcile_monitoring_only -eq 1 ]
    then
      reconcile_compute_monitoring "$cluster_name"
      exit $?
    fi
    if [ "$resize_type" == "reconcile" ] && ! is_autoscaling_compute_deployment "$cluster_name"
    then
      python3 $folder/resize.py ${@} &
      exit 0
    fi
    echo $date >> ${log} 2>&1
    if [ -f "currently_resizing" ] && [[ $2 != FORCE ]]
    then
      echo "The cluster is already being resized"
      exit
    else
      echo $cluster_name >> currently_resizing
      echo `date -u '+%Y%m%d%H%M'` >> $log 2>&1
    fi
  else
    cluster_id=$cluster_name
    shape=`cat /etc/ansible/hosts | grep shape= | awk -F  "=" '{print $2}'`
    queue=`cat /etc/ansible/hosts | grep queue= | awk -F  "=" '{print $2}'`
    log=$logs_folder/resize_${cluster_id}.log
    if [ "$resize_type" == "reconcile" ]
    then
      python3 $folder/resize.py ${@} &
      exit 0
    fi
  fi

  if [ $reconcile_monitoring_only -eq 1 ]
  then
    exit 0
  fi

  if [ -f $monitoring_folder/activated ]
  then
    source $monitoring_folder/env
    mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; UPDATE cluster_log.clusters SET started_resize='$start_timestamp',state='resizing' WHERE id='$cluster_id'" >> $log 2>&1
  fi

  python3 $folder/resize.py ${@} | tee -a $log 2>&1 | grep STDOUT
  status=${PIPESTATUS[0]}
  end=`date -u +%s`
  end_timestamp=`date -u +'%F %T'`
  runtime=$((end-start))

  if [ $status -eq 0 ]
  then
    echo "Successfully Resized cluster $cluster_name in $runtime seconds"
    if [ -f $monitoring_folder/activated ]
    then
      if [ $permanent -eq 0 ] && is_autoscaling_compute_deployment "$cluster_name"
      then
        if ! reconcile_compute_monitoring "$cluster_name"
        then
          echo "The cluster resize succeeded, but monitoring reconciliation failed. Retry only: $folder/resize.sh --cluster_name $cluster_name --reconcile-monitoring"
          mysql -u "$ENV_MYSQL_USER" -p"$ENV_MYSQL_PASS" -e "use $ENV_MYSQL_DATABASE_NAME; UPDATE cluster_log.clusters SET state='running' WHERE id='$cluster_id'" >> "$log" 2>&1
        fi
      else
        nodes_list=`python3 $folder/resize.py --cluster_name $cluster_name list | grep ocid1.instance`

        length=`echo $nodes_list | wc -w`
        newSize=$((length/3))
        existing_nodes=`mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; select hostname from nodes WHERE cluster_id='$cluster_id' and state <> 'deleted';" 2>&1 | grep inst`
        max_index=`mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; select max(cluster_index) from nodes WHERE cluster_id='$cluster_id';" 2>&1 | tail -n 1`
        mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; UPDATE cluster_log.clusters SET nodes=$newSize,state='running',resize_log='$logs_folder/resize_${cluster_id}.log' WHERE id='$cluster_id'" >> $log 2>&1
        if [ $resize_type == "remove" ] || [ $resize_type == "remove_unreachable" ]
        then
          if [ "$nodes" == "NULL" ] || [ $resize_type == "remove_unreachable" ]
          then
            for node in $existing_nodes; do
              if [ `echo $nodes_list | grep $node | wc -l` == 0 ]
              then
                echo $node Deleted
                mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; UPDATE cluster_log.nodes SET started_deletion='$start_timestamp',deleted='$end_timestamp',state='deleted' WHERE cluster_id='$cluster_id' AND hostname='$node'" >> $log 2>&1
              fi
            done
          else
            for node in $nodes; do
              echo $node Deleted
              mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; UPDATE cluster_log.nodes SET started_deletion='$start_timestamp',deleted='$end_timestamp',state='deleted' WHERE cluster_id='$cluster_id' AND hostname='$node'" >> $log 2>&1
            done
          fi
        else
          for node in ${nodes_list}; do
              nl_array+=( $node )
          done
          length=`echo $nodes_list | wc -w`
          for (( c=0; c<=$((length-1)); c=c+3 )); do
            max_index=$((max_index+1))
            ip=`echo ${nl_array[$c+1]}`
            hostname=`echo ${nl_array[$((c))]}`
            ocid=`echo ${nl_array[$((c+2))]}`
            if [ `echo $existing_nodes | grep $hostname | wc -l` == 0 ]
            then
              max_index=$((max_index+1))
              mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; INSERT IGNORE INTO cluster_log.nodes (cluster_id,cluster_index,cpus,created,state,class_name,shape,hostname,ip,node_OCID) VALUES ('$cluster_name',$max_index,36,'$end_timestamp','running','$queue','$shape','${hostname}','${ip}','${ocid}');"  >> $log 2>&1
            fi
          done
        fi
      fi
    fi
  else
    echo "Could not resize cluster $cluster_name in 5 tries (Time: $runtime seconds)"
    if [ -f $monitoring_folder/activated ]
    then
      if [ $permanent -eq 0 ] && is_autoscaling_compute_deployment "$cluster_name"
      then
        reconcile_compute_monitoring "$cluster_name" || echo "Monitoring reconciliation also failed" >> "$log"
      fi
      mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; INSERT INTO cluster_log.errors_timeserie (cluster_id,state,error_log,error_type,created_on_m) VALUES ('$cluster_id','resize','$logs_folder/resize_${cluster_id}.log','`tail $log | grep Error`','$end_timestamp');" >> $log 2>&1
      mysql -u $ENV_MYSQL_USER -p$ENV_MYSQL_PASS -e "use $ENV_MYSQL_DATABASE_NAME; UPDATE cluster_log.clusters SET started_resizing=NULL,state='running' WHERE id='$cluster_id'" >> $log 2>&1
    fi
  fi
  if [ $permanent -eq 0 ]
  then
    rm currently_resizing
  fi
  exit $status
else
  python3 $folder/resize.py ${@} &
fi
