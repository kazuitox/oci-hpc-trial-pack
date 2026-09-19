resource "oci_core_volume_attachment" "backup_volume_attachment" { 
  count = var.controller_block && var.slurm_ha ? 1 : 0
  attachment_type = "iscsi"
  volume_id       = oci_core_volume.controller_volume[0].id
  instance_id     = oci_core_instance.backup[0].id
  display_name    = "${local.cluster_name}-backup-volume-attachment"
  device          = "/dev/oracleoci/oraclevdb"
  is_shareable    = true
} 

resource "oci_core_instance" "backup" {
  count = var.slurm_ha ? 1 : 0
  depends_on = [
    oci_core_subnet.public-subnet,
    oci_identity_policy.compute_management_autoscaling_policy,
    oci_identity_policy.autoscaling_policy,
  ]
  availability_domain = local.controller_ad
  compartment_id      = var.targetCompartment
  shape               = var.controller_shape

  dynamic "shape_config" {
    for_each = local.is_controller_flex_shape
      content {
        ocpus = shape_config.value
        memory_in_gbs = var.controller_custom_memory ? var.controller_memory : 16 * shape_config.value
      }
  }
  agent_config {
    is_management_disabled = true
    }
  display_name        = "${local.cluster_name}-backup"

  freeform_tags = {
    "cluster_name" = local.cluster_name
    "parent_cluster" = local.cluster_name
  }

  metadata = {
    ssh_authorized_keys = "${var.ssh_key}\n${tls_private_key.ssh.public_key_openssh}"
    user_data           = base64encode(data.template_file.controller_config.rendered)
  }
  source_details {
//    source_id   = var.use_standard_image ? data.oci_core_images.linux.images.0.id : local.custom_controller_image_ocid
    source_id = local.controller_image
    boot_volume_size_in_gbs = var.controller_boot_volume_size
    source_type = "image"
  }

  create_vnic_details {
    subnet_id = local.controller_subnet_id
    assign_public_ip = local.controller_bool_ip
  }
} 

