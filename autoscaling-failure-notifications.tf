# Deploy separately from null_resource.cluster so enabling notifications on an
# existing stack does not reconfigure running compute nodes.
resource "null_resource" "autoscaling_failure_notification_deploy" {
  triggers = {
    controller_id = oci_core_instance.controller.id
    helper_hash   = filesha256("${path.module}/bin/autoscaling_failure_notify.py")
    settings_hash = sha256(jsonencode([
      var.autoscaling_failure_notifications_enabled,
      local.autoscaling_failure_topic_id,
      var.region,
      var.targetCompartment,
      var.inst_prin,
      local.controller_username,
    ]))
  }

  depends_on = [
    null_resource.controller,
    oci_identity_policy.autoscaling_failure_controllers,
    oci_ons_subscription.autoscaling_failure_email,
  ]

  provisioner "file" {
    source      = "${path.module}/bin/autoscaling_failure_notify.py"
    destination = "/opt/oci-hpc/bin/autoscaling_failure_notify.py"
    connection {
      host        = local.host
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "file" {
    content = jsonencode({
      enabled            = var.autoscaling_failure_notifications_enabled
      topic_id           = local.autoscaling_failure_topic_id
      region             = var.region
      compartment_id     = var.targetCompartment
      instance_principal = var.inst_prin
      spool              = "/opt/oci-hpc/logs/autoscaling-failure-notifications"
    })
    destination = "/opt/oci-hpc/conf/autoscaling_failure_notification.json"
    connection {
      host        = local.host
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "file" {
    content     = "PATH=/home/${local.controller_username}/.local/bin:/home/${local.controller_username}/bin:/usr/local/bin:/usr/bin:/bin\n* * * * * ${local.controller_username} /usr/bin/python3 /opt/oci-hpc/bin/autoscaling_failure_notify.py retry >> /opt/oci-hpc/logs/autoscaling_failure_notify.log 2>&1\n"
    destination = "/tmp/oci-hpc-autoscaling-failure-notify.cron"
    connection {
      host        = local.host
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }

  provisioner "remote-exec" {
    inline = [
      "chmod 600 /opt/oci-hpc/conf/autoscaling_failure_notification.json",
      var.autoscaling_failure_notifications_enabled ? "sudo install -m 0644 /tmp/oci-hpc-autoscaling-failure-notify.cron /etc/cron.d/oci-hpc-autoscaling-failure-notify" : "[ ! -e /etc/cron.d/oci-hpc-autoscaling-failure-notify ] || sudo rm -f /etc/cron.d/oci-hpc-autoscaling-failure-notify",
      "rm -f /tmp/oci-hpc-autoscaling-failure-notify.cron",
    ]
    connection {
      host        = local.host
      type        = "ssh"
      user        = local.controller_username
      private_key = tls_private_key.ssh.private_key_pem
    }
  }
}
