variable "autoscaling_failure_threshold" {
  type        = number
  default     = 3
  description = "Stop automatic cluster creation after this many consecutive failures; 0 disables the guard."

  validation {
    condition     = var.autoscaling_failure_threshold >= 0 && floor(var.autoscaling_failure_threshold) == var.autoscaling_failure_threshold
    error_message = "autoscaling_failure_threshold must be a nonnegative integer."
  }
}

# Deploy without re-running cluster setup or changing compute resources.
resource "null_resource" "autoscaling_guard_deploy" {
  count = var.slurm_ha ? 2 : 1
  triggers = {
    controller_id = count.index == 0 ? oci_core_instance.controller.id : oci_core_instance.backup[0].id
    setup_id      = count.index == 0 ? null_resource.cluster.id : null_resource.cluster_backup[0].id
    helper_hash = sha256(join("", [
      filesha256("${path.module}/bin/autoscaling_guard.py"),
      filesha256("${path.module}/bin/create_cluster.sh"),
      filesha256("${path.module}/autoscaling/crontab/autoscale_slurm.sh"),
      filesha256("${path.module}/autoscaling/crontab/autoscale_slurm_disable-resize.sh"),
    ]))
    settings_hash = sha256(jsonencode([var.autoscaling_failure_threshold, local.controller_username]))
  }
  depends_on = [null_resource.cluster, null_resource.cluster_backup]
  connection {
    host        = count.index == 0 ? local.host : local.host_backup
    type        = "ssh"
    user        = local.controller_username
    private_key = tls_private_key.ssh.private_key_pem
  }
  provisioner "file" {
    source      = "${path.module}/bin/autoscaling_guard.py"
    destination = "/opt/oci-hpc/bin/autoscaling_guard.py"
  }
  provisioner "file" {
    source      = "${path.module}/bin/create_cluster.sh"
    destination = "/opt/oci-hpc/bin/create_cluster.sh"
  }
  provisioner "file" {
    source      = "${path.module}/autoscaling/crontab/autoscale_slurm.sh"
    destination = "/opt/oci-hpc/autoscaling/crontab/autoscale_slurm.sh"
  }
  provisioner "file" {
    source      = "${path.module}/autoscaling/crontab/autoscale_slurm_disable-resize.sh"
    destination = "/opt/oci-hpc/autoscaling/crontab/autoscale_slurm_disable-resize.sh"
  }
  provisioner "file" {
    content = jsonencode({
      failure_threshold = var.autoscaling_failure_threshold
      admin_user        = local.controller_username
      state_dir         = "/opt/oci-hpc/autoscaling/guard"
    })
    destination = "/opt/oci-hpc/conf/autoscaling_guard.json"
  }
  provisioner "remote-exec" {
    inline = [
      "chmod 600 /opt/oci-hpc/conf/autoscaling_guard.json",
      "chmod 755 /opt/oci-hpc/bin/autoscaling_guard.py /opt/oci-hpc/bin/create_cluster.sh /opt/oci-hpc/autoscaling/crontab/autoscale_slurm.sh /opt/oci-hpc/autoscaling/crontab/autoscale_slurm_disable-resize.sh",
    ]
  }
}
