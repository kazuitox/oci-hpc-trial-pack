import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
EL_SCRIPT = (
    REPOSITORY_ROOT
    / "playbooks/roles/hyperthreading/files/control_hyperthreading.sh"
)
UBUNTU_SCRIPT = (
    REPOSITORY_ROOT
    / "playbooks/roles/hyperthreading/files/control_hyperthreading_ubuntu.sh"
)


class EnterpriseLinuxHyperthreadingTests(unittest.TestCase):
    def run_control(self, siblings, action, offline_cpus=(), blocked_cpu=None):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            cpu_root = temporary_path / "cpu"
            commands_path = temporary_path / "bin"
            commands_path.mkdir()
            for cpu, sibling_list in enumerate(siblings):
                cpu_path = cpu_root / f"cpu{cpu}"
                topology = cpu_path / "topology"
                topology.mkdir(parents=True)
                (topology / "thread_siblings_list").write_text(
                    sibling_list + "\n", encoding="utf-8"
                )
                online = cpu_path / "online"
                if cpu == blocked_cpu:
                    # A directory causes an actual redirection failure even
                    # when the tests run as root.
                    online.mkdir()
                elif cpu != 0:
                    online.write_text(
                        "0\n" if cpu in offline_cpus else "1\n", encoding="utf-8"
                    )
            commands = {
                "id": "printf '0\\n'\n",
                "lscpu": "printf 'On-line CPU(s) list: fixture\\n'\n",
            }
            for name, body in commands.items():
                executable = commands_path / name
                executable.write_text("#!/bin/bash\n" + body, encoding="utf-8")
                executable.chmod(0o755)
            # Redirect sysfs to real files and replace the script's fixed PATH
            # so the root check and final display are safe to run on any host.
            source = EL_SCRIPT.read_text(encoding="utf-8")
            source = source.replace("/sys/devices/system/cpu", str(cpu_root))
            source = "\n".join(
                f'PATH="{commands_path}:$PATH"' if line.startswith("PATH=") else line
                for line in source.splitlines()
            )
            script = temporary_path / "control_hyperthreading.sh"
            script.write_text(source + "\n", encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", str(script), action],
                capture_output=True,
                text=True,
                check=False,
            )
            online_states = {}
            for cpu in range(len(siblings)):
                online = cpu_root / f"cpu{cpu}" / "online"
                if online.is_file():
                    online_states[cpu] = online.read_text(encoding="utf-8").strip()
            return result, online_states

    def test_disabling_keeps_one_logical_cpu_per_core(self):
        cases = [
            (
                "adjacent ranges",
                ["0-1", "0-1", "2-3", "2-3", "4-5", "4-5", "6-7", "6-7"],
                {1, 3, 5, 7},
            ),
            (
                "separated IDs",
                ["0,4", "1,5", "2,6", "3,7", "0,4", "1,5", "2,6", "3,7"],
                {4, 5, 6, 7},
            ),
            ("four adjacent threads", ["0-3"] * 4, {1, 2, 3}),
            ("four separated threads", ["0,1,2,3"] * 4, {1, 2, 3}),
            (
                "mixed IDs and ranges",
                ["0-1,4,5", "0-1,4,5", "2-3,6-7", "2-3,6-7"] * 2,
                {1, 3, 4, 5, 6, 7},
            ),
        ]
        for name, siblings, offline_cpus in cases:
            for action in ["off", "0"]:
                with self.subTest(topology=name, action=action):
                    result, online_states = self.run_control(siblings, action)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(
                        online_states,
                        {
                            cpu: "0" if cpu in offline_cpus else "1"
                            for cpu in range(1, len(siblings))
                        },
                    )

    def test_single_threaded_guest_is_a_no_op(self):
        result, online_states = self.run_control(["0", "1", "2", "3"], "off")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(online_states, {1: "1", 2: "1", 3: "1"})

    def test_enabling_restores_offline_cpus_without_cpu_zero_online_file(self):
        for action in ["on", "1"]:
            with self.subTest(action=action):
                result, online_states = self.run_control(
                    ["0-1", "0-1", "2-3", "2-3"], action, offline_cpus={1, 3}
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(online_states, {1: "1", 2: "1", 3: "1"})

    def test_show_does_not_change_online_cpus(self):
        result, online_states = self.run_control(
            ["0-1", "0-1", "2-3", "2-3"], "show", offline_cpus={1, 3}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(online_states, {1: "0", 2: "1", 3: "0"})

    def test_failed_write_stops_and_is_reported_to_the_service(self):
        result, online_states = self.run_control(
            ["0-1", "0-1", "2-3", "2-3"], "off", blocked_cpu=1
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/cpu1/online", result.stderr)
        self.assertEqual(online_states, {2: "1", 3: "1"})


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
