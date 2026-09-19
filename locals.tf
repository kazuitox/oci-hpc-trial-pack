locals {
  simple_compute_images = {
    OracleLinux8 = {
      source_uri               = "https://objectstorage.ap-tokyo-1.oraclecloud.com/p/AqQaLtfNavruHeb0f0DEDzhIG1WONrMWHAxAYtyDROmT-Z7mx8xxebIwJedxTeSh/n/idqcucnenh88/b/kazuito-bucket/o/Oracle-Linux-8.10-2025.07.21-0-OCA-RHCK-OFED-24.10-3.2.5.0-2025.08.21-0_oci-hpc-v2.10-compute-image-v1.3"
      display_name             = "Oracle-Linux-8.10-2025.07.21-0-OCA-RHCK-OFED-24.10-3.2.5.0-2025.08.21-0_oci-hpc-v2.10-compute-image-v1.3"
      operating_system         = "Oracle Linux"
      operating_system_version = "8.10"
      username                 = "opc"
    }
    "Ubuntu24.04" = {
      source_uri               = "https://objectstorage.ap-tokyo-1.oraclecloud.com/p/vPqvrTncSwkqWKvL7SuKYGucWCzzr3TiO7LnV7zQKjGYginyf80Ntb07SFYJxUrk/n/idqcucnenh88/b/kazuito-bucket/o/Canonical-Ubuntu-24.04-2026.02.28-0-6.8-DOCA-OFED-3.2.1-2026.05.05-0-image-v1.3"
      display_name             = "Canonical-Ubuntu-24.04-2026.02.28-0-6.8-DOCA-OFED-3.2.1-2026.05.05-0-image-v1.3"
      operating_system         = "Ubuntu"
      operating_system_version = "24.04"
      username                 = "ubuntu"
    }
  }
  simple_gpgpu_compute_image = {
    source_uri               = "https://objectstorage.ap-tokyo-1.oraclecloud.com/p/BlMCEroPMUpyFLRofeJNM1-4It0Cn_HxBJeLI7elkF8GGOozRzMmaUPmHv-eDIHK/n/idqcucnenh88/b/kazuito-bucket/o/Canonical-Ubuntu-24.04-2026.02.28-0-6.8-DOCA-OFED-3.2.1-2026.05.05-0-gpu-image-v1.0"
    display_name             = "Canonical-Ubuntu-24.04-2026.02.28-0-6.8-DOCA-OFED-3.2.1-2026.05.05-0-gpu-image-v1.0"
    operating_system         = "Ubuntu"
    operating_system_version = "24.04"
  }
  simple_mode                         = var.ui_mode == "SIMPLE"
  cluster_os_key                      = contains(["Ubuntu 24.04", "Ubuntu24.04"], var.cluster_os) ? "Ubuntu24.04" : "OracleLinux8"
  simple_compute_image                = local.simple_compute_images[local.cluster_os_key]
  simple_preinstalled_compute_image   = local.cluster_os_key == "Ubuntu24.04" ? true : var.use_preinstalled_compute_image
  effective_use_marketplace_image     = local.simple_mode ? !local.simple_preinstalled_compute_image : var.use_marketplace_image
  effective_import_compute_image      = local.simple_mode ? local.simple_preinstalled_compute_image : var.import_compute_image_from_object_storage
  use_imported_compute_image           = local.effective_import_compute_image && !local.effective_use_marketplace_image
  use_imported_compute_image_for_controller = local.simple_mode && local.use_imported_compute_image
  import_gpgpu_compute_image          = local.simple_mode && local.cluster_os_key == "Ubuntu24.04" && var.import_gpcpu_image_from_object_storage
  effective_use_marketplace_image_controller = local.use_imported_compute_image_for_controller ? false : var.use_marketplace_image_controller
  compute_image_source_uri            = local.simple_mode && local.simple_preinstalled_compute_image ? local.simple_compute_image.source_uri : var.compute_image_source_uri
  compute_image_display_name          = local.simple_mode && local.simple_preinstalled_compute_image ? local.simple_compute_image.display_name : var.compute_image_display_name
  compute_image_operating_system      = local.simple_mode && local.simple_preinstalled_compute_image ? local.simple_compute_image.operating_system : var.compute_image_operating_system
  compute_image_operating_system_version = local.simple_mode && local.simple_preinstalled_compute_image ? local.simple_compute_image.operating_system_version : var.compute_image_operating_system_version
  compute_username                    = local.simple_mode && local.simple_preinstalled_compute_image ? local.simple_compute_image.username : var.compute_username
  controller_username                 = local.use_imported_compute_image_for_controller ? local.compute_username : var.controller_username

  # Defined cost tags are always created and managed through the home region.
  region_map = {
    for region in data.oci_identity_regions.regions.regions :
    region.key => region.name
  }
  home_region = local.region_map[data.oci_identity_tenancy.tenancy.home_region_key]

// display names of instances 
  cluster_instances_ids = var.compute_cluster ? oci_core_instance.compute_cluster_instances.*.id : var.cluster_network ? data.oci_core_instance.cluster_network_instances.*.id : data.oci_core_instance.instance_pool_instances.*.id
  cluster_instances_names = var.compute_cluster ? oci_core_instance.compute_cluster_instances.*.display_name : var.cluster_network ? data.oci_core_instance.cluster_network_instances.*.display_name : data.oci_core_instance.instance_pool_instances.*.display_name

  // Source the image OCID from all compatibility entries so deployments wait for their registration.
  imported_compute_image_ocid = one(toset(concat(
    oci_core_image.compute_node_custom_image[*].id,
    [for compatibility in oci_core_shape_management.compute_node_custom_image_compatible_shapes : compatibility.image_id],
  )))
  image_ocid = local.use_imported_compute_image ? local.imported_compute_image_ocid : (
    var.unsupported ? var.image_ocid : var.image
  )
  custom_controller_image_ocid = var.unsupported_controller ? var.unsupported_controller_image : var.custom_controller_image
  custom_login_image_ocid = var.unsupported_login ? var.unsupported_login_image : var.custom_login_image
  controller_ad = var.controller_ad != "" ? var.controller_ad : var.ad
  fss_compartment = var.fss_compartment != "" ? var.fss_compartment : var.targetCompartment
  fss_ad = var.fss_ad != "" ? var.fss_ad : var.ad
  login_ad = var.login_ad != "" ? var.login_ad : var.ad

  shape = var.cluster_network ? var.cluster_network_shape : var.instance_pool_shape
  is_gpu_compute_shape = length(regexall(".*GPU.*", local.shape)) > 0
  instance_pool_flex_ocpus_by_shape = {
    "VM.Standard.E3.Flex"    = var.instance_pool_ocpus_64
    "VM.Standard.E4.Flex"    = var.instance_pool_ocpus_64
    "VM.Standard.E5.Flex"    = var.instance_pool_ocpus
    "VM.Standard.E6.Flex"    = var.instance_pool_ocpus
    "VM.Standard.E6.Ax.Flex" = var.instance_pool_ocpus_94
    "VM.Standard.A1.Flex"    = var.instance_pool_ocpus_76
    "VM.Standard3.Flex"      = var.instance_pool_ocpus_32
    "VM.Standard4.Ax.Flex"   = var.instance_pool_ocpus_39
    "VM.Optimized3.Flex"     = var.instance_pool_ocpus_18
    "VM.DenseIO.E4.Flex"     = var.instance_pool_ocpus_denseIO_flex
    "VM.DenseIO.E5.Flex"     = var.instance_pool_ocpus_denseIO_e5_flex
  }
  instance_pool_ocpus = lookup(local.instance_pool_flex_ocpus_by_shape, local.shape, var.instance_pool_ocpus)
  controller_ocpus = ( var.controller_shape == "VM.DenseIO.E4.Flex" || var.controller_shape == "VM.DenseIO.E5.Flex" ) ? var.controller_ocpus_denseIO_flex : var.controller_ocpus
  login_ocpus = ( var.login_shape == "VM.DenseIO.E4.Flex" || var.login_shape == "VM.DenseIO.E5.Flex" ) ? var.login_ocpus_denseIO_flex : var.login_ocpus
// ips of the instances
  cluster_instances_ips = var.compute_cluster ? oci_core_instance.compute_cluster_instances.*.private_ip : var.cluster_network ? data.oci_core_instance.cluster_network_instances.*.private_ip : data.oci_core_instance.instance_pool_instances.*.private_ip
  first_vcn_ip = cidrhost(data.oci_core_subnet.private_subnet.cidr_block,0)
  cluster_instances_ips_index = [for ip in local.cluster_instances_ips : tostring((tonumber(split(".",ip)[3])-tonumber(split(".",local.first_vcn_ip)[3]))+256*(tonumber(split(".",ip)[2])-tonumber(split(".",local.first_vcn_ip)[2]))+1)]

// vcn id derived either from created vcn or existing if specified
  vcn_id = var.use_existing_vcn ? var.vcn_id : element(concat(oci_core_vcn.vcn.*.id, [""]), 0)
  ood_source_cidr = trimspace(var.ood_source_cidr) != "" ? trimspace(var.ood_source_cidr) : var.ssh_cidr
  ood_enabled = tobool(var.use_ood)
  ood_dcv_enabled = local.ood_enabled && (var.ood_dcv_enabled || var.ood_vnc_use_gpu)
  ood_desktop_gpu_enabled = local.ood_enabled && (var.ood_desktop_use_gpu || var.ood_vnc_use_gpu)

// subnet id derived either from created subnet or existing if specified
//  subnet_id = var.use_existing_vcn ? var.private_subnet_id : element(concat(oci_core_subnet.private-subnet.*.id, [""]), 0)
  subnet_id = var.private_deployment ? var.use_existing_vcn ? var.private_subnet_id : element(concat(oci_core_subnet.private-subnet.*.id, [""]), 1) : var.use_existing_vcn ? var.private_subnet_id : element(concat(oci_core_subnet.private-subnet.*.id, [""]), 0)

  fss_mount_target_count = var.create_fss ? (var.dns_entries ? var.mount_target_count : 1) : 0
  nfs_source_IP = var.create_fss ? (var.dns_entries ? oci_dns_rrset.fss-dns-round-robin[0].domain : oci_file_storage_mount_target.FSSMountTarget[0].ip_address) : var.nfs_source_IP
  nfs_list_of_mount_target_IPs = var.create_fss ? "[\"${join("\",\"",oci_file_storage_mount_target.FSSMountTarget.*.ip_address)}\"]" : var.nfs_source_IP
