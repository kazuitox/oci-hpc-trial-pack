"""Exercise queues.conf validation and the real slurm_config.sh entry point.

Shell tests redirect only /etc/os-release to a fixture and intercept sudo and
Ansible. No service, Slurm configuration, or topology file is modified.
"""

import copy
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / "bin/validate_queues.py"
SLURM_CONFIG = ROOT / "bin/slurm_config.sh"


def instance(shape="VM.Standard.E6.Flex", hyperthreading=False, **extra):
    value = {
        "name": "example-type",
        "shape": shape,
        "instance_keyword": "example",
        "hyperthreading": hyperthreading,
    }
    value.update(extra)
    return value


def queue_config(*instances):
    return {"queues": [{"name": "compute", "instance_types": list(instances)}]}


class QueueValidationTests(unittest.TestCase):
    def run_validator(self, configuration=None, *, source=None):
        with tempfile.TemporaryDirectory(prefix="queues-validation-") as temporary:
            path = Path(temporary) / "queues.conf"
            path.write_text(
                source if source is not None else yaml.safe_dump(configuration),
                encoding="utf-8",
            )
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(path)],
                capture_output=True, text=True, check=False, timeout=15,
            )

    def assert_valid(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_invalid(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_supported_shapes_accept_both_hyperthreading_values(self):
        shapes = (
            "VM.Standard.E3.Flex", "VM.Standard.E4.Flex",
            "VM.Standard.E5.Flex", "VM.Standard.E6.Flex",
            "VM.Standard.E6.Ax.Flex", "VM.DenseIO.E4.Flex",
            "VM.DenseIO.E5.Flex", "VM.Standard.A1.Flex",
            "VM.Standard.A2.Flex", "BM.HPC2.36", "BM.Optimized3.36",
        )
        for shape in shapes:
            for hyperthreading in (True, False, "true", "false"):
                with self.subTest(shape=shape, hyperthreading=hyperthreading):
                    self.assert_valid(self.run_validator(
                        queue_config(instance(shape, hyperthreading))
                    ))

    def test_intel_and_unknown_vm_shapes_reject_ht_off_but_allow_ht_on(self):
        shapes = (
            "VM.Standard2.4", "VM.Standard3.Flex", "VM.Standard4.Ax.Flex",
            "VM.Optimized3.Flex", "VM.DenseIO2.8", "VM.GPU2.1",
            "VM.GPU3.1", "VM.GPU.A10.1", "VM.Unknown.Flex",
        )
        for shape in shapes:
            for hyperthreading in (False, "false"):
                with self.subTest(shape=shape, hyperthreading=hyperthreading):
                    result = self.run_validator(queue_config(instance(shape, hyperthreading)))
                    self.assert_invalid(result)
                    for context in ("compute", "example-type", shape):
                        self.assertIn(context, result.stderr)
            with self.subTest(shape=shape, hyperthreading=True):
                self.assert_valid(self.run_validator(queue_config(instance(shape, True))))

    def test_checks_nondefault_and_permanent_types_in_every_queue(self):
        configuration = queue_config(instance(default=True))
        configuration["queues"].append({
            "name": "secondary", "default": False,
            "instance_types": [instance(
                "VM.Optimized3.Flex", False, name="permanent-intel",
                instance_keyword="intel", default=False, permanent=True,
            )],
        })
        result = self.run_validator(configuration)
        self.assert_invalid(result)
        for context in ("secondary", "permanent-intel", "VM.Optimized3.Flex"):
            self.assertIn(context, result.stderr)

    def test_yaml_comments_quotes_aliases_and_boolean_spelling(self):
        self.assert_valid(self.run_validator(source="""
# instance_keyword: amd  This comment is not another instance.
queues:
  - name: compute
    instance_types:
      - &amd
        name: amd
        shape: 'VM.Standard.E6.Flex'
        instance_keyword: "amd"
        hyperthreading: off
      - <<: *amd
        name: amd-permanent
        instance_keyword: amd-permanent
        permanent: true
      - name: intel
        shape: VM.Standard3.Flex
        instance_keyword: intel
        hyperthreading: "true"
"""))

    def test_duplicate_keywords_are_checked_across_queues_and_formatting(self):
        result = self.run_validator(source="""
queues:
  - name: first
    instance_types:
      - {name: first-amd, shape: VM.Standard.E6.Flex, instance_keyword: same, hyperthreading: false}
      - {name: another-amd, shape: VM.Standard.E5.Flex, instance_keyword: other, hyperthreading: true}
  - name: second
    instance_types:
      - name: second-amd
        shape: VM.Standard.E6.Flex
        instance_keyword: 'same' # Equivalent value; different text.
        hyperthreading: false
""")
        self.assert_invalid(result)
        for context in ("same", "first", "second", "first-amd", "second-amd"):
            self.assertIn(context, result.stderr)

    def test_invalid_and_missing_hyperthreading_are_not_silently_defaulted(self):
        for value in (None, 0, 1, "", "disabled", "TRUE", "False", [], {}):
            with self.subTest(value=value):
                result = self.run_validator(queue_config(instance(hyperthreading=value)))
                self.assert_invalid(result)
                self.assertIn("hyperthreading", result.stderr)
        missing = instance()
        del missing["hyperthreading"]
        result = self.run_validator(queue_config(missing))
        self.assert_invalid(result)
        self.assertIn("hyperthreading", result.stderr)

    def test_malformed_structure_and_yaml_fail_cleanly(self):
        configurations = (
            None, [], {}, {"queues": None}, {"queues": {}},
            {"queues": [None]}, {"queues": [{"name": "compute"}]},
            {"queues": [{"name": "compute", "instance_types": {}}]},
            queue_config(None),
        )
        for configuration in configurations:
            with self.subTest(configuration=configuration):
                self.assert_invalid(self.run_validator(configuration))
        for field in ("name", "shape", "instance_keyword"):
            for value in (None, "", 123):
                invalid = instance()
                invalid[field] = value
                with self.subTest(field=field, value=value):
                    self.assert_invalid(self.run_validator(queue_config(invalid)))
        for source in ("queues: [", "queues: []\n---\nqueues: []\n"):
            with self.subTest(source=source):
                self.assert_invalid(self.run_validator(source=source))


class SlurmConfigValidationEntryPointTests(unittest.TestCase):
    def run_slurm_config(self, configuration, *, initial=False):
        with tempfile.TemporaryDirectory(prefix="slurm-config-validation-") as temporary:
            root = Path(temporary)
            script_dir = root / "bin"
            command_dir = root / "commands"
            conf_dir = root / "conf"
            for directory in (script_dir, command_dir, conf_dir, root / "playbooks"):
                directory.mkdir()
            (conf_dir / "queues.conf").write_text(yaml.safe_dump(configuration), encoding="utf-8")
            shutil.copy2(VALIDATOR, script_dir / VALIDATOR.name)
            os_release = root / "os-release"
            os_release.write_text("ID=ol\n", encoding="utf-8")
            source = SLURM_CONFIG.read_text(encoding="utf-8")
            source = source.replace("source /etc/os-release", "source " + shlex.quote(str(os_release)))
            script = script_dir / SLURM_CONFIG.name
            script.write_text(source, encoding="utf-8")
            trace = root / "invocations"
            trace.write_text("", encoding="utf-8")
            commands = {
                "python3": "exec " + shlex.quote(sys.executable) + ' "$@"\n',
                "sudo": 'printf "sudo %s\\n" "$*" >> "$QUEUE_TEST_TRACE"\nexit 0\n',
                "ansible-playbook": 'printf "ansible %s\\n" "$*" >> "$QUEUE_TEST_TRACE"\nexit 0\n',
            }
            for name, body in commands.items():
                command = command_dir / name
                command.write_text("#!/bin/bash\n" + body, encoding="utf-8")
                command.chmod(0o755)
            environment = dict(os.environ)
            environment.update(
                PATH=str(command_dir) + os.pathsep + os.environ["PATH"],
                QUEUE_TEST_TRACE=str(trace),
            )
            result = subprocess.run(
                ["/bin/bash", str(script)] + (["--initial"] if initial else []),
                env=environment, cwd=root, capture_output=True, text=True,
                check=False, timeout=15,
            )
            return result, trace.read_text(encoding="utf-8").splitlines()

    def test_rejected_ht_configuration_has_no_effect_even_with_initial(self):
        for initial in (False, True):
            with self.subTest(initial=initial):
                result, trace = self.run_slurm_config(
                    queue_config(instance("VM.Standard3.Flex", False)), initial=initial,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Error:", result.stderr)
                self.assertEqual(trace, [], "validation must precede sudo and Ansible")

    def test_duplicate_configuration_has_no_effect_even_with_initial(self):
        first = instance()
        second = copy.deepcopy(first)
        second["name"] = "another-type"
        for initial in (False, True):
            with self.subTest(initial=initial):
                result, trace = self.run_slurm_config(queue_config(first, second), initial=initial)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(trace, [])

    def test_valid_configuration_reaches_normal_ansible_application(self):
        for shape, hyperthreading in (("VM.Standard.E6.Flex", False), ("VM.Standard3.Flex", True)):
            with self.subTest(shape=shape, hyperthreading=hyperthreading):
                result, trace = self.run_slurm_config(queue_config(instance(shape, hyperthreading)))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(trace), 1, trace)
                self.assertTrue(trace[0].startswith("ansible "), trace)
                self.assertIn("slurm_config.yml", trace[0])


if __name__ == "__main__":
    unittest.main()
