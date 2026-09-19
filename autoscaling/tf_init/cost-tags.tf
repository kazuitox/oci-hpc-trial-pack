locals {
  # The initial stack owns the namespace and key; dynamic clusters only use it.
  user_cost_tags = { "hpc-cost.User" = var.tags }
}
