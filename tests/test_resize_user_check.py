import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
ID_STUB = '''#!/bin/bash
case "$1" in
  -u) if [ "${TEST_ID_FAIL-}" = uid ]; then exit 1; fi
      printf '%s\\n' "${TEST_ID_UID-1000}" ;;
  -un) if [ "${TEST_ID_FAIL-}" = name ]; then exit 1; fi
       printf '%s\\n' "${TEST_ID_NAME-ubuntu}" ;;
  *) exit 2 ;;
esac
'''


class ResizeUserCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.bin = self.root / "bin"
        self.commands = self.root / "commands"
        self.bin.mkdir()
        self.commands.mkdir()
        self.copy_resize(self.bin / "resize.sh")
        self.marker = self.root / "reached-python"
        self.write_command(self.commands / "id", ID_STUB)
        self.write_command(
            self.commands / "python3",
            '#!/bin/bash\nprintf "reached\\n" > "$TEST_SIDE_EFFECT"\n',
        )

    @staticmethod
    def write_command(path, content):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def copy_resize(self, destination):
        source = (ROOT / "bin/resize.sh").read_text(encoding="utf-8")
        # Only the id executable is replaced; execute the same shell guard.
        destination.write_text(source.replace("/usr/bin/id", str(self.commands / "id")), encoding="utf-8")
        destination.chmod(0o755)

    def run_resize(self, actual_user, user_var, uid="1000", id_fail=None):
        environment = dict(os.environ)
        environment["PATH"] = str(self.commands) + os.pathsep + os.environ["PATH"]
        environment["TEST_ID_UID"] = uid
        environment["TEST_ID_NAME"] = actual_user
        environment["TEST_SIDE_EFFECT"] = str(self.marker)
        if id_fail:
            environment["TEST_ID_FAIL"] = id_fail
        else:
            environment.pop("TEST_ID_FAIL", None)
        if user_var is None:
            environment.pop("USER", None)
        else:
            environment["USER"] = user_var
        return subprocess.run(
            ["/bin/bash", str(self.bin / "resize.sh")], env=environment,
            capture_output=True, text=True, check=False,
        )

    def test_allowed_identity_ignores_user_environment(self):
        for actual_user in ("ubuntu", "opc"):
            for user_var in (actual_user, None, "", "root", "with space"):
                with self.subTest(actual_user=actual_user, user_var=user_var):
                    self.marker.unlink(missing_ok=True)
                    result = self.run_resize(actual_user, user_var)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(self.marker.exists())
                    self.assertNotIn("unary operator expected", result.stderr)

    def test_rejected_identity_exits_before_following_command(self):
        for actual_user, uid, user_var in (
            ("root", "0", "ubuntu"), ("other", "1001", "opc"),
            ("with space", "1001", "ubuntu"),
        ):
            with self.subTest(actual_user=actual_user, uid=uid):
                self.marker.unlink(missing_ok=True)
                result = self.run_resize(actual_user, user_var, uid)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Run this script as opc or ubuntu", result.stderr)
                self.assertFalse(self.marker.exists())

    def test_unresolved_identity_exits_before_following_command(self):
        for name, uid, failure in (("ubuntu", "", None), ("", "1000", None),
                                   ("ubuntu", "1000", "uid"), ("ubuntu", "1000", "name")):
            with self.subTest(name=name, uid=uid, failure=failure):
                self.marker.unlink(missing_ok=True)
                result = self.run_resize(name, None, uid, failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Cannot determine the effective user", result.stderr)
                self.assertFalse(self.marker.exists())


class ConfigureResizeUserCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.bin = self.root / "bin"
        self.commands = self.root / "commands"
        self.cluster = self.root / "autoscaling/clusters/fixture-cluster"
        for path in (self.bin, self.commands, self.cluster):
            path.mkdir(parents=True)
        shutil.copy2(ROOT / "bin/configure_as.sh", self.bin / "configure_as.sh")
        source = (ROOT / "bin/resize.sh").read_text(encoding="utf-8")
        (self.bin / "resize.sh").write_text(
            source.replace("/usr/bin/id", str(self.commands / "id")), encoding="utf-8",
        )
        self.stage = self.cluster / ".initial-configure-stage"
        self.stage.write_text("fixture stage\n")
        (self.cluster / "inventory").write_text("cluster_network=false\n")
        (self.cluster / "variables.tf").write_text("# fixture\n")
        (self.cluster / "cluster_id").write_text("fixture-id\n")
        ResizeUserCheckTests.write_command(self.commands / "id", ID_STUB)
        ResizeUserCheckTests.write_command(
            self.commands / "python3",
            '#!/bin/bash\ncase "$2" in\n'
            '  read) printf "monitoring\\n" ;;\n'
            '  clear) rm "$TEST_STAGE" ;;\n'
            '  *) exit 2 ;;\n'
            'esac\n',
        )

    def test_monitoring_retry_propagates_allowed_and_rejected_identity(self):
        for actual_user, user_var, expected_success in (
            ("ubuntu", None, True), ("opc", "wrong value", True),
            ("other", "ubuntu", False), ("root", "opc", False),
        ):
            with self.subTest(actual_user=actual_user, user_var=user_var):
                environment = dict(os.environ)
                environment["PATH"] = str(self.commands) + os.pathsep + os.environ["PATH"]
                environment["TEST_STAGE"] = str(self.stage)
                environment["TEST_ID_NAME"] = actual_user
                environment["TEST_ID_UID"] = "0" if actual_user == "root" else "1000"
                if user_var is None:
                    environment.pop("USER", None)
                else:
                    environment["USER"] = user_var
                if not self.stage.exists():
                    self.stage.write_text("fixture stage\n")
                result = subprocess.run(
                    ["/bin/bash", str(self.bin / "configure_as.sh"), "fixture-cluster"],
                    env=environment, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode == 0, expected_success, result.stderr)
                self.assertEqual(self.stage.exists(), not expected_success)
                if not expected_success:
                    self.assertIn("Failed to reconcile compute monitoring", result.stderr)
