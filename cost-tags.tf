# Cost tags are optional for new deployments. Protect definitions already in
# use: stop runtime updates with slurm_user_tags_enabled instead of deleting them.
resource "oci_identity_tag_namespace" "hpc_cost" {
  count          = var.cost_tags_enabled ? 1 : 0
  provider       = oci.home
  compartment_id = var.targetCompartment
  name           = "hpc-cost"
  description    = "Tags for tracking HPC compute costs by Slurm user."

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_identity_tag" "hpc_cost_user" {
  count            = var.cost_tags_enabled ? 1 : 0
  provider         = oci.home
  tag_namespace_id = oci_identity_tag_namespace.hpc_cost[0].id
  name             = "User"
  description      = "Slurm user responsible for compute usage, or Management when the node is idle."

  # No validator means Static value: accept any Slurm user name.
  lifecycle {
    prevent_destroy = true
  }
}

moved {
  from = oci_identity_tag_namespace.hpc_cost
  to   = oci_identity_tag_namespace.hpc_cost[0]
}

moved {
  from = oci_identity_tag.hpc_cost_user
  to   = oci_identity_tag.hpc_cost_user[0]
}

locals {
  # Referencing the definition orders node creation after the tag is available.
  user_cost_tag_key = var.cost_tags_enabled ? "${oci_identity_tag_namespace.hpc_cost[0].name}.${oci_identity_tag.hpc_cost_user[0].name}" : ""
  user_cost_tags    = var.cost_tags_enabled ? { (local.user_cost_tag_key) = var.tags } : {}
}
