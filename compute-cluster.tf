resource "oci_core_compute_cluster" "compute_cluster" {
  count      = var.compute_cluster && var.cluster_network && var.node_count > 0 ? 1 : 0
  depends_on = [
    oci_identity_policy.compute_management_autoscaling_policy,
    oci_identity_policy.autoscaling_policy,
  ]

  #Required
  availability_domain = var.ad
  compartment_id      = var.targetCompartment

  #Optional
  display_name = local.cluster_name
  defined_tags = local.user_cost_tags
  freeform_tags = {
    "cluster_name"  = local.cluster_name
    "parent_cluster" = local.cluster_name
  }
}