// subnet id derived either from created subnet or existing if specified
// controller_subnet_id = var.use_existing_vcn ? var.public_subnet_id : element(concat(oci_core_subnet.public-subnet.*.id, [""]), 0)
  controller_subnet_id = var.private_deployment ? var.use_existing_vcn ? var.public_subnet_id : element(concat(oci_core_subnet.private-subnet.*.id, [""]), 0) : var.use_existing_vcn ? var.public_subnet_id : element(concat(oci_core_subnet.public-subnet.*.id, [""]), 0)
  
  cluster_name = var.use_custom_name ? var.cluster_name : random_pet.name.id

  slurm_notification_cluster_scope   = "${substr(local.cluster_name, 0, 128)}:${random_pet.name.id}:${substr(md5(var.targetCompartment), 0, 12)}"
  slurm_notification_deployment_id   = substr(sha256(tls_private_key.ssh.public_key_openssh), 0, 32)
  slurm_notification_identity_suffix = substr(md5(local.slurm_notification_cluster_scope), 0, 12)
  slurm_notification_admin_email = var.slurm_job_notifications_enabled ? regex(
    "^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,63}$",
    trimspace(var.slurm_notification_admin_email),
  ) : trimspace(var.slurm_notification_admin_email)
  slurm_notification_payload_hash = sha256(join("", [
    filesha256("${path.module}/playbooks/roles/cluster-cli/files/cluster"),
    filesha256("${path.module}/playbooks/roles/openondemand/templates/opencomposer_slurm_form.yml.j2"),
    filesha256("${path.module}/playbooks/roles/slurm/defaults/main.yml"),
    filesha256("${path.module}/playbooks/roles/slurm/handlers/main.yml"),
    filesha256("${path.module}/playbooks/roles/slurm/tasks/server.yml"),
    filesha256("${path.module}/playbooks/roles/slurm/tasks/backup_server.yml"),
    filesha256("${path.module}/playbooks/roles/slurm/tasks/notifications.yml"),
    filesha256("${path.module}/playbooks/roles/slurm/files/slurm_notification_registry_seed.py"),
    filesha256("${path.module}/playbooks/roles/slurm/files/slurm_oci_notification_cleanup.py"),
    filesha256("${path.module}/playbooks/roles/slurm/files/slurm_oci_mailprog.py"),
    filesha256("${path.module}/playbooks/roles/slurm/files/slurm_oci_notify_worker.py"),
    filesha256("${path.module}/playbooks/roles/slurm/templates/slurm.conf.j2"),
    filesha256("${path.module}/playbooks/roles/slurm/templates/slurm_notification_bootstrap.json.j2"),
    filesha256("${path.module}/playbooks/roles/slurm/templates/slurm-oci-notify-tmpfiles.conf.j2"),
    filesha256("${path.module}/playbooks/roles/slurm/templates/systemd/slurm-oci-notify.path.j2"),
    filesha256("${path.module}/playbooks/roles/slurm/templates/systemd/slurm-oci-notify.service.j2"),
    filesha256("${path.module}/playbooks/roles/slurm/templates/systemd/slurm-oci-notify.timer.j2"),
  ]))
  slurm_notification_topic_id        = var.slurm_job_notifications_enabled ? (
    oci_ons_notification_topic.slurm_local_user[0].id
  ) : ""
  slurm_notification_subscription_id = var.slurm_job_notifications_enabled ? (
    oci_ons_subscription.slurm_local_user_email[0].id
  ) : ""
  slurm_notification_destroy_cleanup_config_base64 = base64encode(jsonencode({
    region              = var.region
    compartment_id      = var.targetCompartment
    cluster_name        = local.cluster_name
    cluster_scope_id    = local.slurm_notification_cluster_scope
    deployment_id       = local.slurm_notification_deployment_id
    registry_path       = "/opt/oci-hpc/conf/slurm_notification_users.json"
    protected_topic_ids = var.slurm_job_notifications_enabled ? [local.slurm_notification_topic_id] : []
  }))
  slurm_notification_local_topic_suffix = substr(
    sha256("${local.slurm_notification_cluster_scope}:${local.controller_username}"),
    0,
    12,
  )
  slurm_notification_controller_instance_ids = concat(
    [oci_core_instance.controller.id],
    oci_core_instance.backup[*].id,
  )
  slurm_notification_controller_matching_rule = length(local.slurm_notification_controller_instance_ids) == 1 ? "instance.id = '${local.slurm_notification_controller_instance_ids[0]}'" : format(
    "Any {%s}",
    join(", ", [for instance_id in local.slurm_notification_controller_instance_ids : "instance.id = '${instance_id}'"]),
  )

  controller_image = local.use_imported_compute_image_for_controller ? local.image_ocid : (
    local.effective_use_marketplace_image_controller ? oci_core_app_catalog_subscription.controller_mp_image_subscription[0].listing_resource_id : local.custom_controller_image_ocid
  )

  login_image = var.login_node &&  var.use_marketplace_image_login ? oci_core_app_catalog_subscription.login_mp_image_subscription[0].listing_resource_id : local.custom_login_image_ocid

  compute_node_image_ocid = local.import_gpgpu_compute_image && local.is_gpu_compute_shape ? oci_core_image.compute_node_gpgpu_custom_image[0].id : local.image_ocid

  cluster_network_image = local.effective_use_marketplace_image ? oci_core_app_catalog_subscription.mp_image_subscription[0].listing_resource_id : local.compute_node_image_ocid

  instance_pool_image = ! var.cluster_network && local.effective_use_marketplace_image ? oci_core_app_catalog_subscription.mp_image_subscription[0].listing_resource_id : local.compute_node_image_ocid

  ood_desktop_image = local.ood_desktop_gpu_enabled ? oci_core_image.ood_vnc_gpu_custom_image[0].id : local.image_ocid
  ood_desktop_shape = local.ood_desktop_gpu_enabled ? "VM.GPU.A10.1" : "VM.Standard.E6.Flex"
  ood_desktop_instance_pool_ocpus = local.ood_desktop_gpu_enabled ? 8 : 2

