
locals {
  controller_path = "${var.autoscaling_folder}/clusters/${var.cluster_name}"
}

resource "null_resource" "create_path" {
  provisioner "local-exec" {
    command = "mkdir -p ${local.controller_path}"
  }
}

resource "local_file" "hosts" {
    depends_on = [null_resource.create_path,oci_core_cluster_network.cluster_network]
    content     = join("\n", local.cluster_instances_ips)
    filename = "${local.controller_path}/hosts_${var.cluster_name}"
  }

resource "local_file" "inventory" {
  depends_on     = [oci_core_cluster_network.cluster_network]
  content        = templatefile("${local.controller_path}/inventory.tpl", {  
    controller_name = var.controller_name,
    controller_ip = var.controller_ip, 
    backup_name = var.backup_name,
    backup_ip = var.backup_ip,
    login_name = var.login_name,
    login_ip = var.login_ip,
    compute = var.node_count > 0 ? zipmap(local.cluster_instances_names, local.cluster_instances_ips) : zipmap([],[])
    compute_instance_ids = var.node_count > 0 ? zipmap(local.cluster_instances_names, local.cluster_instances_ids) : zipmap([],[])
    public_subnet = var.public_subnet, 
    private_subnet = var.private_subnet, 
    rdma_network = cidrhost(var.rdma_subnet, 0),
    rdma_netmask = cidrnetmask(var.rdma_subnet),
    zone_name = var.zone_name,
    dns_entries = var.dns_entries,
    nfs = var.use_scratch_nfs ? local.cluster_instances_names[0] : "",
    scratch_nfs = var.use_scratch_nfs,
    cluster_nfs = var.use_cluster_nfs,
    home_nfs = var.home_nfs,
    create_fss = var.create_fss,
    home_fss = var.home_fss,
    add_nfs = var.add_nfs,
    slurm_nfs_path = var.slurm_nfs_path,
    rack_aware = var.rack_aware,
    nfs_target_path = var.nfs_target_path,
    nfs_source_IP = var.nfs_source_IP,
    nfs_source_path = var.nfs_source_path,
    nfs_options = var.nfs_options,
    localdisk = var.localdisk,
    log_vol = var.log_vol,
    redundancy = var.redundancy,
    cluster_nfs_path = var.cluster_nfs_path,
    scratch_nfs_path = var.scratch_nfs_path,
    use_local_block_volume = var.use_local_block_volume,
    local_block_volume_size = var.local_block_volume_size,
    local_block_volume_performance = var.local_block_volume_performance,
    local_block_volume_mount_point = var.local_block_volume_mount_point,
    cluster_network = var.cluster_network,
    slurm = var.slurm,
    slurm_user_tags_enabled = var.slurm_user_tags_enabled,
    slurm_job_notifications_enabled = var.slurm_job_notifications_enabled,
    slurm_notification_region = var.slurm_notification_region,
    slurm_notification_compartment_id = var.slurm_notification_compartment_id,
    slurm_notification_cluster_scope = var.slurm_notification_cluster_scope,
    slurm_notification_deployment_id = var.slurm_notification_deployment_id,
    slurm_notification_local_user = var.slurm_notification_local_user,
    slurm_notification_admin_email = var.slurm_notification_admin_email,
    slurm_notification_topic_id = var.slurm_notification_topic_id,
    slurm_notification_subscription_id = var.slurm_notification_subscription_id,
    pyxis = var.pyxis,
    enroot = var.enroot,
    spack = var.spack,
    install_application = var.install_application,
    install_openfoam = var.install_openfoam,
    application = var.application,
    application_version = var.application_version,
    openfoam_version = var.openfoam_version,
    install_paraview = var.install_paraview,
    paraview_version = var.paraview_version,
    ldap = var.ldap,
    timezone = var.timezone,
    tags = var.tags,
    use_ood = var.use_ood,
    ood_dcv_enabled = var.ood_dcv_enabled,
    ood_desktop_use_gpu = var.ood_desktop_use_gpu,
    controller_block = var.controller_block,
    login_block = var.login_block,
    scratch_nfs_type = local.scratch_nfs_type,
    controller_mount_ip = var.controller_mount_ip,
    login_mount_ip = var.login_mount_ip,
    cluster_mount_ip = local.mount_ip,
    cluster_name = local.cluster_name,
    shape = var.cluster_network ? var.cluster_network_shape : var.instance_pool_shape,
    instance_pool_ocpus=local.instance_pool_ocpus,
    queue=var.queue,
    instance_type=var.instance_type,
    monitoring=var.monitoring,
    autoscaling_monitoring = var.autoscaling_monitoring,
    unsupported = var.unsupported,
    hyperthreading = var.hyperthreading,
    privilege_sudo = var.privilege_sudo,
    privilege_group_name = var.privilege_group_name,
    latency_check = var.latency_check,
    controller_username = var.controller_username,
    compute_username = var.compute_username,
    ood_opc_password = var.ood_opc_password,
    pam = var.pam,
    sacct_limits = var.sacct_limits,
    use_compute_agent=var.use_compute_agent,
    healthchecks=var.healthchecks
    })
  filename   = "${local.controller_path}/inventory"
}


resource "null_resource" "configure" {
  depends_on          = [oci_core_cluster_network.cluster_network,local_file.inventory,local_file.hosts]
  provisioner "local-exec" {
    command = "timeout 60m ${var.scripts_folder}/configure_as.sh ${local.cluster_name}"  
  }
}
