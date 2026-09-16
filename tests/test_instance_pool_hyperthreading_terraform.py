"""Plan the production HT blocks using Terraform's mocked OCI 5.37.0 provider.

Requires Terraform >= 1.7 (the deployed modules still support Terraform 1.5).
Set TERRAFORM_BINARY to select Terraform and TF_PLUGIN_DIR to an existing
provider mirror for offline runs; otherwise terraform init installs OCI 5.37.0.
No OCI credentials are used; plans and state setup use a mock provider only.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG = "oci_core_instance_configuration.instance_pool_configuration"
PLATFORM = CONFIG + "[0].instance_details[0].launch_details[0].platform_config"
SHAPES = "data.oci_core_shapes.instance_pool_vm_shape"


def blocks(source, header):
    """Extract complete HCL blocks without changing their expressions."""
    result = []
    for match in re.finditer(header + r"\s*\{", source):
        depth = 1
        for token in re.finditer(
            r'"(?:\\.|[^"\\])*"|//[^\n]*|\#[^\n]*|/\*[\s\S]*?\*/|[{}]',
            source[match.end() :],
        ):
            if token.group() == "{":
                depth += 1
            elif token.group() == "}":
                depth -= 1
                if depth == 0:
                    result.append(source[match.start() : match.end() + token.end()])
                    break
        else:
            raise AssertionError("Unclosed HCL block: " + match.group())
    return result


def assertions(conditions):
    return "\n".join(
        'assert {\n condition = %s\n error_message = %s\n}'
        % (condition, json.dumps("HT regression: " + condition))
        for condition in conditions
    )


def shape_response(platform_type="AMD_VM", allowed=(False, True)):
    return [{
        "name": "VM.Standard.E5.Flex",
        "platform_config_options": [{
            "type": platform_type,
            "symmetric_multi_threading_options": [{"allowed_values": allowed}],
        }],
    }]


def scenarios(autoscaling):
    """Cases assert the resulting plan, including expected precondition failures."""
    cases = []

    def add(name, variables=None, shapes=None, platform_type=None, ht=None,
            fails=False, queried=True, configured=True, extra=(), command="plan"):
        conditions = ["length(%s) == %s" % (SHAPES, int(queried))]
        conditions.append("length(%s) == %s" % (CONFIG, int(configured)))
        if configured and not fails:
            conditions.append("length(%s) == %s" % (PLATFORM, int(platform_type is not None)))
            if platform_type:
                conditions.extend([
                    "%s[0].type == %s" % (PLATFORM, json.dumps(platform_type)),
                    "%s[0].is_symmetric_multi_threading_enabled == %s"
                    % (PLATFORM, json.dumps(ht)),
                ])
        if queried:
            conditions.extend([
                SHAPES + "[0].compartment_id == var.targetCompartment",
                SHAPES + "[0].availability_domain == var.ad",
                "one(%s[0].filter).name == \"name\"" % SHAPES,
                "one(%s[0].filter).values == tolist([var.instance_pool_shape])" % SHAPES,
            ])
        body = "command = %s\nvariables {\n%s\n}\n" % (command, "\n".join(
            "%s = %s" % (key, json.dumps(value))
            for key, value in (variables or {}).items()
        ))
        if queried:
            body += "override_data {\n target = %s[0]\n values = %s\n}\n" % (
                SHAPES, json.dumps({"shapes": shapes if shapes is not None else shape_response()})
            )
        if fails:
            body += "expect_failures = [%s]\n" % CONFIG
        else:
            body += assertions(conditions + list(extra))
        cases.append('run "%s" {\n%s\n}' % (name, body))

    for platform_type in ("AMD_VM", "INTEL_VM"):
        for ht in (False, True):
            add("%s_ht_%s" % (platform_type.lower(), str(ht).lower()),
                {"hyperthreading": ht, "SMT": not ht, "BIOS": True},
                shape_response(platform_type), platform_type, ht)
    add("bios_disabled_still_controls_vm", {"BIOS": False}, platform_type="AMD_VM", ht=False)
    add("string_queue_value_is_boolean", {"hyperthreading": "false"}, platform_type="AMD_VM", ht=False)

    missing = {
        "empty_shapes": [],
        "empty_platform": [{"name": "VM.Standard.E5.Flex", "platform_config_options": []}],
        "empty_smt": [{"name": "VM.Standard.E5.Flex", "platform_config_options": [{
            "type": "AMD_VM", "symmetric_multi_threading_options": []}]}],
        "empty_allowed_values": shape_response(allowed=[]),
        "null_allowed_values": shape_response(allowed=None),
    }
    for label, shapes in missing.items():
        add(label + "_off_rejected", shapes=shapes, fails=True)
        add(label + "_on_uses_default", {"hyperthreading": True}, shapes=shapes)
    add("only_on_rejects_off", shapes=shape_response(allowed=[True]), fails=True)
    add("only_off_rejects_on", {"hyperthreading": True}, shape_response(allowed=[False]), fails=True)
    add("only_off_accepts_off", shapes=shape_response(allowed=[False]), platform_type="AMD_VM", ht=False)
    add("unknown_platform_rejects_off", shapes=shape_response("GENERIC_BM"), fails=True)
    add("unknown_platform_with_capability_rejects_on", {"hyperthreading": True},
        shape_response("GENERIC_BM"), fails=True)

    for shape in ("VM.Standard.A1.Flex", "VM.Standard.A2.Flex"):
        for ht in (False, True):
            add("arm_%s_%s" % (shape.split(".")[2].lower(), str(ht).lower()),
                {"instance_pool_shape": shape, "hyperthreading": ht, "BIOS": True}, queried=False)
    for smt in (False, True):
        add("bm_retains_smt_%s" % str(smt).lower(), {
            "instance_pool_shape": "BM.Standard.E4.128", "BIOS": True,
            "SMT": smt, "hyperthreading": not smt,
        }, platform_type="AMD_MILAN_BM", ht=smt, queried=False, extra=[
            PLATFORM + '[0].numa_nodes_per_socket == "NPS4"',
            PLATFORM + "[0].percentage_of_cores_enabled == 100",
            PLATFORM + "[0].is_input_output_memory_management_unit_enabled == true",
        ])
    add("bm_bios_disabled", {"instance_pool_shape": "BM.Standard.E4.128", "BIOS": False}, queried=False)
    add("cluster_network_is_unchanged", {"cluster_network": True}, queried=False, configured=False)
    add("zero_node_count", {"node_count": 0}, queried=autoscaling,
        configured=autoscaling, platform_type="AMD_VM" if autoscaling else None, ht=False)
    # Mock apply persists state for the next plan, exercising changes to an
    # existing configuration. The mocked provider never contacts OCI.
    add("existing_vm_ht_on", {"hyperthreading": True},
        platform_type="AMD_VM", ht=True, command="apply")
    add("existing_vm_changes_to_ht_off", {"hyperthreading": False},
        platform_type="AMD_VM", ht=False)
    return 'mock_provider "oci" {}\n\n' + "\n\n".join(cases)


def make_fixture(destination, source):
    """Keep production HT logic, resource count and preconditions verbatim.

    Only unrelated launch attributes and infrastructure dependencies are replaced
    with constants; Terraform still expands dynamic blocks using OCI's schema.
    """
    configuration = (source / "instance-pool-configuration.tf").read_text()
    platform_blocks = blocks(configuration, r'dynamic\s+"platform_config"')
    lifecycle = blocks(configuration, r"\blifecycle")
    if len(platform_blocks) != 2 or len(lifecycle) != 1:
        raise AssertionError("Expected VM/BM platform blocks and one lifecycle block")
    count = re.search(r"^\s*count\s*=([^\n]+)", configuration, re.MULTILINE).group(1)
    shutil.copyfile(source / "instance-pool-platform.tf", destination / "instance-pool-platform.tf")
    defaults = {
        "cluster_network": False, "node_count": 1,
        "targetCompartment": "ocid1.compartment.oc1..test", "ad": "test:AD-1",
        "instance_pool_shape": "VM.Standard.E5.Flex", "hyperthreading": False,
        "BIOS": False, "SMT": True, "virt_instr": False, "access_ctrl": False,
        "IOMMU": True, "numa_nodes_per_socket": "Default",
        "percentage_of_cores_enabled": "Default",
    }
    variables = "\n".join('variable "%s" { default = %s }' % (key, json.dumps(value))
                          for key, value in defaults.items())
    (destination / "fixture.tf").write_text('''
terraform {
  required_version = ">= 1.7.0"
  required_providers {
    oci = { source = "oracle/oci", version = "5.37.0" }
  }
}
%s
locals { platform_type = "AMD_MILAN_BM" }
resource "oci_core_instance_configuration" "instance_pool_configuration" {
  count = %s
  compartment_id = var.targetCompartment
  instance_details {
    instance_type = "compute"
    launch_details {
      compartment_id = var.targetCompartment
      availability_domain = var.ad
      shape = var.instance_pool_shape
      %s
    }
  }
  %s
}
''' % (variables, count, "\n".join(platform_blocks), lifecycle[0]))
    (destination / "hyperthreading.tftest.hcl").write_text(scenarios(source != REPOSITORY_ROOT))


class InstancePoolHyperthreadingTerraformTests(unittest.TestCase):
    def test_vm_platform_does_not_send_bm_only_attributes(self):
        for source in (REPOSITORY_ROOT, REPOSITORY_ROOT / "autoscaling/tf_init"):
            with self.subTest(module=str(source)):
                content = (source / "instance-pool-configuration.tf").read_text()
                vm = blocks(content, r'dynamic\s+"platform_config"')[0]
                attributes = set(re.findall(r"^\s*(\w+)\s*=", blocks(vm, "content")[0], re.MULTILINE))
                self.assertEqual(attributes, {"type", "is_symmetric_multi_threading_enabled"})
                self.assertRegex(blocks(content, r"\blifecycle")[0],
                                 r"\bcreate_before_destroy\s*=\s*true\b")

    def test_production_blocks_with_mocked_oci_provider(self):
        terraform = os.environ.get("TERRAFORM_BINARY") or shutil.which("terraform")
        if not terraform:
            self.skipTest("Set TERRAFORM_BINARY to Terraform >= 1.7 to run mock plan tests")
        version = json.loads(subprocess.check_output([terraform, "version", "-json"], text=True))
        if tuple(int(part) for part in version["terraform_version"].split(".")[:2]) < (1, 7):
            self.skipTest("Terraform mock providers require Terraform >= 1.7")
        for source in (REPOSITORY_ROOT, REPOSITORY_ROOT / "autoscaling/tf_init"):
            with self.subTest(module=str(source)), tempfile.TemporaryDirectory(prefix="oci-vm-ht-") as directory:
                destination = Path(directory)
                make_fixture(destination, source)
                init = ["init", "-backend=false", "-input=false", "-no-color"]
                if os.environ.get("TF_PLUGIN_DIR"):
                    init.append("-plugin-dir=" + os.environ["TF_PLUGIN_DIR"])
                for arguments in (init, ["validate", "-no-color"], ["test", "-no-color"]):
                    result = subprocess.run([terraform] + arguments, cwd=destination, text=True,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
                    self.assertEqual(result.returncode, 0, str(source) + "\n" + result.stdout)


if __name__ == "__main__":
    unittest.main()
