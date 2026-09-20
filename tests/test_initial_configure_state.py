import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HELPER_PATH = Path(__file__).resolve().parents[1] / "bin" / "initial_configure_state.py"
SPEC = importlib.util.spec_from_file_location("initial_configure_state", HELPER_PATH)
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class InitialConfigureStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.inventory = Path(self.directory.name) / "inventory"
        self.state = Path(self.directory.name) / HELPER.STATE_FILENAME
        self.write_inventory()

    def write_inventory(self, alias="temporary-node", instance_id="ocid1.instance.test", ip="192.0.2.10", cluster="batch-1"):
        self.inventory.write_text(
            "[compute_to_add]\n"
            + alias + " ansible_host=" + ip + " oci_instance_id=" + instance_id + " ansible_user=opc # comment\n"
            "[compute_configured]\n"
            "[all:vars]\n"
            "cluster_name = " + cluster + "\n",
            encoding="utf-8",
        )

    def write_document(self, document):
        self.state.write_text(json.dumps(document), encoding="utf-8")
        self.state.chmod(0o600)

    def cli(self, operation, stage=None):
        command = [sys.executable, str(HELPER_PATH), operation, "--inventory", str(self.inventory)]
        if stage is not None:
            command += ["--stage", stage]
        return subprocess.run(command, text=True, capture_output=True)

    def test_absent_state_read_does_not_need_inventory(self):
        self.inventory.unlink()
        result = self.cli("read")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_cli_round_trip_every_stage_and_clear(self):
        for stage in HELPER.STAGES:
            written = self.cli("write", stage)
            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertEqual(written.stdout, "")
            read = self.cli("read")
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertEqual(read.stdout, stage + "\n")
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(self.cli("clear").returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.cli("clear").returncode, 0)

    def test_atomic_write_syncs_file_before_replace_and_directory_after(self):
        events = []
        original_fsync = os.fsync
        original_replace = os.replace

        def fsync(descriptor):
            events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
            return original_fsync(descriptor)

        def replace(source, destination):
            events.append("replace")
            self.assertEqual(Path(source).parent, self.state.parent)
            self.assertEqual(stat.S_IMODE(os.stat(source).st_mode), 0o600)
            self.assertEqual(json.loads(Path(source).read_text())["stage"], "sync")
            return original_replace(source, destination)

        with mock.patch.object(HELPER.os, "fsync", side_effect=fsync), mock.patch.object(HELPER.os, "replace", side_effect=replace):
            HELPER.write_state(self.inventory, "sync")
        self.assertEqual(events, ["file", "replace", "directory"])
        self.assertEqual(set(Path(self.directory.name).iterdir()), {self.inventory, self.state})

    def test_failed_replace_preserves_previous_phase_and_removes_temporary_file(self):
        HELPER.write_state(self.inventory, "sync")
        original = self.state.read_bytes()
        with mock.patch.object(HELPER.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaisesRegex(OSError, "injected"):
                HELPER.write_state(self.inventory, "configure")
        self.assertEqual(self.state.read_bytes(), original)
        self.assertEqual(set(Path(self.directory.name).iterdir()), {self.inventory, self.state})

    def test_clear_syncs_directory_after_deletion(self):
        HELPER.write_state(self.inventory, "monitoring")

        def verify_deleted(directory):
            self.assertEqual(directory, self.directory.name)
            self.assertFalse(self.state.exists())

        with mock.patch.object(HELPER, "fsync_directory", side_effect=verify_deleted) as synced:
            HELPER.clear_state(self.inventory)
        synced.assert_called_once()

    def test_changed_alias_and_inventory_section_keep_same_identity(self):
        HELPER.write_state(self.inventory, "sync")
        self.write_inventory(alias="final-slurm-node")
        contents = self.inventory.read_text().replace("[compute_to_add]", "[replaced]").replace("[compute_configured]", "[compute_to_add]").replace("[replaced]", "[compute_configured]")
        self.inventory.write_text(contents)
        self.assertEqual(HELPER.read_state(self.inventory)["stage"], "sync")
        HELPER.write_state(self.inventory, "configure")
        self.assertEqual(HELPER.read_state(self.inventory)["stage"], "configure")

    def test_changed_ocid_ip_or_cluster_rejects_read_write_and_clear(self):
        for changed in (
            {"instance_id": "ocid1.instance.replacement"},
            {"ip": "192.0.2.11"},
            {"cluster": "other-cluster"},
        ):
            with self.subTest(changed=changed):
                self.write_inventory()
                HELPER.write_state(self.inventory, "configure")
                original = self.state.read_bytes()
                self.write_inventory(**changed)
                for operation, stage in (("read", None), ("write", "monitoring"), ("clear", None)):
                    result = self.cli(operation, stage)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("does not match", result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(self.state.read_bytes(), original)

    def test_added_and_removed_members_are_rejected(self):
        original_inventory = self.inventory.read_text()
        added_member = "other-node ansible_host=192.0.2.11 oci_instance_id=ocid1.instance.other\n"
        HELPER.write_state(self.inventory, "configure")
        self.inventory.write_text(original_inventory.replace("[compute_configured]\n", "[compute_configured]\n" + added_member))
        with self.assertRaisesRegex(ValueError, "does not match"):
            HELPER.read_state(self.inventory)
        self.inventory.write_text(original_inventory)
        HELPER.clear_state(self.inventory)
        self.inventory.write_text(original_inventory.replace("[compute_configured]\n", "[compute_configured]\n" + added_member))
        HELPER.write_state(self.inventory, "configure")
        self.inventory.write_text(original_inventory)
        with self.assertRaisesRegex(ValueError, "does not match"):
            HELPER.read_state(self.inventory)

    def test_invalid_stage_schema_cannot_be_read_overwritten_or_cleared(self):
        identity = HELPER.inventory_identity(self.inventory)
        malformed_documents = [
            [],
            {"version": 1, "stage": "done", "identity": identity},
            {"version": True, "stage": "sync", "identity": identity},
            {"version": 2, "stage": "sync", "identity": identity},
            {"version": 1, "stage": "sync"},
            {"version": 1, "stage": "sync", "identity": {"cluster_name": "batch-1", "members": {}}},
            {"version": 1, "stage": "sync", "identity": identity, "extra": True},
        ]
        for document in malformed_documents:
            with self.subTest(document=document):
                self.write_document(document)
                original = self.state.read_bytes()
                for action in (lambda: HELPER.read_state(self.inventory), lambda: HELPER.write_state(self.inventory, "configure"), lambda: HELPER.clear_state(self.inventory)):
                    with self.assertRaises(ValueError):
                        action()
                    self.assertEqual(self.state.read_bytes(), original)

    def test_invalid_json_and_duplicate_json_keys_fail_closed(self):
        for content in ("sync\n", "{", '{"version":1,"version":1}'):
            self.state.write_text(content)
            self.state.chmod(0o600)
            with self.assertRaises(ValueError):
                HELPER.read_state(self.inventory)

    def test_state_symlink_directory_and_unsafe_permissions_are_rejected(self):
        self.state.symlink_to(self.inventory)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            HELPER.read_state(self.inventory)
        self.state.unlink()
        self.state.mkdir()
        with self.assertRaisesRegex(ValueError, "regular"):
            HELPER.write_state(self.inventory, "sync")
        self.state.rmdir()
        HELPER.write_state(self.inventory, "sync")
        self.state.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "permissions"):
            HELPER.clear_state(self.inventory)
        self.assertTrue(self.state.exists())

    def test_inventory_identity_requires_unambiguous_instance_and_ip(self):
        original = self.inventory.read_text()
        invalid_inventories = [
            original.replace("oci_instance_id=ocid1.instance.test", ""),
            original.replace("oci_instance_id=ocid1.instance.test", "oci_instance_id=ocid1.instance.test oci_instance_id=ocid1.instance.other"),
            original.replace("ansible_host=192.0.2.10", "ansible_host=not-an-ip"),
            original.replace("cluster_name = batch-1", "cluster_name = batch-1\ncluster_name = batch-1"),
            original.replace("[compute_configured]\n", ""),
            original.replace("[compute_configured]\n", "[compute_configured]\nsecond-node ansible_host=192.0.2.11 oci_instance_id=ocid1.instance.test\n"),
            original.replace("[compute_configured]\n", "[compute_configured]\nsecond-node ansible_host=192.0.2.10 oci_instance_id=ocid1.instance.other\n"),
        ]
        for inventory in invalid_inventories:
            with self.subTest(inventory=inventory):
                self.inventory.write_text(inventory)
                with self.assertRaises(ValueError):
                    HELPER.write_state(self.inventory, "sync")
                self.assertFalse(self.state.exists())

    def test_cli_requires_stage_only_for_write(self):
        for operation, stage in (("write", None), ("read", "sync"), ("clear", "sync")):
            result = self.cli(operation, stage)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--stage is required only for write", result.stderr)


if __name__ == "__main__":
    unittest.main()
