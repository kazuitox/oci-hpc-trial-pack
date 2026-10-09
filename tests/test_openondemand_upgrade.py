import base64
import concurrent.futures
import getpass
import grp
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "playbooks/roles/openondemand"
spec = importlib.util.spec_from_file_location(
    "ood_secret", ROLE / "files/ood-oidc-passphrase.py"
)
secret = importlib.util.module_from_spec(spec)
spec.loader.exec_module(secret)


class OpenOnDemandUpgradeTests(unittest.TestCase):
    def test_real_ansible_secret_tasks_reuse_secret_without_logging_it(self):
        ansible = shutil.which("ansible-playbook")
        if not ansible:
            sibling = Path(sys.executable).with_name("ansible-playbook")
            ansible = str(sibling) if sibling.exists() else None
        if not ansible:
            self.skipTest("ansible-playbook is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            text = (ROLE / "tasks/oidc_passphrase.yml").read_text().replace(
                "/etc/ood/config", str(config))
            tasks = yaml.safe_load(text)
            for task in tasks:
                if "ansible.builtin.file" in task:
                    task["ansible.builtin.file"]["owner"] = getpass.getuser()
                    task["ansible.builtin.file"]["group"] = grp.getgrgid(os.getgid()).gr_name
                if "ansible.builtin.script" in task:
                    module = task["ansible.builtin.script"]
                    module["cmd"] = module["cmd"].replace(
                        "ood-oidc-passphrase.py", str(ROLE / "files/ood-oidc-passphrase.py"))
                    module["executable"] = sys.executable
            play = Path(directory) / "play.yml"
            play.write_text(yaml.safe_dump([{
                "hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {"ansible_python_interpreter": sys.executable}, "tasks": tasks}]))
            env = dict(os.environ, ANSIBLE_LOCAL_TEMP=str(Path(directory) / "ansible"))
            outputs = []
            for _ in range(2):
                result = subprocess.run([ansible, "-i", "localhost,", str(play)],
                                        env=env, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                value = (config / ".oidc_crypto_passphrase").read_text().strip()
                self.assertNotIn(value, result.stdout + result.stderr)
                outputs.append(value)
            self.assertEqual(outputs[0], outputs[1])

    def test_secret_is_random_persistent_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passphrase"
            self.assertTrue(secret.ensure_passphrase(path))
            value = path.read_text()
            self.assertRegex(value, r"^[0-9a-f]{64}\n$")
            self.assertFalse(secret.ensure_passphrase(path))
            self.assertEqual(path.read_text(), value)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            other = Path(directory) / "other"
            secret.ensure_passphrase(other)
            self.assertNotEqual(other.read_text(), value)

    def test_parallel_creation_preserves_one_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passphrase"
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: secret.ensure_passphrase(path), range(8)))
            self.assertEqual(sum(results), 1)
            self.assertRegex(path.read_text(), r"^[0-9a-f]{64}\n$")

    def test_invalid_secret_is_not_silently_rotated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passphrase"
            for invalid in ("", "default", "broken\n"):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    secret.ensure_passphrase(path)
                self.assertEqual(path.read_text(), invalid)

    def test_secret_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "passphrase"
            target = Path(directory) / "target"
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                secret.ensure_passphrase(path)
            self.assertFalse(target.exists())

    def test_portal_renders_secret_and_authentication_modes(self):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters["bool"] = bool
        env.filters["b64decode"] = lambda value: base64.b64decode(value).decode()
        template = env.from_string((ROLE / "templates/ood_portal.yml.j2").read_text())
        values = {
            "ood_servername": "ood.example.invalid",
            "ood_oidc_passphrase": {"content": base64.b64encode(("a" * 64 + "\n").encode()).decode()},
            "ood_oidc_remote_user_claim": "email",
            "ood_user_map_cmd": "/usr/local/sbin/ood-user-map",
            "ood_oidc_ssl_validate_server": "On",
            "ood_tls_cert": "/test/cert", "ood_tls_key": "/test/key",
            "ood_effective_ldap_host": "controller.cluster:636",
            "ood_ldap_insecure_skip_verify": False,
            "ood_ldap_root_ca": "/test/ca",
            "ood_ldap_anonymous_bind": True,
            "ood_ldap_user_base_dn": "dc=local",
            "ood_ldap_user_filter": "(objectClass=posixAccount)",
            "ood_ldap_username_attr": "uid", "ood_ldap_id_attr": "uid",
            "ood_ldap_email_attr": "uid", "ood_ldap_name_attr": "gecos",
            "ood_ldap_preferred_username_attr": "uid",
            "ood_dex_static_email": "test@example.invalid",
            "ood_dex_static_password_hash_effective": "dummy-hash",
            "ood_dex_static_username": "test", "ood_dex_static_user_id": "test",
            "ood_host_regex": "compute-.*",
        }
        for ldap, static in ((True, False), (False, True), (True, True)):
            with self.subTest(ldap=ldap, static=static):
                config = yaml.safe_load(template.render(
                    **values, ood_enable_dex_ldap=ldap, ood_dex_static_enabled=static))
                self.assertEqual(config["oidc_crypto_passphrase"], "a" * 64)
                self.assertEqual("connectors" in config["dex"], ldap)
                self.assertEqual("static_passwords" in config["dex"], static)

    def test_package_upgrade_and_generator_failures_are_enforced(self):
        for os, package, dex in (("el8", "ondemand-4.2.5-1.el8", "ondemand-dex-2.45.1-1.el8"),
                                 ("ubuntu24", "ondemand=4.2.5", "ondemand-dex=2.45.1")):
            with self.subTest(os=os):
                text = (ROLE / f"tasks/{os}.yml").read_text()
                tasks = yaml.safe_load(text)[0]["block"]
                install = next(task for task in tasks if task.get("register") == "ood_package_install")
                module = install.get("ansible.builtin.dnf", install.get("ansible.builtin.apt"))
                self.assertIn(package, module["name"])
                self.assertIn(dex, module["name"])
                self.assertNotIn("/ondemand/4.0/", text)
                self.assertNotIn("ExecStartPre=-", text)
                self.assertNotIn("ExecReload=-", text)
                self.assertIn("oidc_passphrase.yml", text)
                self.assertIn("opencomposer.yml", text)
                self.assertIn("finish_upgrade.yml", text)
                portal = next(task for task in tasks if task.get("name") == "Deploy ood_portal.yml for Dex login")
                self.assertEqual(portal["ansible.builtin.template"]["mode"], "0600")
                self.assertTrue(portal["no_log"])
        self.assertIn("nodejs:22", (ROLE / "tasks/el8.yml").read_text())

    def test_restart_is_retried_until_upgrade_marker_is_written(self):
        tasks = yaml.safe_load((ROLE / "tasks/finish_upgrade.yml").read_text())
        completion = tasks[-1]
        self.assertIn("ood_completed_version.content", completion["when"])
        block = completion["block"]
        self.assertIn("nginx_clean -f", block[-2]["ansible.builtin.command"])
        self.assertEqual(block[-1]["ansible.builtin.copy"]["content"], "4.2.5\n")

    def test_standard_dashboard_code_is_not_overwritten(self):
        tasks = (ROLE / "tasks/ood_dashboard_customizations.yml").read_text()
        self.assertNotIn("dashboard/app/apps/ood_app.rb", tasks)
        self.assertFalse((ROLE / "files/var/www/ood/apps/sys/dashboard/app/apps/ood_app.rb").exists())
        locale = yaml.safe_load((ROLE / "files/etc/ood/config/locales/en.yml").read_text())
        self.assertEqual(locale["en"]["dashboard"]["home_directory"], "ホームディレクトリ")


if __name__ == "__main__":
    unittest.main()
