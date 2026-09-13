import ipaddress
import json
import re
import shlex
import subprocess
import unittest
from pathlib import Path

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "playbooks/roles/slurm"


def environment():
    result = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROLE / "templates")))
    result.filters["bool"] = lambda value: str(value).lower() in ("true", "1", "yes")
    result.filters["ipaddr"] = lambda value, operation: ipaddress.ip_network(value).num_addresses
    result.filters["to_nice_json"] = lambda value: json.dumps(value, indent=2)
    result.filters["quote"] = shlex.quote
    return result


def flatten_tasks(tasks):
    for task in tasks:
        yield task
        yield from flatten_tasks(task.get("block", []))


class SlurmUserTagsConfigurationTests(unittest.TestCase):
    def render_slurm(self, **changes):
        values = {
            "groups": {"controller": ["controller"], "slurm_backup": [], "login": []},
            "hostvars": {"controller": {"ansible_fqdn": "controller.example"}},
            "pyxis": False,
            "healthchecks": False,
            "sacct_limits": False,
            "slurm_nfs_path": "/mnt/cluster",
            "queues": [],
        }
        values.update(changes)
        return environment().get_template("slurm.conf.j2").render(**values)

    def test_enabled_by_default_without_other_prologs(self):
        rendered = self.render_slurm()
        self.assertIn("Prolog=/etc/slurm/prolog.d/*", rendered)
        self.assertIn("Epilog=/etc/slurm/epilog.d/oci-user-tags.sh", rendered)
        self.assertIn("PrologFlags=contain", rendered)
        self.assertNotIn("PrologSlurmctld=", rendered)

    def test_disable_preserves_existing_prologs_and_contain(self):
        for pyxis, healthchecks in [(False, False), (True, False), (False, True), (True, True)]:
            with self.subTest(pyxis=pyxis, healthchecks=healthchecks):
                rendered = self.render_slurm(
                    slurm_user_tags_enabled=False, pyxis=pyxis, healthchecks=healthchecks
                )
                self.assertEqual("Prolog=/etc/slurm/prolog.d/*" in rendered, pyxis or healthchecks)
                self.assertNotIn("Epilog=", rendered)
                self.assertIn("PrologFlags=contain", rendered)
                if healthchecks:
                    self.assertIn("HealthCheckProgram=/etc/slurm/prolog.d/healthchecks.sh", rendered)

    def test_config_matches_runtime_contract_and_only_user_tag(self):
        rendered = environment().get_template("oci-user-tags.json.j2").render(
            slurm_user_tags_enabled=True,
            slurm_user_tags_state_dir="/mnt/shared cluster/oci-user-tags",
            slurm_user_tags_local_cache_dir="/var/lib/slurm-oci-user-tags",
            slurm_exec="/usr",
        )
        config = json.loads(rendered)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["state_dir"], "/mnt/shared cluster/oci-user-tags")
        self.assertEqual(config["local_state_dir"], "/var/lib/slurm-oci-user-tags")
        self.assertEqual(config["tag_key"], "user")
        self.assertEqual(config["management_value"], "Management")
        self.assertEqual(config["oci_cli"], "/opt/slurm-oci-user-tags/venv/bin/oci")
        self.assertNotIn("job_tag", config)

    def test_scheduler_binary_path_matches_each_os_slurm_installation(self):
        for variables_file, expected_path in (
            ("el_vars.yml", "/usr/bin"),
            ("ubuntu_vars.yml", "/usr/local/bin"),
        ):
            with self.subTest(variables_file=variables_file):
                os_variables = yaml.safe_load((ROLE / "vars" / variables_file).read_text())
                rendered = environment().get_template("oci-user-tags.json.j2").render(
                    slurm_user_tags_enabled=True,
                    slurm_user_tags_state_dir="/mnt/cluster/oci-user-tags",
                    slurm_user_tags_local_cache_dir="/var/lib/slurm-oci-user-tags",
                    **os_variables,
                )
                config = json.loads(rendered)
                self.assertEqual(config["slurm_bin_dir"], expected_path)

    def test_accounting_wrapper_reports_failure_without_failing_job(self):
        for mode in ("prolog", "epilog"):
            wrapper = environment().get_template("oci-user-tags-hook.sh.j2").render(
                item=mode, slurm_conf_path="/etc/slurm"
            )
            # The command stub represents a timeout or a failed local record write.
            wrapper = re.sub(r"^/usr/bin/timeout .*", "(exit 124)", wrapper, flags=re.MULTILINE)
            result = subprocess.run(["/bin/sh"], input=wrapper, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("deferred (status=124)", result.stderr)

    def test_deployment_protects_state_and_preserves_unrelated_hooks(self):
        tasks = list(flatten_tasks(yaml.safe_load((ROLE / "tasks/user_tags.yml").read_text())))
        config = next(task for task in tasks if task["name"] == "Write protected Slurm user tag configuration")
        self.assertEqual(config["ansible.builtin.template"]["mode"], "0600")
        self.assertEqual(config["ansible.builtin.template"]["owner"], "root")
        shared = next(task for task in tasks if task["name"] == "Create protected shared Slurm user tag state directory")
        self.assertTrue(shared["run_once"])
        self.assertEqual(shared["ansible.builtin.file"]["mode"], "0700")
        removal = next(task for task in tasks if task["name"] == "Remove disabled Slurm user tag integration only")
        self.assertTrue(all("oci-user-tags" in path for path in removal["loop"]))
        self.assertNotIn("{{ slurm_user_tags_state_dir }}", removal["loop"])
        registration = next(task for task in tasks if task["name"] == "Register compute node before Slurm starts accepting jobs")
        self.assertIn("3s", registration["ansible.builtin.command"]["argv"])
        self.assertFalse(registration["failed_when"])
        for path in ("tasks/common.yml", "tasks/lite_common.yml"):
            self.assertIn("include_tasks: user_tags.yml", (ROLE / path).read_text())

    def test_controller_worker_is_bounded_and_uses_trusted_path(self):
        service = environment().get_template("systemd/slurm-oci-user-tags.service.j2").render(
            slurm_user_tags_state_dir="/mnt/cluster/oci-user-tags", slurm_conf_path="/etc/slurm"
        )
        timer = (ROLE / "templates/systemd/slurm-oci-user-tags.timer.j2").read_text()
        self.assertIn("User=root", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("TimeoutStartSec=60s", service)
        self.assertIn("KillMode=control-group", service)
        self.assertIn("reconcile --config /etc/slurm/oci-user-tags.json", service)
        self.assertNotIn("/home/", service)
        self.assertIn("OnUnitInactiveSec=5s", timer)

    def test_cli_is_installed_in_root_owned_environment_on_both_controllers(self):
        tasks = list(flatten_tasks(yaml.safe_load((ROLE / "tasks/user_tags.yml").read_text())))
        controller_block = next(task for task in tasks if task["name"] == "Install controller Slurm user tag worker")
        self.assertIn("'controller' in group_names", controller_block["when"])
        self.assertIn("'slurm_backup' in group_names", controller_block["when"])
        cli_directory = next(task for task in tasks if task["name"] == "Create root-owned controller OCI CLI directory")
        self.assertEqual(cli_directory["ansible.builtin.file"]["path"], "/opt/slurm-oci-user-tags")
        self.assertEqual(cli_directory["ansible.builtin.file"]["owner"], "root")
        self.assertEqual(cli_directory["ansible.builtin.file"]["mode"], "0700")
        install = next(task for task in tasks if task["name"] == "Install OCI CLI in protected controller environment")
        self.assertTrue(install["become"])
        self.assertEqual(install["ansible.builtin.pip"]["executable"], "/opt/slurm-oci-user-tags/venv/bin/pip")
        self.assertEqual(install["environment"]["PIP_USER"], "false")
        verify = next(task for task in tasks if task["name"] == "Verify isolated controller OCI CLI starts")
        self.assertNotIn("failed_when", verify)

    def test_flag_reaches_initial_ha_and_autoscaling_inventory(self):
        for filename in ("controller.tf", "slurm_ha.tf"):
            source = (ROOT / filename).read_text()
            self.assertEqual(source.count("slurm_user_tags_enabled = var.slurm && var.slurm_user_tags_enabled,"), 2)
            self.assertEqual(source.count("slurm_user_tags_enabled = tostring(var.slurm && var.slurm_user_tags_enabled)"), 2)
        for filename in ("inventory.tpl", "autoscaling/tf_init/inventory.tpl"):
            self.assertIn("slurm_user_tags_enabled=${slurm_user_tags_enabled}", (ROOT / filename).read_text())
        self.assertIn(
            "slurm_user_tags_enabled = var.slurm_user_tags_enabled,",
            (ROOT / "autoscaling/tf_init/controller_update.tf").read_text(),
        )
        self.assertIn('variable "slurm_user_tags_enabled"', (ROOT / "conf/variables.tpl").read_text())
        schema = yaml.safe_load((ROOT / "schema.yaml").read_text())
        self.assertTrue(schema["variables"]["slurm_user_tags_enabled"]["default"])
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.assertTrue(defaults["slurm_user_tags_enabled"])

    def test_controller_and_autoscaling_share_the_selected_slurm_state_path(self):
        for filename in ("controller.tf", "slurm_ha.tf"):
            source = (ROOT / filename).read_text()
            path_selections = re.findall(
                r"slurm_nfs_path\s*=\s*var\.(\w+)\s*\?\s*var\.(\w+)\s*:\s*var\.(\w+)",
                source,
            )
            # First selection renders the initial inventory; the second writes
            # the variable template inherited by every autoscaling cluster.
            self.assertEqual(len(path_selections), 2)
            for add_nfs, slurm_nfs in ((False, False), (True, False), (True, True)):
                with self.subTest(filename=filename, add_nfs=add_nfs, slurm_nfs=slurm_nfs):
                    variables = {
                        "add_nfs": add_nfs,
                        "slurm_nfs": slurm_nfs,
                        "cluster_nfs_path": "/nfs/cluster",
                        "nfs_source_path": "/share",
                    }
                    selected = [
                        variables[yes] if variables[condition] else variables[no]
                        for condition, yes, no in path_selections
                    ]
                    expected = "/share" if slurm_nfs else "/nfs/cluster"
                    self.assertEqual(selected, [expected, expected])
        self.assertIn(
            'variable "slurm_nfs_path" { default = "${slurm_nfs_path}" }',
            (ROOT / "conf/variables.tpl").read_text(),
        )
        self.assertIn(
            "slurm_nfs_path = var.slurm_nfs_path,",
            (ROOT / "autoscaling/tf_init/controller_update.tf").read_text(),
        )
        self.assertIn(
            "slurm_nfs_path = ${slurm_nfs_path}",
            (ROOT / "autoscaling/tf_init/inventory.tpl").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
