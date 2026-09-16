import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
UBUNTU_SCRIPT = (
    REPOSITORY_ROOT
    / "playbooks/roles/hyperthreading/files/control_hyperthreading_ubuntu.sh"
)


class UbuntuHyperthreadingTests(unittest.TestCase):
    def run_control(self, state, action, write_failure=False):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            writes = temporary_path / "writes"
            control = temporary_path / "control"
            if state is not None:
                control.write_text(state + "\n", encoding="utf-8")
            # Redirect only the sysfs path to a real temporary fixture so both
            # file-existence checks and state reads exercise the shell code.
            script = temporary_path / "control_hyperthreading_ubuntu.sh"
            script.write_text(
                UBUNTU_SCRIPT.read_text(encoding="utf-8").replace(
                    "/sys/devices/system/cpu/smt/control", str(control)
                ),
                encoding="utf-8",
            )
            commands = {
                "id": "printf '0\\n'\n",
                # Intercept the complete privileged write, never touching sysfs.
                "sudo": (
                    '[ "$1" = tee ] || exit 99\n'
                    '[ "$2" = "$TEST_SMT_CONTROL" ] || exit 99\n'
                    'read -r requested_state\n'
                    'printf "%s\\n" "$requested_state" >> "$TEST_SMT_WRITES"\n'
                    'case "$TEST_SMT_STATE" in\n'
                    'forceoff|notsupported|notimplemented) '
                    'echo "write rejected" >&2; exit 1;;\n'
                    'esac\n'
                    '[ "$TEST_SMT_WRITE_FAILURE" = 0 ] || exit 1\n'
                ),
                "lscpu": "printf 'On-line CPU(s) list: 0-3\\n'\n",
            }
            for name, body in commands.items():
                executable = temporary_path / name
                executable.write_text("#!/bin/bash\n" + body, encoding="utf-8")
                executable.chmod(0o755)
            environment = dict(os.environ)
            environment.update(
                PATH=str(temporary_path) + os.pathsep + os.environ["PATH"],
                TEST_SMT_STATE=state or "",
                TEST_SMT_CONTROL=str(control),
                TEST_SMT_WRITES=str(writes),
                TEST_SMT_WRITE_FAILURE="1" if write_failure else "0",
            )
            result = subprocess.run(
                ["/bin/bash", str(script), action],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            written_states = writes.read_text().splitlines() if writes.exists() else []
            return result, written_states

    def test_unavailable_guest_smt_never_attempts_a_sysfs_write(self):
        for state in ["notsupported", "forceoff", "notimplemented"]:
            # Service startup disables HT; stopping the service requests on.
            for action in ["off", "on"]:
                with self.subTest(state=state, action=action):
                    result, writes = self.run_control(state, action)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(writes, [])

    def test_kernel_without_smt_control_is_a_no_op(self):
        for action in ["off", "on"]:
            with self.subTest(action=action):
                result, writes = self.run_control(None, action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(writes, [])

    def test_already_requested_state_does_not_write(self):
        for state in ["off", "on"]:
            with self.subTest(state=state):
                result, writes = self.run_control(state, state)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(writes, [])

    def test_existing_guest_ht_toggle_remains_available(self):
        for state, action in [("on", "off"), ("off", "on")]:
            with self.subTest(state=state, action=action):
                result, writes = self.run_control(state, action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(writes, [action])

    def test_failed_write_is_reported_to_the_service(self):
        result, writes = self.run_control("on", "off", write_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(writes, ["off"])


if __name__ == "__main__":
    unittest.main()
