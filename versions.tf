terraform {
  # Resource Manager identifies the supported minor series from this constraint.
  # The controller's standalone Autoscaling configuration has its own minimum.
  required_version = "~> 1.5.0, < 1.6"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "5.37.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.6.3"
    }
  }
}
