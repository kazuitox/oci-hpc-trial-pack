"""Ansible mock runs; set LOCALDISK_ANSIBLE_PLAYBOOK to opt in."""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

try:
    import yaml
except ImportError:
    yaml = None


ROLE = pathlib.Path(__file__).resolve().parents[1]
ANSIBLE = os.environ.get("LOCALDISK_ANSIBLE_PLAYBOOK")


@unittest.skipUnless(ANSIBLE and yaml, "ansible-playbook and PyYAML are required")
class AnsibleMockTests(unittest.TestCase):
    def run_case(self, host_role, initial, stage):
        with tempfile.TemporaryDirectory(prefix="localdisk-ansible-") as temporary:
            base = pathlib.Path(temporary)
            destination = base / "roles" / "localdisk"
            shutil.copytree(ROLE, destination, ignore=shutil.ignore_patterns("__pycache__"))
            script = destination / "files" / "localdisk.py"
            if stage in ("create", "format"):
                script.write_text("#!/usr/bin/env python3\nimport sys\n"
                                  "print('localdisk: {} /dev/nvme0n1 mocked failure', file=sys.stderr)\n"
                                  "sys.exit(1)\n".format("vgcreate" if stage == "create" else "mkfs.xfs"))
            else:
                result = {"changed": False, "actions": [], "unused": [],
                          "mounts": [{"path": "/unused-mock-path", "src": "/dev/vg_nvmes/lv_nvmes",
                                      "opts": "defaults,noatime,nofail"}]}
                script.write_text("#!/usr/bin/env python3\nprint({!r})\n".format(json.dumps(result)))
                tasks = yaml.safe_load((destination / "tasks" / "common.yml").read_text())
                mount_task = next(task for task in tasks[1]["block"]
                                  if task["name"] == "Mount local NVMe scratch")
                del mount_task["mount"]
                mount_task["fail"] = {"msg": "mount /dev/vg_nvmes/lv_nvmes mocked failure"}
                (destination / "tasks" / "common.yml").write_text(yaml.safe_dump(tasks, sort_keys=False))
            script.chmod(0o755)
            playbook = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                         "vars": {"ansible_devices": {"nvme0n1": {}, "nvme1n1": {}},
                                  "nvme_path": "/unused-mock-path", "role": host_role,
                                  "initial": initial, "log_vol": True, "redundancy": True,
                                  "ansible_facts": {"distribution": "OracleLinux"}},
                         "tasks": [{"include_role": {"name": "localdisk"}},
                                   {"debug": {"msg": "FOLLOWED"}}]}]
            playbook_path = base / "mock.yml"
            playbook_path.write_text(yaml.safe_dump(playbook, sort_keys=False))
            env = os.environ.copy()
            env.update({"ANSIBLE_ROLES_PATH": str(base / "roles"),
                        "ANSIBLE_COLLECTIONS_PATH": os.environ.get("LOCALDISK_ANSIBLE_COLLECTIONS", ""),
                        "ANSIBLE_HOME": str(base / "home"),
                        "ANSIBLE_LOCAL_TEMP": str(base / "local-tmp"),
                        "ANSIBLE_REMOTE_TEMP": str(base / "remote-tmp"),
                        "ANSIBLE_NOCOLOR": "1"})
            result = subprocess.run([ANSIBLE, "-i", "localhost,", str(playbook_path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    universal_newlines=True, env=env, timeout=60)
            return result.returncode, result.stdout

    def test_autoscaling_compute_warns_and_continues_on_create_format_mount(self):
        for stage in ("create", "format", "mount"):
            with self.subTest(stage=stage):
                rc, output = self.run_case("compute", False, stage)
                self.assertEqual(rc, 0, output)
                self.assertIn("WARNING: localdisk failed", output)
                self.assertIn("FOLLOWED", output)
                self.assertIn("nvme0n1", output)
                self.assertIn("mocked failure", output)

    def test_initial_compute_and_other_hosts_still_fail(self):
        for host_role, initial in (("compute", True), ("controller", False), ("nfs", False)):
            with self.subTest(role=host_role, initial=initial):
                rc, output = self.run_case(host_role, initial, "create")
                self.assertNotEqual(rc, 0, output)
                self.assertIn("localdisk failed", output)
                self.assertNotIn("FOLLOWED", output)


if __name__ == "__main__":
    unittest.main()
