# Initial cost tags are applied even when Slurm's runtime updates are disabled.
# Keep their definitions independent of slurm_user_tags_enabled so disabling the
# worker does not delete tags from existing resources.
resource "oci_identity_tag_namespace" "hpc_cost" {
  provider       = oci.home
  compartment_id = var.targetCompartment
  name           = "hpc-cost"
  description    = "Tags for tracking HPC compute costs by Slurm user."
}

resource "oci_identity_tag" "hpc_cost_user" {
  provider         = oci.home
  tag_namespace_id = oci_identity_tag_namespace.hpc_cost.id
  name             = "User"
  description      = "Slurm user responsible for compute usage, or Management when the node is idle."

  # No validator means Static value: accept any Slurm user name.
}

locals {
  # Referencing the definition orders node creation after the tag is available.
  user_cost_tag_key = "${oci_identity_tag_namespace.hpc_cost.name}.${oci_identity_tag.hpc_cost_user.name}"
  user_cost_tags    = { (local.user_cost_tag_key) = var.tags }
}