resource "null_resource" "backup" { 
  count = var.slurm_ha ? 1 : 0
  depends_on = [oci_core_instance.backup] 
  triggers = merge({
    slurm_user_tags_enabled = tostring(var.slurm && var.slurm_user_tags_enabled)
    user_cost_tag_key       = local.user_cost_tag_key
    backup = oci_core_instance.backup[0].id
  }, var.slurm_job_notifications_enabled ? {
    slurm_notification_payload = local.slurm_notification_payload_hash
  } : {})

  provisioner "remote-exec" {
    inline = [
      "#!/bin/bash",
      "sudo mkdir -p /opt/oci-hpc",      
      "sudo chown ${local.controller_username}:${local.controller_username} /opt/oci-hpc/",
      "mkdir -p /opt/oci-hpc/bin",
      "mkdir -p /opt/oci-hpc/playbooks"
      ]
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }
  provisioner "file" {
    source        = "playbooks"
    destination   = "/opt/oci-hpc/"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "file" {
    source      = "autoscaling"
    destination = "/opt/oci-hpc/"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "file" {
    source      = "bin"
    destination = "/opt/oci-hpc/"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "file" {
    source      = "conf"
    destination = "/opt/oci-hpc/"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }
  provisioner "file" {
    source      = "logs"
    destination = "/opt/oci-hpc/"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }
  provisioner "file" {
    source      = "samples"
    destination = "/opt/oci-hpc/"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }
  provisioner "file" { 
    content        = templatefile("${path.module}/configure.tpl", { 
      configure = var.configure
    })
    destination   = "/tmp/configure.conf"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "file" {
    content     = tls_private_key.ssh.private_key_pem
    destination = "/home/${local.controller_username}/.ssh/cluster.key"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }


  provisioner "remote-exec" {
    inline = [
      "#!/bin/bash",
      "chmod 600 /home/${local.controller_username}/.ssh/cluster.key",
      "cp /home/${local.controller_username}/.ssh/cluster.key /home/${local.controller_username}/.ssh/id_rsa",
      "chmod a+x /opt/oci-hpc/bin/*.sh",
      "timeout --foreground 60m /opt/oci-hpc/bin/controller.sh"
      ]
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }
}
resource "null_resource" "cluster_backup" { 
  count = var.slurm_ha ? 1 : 0
  depends_on = [
    null_resource.backup,
    oci_core_compute_cluster.compute_cluster,
    oci_core_cluster_network.cluster_network,
    oci_core_instance.backup,
    oci_identity_policy.slurm_notification_controllers,
  ]
  triggers = merge({
    slurm_user_tags_enabled = tostring(var.slurm && var.slurm_user_tags_enabled)
    user_cost_tag_key       = local.user_cost_tag_key
    cluster_instances           = join(", ", local.cluster_instances_names)
    cluster_instance_ids        = join(", ", local.cluster_instances_ids)
    local_block_volume_settings = sha256(jsonencode([var.use_local_block_volume, var.local_block_volume_size, var.local_block_volume_performance, var.local_block_volume_mount_point]))
  }, var.slurm_job_notifications_enabled ? {
    slurm_notification_settings = sha256(jsonencode([
      local.slurm_notification_cluster_scope,
      local.slurm_notification_deployment_id,
      local.slurm_notification_topic_id,
      local.slurm_notification_subscription_id,
      local.slurm_notification_controller_instance_ids,
      local.slurm_notification_payload_hash,
    ]))
  } : {})

  provisioner "file" {
    content        = templatefile("${path.module}/inventory.tpl", {  
      controller_name = oci_core_instance.controller.display_name, 
      controller_public_ip = oci_core_instance.controller.public_ip,
      controller_ip = oci_core_instance.controller.private_ip,
      backup_name = var.slurm_ha ? oci_core_instance.backup[0].display_name : "",
      backup_ip = var.slurm_ha ? oci_core_instance.backup[0].private_ip: "",
      login_name = var.login_node ? oci_core_instance.login[0].display_name : "",
      login_ip = var.login_node ? oci_core_instance.login[0].private_ip: "",
      compute = var.node_count > 0 ? zipmap(local.cluster_instances_names, local.cluster_instances_ips) : zipmap([],[])
      compute_instance_ids = var.node_count > 0 ? zipmap(local.cluster_instances_names, local.cluster_instances_ids) : zipmap([],[])
      public_subnet = data.oci_core_subnet.public_subnet.cidr_block, 
      private_subnet = data.oci_core_subnet.private_subnet.cidr_block, 
      rdma_network = cidrhost(var.rdma_subnet, 0),
      rdma_netmask = cidrnetmask(var.rdma_subnet),
      zone_name = local.zone_name,
      dns_entries = var.dns_entries,
      nfs = var.node_count > 0 ? local.cluster_instances_names[0] : "",
      home_nfs = var.home_nfs,
      create_fss = var.create_fss,
      home_fss = var.home_fss,
      scratch_nfs = var.use_scratch_nfs && var.node_count > 0,
      cluster_nfs = var.use_cluster_nfs,
      cluster_nfs_path = var.cluster_nfs_path,
      scratch_nfs_path = var.scratch_nfs_path,
      use_local_block_volume = var.use_local_block_volume,
      local_block_volume_size = var.local_block_volume_size,
      local_block_volume_performance = var.local_block_volume_performance,
      local_block_volume_mount_point = var.local_block_volume_mount_point,
      add_nfs = var.add_nfs,
      nfs_target_path = var.nfs_target_path,
      nfs_source_IP = local.nfs_source_IP,
      nfs_source_path = var.nfs_source_path,
      nfs_options = var.nfs_options,
      localdisk = var.localdisk,
      log_vol = var.log_vol,
      redundancy = var.redundancy,
      cluster_network = var.cluster_network,
      use_compute_agent = var.use_compute_agent,
      slurm = var.slurm,
      slurm_user_tags_enabled = var.slurm && var.slurm_user_tags_enabled,
      slurm_job_notifications_enabled = var.slurm_job_notifications_enabled,
      slurm_notification_region = var.region,
      slurm_notification_compartment_id = var.targetCompartment,
      slurm_notification_cluster_scope = local.slurm_notification_cluster_scope,
      slurm_notification_deployment_id = local.slurm_notification_deployment_id,
      slurm_notification_local_user = local.controller_username,
      slurm_notification_admin_email = local.slurm_notification_admin_email,
      slurm_notification_topic_id = local.slurm_notification_topic_id,
      slurm_notification_subscription_id = local.slurm_notification_subscription_id,
      slurm_nfs_path = var.slurm_nfs ? var.nfs_source_path : var.cluster_nfs_path,
      rack_aware = var.rack_aware,
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
      use_ood = var.use_ood,
      ood_dcv_enabled = local.ood_dcv_enabled,
      ood_desktop_use_gpu = local.ood_desktop_gpu_enabled,
      tags = var.tags,
      controller_block = var.controller_block, 
      login_block = var.login_block, 
      scratch_nfs_type = local.scratch_nfs_type,
      controller_mount_ip = local.controller_mount_ip,
      login_mount_ip = local.login_mount_ip,
      cluster_mount_ip = local.mount_ip,
      autoscaling = var.autoscaling,
      import_compute_image_from_object_storage = local.effective_import_compute_image,
      cluster_name = local.cluster_name,
      shape = var.cluster_network ? var.cluster_network_shape : var.instance_pool_shape,
      instance_pool_ocpus = local.instance_pool_ocpus,
      queue=var.queue,
      monitoring = var.monitoring,
      hyperthreading = var.hyperthreading,
      controller_username = local.controller_username,
      compute_username = local.compute_username,
      autoscaling_monitoring = var.autoscaling_monitoring,
      autoscaling_mysql_service = var.autoscaling_mysql_service,
      monitoring_mysql_ip = var.autoscaling_monitoring && var.autoscaling_mysql_service ? oci_mysql_mysql_db_system.monitoring_mysql_db_system[0].ip_address : "localhost",
      admin_password = var.admin_password,
      admin_username = var.autoscaling_mysql_service ? var.admin_username : "root",
      ood_opc_password = random_password.ood_opc_password.result,
      enroot = var.enroot,
      pyxis = var.pyxis,
      pam = var.pam,
      sacct_limits = var.sacct_limits,
      privilege_sudo = var.privilege_sudo,
      privilege_group_name = var.privilege_group_name,
      latency_check = var.latency_check,
      inst_prin = var.inst_prin,
      region = var.region,
      tenancy_ocid = var.tenancy_ocid,
      api_fingerprint = var.api_fingerprint,
      api_user_ocid = var.api_user_ocid,
      healthchecks = var.healthchecks    
      })

    destination   = "/opt/oci-hpc/playbooks/inventory"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }


  provisioner "file" {
    content     = var.node_count > 0 ? join("\n",local.cluster_instances_ips) : "\n"
    destination = "/tmp/hosts"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "file" {
    content        = templatefile(var.inst_prin ? "${path.module}/autoscaling/provider_inst_prin.tpl" : "${path.module}/autoscaling/provider_user.tpl", {  
      api_user_ocid = var.api_user_ocid, 
      api_fingerprint = var.api_fingerprint,
      private_key_path = "/opt/oci-hpc/autoscaling/credentials/key.pem",
      tenancy_ocid = var.tenancy_ocid
      })

    destination   = "/opt/oci-hpc/autoscaling/tf_init/provider.tf"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "file" {
    content        = templatefile("${path.module}/queues.conf", {  
      cluster_network = var.cluster_network,
      use_compute_agent = var.use_compute_agent,
      compute_cluster = var.compute_cluster,
      marketplace_listing = var.marketplace_listing,
      image = local.compute_node_image_ocid,
      ood_desktop_image = local.ood_desktop_image,
      ood_desktop_shape = local.ood_desktop_shape,
      ood_desktop_instance_pool_ocpus = local.ood_desktop_instance_pool_ocpus,
      ood_dcv_enabled = local.ood_dcv_enabled,
      use_marketplace_image = local.effective_use_marketplace_image,
      boot_volume_size = var.boot_volume_size,
      use_local_block_volume = var.use_local_block_volume,
      local_block_volume_size = var.local_block_volume_size,
      local_block_volume_performance = var.local_block_volume_performance,
      local_block_volume_mount_point = var.local_block_volume_mount_point,
      shape = var.cluster_network ? var.cluster_network_shape : var.instance_pool_shape,
      region = var.region,
      ad = var.use_multiple_ads? join(" ", [var.ad, var.secondary_ad, var.third_ad]) : var.ad,
      private_subnet = var.private_subnet,
      private_subnet_id = var.private_subnet_id,
      targetCompartment = var.targetCompartment,
      instance_pool_ocpus = local.instance_pool_ocpus,
      instance_pool_memory = var.instance_pool_memory,
      instance_pool_custom_memory = var.instance_pool_custom_memory,
      use_ood = tobool(var.use_ood),
      queue=var.queue,
      hyperthreading = var.hyperthreading,
      tags = var.tags
      })

    destination   = "/opt/oci-hpc/conf/queues.conf"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }
  
  provisioner "file" {
    content        = templatefile("${path.module}/conf/variables.tpl", {  
      controller_name = oci_core_instance.controller.display_name, 
      controller_public_ip = oci_core_instance.controller.public_ip,
      controller_ip = oci_core_instance.controller.private_ip, 
      backup_name = var.slurm_ha ? oci_core_instance.backup[0].display_name : "",
      backup_ip = var.slurm_ha ? oci_core_instance.backup[0].private_ip: "",
      login_name = var.login_node ? oci_core_instance.login[0].display_name : "",
      login_ip = var.login_node ? oci_core_instance.login[0].private_ip: "",
      compute = var.node_count > 0 ? zipmap(local.cluster_instances_names, local.cluster_instances_ips) : zipmap([],[])
      public_subnet = data.oci_core_subnet.public_subnet.cidr_block,
      public_subnet_id = local.controller_subnet_id,
      private_subnet = data.oci_core_subnet.private_subnet.cidr_block, 
      private_subnet_id = local.subnet_id,
      rdma_subnet = var.rdma_subnet,
      nfs = var.node_count > 0 && var.use_scratch_nfs ? local.cluster_instances_names[0] : "",
      scratch_nfs = var.use_scratch_nfs && var.node_count > 0,
      scratch_nfs_path = var.scratch_nfs_path,
      use_scratch_nfs = var.use_scratch_nfs,
      slurm = var.slurm,
      slurm_user_tags_enabled = var.slurm && var.slurm_user_tags_enabled,
      slurm_job_notifications_enabled = var.slurm_job_notifications_enabled,
      slurm_notification_region = var.region,
      slurm_notification_compartment_id = var.targetCompartment,
      slurm_notification_cluster_scope = local.slurm_notification_cluster_scope,
      slurm_notification_deployment_id = local.slurm_notification_deployment_id,
      slurm_notification_local_user = local.controller_username,
      slurm_notification_admin_email = local.slurm_notification_admin_email,
      slurm_notification_topic_id = local.slurm_notification_topic_id,
      slurm_notification_subscription_id = local.slurm_notification_subscription_id,
      slurm_nfs_path = var.slurm_nfs ? var.nfs_source_path : var.cluster_nfs_path,
      rack_aware = var.rack_aware,
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
      use_ood = var.use_ood,
      ood_dcv_enabled = local.ood_dcv_enabled,
      ood_desktop_use_gpu = local.ood_desktop_gpu_enabled,
      tags = var.tags,
      controller_block = var.controller_block, 
      login_block = var.login_block, 
      scratch_nfs_type = local.scratch_nfs_type,
      controller_mount_ip = local.controller_mount_ip,
      login_mount_ip = local.login_mount_ip,
      cluster_mount_ip = local.mount_ip,
      scratch_nfs_type_cluster = var.scratch_nfs_type_cluster,
      scratch_nfs_type_pool = var.scratch_nfs_type_pool,
      controller_block_volume_performance = var.controller_block_volume_performance,
      region = var.region,
      tenancy_ocid = var.tenancy_ocid,
      vcn_subnet = var.vcn_subnet,
      vcn_id = local.vcn_id,
      zone_name = local.zone_name,
      dns_entries = var.dns_entries,
      cluster_block_volume_size = var.cluster_block_volume_size,
      cluster_block_volume_performance = var.cluster_block_volume_performance,
      ssh_cidr = var.ssh_cidr,
      ood_source_cidr = local.ood_source_cidr,
      use_cluster_nfs = var.use_cluster_nfs,
      cluster_nfs_path = var.cluster_nfs_path,
      home_nfs = var.home_nfs,
      create_fss = var.create_fss,
      home_fss = var.home_fss,
      add_nfs = var.add_nfs,
      nfs_target_path = var.nfs_target_path,
      nfs_source_IP = local.nfs_source_IP,
      nfs_source_path = var.nfs_source_path,
      nfs_options = var.nfs_options,
      localdisk = var.localdisk,
      log_vol = var.log_vol,
      redundancy = var.redundancy,
      monitoring = var.monitoring,
      hyperthreading = var.hyperthreading,
      unsupported = var.unsupported,
      autoscaling_monitoring = var.autoscaling_monitoring,
      enroot = var.enroot,
      pyxis = var.pyxis,
      pam = var.pam,
      sacct_limits = var.sacct_limits,
      privilege_sudo = var.privilege_sudo,
      privilege_group_name = var.privilege_group_name,
      latency_check = var.latency_check,
      private_deployment = var.private_deployment,
      controller_username = local.controller_username,
      compute_username = local.compute_username,
      ood_opc_password = random_password.ood_opc_password.result,
      use_multiple_ads = var.use_multiple_ads, 
      use_compute_agent = var.use_compute_agent,
      BIOS = var.BIOS,
      IOMMU = var.IOMMU,
      SMT = var.SMT,
      virt_instr = var.virt_instr,
      access_ctrl = var.access_ctrl,
      numa_nodes_per_socket = var.numa_nodes_per_socket,
      percentage_of_cores_enabled = var.percentage_of_cores_enabled,
      healthchecks = var.healthchecks
      })

    destination   = "/opt/oci-hpc/conf/variables.tf"
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }


  provisioner "file" {
    content     = base64decode(var.api_user_key)
    destination   = "/opt/oci-hpc/autoscaling/credentials/key.initial" 
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "remote-exec" {
    inline = [
      "#!/bin/bash",
      "chmod 755 /opt/oci-hpc/autoscaling/crontab/*.sh",
      "chmod 755 /opt/oci-hpc/autoscaling/credentials/key.sh",
      "/opt/oci-hpc/autoscaling/credentials/key.sh /opt/oci-hpc/autoscaling/credentials/key.initial /opt/oci-hpc/autoscaling/credentials/key.pem > /opt/oci-hpc/autoscaling/credentials/key.log",
      "chmod 600 /opt/oci-hpc/autoscaling/credentials/key.pem",
      "echo ${var.configure} > /tmp/configure.conf"]
    connection {
      host        = local.host_backup
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }
}


resource "oci_dns_rrset" "rrset-backup" {
  count = var.slurm_ha && var.dns_entries ? 1 : 0
  zone_name_or_id = data.oci_dns_zones.dns_zones.zones[0].id
  domain          = "${var.slurm_ha ? oci_core_instance.backup[0].display_name : ""}.${local.zone_name}"
  rtype           = "A"
  items {
    domain = "${var.slurm_ha ? oci_core_instance.backup[0].display_name : ""}.${local.zone_name}"
    rtype  = "A"
    rdata  = var.slurm_ha ? oci_core_instance.backup[0].private_ip: ""
    ttl    = 3600
  }
  scope = "PRIVATE"
  view_id = data.oci_dns_views.dns_views.views[0].id
}
