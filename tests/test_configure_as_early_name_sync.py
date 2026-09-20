import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
CONFIGURE_SCRIPT = REPOSITORY_ROOT / "bin/configure_as.sh"
CLUSTER_NAME = "batch-1-standard"


# Execute the real shell orchestration while replacing only its external
# services. Each recorded event includes the inventory and durable stage that
# the command actually received, so ordering and restart tests do not rely on
# matching snippets of the shell source.
FIXTURE_COMMAND = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

kind, *args = sys.argv[1:]
root = Path(os.environ["TEST_CONFIGURE_ROOT"])
cluster = root / "autoscaling/clusters/batch-1-standard"
stage_file = cluster / ".initial-configure-stage"
journal = cluster / ".instance-pool-hostname-sync.json"

if kind == "initial_configure_state.py":
    stage = args[args.index("--stage") + 1] if "--stage" in args else None
    if args[0] == "write" and stage == os.environ.get("TEST_FAIL_STAGE_COMMIT"):
        print("fixture: atomic stage commit rejected", file=sys.stderr)
        sys.exit(29)
    sys.exit(subprocess.run([
        sys.executable, str(root / "bin/initial_configure_state_real.py"), *args
    ]).returncode)

event = {"args": args, "stage": None}
if stage_file.is_file():
    event["stage"] = json.loads(stage_file.read_text())["stage"]

if kind == "ansible-playbook":
    playbook = next(arg for arg in args if arg.endswith(".yml"))
    event["operation"] = {
        "new_nodes_hostname.yml": "hostname",
        "new_nodes.yml": "configure",
    }[Path(playbook).name]
    inventory = Path(args[args.index("-i") + 1])
    event["inventory"] = inventory.read_text()
    if "--extra-vars" in args:
        event["extra_vars"] = json.loads(args[args.index("--extra-vars") + 1])
elif kind == "resize.py":
    event["operation"] = {
        "prepare_local_block_volume": "prepare",
        "sync_instance_pool_names": "sync",
    }[args[-1]]
    inventory = Path(args[args.index("--inventory") + 1])
    if event["operation"] == "sync":
        journal.write_text('{"fixture": "pending"}\n')
elif kind == "wait_for_hosts.sh":
    event["operation"] = "wait"
elif kind == "resize.sh":
    assert "--reconcile-monitoring" in args, args
    event["operation"] = "monitoring"
elif kind == "ansible":
    event["operation"] = "unexpected-global-facts"
else:
    raise AssertionError(kind)

with (root / "events.jsonl").open("a") as output:
    output.write(json.dumps(event) + "\n")
if event["operation"] == os.environ.get("TEST_FAIL_OPERATION"):
    print("fixture: requested operation failed", file=sys.stderr)
    sys.exit(23)
if event["operation"] == "sync":
    inventory.write_text(inventory.read_text().replace("initial-node", "final-node"))
    journal.unlink()
