# Independent deployment: enabling monitoring must not rebuild compute nodes.
resource "oci_identity_policy" "autoscaling_instance_monitor" {
  count          = var.autoscaling_failure_notifications_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "autoscaling-instance-monitor-${local.slurm_notification_identity_suffix}"
  description    = "Read-only instance reconciliation on the controller."
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.autoscaling_failure_controllers[0].name} to read instances in compartment id ${var.targetCompartment}",
    "Allow dynamic-group ${oci_identity_dynamic_group.autoscaling_failure_controllers[0].name} to read virtual-network-family in compartment id ${var.targetCompartment}",
  ]
}

resource "null_resource" "autoscaling_instance_monitor_deploy" {
  triggers = {
    controller_id = oci_core_instance.controller.id
    helper_hash   = sha256(join("", [filesha256("${path.module}/bin/autoscaling_instance_monitor.py"), filesha256("${path.module}/autoscaling/tf_init/instance-pool-configuration.tf"), filesha256("${path.module}/autoscaling/tf_init/cluster-network-configuration.tf"), filesha256("${path.module}/autoscaling/tf_init/compute-nodes.tf")]))
    settings_hash = sha256(jsonencode([
      var.autoscaling_failure_notifications_enabled,
      local.autoscaling_failure_topic_id,
      local.slurm_notification_deployment_id,
      var.region, var.targetCompartment, var.inst_prin, local.controller_username,
    ]))
  }
  lifecycle {
    precondition {
      condition     = !var.autoscaling_failure_notifications_enabled || var.slurm
      error_message = "Administrator notifications include instance monitoring and require Slurm."
    }
  }
  depends_on = [
    null_resource.cluster,
    null_resource.autoscaling_failure_notification_deploy,
    oci_identity_policy.autoscaling_instance_monitor,
  ]
  connection {
    host        = local.host
    type        = "ssh"
    user        = local.controller_username
    private_key = tls_private_key.ssh.private_key_pem
  }
  provisioner "file" {
    source      = "${path.module}/autoscaling/tf_init/instance-pool-configuration.tf"
    destination = "/opt/oci-hpc/autoscaling/tf_init/instance-pool-configuration.tf"
  }
  provisioner "file" {
    source      = "${path.module}/autoscaling/tf_init/cluster-network-configuration.tf"
    destination = "/opt/oci-hpc/autoscaling/tf_init/cluster-network-configuration.tf"
  }
  provisioner "file" {
    source      = "${path.module}/autoscaling/tf_init/compute-nodes.tf"
    destination = "/opt/oci-hpc/autoscaling/tf_init/compute-nodes.tf"
  }
  provisioner "file" {
    source      = "${path.module}/bin/autoscaling_instance_monitor.py"
    destination = "/opt/oci-hpc/bin/autoscaling_instance_monitor.py"
  }
  provisioner "file" {
    content = jsonencode({
      enabled                = var.autoscaling_failure_notifications_enabled
      topic_id               = local.autoscaling_failure_topic_id
      deployment_id          = local.slurm_notification_deployment_id
      region                 = var.region
      compartment_id         = var.targetCompartment
      instance_principal     = var.inst_prin
      state_dir              = "/opt/oci-hpc/logs/autoscaling-instance-monitor"
      clusters_path          = "/opt/oci-hpc/autoscaling/clusters"
      queues_conf            = "/opt/oci-hpc/conf/queues.conf"
      excluded_cluster_names = [local.cluster_name]
    })
    destination = "/opt/oci-hpc/conf/autoscaling_instance_monitor.json"
  }
  provisioner "file" {
    content     = "PATH=/home/${local.controller_username}/.local/bin:/home/${local.controller_username}/bin:/opt/slurm/bin:/usr/local/bin:/usr/bin:/bin\n*/5 * * * * ${local.controller_username} /usr/bin/python3 /opt/oci-hpc/bin/autoscaling_instance_monitor.py >> /opt/oci-hpc/logs/autoscaling_instance_monitor.log 2>&1\n"
    destination = "/tmp/oci-hpc-autoscaling-instance-monitor.cron"
  }
  provisioner "remote-exec" {
    inline = [
      "chmod 600 /opt/oci-hpc/conf/autoscaling_instance_monitor.json",
      var.autoscaling_failure_notifications_enabled ? "sudo install -m 0644 /tmp/oci-hpc-autoscaling-instance-monitor.cron /etc/cron.d/oci-hpc-autoscaling-instance-monitor" : "[ ! -e /etc/cron.d/oci-hpc-autoscaling-instance-monitor ] || sudo rm -f /etc/cron.d/oci-hpc-autoscaling-instance-monitor",
      "rm -f /tmp/oci-hpc-autoscaling-instance-monitor.cron",
    ]
  }
}
