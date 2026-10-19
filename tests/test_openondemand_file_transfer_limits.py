import getpass
import grp
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "playbooks/roles/openondemand"


class FileTransferLimitTests(unittest.TestCase):
    def test_portal_limits_only_files_requests(self):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters["bool"] = bool
        env.filters["b64decode"] = lambda value: "dummy-passphrase"
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        config = yaml.safe_load(env.from_string(
            (ROLE / "templates/ood_portal.yml.j2").read_text()).render(
                **dict(defaults, ood_servername="ood.example.invalid",
                       ood_oidc_passphrase={"content": "dummy"},
                       ood_enable_dex_ldap=False, ood_dex_static_enabled=False)))
        self.assertEqual(config["custom_vhost_directives"], [
            '<Location "/pun/sys/dashboard/files">',
            '  LimitRequestBody 0', '</Location>'])
        self.assertEqual(defaults["ood_file_transfer_max_bytes"], 50_000_000_000)

    def test_both_os_share_limits_and_disabled_ood_is_guarded(self):
        for name in ("el8.yml", "ubuntu24.yml"):
            tasks = yaml.safe_load((ROLE / "tasks" / name).read_text())[0]["block"]
            self.assertTrue(any(task.get("ansible.builtin.include_tasks") ==
                                "ood_dashboard_customizations.yml" for task in tasks))
        tasks = yaml.safe_load((ROLE / "tasks/ood_dashboard_customizations.yml").read_text())
        self.assertTrue(any(task.get("ansible.builtin.include_tasks") ==
                            "file_transfer_limits.yml" for task in tasks))
        for task in yaml.safe_load((ROLE / "tasks/main.yml").read_text()):
            self.assertIn("use_ood | default(false) | bool", task["when"])

    def test_real_ansible_preserves_settings_and_restarts_only_on_changes(self):
        ansible = shutil.which("ansible-playbook")
        if not ansible:
            sibling = Path(sys.executable).with_name("ansible-playbook")
            ansible = str(sibling) if sibling.exists() else None
        if not ansible:
            self.skipTest("ansible-playbook is unavailable")
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "config"
                dashboard = config / "apps/dashboard"
                calls = root / "calls"
                fail = root / "fail"
                if existing:
                    dashboard.mkdir(parents=True)
                    (config / "nginx_stage.yml").write_text(
                        "---\nnginx_file_upload_max: 10737420000\npun_custom_env:\n  KEEP_ME: yes\n")
                    (dashboard / "env").write_text(
                        "KEEP_ME=unchanged\nFILE_UPLOAD_MAX=123\n"
                        "OOD_FILE_DOWNLOAD_MAX=456\nOOD_DOWNLOAD_FILE_MAX=789\n"
                        "OOD_DOWNLOAD_DIR_MAX=1000\n")
                stub = root / "stage.py"
                stub.write_text("from pathlib import Path\nimport sys\n"
                                f"if Path({str(fail)!r}).exists(): sys.exit(1)\n"
                                f"with Path({str(calls)!r}).open('a') as f: "
                                "f.write(' '.join(sys.argv[1:]) + '\\n')\n")
                tasks = yaml.safe_load((ROLE / "tasks/file_transfer_limits.yml").read_text())
                for task in tasks:
                    for module in ("ansible.builtin.file", "ansible.builtin.lineinfile",
                                   "ansible.builtin.copy", "ansible.builtin.stat"):
                        if module in task:
                            opts = task[module]
                            key = "dest" if module == "ansible.builtin.copy" else "path"
                            opts[key] = opts[key].replace("/etc/ood/config", str(config))
                            if module != "ansible.builtin.stat":
                                opts["owner"] = getpass.getuser()
                                opts["group"] = grp.getgrgid(os.getgid()).gr_name
                    if "ansible.builtin.command" in task:
                        command = task["ansible.builtin.command"]
                        task["ansible.builtin.command"] = (
                            f"{sys.executable} {stub} " + Path(command.split()[0]).name +
                            " " + " ".join(command.split()[1:]))
                play = root / "play.yml"
                def run(limit, success=True):
                    play.write_text(yaml.safe_dump([{
                        "hosts": "localhost", "connection": "local", "gather_facts": False,
                        "vars": {"ansible_python_interpreter": sys.executable,
                                 "ood_file_transfer_max_bytes": limit}, "tasks": tasks}]))
                    result = subprocess.run([ansible, "-i", "localhost,", str(play)],
                        env=dict(os.environ, ANSIBLE_LOCAL_TEMP=str(root / "ansible"),
                                 ANSIBLE_REMOTE_TEMP=str(root / "remote")),
                        text=True, capture_output=True, timeout=60)
                    if success:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    return result.stdout
                # A failed activation leaves a marker; an unchanged retry must recover.
                fail.touch()
                run(defaults["ood_file_transfer_max_bytes"], success=False)
                self.assertTrue((config / ".oci-hpc-file-transfer-pending").exists())
                fail.unlink()
                for limit in (defaults["ood_file_transfer_max_bytes"], 60_000_000_000):
                    run(limit)
                    self.assertFalse((config / ".oci-hpc-file-transfer-pending").exists())
                    nginx = yaml.safe_load((config / "nginx_stage.yml").read_text())
                    self.assertEqual(nginx["nginx_file_upload_max"], limit + 1_048_576)
                    entries = dict(line.split("=", 1) for line in
                                   (dashboard / "env").read_text().splitlines())
                    for name in ("FILE_UPLOAD_MAX", "OOD_DOWNLOAD_FILE_MAX",
                                 "OOD_FILE_DOWNLOAD_MAX", "OOD_DOWNLOAD_DIR_MAX"):
                        self.assertEqual(int(entries[name]), limit)
                    if existing:
                        self.assertEqual(nginx["pun_custom_env"], {"KEEP_ME": True})
                        self.assertEqual(entries["KEEP_ME"], "unchanged")
                    before = calls.read_text()
                    self.assertIn("changed=0", run(limit))
                    self.assertEqual(calls.read_text(), before)
                self.assertEqual(calls.read_text().splitlines(), [
                    "update_nginx_stage --quiet", "nginx_stage nginx_clean -f",
                    "update_nginx_stage --quiet", "nginx_stage nginx_clean -f"])


if __name__ == "__main__":
    unittest.main()
