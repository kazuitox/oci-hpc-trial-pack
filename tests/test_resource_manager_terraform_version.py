"""Protect Resource Manager version discovery separately from dynamic clusters.

The static checks cover the documented Resource Manager constraint prefix, not
the OCI upload service. Set TERRAFORM_BINARY for an optional provider-free CLI
check of the actual root constraint; no OCI credentials or resources are used.
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
REQUIRED_VERSION = re.compile(r'^\s*required_version\s*=\s*"([^"]+)"', re.MULTILINE)


def required_version(source):
    declarations = REQUIRED_VERSION.findall(source)
    if len(declarations) != 1:
        raise AssertionError("Expected exactly one required_version declaration")
    return declarations[0]


class ResourceManagerTerraformVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_versions = (REPOSITORY_ROOT / "versions.tf").read_text(encoding="utf-8")
        cls.dynamic_versions = (
            REPOSITORY_ROOT / "autoscaling" / "tf_init" / "versions.tf"
        ).read_text(encoding="utf-8")
        cls.root_constraint = required_version(cls.root_versions)

    def test_root_module_has_a_single_version_declaration(self):
        declarations = []
        for source in sorted(REPOSITORY_ROOT.glob("*.tf")):
            declarations.extend(
                (source.name, constraint)
                for constraint in REQUIRED_VERSION.findall(source.read_text(encoding="utf-8"))
            )
        for source in sorted(REPOSITORY_ROOT.glob("*.tf.json")):
            terraform = json.loads(source.read_text(encoding="utf-8")).get("terraform", {})
            if "required_version" in terraform:
                declarations.append((source.name, terraform["required_version"]))
        self.assertEqual(declarations, [("versions.tf", self.root_constraint)])

    def test_root_uses_documented_resource_manager_constraint_format(self):
        # Oracle's Marketplace stack guidelines document ~> major.minor.0 for
        # Resource Manager version discovery; this is not a Console UI test.
        match = re.match(r"^~>\s*(\d+)\.(\d+)\.0(?=\s*(?:,|$))", self.root_constraint)
        self.assertIsNotNone(match, "Resource Manager needs a leading ~> major.minor.0 constraint")
        self.assertEqual(match.groups(), ("1", "5"))

    def test_root_preserves_the_1_5_minimum_and_patch_series(self):
        constraints = [constraint.strip() for constraint in self.root_constraint.split(",")]
        self.assertEqual(constraints[0], "~> 1.5.0")
        self.assertEqual(len(constraints), 2)

    def test_root_has_an_explicit_upper_bound_for_resource_manager(self):
        constraints = [constraint.strip() for constraint in self.root_constraint.split(",")]
        self.assertIn("< 1.6", constraints)

    def test_dynamic_clusters_keep_the_unbounded_local_cli_constraint(self):
        self.assertEqual(required_version(self.dynamic_versions), ">= 1.5.0")

    def test_oci_provider_pin_is_unchanged_in_both_execution_paths(self):
        for name, source in (("Resource Manager", self.root_versions),
                             ("Autoscaling", self.dynamic_versions)):
            with self.subTest(module=name):
                match = re.search(r"\boci\s*=\s*\{([^}]+)\}", source)
                self.assertIsNotNone(match)
                self.assertRegex(match.group(1), r'source\s*=\s*"oracle/oci"')
                self.assertRegex(match.group(1), r'version\s*=\s*"5\.37\.0"')

    def test_cli_enforces_the_actual_root_constraint_without_providers(self):
        terraform = os.environ.get("TERRAFORM_BINARY") or shutil.which("terraform")
        if not terraform:
            self.skipTest("Set TERRAFORM_BINARY to run the provider-free version constraint check")
        terraform = os.path.abspath(terraform)
        with tempfile.TemporaryDirectory(prefix="oci-hpc-tf-version-") as temporary:
            workdir = Path(temporary)
            cli_config = workdir / "terraform.rc"
            cli_config.write_text("disable_checkpoint = true\n", encoding="utf-8")
            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith("TF_CLI_ARGS")
            }
            environment.update({
                "TF_CLI_CONFIG_FILE": str(cli_config),
                "TF_DATA_DIR": str(workdir / ".terraform"),
                "TF_IN_AUTOMATION": "1",
                "CHECKPOINT_DISABLE": "1",
            })

            def run(*arguments):
                return subprocess.run(
                    [terraform, *arguments], cwd=workdir, env=environment,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=30, check=False,
                )

            version_result = run("version", "-json")
            self.assertEqual(version_result.returncode, 0, version_result.stdout)
            version = json.loads(version_result.stdout)["terraform_version"]
            match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version)
            if match is None:
                self.skipTest("The version constraint check requires a stable Terraform release")
            selected = tuple(int(part) for part in match.groups())
            (workdir / "main.tf").write_text(
                "terraform {\n  required_version = "
                + json.dumps(self.root_constraint)
                + "\n}\noutput \"version_constraint_check\" { value = true }\n",
                encoding="utf-8",
            )
            initialized = run("init", "-backend=false", "-input=false", "-no-color")
            if (1, 5, 0) <= selected < (1, 6, 0):
                self.assertEqual(initialized.returncode, 0, initialized.stdout)
                validated = run("validate", "-no-color")
                self.assertEqual(validated.returncode, 0, validated.stdout)
            else:
                self.assertNotEqual(initialized.returncode, 0, initialized.stdout)
                self.assertIn("Unsupported Terraform Core version", initialized.stdout)


if __name__ == "__main__":
    unittest.main()
