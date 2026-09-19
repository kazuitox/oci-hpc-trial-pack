"""Run the real hyperthreading role with isolated Ansible action plugins.

Set ANSIBLE_PLAYBOOK_BINARY to an ansible-playbook executable to run these
optional integration tests. Only module action names in a temporary role copy
are redirected: conditions, loop handling, registered results, task order, and
module arguments are evaluated by Ansible itself. A connection plugin rejects
all remote execution and file transfer, so no real services or sysfs change.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
ROLE = REPOSITORY_ROOT / "playbooks/roles/hyperthreading"
SCRIPT_NAMES = ("control_hyperthreading.sh", "control_hyperthreading_ubuntu.sh")
UNIT_NAMES = ("disable-hyperthreading.service", "disable-hyperthreading_ubuntu.service")


ACTION_PLUGIN = r'''
import hashlib
import json
import os
from pathlib import Path
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        kind = self._task.action.rsplit(".", 1)[-1].removeprefix("fixture_")
        args = dict(self._task.args)
        fixture = json.loads(Path(os.environ["HT_ROLE_FIXTURE"]).read_text())
        record = {"kind": kind, "args": args, "task": self._task.get_name()}
        result = {"changed": False}
        if kind == "command":
            if args != {"argv": ["systemd-detect-virt", "--vm"]}:
                raise AssertionError("unexpected command: " + repr(args))
            result.update(rc=fixture["rc"], stdout=fixture["stdout"], stderr="")
        elif kind == "stat":
            result["stat"] = {"exists": args["path"] in fixture["existing"]}
        elif kind == "copy":
            source = Path(self._task._role._role_path) / "files" / args["src"]
            record["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            result["changed"] = True
        elif kind in ("file", "systemd"):
            result["changed"] = True
        else:
            raise AssertionError("unexpected fixture action: " + kind)
        with Path(os.environ["HT_ROLE_TRACE"]).open("a") as trace:
            trace.write(json.dumps(record) + "\n")
        return result
'''


CONNECTION_PLUGIN = r'''
from ansible.errors import AnsibleError
from ansible.plugins.connection import ConnectionBase


class Connection(ConnectionBase):
    transport = "fixture_noexec"
    has_pipelining = False

    def _connect(self):
        return self

    def exec_command(self, *args, **kwargs):
        raise AnsibleError("hyperthreading role test forbids command execution")

    def put_file(self, *args, **kwargs):
        raise AnsibleError("hyperthreading role test forbids file upload")

    def fetch_file(self, *args, **kwargs):
        raise AnsibleError("hyperthreading role test forbids file download")

    def close(self):
        self._connected = False
'''


class HyperthreadingRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("ANSIBLE_PLAYBOOK_BINARY")
        if not configured:
            raise unittest.SkipTest("set ANSIBLE_PLAYBOOK_BINARY to run real Ansible role tests")
        cls.ansible = shutil.which(configured)
        if not cls.ansible:
            raise AssertionError("ANSIBLE_PLAYBOOK_BINARY does not identify an executable")

    def run_role(self, *, vm=True, hyperthreading=False, ubuntu=False,
                 existing=(), detection=None, shape=None):
        with tempfile.TemporaryDirectory(prefix="hyperthreading-role-") as temporary:
            root = Path(temporary)
            copied_role = root / "roles/hyperthreading"
            shutil.copytree(ROLE, copied_role)
            actions = root / "action_plugins"
            connections = root / "connection_plugins"
            actions.mkdir()
            connections.mkdir()
            for name in ("command", "stat", "copy", "file", "systemd"):
                (actions / f"fixture_{name}.py").write_text(ACTION_PLUGIN, encoding="utf-8")
            (connections / "fixture_noexec.py").write_text(CONNECTION_PLUGIN, encoding="utf-8")
            # Replace only module names. No conditions or expressions are
            # interpreted or reproduced by this test harness.
            for path in (copied_role / "tasks").glob("*.yml"):
                source = path.read_text(encoding="utf-8")
                source = re.sub(
                    r"^(\s*)(?:ansible\.builtin\.)?(command|stat|copy|file|systemd):",
                    r"\1fixture_\2:", source, flags=re.MULTILINE,
                )
                path.write_text(source, encoding="utf-8")
            fixture_path = root / "fixture.json"
            rc, stdout = detection if detection is not None else ((0, "kvm") if vm else (1, "none"))
            fixture_path.write_text(json.dumps({"rc": rc, "stdout": stdout, "existing": list(existing)}))
            trace_path = root / "trace.jsonl"
            trace_path.write_text("")
            variables = {
                "ansible_connection": "fixture_noexec",
                "ansible_os_family": "Debian" if ubuntu else "RedHat",
                "ansible_distribution": "Ubuntu" if ubuntu else "OracleLinux",
            }
            if hyperthreading is not None:
                variables["hyperthreading"] = hyperthreading
            if shape is not None:
                variables["shape"] = shape
            playbook = root / "playbook.json"
            playbook.write_text(json.dumps([{
                "hosts": "all", "gather_facts": False, "vars": variables,
                "roles": ["hyperthreading"],
            }]))
            config = root / "ansible.cfg"
            config.write_text(
                "[defaults]\n"
                f"roles_path = {root / 'roles'}\n"
                f"action_plugins = {actions}\n"
                f"connection_plugins = {connections}\n"
                "retry_files_enabled = False\n"
                "host_key_checking = False\n"
                "[privilege_escalation]\nbecome = False\n"
            )
            environment = os.environ.copy()
            environment.update({
                "ANSIBLE_CONFIG": str(config),
                "ANSIBLE_LOCAL_TEMP": str(root / "ansible-tmp"),
                "ANSIBLE_NOCOLOR": "1",
                "HT_ROLE_FIXTURE": str(fixture_path),
                "HT_ROLE_TRACE": str(trace_path),
            })
            result = subprocess.run(
                [self.ansible, "-i", "fixture,", str(playbook)], cwd=root,
                env=environment, capture_output=True, text=True, check=False,
                timeout=45,
            )
            records = [json.loads(line) for line in trace_path.read_text().splitlines()]
            return result, records

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutations(self, records):
        return [record for record in records if record["kind"] in ("copy", "file", "systemd")]

    def test_fresh_vms_do_not_install_or_start_guest_controls(self):
        for ubuntu in (False, True):
            for hyperthreading in (False, True):
                with self.subTest(ubuntu=ubuntu, hyperthreading=hyperthreading):
                    result, records = self.run_role(ubuntu=ubuntu, hyperthreading=hyperthreading)
                    self.assert_success(result)
                    self.assertEqual(self.mutations(records), [])
                    self.assertEqual(records[0]["kind"], "command")

    def test_existing_vm_controls_are_guarded_then_disabled_without_stopping(self):
        existing = [f"/opt/oci-hpc/sbin/{name}" for name in SCRIPT_NAMES]
        existing += [f"/etc/systemd/system/{name}" for name in UNIT_NAMES]
        for hyperthreading in (False, True):
            with self.subTest(hyperthreading=hyperthreading):
                result, records = self.run_role(existing=existing, hyperthreading=hyperthreading)
                self.assert_success(result)
                mutations = self.mutations(records)
                self.assertEqual([record["kind"] for record in mutations],
                                 ["copy", "copy", "systemd", "systemd"])
                for record, name in zip(mutations[:2], SCRIPT_NAMES):
                    self.assertEqual(record["args"], {
                        "src": name, "dest": f"/opt/oci-hpc/sbin/{name}", "mode": "0755",
                    })
                    expected = hashlib.sha256((ROLE / "files" / name).read_bytes()).hexdigest()
                    self.assertEqual(record["source_sha256"], expected)
                self.assertEqual([record["args"] for record in mutations[2:]],
                                 [{"name": name, "enabled": False} for name in UNIT_NAMES])

    def test_vm_updates_only_files_and_units_that_already_exist(self):
        existing = ("/opt/oci-hpc/sbin/control_hyperthreading_ubuntu.sh",
                    "/etc/systemd/system/disable-hyperthreading_ubuntu.service")
        result, records = self.run_role(ubuntu=True, existing=existing)
        self.assert_success(result)
        mutations = self.mutations(records)
        self.assertEqual([record["kind"] for record in mutations], ["copy", "systemd"])
        self.assertEqual(mutations[0]["args"]["src"], "control_hyperthreading_ubuntu.sh")
        self.assertEqual(mutations[1]["args"], {"name": "disable-hyperthreading_ubuntu.service", "enabled": False})

    def test_bare_metal_disable_retains_existing_el_and_ubuntu_services(self):
        for ubuntu, script, service in (
            (False, "control_hyperthreading.sh", "disable-hyperthreading.service"),
            (True, "control_hyperthreading_ubuntu.sh", "disable-hyperthreading_ubuntu.service"),
        ):
            with self.subTest(ubuntu=ubuntu):
                result, records = self.run_role(vm=False, ubuntu=ubuntu)
                self.assert_success(result)
                mutations = self.mutations(records)
                copies = [record["args"]["src"] for record in mutations if record["kind"] == "copy"]
                self.assertIn(script, copies)
                self.assertIn(service, copies)
                self.assertEqual(mutations[-1]["args"], {"name": service, "state": "started", "enabled": True})
                self.assertFalse(any(record["kind"] == "stat" for record in records))

    def test_bare_metal_hyperthreading_enabled_does_not_modify_services(self):
        for ubuntu in (False, True):
            with self.subTest(ubuntu=ubuntu):
                result, records = self.run_role(vm=False, hyperthreading=True, ubuntu=ubuntu)
                self.assert_success(result)
                self.assertEqual(self.mutations(records), [])

    def test_undefined_hyperthreading_does_not_disable_bare_metal_cpus(self):
        result, records = self.run_role(vm=False, hyperthreading=None)
        self.assert_success(result)
        self.assertEqual(self.mutations(records), [])

    def test_actual_machine_detection_takes_precedence_over_inventory_shape(self):
        result, records = self.run_role(vm=True, shape="BM.Standard.E4.128")
        self.assert_success(result)
        self.assertEqual(self.mutations(records), [])
        result, records = self.run_role(vm=False, shape="VM.Standard.E6.Flex")
        self.assert_success(result)
        self.assertTrue(any(record["kind"] == "systemd" and record["args"].get("state") == "started"
                            for record in records))

    def test_failed_or_inconsistent_machine_detection_cannot_mutate_state(self):
        for detection in ((127, ""), (2, "error"), (1, ""), (1, "kvm"), (0, ""), (0, "none")):
            with self.subTest(detection=detection):
                result, records = self.run_role(detection=detection)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([record["kind"] for record in records], ["command"])
                self.assertEqual(self.mutations(records), [])


if __name__ == "__main__":
    unittest.main()
