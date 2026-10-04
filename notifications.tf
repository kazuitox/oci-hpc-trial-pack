resource "oci_identity_dynamic_group" "slurm_notification_controllers" {
  count          = var.slurm_job_notifications_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "slurm_notification_controllers_${local.slurm_notification_identity_suffix}"
  description    = "Controllers allowed to manage Slurm job notifications for ${local.cluster_name}."
  matching_rule  = local.slurm_notification_controller_matching_rule

  freeform_tags = {
    "cluster_name"               = local.cluster_name
    "parent_cluster"             = local.cluster_name
    "notification_scope"         = local.slurm_notification_cluster_scope
    "notification_deployment_id" = local.slurm_notification_deployment_id
    "managed_by"                 = "terraform"
  }
}

resource "oci_identity_dynamic_group" "autoscaling_failure_controllers" {
  count          = var.autoscaling_failure_notifications_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "autoscaling_failure_controllers_${local.slurm_notification_identity_suffix}"
  description    = "Controllers allowed to publish autoscaling creation failures for ${local.cluster_name}."
  matching_rule  = local.slurm_notification_controller_matching_rule
}

resource "oci_identity_policy" "autoscaling_failure_controllers" {
  count          = var.autoscaling_failure_notifications_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "autoscaling-failure-controllers-${local.slurm_notification_identity_suffix}"
  description    = "Allows controllers to publish autoscaling creation failures."
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.autoscaling_failure_controllers[0].name} to use ons-topics in compartment id ${var.targetCompartment}",
  ]
}

resource "oci_ons_notification_topic" "autoscaling_failure" {
  count          = var.autoscaling_failure_notifications_enabled ? 1 : 0
  compartment_id = var.targetCompartment
  name           = "autoscaling-failure-${local.slurm_notification_identity_suffix}"
  description    = "Autoscaling cluster creation failures for ${local.cluster_name}."
  freeform_tags = {
    "cluster_name" = local.cluster_name
    "managed_by"   = "terraform"
  }
}

resource "oci_ons_subscription" "autoscaling_failure_email" {
  count          = var.autoscaling_failure_notifications_enabled ? 1 : 0
  compartment_id = var.targetCompartment
  topic_id       = oci_ons_notification_topic.autoscaling_failure[0].id
  protocol       = "EMAIL"
  endpoint       = local.autoscaling_failure_admin_email
}

resource "oci_identity_policy" "slurm_notification_controllers" {
  count          = var.slurm_job_notifications_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "slurm-notification-controllers-${local.slurm_notification_identity_suffix}"
  description    = "Allows the ${local.cluster_name} controllers to manage Slurm notification topics and subscriptions."

  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.slurm_notification_controllers[0].name} to manage ons-topics in compartment id ${var.targetCompartment}",
  ]

  freeform_tags = {
    "cluster_name"               = local.cluster_name
    "parent_cluster"             = local.cluster_name
    "notification_scope"         = local.slurm_notification_cluster_scope
    "notification_deployment_id" = local.slurm_notification_deployment_id
    "managed_by"                 = "terraform"
  }
}

resource "oci_ons_notification_topic" "slurm_local_user" {
  count          = var.slurm_job_notifications_enabled ? 1 : 0
  compartment_id = var.targetCompartment
  name           = "slurm-${local.slurm_notification_identity_suffix}-${local.slurm_notification_local_topic_suffix}"
  description    = "Slurm job notifications for ${local.controller_username} on ${local.cluster_name}."

  freeform_tags = {
    "cluster_name"               = local.cluster_name
    "parent_cluster"             = local.cluster_name
    "notification_scope"         = local.slurm_notification_cluster_scope
    "notification_deployment_id" = local.slurm_notification_deployment_id
    "slurm_user"                 = local.controller_username
    "managed_by"                 = "terraform"
  }
}

resource "oci_ons_subscription" "slurm_local_user_email" {
  count          = var.slurm_job_notifications_enabled ? 1 : 0
  compartment_id = var.targetCompartment
  topic_id       = oci_ons_notification_topic.slurm_local_user[0].id
  protocol       = "EMAIL"
  endpoint       = local.slurm_notification_admin_email

  freeform_tags = {
    "cluster_name"               = local.cluster_name
    "parent_cluster"             = local.cluster_name
    "notification_scope"         = local.slurm_notification_cluster_scope
    "notification_deployment_id" = local.slurm_notification_deployment_id
    "slurm_user"                 = local.controller_username
    "managed_by"                 = "terraform"
  }
}

# Runtime Topics are created by `cluster user add`, so they are not represented
# directly in Terraform state. Run their controller-side cleanup while the
# controller and its notification IAM policy still exist.
resource "terraform_data" "slurm_notification_runtime_cleanup" {
  count = var.slurm_job_notifications_enabled ? 1 : 0

  input = {
    host          = local.host
    user          = local.controller_username
    private_key   = tls_private_key.ssh.private_key_pem
    config_base64 = local.slurm_notification_destroy_cleanup_config_base64
    slurm_enabled = var.slurm
  }

  # This resource is a teardown sentinel, not a representation of a Topic.
  # Keep its connection and cleanup inputs updated in place so routine stack
  # changes never delete Topics while their OCIDs remain in the registry.

  lifecycle {
    precondition {
      condition     = var.slurm
      error_message = "slurm_job_notifications_enabled requires slurm to remain enabled. Disable notifications at the same time when intentionally removing Slurm."
    }
  }

  depends_on = [
    null_resource.cluster,
    oci_ons_subscription.slurm_local_user_email,
    oci_identity_policy.slurm_notification_controllers,
  ]

  provisioner "remote-exec" {
    when       = destroy
    on_failure = fail

    inline = [
      "/usr/local/libexec/oci-hpc/slurm-notification-destroy-cleanup --config-base64 '${self.output.config_base64}'",
    ]

    connection {
      type        = "ssh"
      host        = self.output.host
      user        = self.output.user
      private_key = self.output.private_key
    }
  }
}
