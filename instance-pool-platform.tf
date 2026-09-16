# Query the launch AD separately from the regional image-compatibility lookup.
data "oci_core_shapes" "instance_pool_vm_shape" {
  count = !var.cluster_network && var.node_count > 0 && local.instance_pool_is_vm && !local.instance_pool_is_arm ? 1 : 0

  compartment_id      = var.targetCompartment
  availability_domain = var.ad

  filter {
    name   = "name"
    values = [var.instance_pool_shape]
  }
}

locals {
  instance_pool_is_vm = length(regexall("^VM\\.", var.instance_pool_shape)) > 0
  # Arm has one hardware thread per core and does not use AMD_VM / INTEL_VM.
  instance_pool_is_arm = length(regexall("^VM\\.Standard\\.A[0-9]+\\.", var.instance_pool_shape)) > 0

  instance_pool_vm_platform_type = try(coalesce(data.oci_core_shapes.instance_pool_vm_shape[0].shapes[0].platform_config_options[0].type, "UNKNOWN"), "UNKNOWN")
  instance_pool_vm_smt_allowed_values = try(
    data.oci_core_shapes.instance_pool_vm_shape[0].shapes[0].platform_config_options[0].symmetric_multi_threading_options[0].allowed_values,
    []
  )
  instance_pool_vm_smt_options_known = try(length(local.instance_pool_vm_smt_allowed_values) > 0, false)
  instance_pool_vm_smt_supported = (
    contains(["AMD_VM", "INTEL_VM"], local.instance_pool_vm_platform_type) &&
    try(contains(local.instance_pool_vm_smt_allowed_values, tobool(var.hyperthreading)), false)
  )
  instance_pool_vm_platform_config = local.instance_pool_is_vm && !local.instance_pool_is_arm && local.instance_pool_vm_smt_supported ? [local.instance_pool_vm_platform_type] : []

  # Older VM shapes may omit SMT capabilities. Keep their default for HT On,
  # but never silently launch an HT Off request with the default (HT On).
  instance_pool_vm_hyperthreading_valid = (
    !local.instance_pool_is_vm || local.instance_pool_is_arm ||
    length(local.instance_pool_vm_platform_config) > 0 ||
    (tobool(var.hyperthreading) && !local.instance_pool_vm_smt_options_known)
  )
}