'''


class ConfigureAutoscalingNameOrderTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.bin_path = self.root / "bin"
        self.commands = self.root / "commands"
        self.playbooks = self.root / "playbooks"
        self.cluster = self.root / "autoscaling/clusters" / CLUSTER_NAME
        for directory in (self.bin_path, self.commands, self.playbooks, self.cluster):
            directory.mkdir(parents=True)
        self.script = self.bin_path / "configure_as.sh"
        shutil.copy2(CONFIGURE_SCRIPT, self.script)
        self.real_stage_helper = self.bin_path / "initial_configure_state_real.py"
        shutil.copy2(
            REPOSITORY_ROOT / "bin/initial_configure_state.py", self.real_stage_helper
        )
        self.inventory = self.cluster / "inventory"
        self.variables = self.cluster / "variables.tf"
        self.stage = self.cluster / ".initial-configure-stage"
        self.journal = self.cluster / ".instance-pool-hostname-sync.json"
        self.events_path = self.root / "events.jsonl"
        self.inventory.write_text(
            "[compute_configured]\n\n[compute_to_add]\n"
            "initial-node ansible_host=192.0.2.10 oci_instance_id=ocid1.instance.test\n"
            "[all:vars]\ncluster_name=" + CLUSTER_NAME
            + "\ncluster_network=false\ncompute_username=opc\n",
            encoding="utf-8",
        )
        self.variables.write_text("# fixture deployment\n", encoding="utf-8")
        (self.cluster / ("hosts_" + CLUSTER_NAME)).write_text(
            "192.0.2.10\n", encoding="utf-8"
        )
        for playbook in ("new_nodes_hostname.yml", "new_nodes.yml"):
            (self.playbooks / playbook).write_text("---\n", encoding="utf-8")

        runner = self.root / "fixture.py"
        runner.write_text(FIXTURE_COMMAND, encoding="utf-8")
        for kind, directory in (
            ("ansible-playbook", self.commands),
            ("ansible", self.commands),
            ("wait_for_hosts.sh", self.bin_path),
            ("resize.sh", self.bin_path),
        ):
            self.write_command(
                directory / kind,
                "exec " + shlex.quote(sys.executable) + " "
                + shlex.quote(str(runner)) + " " + shlex.quote(kind) + ' "$@"\n',
            )
        for script_name in ("resize.py", "initial_configure_state.py"):
            (self.bin_path / script_name).write_text(
                "import sys\nsys.argv.insert(1, " + repr(script_name) + ")\n"
                + FIXTURE_COMMAND, encoding="utf-8",
            )
        self.write_command(
            self.commands / "python3",
            "exec " + shlex.quote(sys.executable) + ' "$@"\n',
        )
        self.write_command(
            self.commands / "realpath",
            "exec " + shlex.quote(sys.executable)
            + ' -c \'import os, sys; print(os.path.realpath(sys.argv[1]))\' "$1"\n',
        )
        self.environment = dict(
            os.environ,
            PATH=str(self.commands) + os.pathsep + os.environ.get("PATH", ""),
            TEST_CONFIGURE_ROOT=str(self.root),
        )
        self.environment.pop("TEST_FAIL_OPERATION", None)
        self.environment.pop("TEST_FAIL_STAGE_COMMIT", None)

    @staticmethod
    def write_command(path, body):
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def run_configure(self, fail_operation=None, fail_stage=None):
        environment = dict(self.environment)
        if fail_operation:
            environment["TEST_FAIL_OPERATION"] = fail_operation
        if fail_stage:
            environment["TEST_FAIL_STAGE_COMMIT"] = fail_stage
        return subprocess.run(
            ["/bin/bash", str(self.script), CLUSTER_NAME],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def events(self):
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines()]

    def operations(self):
        return [event["operation"] for event in self.events()]

    def clear_events(self):
        self.events_path.unlink(missing_ok=True)

    def saved_stage(self):
        return json.loads(self.stage.read_text())["stage"]

    def write_stage(self, stage):
        result = subprocess.run(
            [sys.executable, str(self.real_stage_helper), "write", "--inventory",
             str(self.inventory), "--stage", stage],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_names_and_inventory_are_final_before_slurm_configuration(self):
        for cluster_network, compute_cluster in (
            ("false", "false"), ("true", "false"), ("true", "true")
        ):
            with self.subTest(cluster_network=cluster_network, compute_cluster=compute_cluster):
                self.inventory.write_text(
                    "[compute_configured]\n\n[compute_to_add]\n"
                    "initial-node ansible_host=192.0.2.10 oci_instance_id=ocid1.instance.test\n"
                    "[all:vars]\ncluster_name=" + CLUSTER_NAME
                    + "\ncluster_network=" + cluster_network
                    + "\ncompute_cluster=" + compute_cluster + "\ncompute_username=opc\n",
                    encoding="utf-8",
                )
                self.clear_events()
                result = self.run_configure()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.operations(),
                    ["prepare", "wait", "hostname", "sync", "configure", "monitoring"],
                )
                events = self.events()
                self.assertIn("initial-node", events[2]["inventory"])
                self.assertIn("final-node", events[4]["inventory"])
                self.assertNotIn("initial-node", events[4]["inventory"])
                self.assertIs(events[4]["extra_vars"]["autoscaling_names_prepared"], True)
                self.assertEqual(events[3]["stage"], "sync")
                self.assertEqual(events[4]["stage"], "configure")
                self.assertEqual(events[5]["stage"], "monitoring")
                self.assertFalse(self.stage.exists())
                self.assertFalse(self.journal.exists())

    def test_prepare_failure_never_reaches_hostname_or_slurm(self):
        result = self.run_configure(fail_operation="prepare")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), ["prepare"])
        self.assertFalse(self.stage.exists())

    def test_ssh_failure_never_reaches_hostname_or_slurm(self):
        result = self.run_configure(fail_operation="wait")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), ["prepare", "wait"])
        self.assertFalse(self.stage.exists())

    def test_hostname_failure_never_updates_oci_or_starts_slurm(self):
        result = self.run_configure(fail_operation="hostname")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), ["prepare", "wait", "hostname"])
        self.assertFalse(self.stage.exists())

    def test_sync_failure_resumes_immutable_plan_then_completes_configuration(self):
        result = self.run_configure(fail_operation="sync")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), ["prepare", "wait", "hostname", "sync"])
        self.assertEqual(self.saved_stage(), "sync")
        self.assertTrue(self.journal.exists())
        self.clear_events()
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["prepare", "wait", "sync", "configure", "monitoring"])
        self.assertIn("final-node", self.events()[3]["inventory"])
        self.assertFalse(self.stage.exists())

    def test_sync_stage_without_journal_still_continues_with_configuration(self):
        # A process can stop after deleting the OCI journal but before recording
        # the next stage. The shell marker must not be mistaken for legacy work.
        self.write_stage("sync")
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["prepare", "wait", "sync", "configure", "monitoring"])

    def test_configuration_failure_retries_only_remaining_configuration(self):
        result = self.run_configure(fail_operation="configure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.operations(), ["prepare", "wait", "hostname", "sync", "configure"]
        )
        self.assertEqual(self.saved_stage(), "configure")
        self.clear_events()
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["prepare", "wait", "configure", "monitoring"])
        self.assertIn("final-node", self.events()[2]["inventory"])
        self.assertFalse(self.stage.exists())

    def test_monitoring_failure_does_not_repeat_node_configuration(self):
        result = self.run_configure(fail_operation="monitoring")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.saved_stage(), "monitoring")
        self.clear_events()
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["monitoring"])
        self.assertFalse(self.stage.exists())

    def test_legacy_pending_journal_resumes_only_sync_and_monitoring(self):
        self.journal.write_text('{"fixture": "legacy"}\n', encoding="utf-8")
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["sync", "monitoring"])
        self.assertFalse(self.stage.exists())
        self.assertFalse(self.journal.exists())

    def test_legacy_sync_failure_never_runs_new_node_configuration_on_retry(self):
        self.journal.write_text('{"fixture": "legacy"}\n', encoding="utf-8")
        result = self.run_configure(fail_operation="sync")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), ["sync"])
        self.assertEqual(self.saved_stage(), "legacy-sync")
        self.clear_events()
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["sync", "monitoring"])
        self.assertFalse(self.stage.exists())

    def test_legacy_monitoring_failure_resumes_without_configuring_nodes(self):
        self.journal.write_text('{"fixture": "legacy"}\n', encoding="utf-8")
        result = self.run_configure(fail_operation="monitoring")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), ["sync", "monitoring"])
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.saved_stage(), "monitoring")
        self.clear_events()
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["monitoring"])
        self.assertFalse(self.stage.exists())

    def test_legacy_phase_commit_failure_preserves_recovery_after_journal_removed(self):
        self.journal.write_text('{"fixture": "legacy"}\n', encoding="utf-8")
        result = self.run_configure(fail_stage="monitoring")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), ["sync"])
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.saved_stage(), "legacy-sync")
        self.clear_events()
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["sync", "monitoring"])
        self.assertFalse(self.stage.exists())

    def test_legacy_initial_commit_failure_does_not_begin_sync(self):
        self.journal.write_text('{"fixture": "legacy"}\n', encoding="utf-8")
        result = self.run_configure(fail_stage="legacy-sync")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), [])
        self.assertTrue(self.journal.exists())
        self.assertFalse(self.stage.exists())

    def test_replaced_instance_cannot_resume_an_old_configuration_stage(self):
        self.write_stage("configure")
        self.inventory.write_text(
            self.inventory.read_text().replace(
                "ocid1.instance.test", "ocid1.instance.replacement"
            ), encoding="utf-8",
        )
        result = self.run_configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.saved_stage(), "configure")

    def test_invalid_or_empty_stage_cannot_skip_name_sync(self):
        for stage in ("", "unknown\n", "configure\nmonitoring\n"):
            with self.subTest(stage=stage):
                self.stage.write_text(stage, encoding="utf-8")
                self.stage.chmod(0o600)
                self.clear_events()
                result = self.run_configure()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.operations(), [])
                self.assertEqual(self.stage.read_text(), stage)

    def test_atomic_stage_commit_failure_stops_before_next_operation(self):
        cases = (
            ("sync", None, ["prepare", "wait", "hostname"]),
            ("configure", "sync", ["prepare", "wait", "hostname", "sync"]),
            (
                "monitoring", "configure",
                ["prepare", "wait", "hostname", "sync", "configure"],
            ),
        )
        for failed_stage, previous_stage, expected_operations in cases:
            with self.subTest(failed_stage=failed_stage):
                self.stage.unlink(missing_ok=True)
                self.clear_events()
                result = self.run_configure(fail_stage=failed_stage)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.operations(), expected_operations)
                if previous_stage is None:
                    self.assertFalse(self.stage.exists())
                else:
                    self.assertEqual(self.saved_stage(), previous_stage)

    def test_non_autoscaling_inventory_keeps_single_full_playbook(self):
        self.inventory.write_text(
            "[compute_to_add]\ninitial-node ansible_host=192.0.2.10\n"
            "[all:vars]\ncompute_username=opc\n", encoding="utf-8"
        )
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["prepare", "wait", "configure"])
        self.assertNotIn("extra_vars", self.events()[2])
        self.assertFalse(self.stage.exists())

    def test_missing_variables_keeps_non_autoscaling_behavior(self):
        self.variables.unlink()
        result = self.run_configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.operations(), ["prepare", "wait", "configure"])
        self.assertNotIn("extra_vars", self.events()[2])
        self.assertFalse(self.stage.exists())


class InitialHostnamePlaybookTests(unittest.TestCase):
    @staticmethod
    def read_playbook(filename):
        return yaml.safe_load(
            (REPOSITORY_ROOT / "playbooks" / filename).read_text(encoding="utf-8")
        )

    @staticmethod
    def evaluate_when(expression, **variables):
        environment = Environment(undefined=StrictUndefined)
        # These cases intentionally use Boolean inputs, matching the JSON
        # extra-vars passed by configure_as.sh, rather than emulating Ansible's
        # additional string-to-Boolean conversions.
        environment.filters["bool"] = bool
        return environment.compile_expression(expression)(**variables)

    @classmethod
    def role_names(cls, document):
        names = []
        if isinstance(document, list):
            for item in document:
                names.extend(cls.role_names(item))
        elif isinstance(document, dict):
            for key, value in document.items():
                if key == "roles":
                    for role in value:
                        names.append(role if isinstance(role, str) else role["role"])
                elif key in (
                    "include_role", "import_role",
                    "ansible.builtin.include_role", "ansible.builtin.import_role",
                ):
                    names.append(value["name"])
                else:
                    names.extend(cls.role_names(value))
        return names

    def test_full_and_lite_playbooks_prepare_names_only_when_not_already_prepared(self):
        for filename in ("new_nodes.yml", "lite_new_nodes.yml"):
            with self.subTest(playbook=filename):
                plays = self.read_playbook(filename)
                preparation = plays[0]
                self.assertEqual(preparation["import_playbook"], "new_nodes_hostname.yml")
                self.assertEqual(
                    sum(play.get("import_playbook") == "new_nodes_hostname.yml" for play in plays),
                    1,
                )
                self.assertTrue(self.evaluate_when(preparation["when"]))
                self.assertTrue(self.evaluate_when(
                    preparation["when"], autoscaling_names_prepared=False
                ))
                self.assertFalse(self.evaluate_when(
                    preparation["when"], autoscaling_names_prepared=True
                ))

    def test_common_preparation_sets_compute_names_in_order_with_slurm_condition(self):
        plays = self.read_playbook("new_nodes_hostname.yml")
        self.assertTrue(plays)
        self.assertTrue(all(play["hosts"] == "compute" for play in plays))
        self.assertEqual(self.role_names(plays), ["oci-hostname", "hostname"])
        hostname_tasks = [
            task for play in plays for task in play.get("tasks", [])
            if task.get("include_role", {}).get("name") == "hostname"
        ]
        self.assertEqual(len(hostname_tasks), 1)
        condition = hostname_tasks[0]["when"]
        self.assertFalse(self.evaluate_when(condition))
        self.assertFalse(self.evaluate_when(condition, slurm=False))
        self.assertTrue(self.evaluate_when(condition, slurm=True))

    def test_remaining_configuration_cannot_reapply_hostname_roles(self):
        for filename in ("new_nodes.yml", "lite_new_nodes.yml"):
            with self.subTest(playbook=filename):
                remaining_plays = self.read_playbook(filename)[1:]
                self.assertTrue(remaining_plays)
                self.assertTrue(
                    {"oci-hostname", "hostname"}.isdisjoint(self.role_names(remaining_plays))
                )


if __name__ == "__main__":
    unittest.main()