//  image = (var.cluster_network && var.use_marketplace_image == true) || (var.cluster_network == false && var.use_marketplace_image == false) ? var.image : data.oci_core_images.linux.images.0.id

  is_controller_flex_shape = length(regexall(".*VM.*.*Flex$", var.controller_shape)) > 0 ? [local.controller_ocpus]:[]
  is_login_flex_shape = length(regexall(".*VM.*.*Flex$", var.login_shape)) > 0 ? [local.login_ocpus]:[]

  is_instance_pool_flex_shape = length(regexall(".*VM.*.*Flex$", var.instance_pool_shape)) > 0 ? [local.instance_pool_ocpus]:[]
  
  controller_mount_ip = var.controller_block ? element(concat(oci_core_volume_attachment.controller_volume_attachment.*.ipv4, [""]), 0) : "none"
  login_mount_ip = var.login_block ? element(concat(oci_core_volume_attachment.login_volume_attachment.*.ipv4, [""]), 0) : "none"

  scratch_nfs_type = var.cluster_network ? var.scratch_nfs_type_cluster : var.scratch_nfs_type_pool 

  iscsi_ip = var.cluster_network ? element(concat(oci_core_volume_attachment.cluster_network_volume_attachment.*.ipv4, [""]), 0) : element(concat(oci_core_volume_attachment.instance_pool_volume_attachment.*.ipv4, [""]), 0)

  mount_ip = local.scratch_nfs_type == "block" ? local.iscsi_ip : "none" 

