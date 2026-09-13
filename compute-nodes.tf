resource "oci_core_volume" "nfs-compute-cluster-volume" { 
  count = var.compute_cluster && var.scratch_nfs_type_cluster == "block" && var.node_count > 0 ? 1 : 0 
  availability_domain = var.ad
  compartment_id = var.targetCompartment
  display_name = "${local.cluster_name}-nfs-volume"
  
  size_in_gbs = var.cluster_block_volume_size
  vpus_per_gb = split(".", var.cluster_block_volume_performance)[0]
}

resource "oci_core_volume_attachment" "compute_cluster_volume_attachment" { 
  count = var.compute_cluster && var.scratch_nfs_type_cluster == "block" && var.node_count > 0 ? 1 : 0 
  attachment_type = "iscsi"
  volume_id       = oci_core_volume.nfs-compute-cluster-volume[0].id
  instance_id     = oci_core_instance.compute_cluster_instances[0].id
  display_name    = "${local.cluster_name}-compute-cluster-volume-attachment"
  device          = "/dev/oracleoci/oraclevdb"
} 

resource "oci_core_instance" "compute_cluster_instances" {
  count = var.compute_cluster ? var.node_count : 0
  depends_on = [
    oci_core_compute_cluster.compute_cluster,
    oci_identity_policy.compute_management_autoscaling_policy,
    oci_identity_policy.autoscaling_policy,
  ]
  availability_domain = var.ad
  compartment_id      = var.targetCompartment
  shape               = var.cluster_network_shape

  agent_config {

        are_all_plugins_disabled = false
        is_management_disabled   = true
        is_monitoring_disabled   = false

        plugins_config {
          desired_state = "DISABLED"
          name          = "OS Management Service Agent"
          }
        dynamic plugins_config {
          for_each = tobool(var.use_local_block_volume) ? [1] : []
          content {
            name          = "Block Volume Management"
            desired_state = "ENABLED"
          }
        }
        dynamic plugins_config {
          
          for_each = var.use_compute_agent ? ["ENABLED"] : ["DISABLED"]
          content {
          name = "Compute HPC RDMA Authentication"
          desired_state = plugins_config.value
           }
         }
        dynamic plugins_config {
          for_each = var.use_compute_agent ? ["ENABLED"] : ["DISABLED"]
          content {
          name = "Compute HPC RDMA Auto-Configuration"
          desired_state = plugins_config.value
          }
        }
        dynamic plugins_config {
          for_each = length(regexall(".*GPU.*", var.cluster_network_shape)) > 0 ? ["ENABLED"] : ["DISABLED"]
          content {
          name = "Compute RDMA GPU Monitoring"
          desired_state = plugins_config.value
          }
        }
      }

  display_name        = "${local.cluster_name}-node-${var.compute_cluster_start_index+count.index}"

  freeform_tags = {
    "cluster_name"                       = local.cluster_name
    "parent_cluster"                     = local.cluster_name
    "user"                               = var.tags
    "oci_hpc_local_block_volume"         = tostring(tobool(var.use_local_block_volume))
    "oci_hpc_local_block_volume_size"    = tostring(tonumber(var.local_block_volume_size))
    "oci_hpc_local_block_volume_vpus"    = tostring(tonumber(split(".", var.local_block_volume_performance)[0]))
    "oci_hpc_local_block_volume_mount"   = var.local_block_volume_mount_point
  }

  metadata = {
    ssh_authorized_keys = "${var.ssh_key}\n${tls_private_key.ssh.public_key_openssh}"
    user_data           = base64encode(data.template_file.controller_config.rendered)
  }
  source_details {
    source_id = local.cluster_network_image
    source_type             = "image"
    boot_volume_size_in_gbs = var.boot_volume_size
  }
  dynamic "launch_volume_attachments" {
    for_each = tobool(var.use_local_block_volume) ? [1] : []
    content {
      type                              = "iscsi"
      device                            = "/dev/oracleoci/oraclevdc"
      display_name                      = "${local.cluster_name}-node-${var.compute_cluster_start_index + count.index}-local-scratch-attachment"
      is_agent_auto_iscsi_login_enabled = true
      is_read_only                      = false
      is_shareable                      = false
      use_chap                          = false

      launch_create_volume_details {
        compartment_id       = var.targetCompartment
        display_name         = "${local.cluster_name}-node-${var.compute_cluster_start_index + count.index}-local-scratch"
        size_in_gbs          = tonumber(var.local_block_volume_size)
        volume_creation_type = "ATTRIBUTES"
        vpus_per_gb          = tonumber(split(".", var.local_block_volume_performance)[0])
      }
    }
  }
  preserve_data_volumes_created_at_launch = !tobool(var.use_local_block_volume)
  compute_cluster_id=length(var.compute_cluster_id) > 2 ? var.compute_cluster_id : oci_core_compute_cluster.compute_cluster[0].id
  create_vnic_details {
    subnet_id = local.subnet_id
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [
      launch_volume_attachments,
      preserve_data_volumes_created_at_launch,
      freeform_tags["user"],
      freeform_tags["oci_hpc_local_block_volume"],
      freeform_tags["oci_hpc_local_block_volume_size"],
      freeform_tags["oci_hpc_local_block_volume_vpus"],
      freeform_tags["oci_hpc_local_block_volume_mount"],
    ]
  }
} 