// Cluster OCID


  cluster_ocid = var.node_count > 0 ? var.compute_cluster ? oci_core_compute_cluster.compute_cluster[0].id : var.cluster_network ? oci_core_cluster_network.cluster_network[0].id : oci_core_instance_pool.instance_pool[0].id : ""
  host = var.private_deployment ? data.oci_resourcemanager_private_endpoint_reachable_ip.private_endpoint_reachable_ip[0].ip_address : oci_core_instance.controller.public_ip
  controller_bool_ip = var.private_deployment ? false : true
  login_bool_ip = var.private_deployment ? false : true
  controller_subnet = var.private_deployment ? oci_core_subnet.private-subnet : oci_core_subnet.public-subnet
  private_subnet_cidr = var.private_deployment ? [var.public_subnet, var.private_subnet] : [var.private_subnet]
  host_backup = var.slurm_ha ? var.private_deployment ? data.oci_resourcemanager_private_endpoint_reachable_ip.private_endpoint_reachable_ip_backup[0].ip_address : oci_core_instance.backup[0].public_ip : "none"
  host_login = var.login_node ? var.private_deployment ? data.oci_resourcemanager_private_endpoint_reachable_ip.private_endpoint_reachable_ip_login[0].ip_address : oci_core_instance.login[0].public_ip : "none"

  timeout_per_batch= var.cluster_network ? 30 : 15
  timeout_ip = join("",[ (( var.node_count - ( var.node_count % 20 ) )/20 + 1 ) * local.timeout_per_batch,"m"])

  zone_name = var.use_existing_vcn ? var.zone_name : "${local.cluster_name}.local"
  platform_type = local.shape == "BM.GPU4.8" ? "AMD_ROME_BM_GPU" : local.shape == "BM.GPU.B4.8" || local.shape == "BM.GPU.H100.8" || local.shape == "BM.GPU.A100-v2.8" ? "AMD_MILAN_BM_GPU" : local.shape == "BM.Standard.E3.128" ? "AMD_ROME_BM" :  local.shape == "BM.Standard.E4.128" || local.shape == "BM.DenseIO.E4.128" ? "AMD_MILAN_BM" : "GENERIC_BM" 
}
