"""Disk-free tests for the localdisk safety gate and command plan."""

import importlib.util
import json
import pathlib
import unittest
from unittest import mock


ROLE = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("localdisk", ROLE / "files" / "localdisk.py")
localdisk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(localdisk)
MOUNT = "/nonexistent-localdisk-unit-test"


class FakeRunner:
    def __init__(self, states, vg=False, lv=False, lv_type="raid1", fs=None):
        self.states = dict(states)
        self.vg = vg
        self.lv = lv
        self.lv_type = lv_type
        self.fs = fs
        self.partition_fs = {}
        self.calls = []
        self.fail_tool = None
        self.bad_json = None
        self.blkid_override = {}
        self.wipefs_override = {}
        self.disk_mount = {}
        self.mount = None
        self.pe_count = 228881
        self.pe_alloc = 0

    def run(self, argv, allowed=(0,)):
        self.calls.append(argv[:])
        tool = argv[0]
        if tool == self.fail_tool:
            raise localdisk.ScratchError("{}: mocked I/O error on {}".format(tool, argv[-1]))
        if tool == self.bad_json:
            return 0, "{broken"
        if tool == "lsblk":
            rows = []
            for disk in argv[7:]:
                status = self.states[disk]
                number = disk.split("nvme")[1].split("n1")[0]
                rows.append({"name": disk, "type": "disk", "fstype": "LVM2_member" if status in ("pv", "foreign", "orphan") else None,
                             "label": None, "mountpoint": self.disk_mount.get(disk),
                             "size": "960000000000", "ro": "0", "maj:min": "259:" + number})
                if status == "part":
                    part = disk + "p1"
                    rows.append({"name": part, "type": "part", "fstype": self.partition_fs.get(part),
                                 "label": "locscratch" + number if self.partition_fs.get(part) == "xfs" else None,
                                 "mountpoint": self.disk_mount.get(part), "size": "959000000000",
                                 "ro": "0", "maj:min": "259:1"})
                if status == "pv" and self.lv:
                    rows.append({"name": localdisk.LV_PATH, "type": "lvm", "fstype": self.fs,
                                 "label": None, "mountpoint": self.mount, "size": "900000000000",
                                 "ro": "0", "maj:min": "253:0"})
            return 0, json.dumps({"blockdevices": rows})
        if tool == "findmnt":
            roots = [{"source": "/dev/root", "target": "/", "fstype": "xfs"}]
            if self.mount:
                roots.append({"source": localdisk.LV_PATH, "target": self.mount, "fstype": "xfs"})
            for device, target in self.disk_mount.items():
                roots.append({"source": device, "target": target, "fstype": "xfs"})
            return 0, json.dumps({"filesystems": roots})
        if tool == "pvs":
            rows = [{"pv_name": disk,
                     "vg_name": localdisk.VG if state == "pv" else ("other_vg" if state == "foreign" else ""),
                     "pv_pe_count": str(self.pe_count), "pv_pe_alloc_count": str(self.pe_alloc)}
                    for disk, state in self.states.items() if state in ("pv", "foreign", "orphan")]
            return 0, json.dumps({"report": [{"pv": rows}]})
        if tool == "vgs":
            rows = [{"vg_name": localdisk.VG, "vg_extent_size": "4194304.00",
                     "pv_count": str(sum(state == "pv" for state in self.states.values()))}] if self.vg else []
            return 0, json.dumps({"report": [{"vg": rows}]})
        if tool == "lvs":
            rows = []
            if self.lv:
                disks = [disk for disk, state in self.states.items() if state == "pv"]
                devices = ",".join("{}({})".format(disk, 0) for disk in disks)
                rows.append({"vg_name": localdisk.VG, "lv_name": localdisk.LV,
                             "segtype": self.lv_type, "devices": devices})
                if self.lv_type in ("raid1", "raid10"):
                    for index, disk in enumerate(disks):
                        for suffix in ("rimage", "rmeta"):
                            rows.append({"vg_name": localdisk.VG,
                                         "lv_name": "[{}_{}_{}]".format(localdisk.LV, suffix, index),
                                         "segtype": "linear", "devices": "{}(1)".format(disk)})
            return 0, json.dumps({"report": [{"lv": rows}]})
        if tool == "blkid":
            device = argv[-1]
            if device in self.blkid_override:
                return self.blkid_override[device]
            if device == localdisk.LV_PATH:
                return (0, "TYPE=xfs\n") if self.fs == "xfs" else (2, "")
            if device.endswith("p1"):
                fs = self.partition_fs.get(device)
                number = device.split("nvme")[1].split("n1")[0]
                return (0, "TYPE=xfs\nLABEL=locscratch{}\n".format(number)) if fs == "xfs" else (2, "")
            status = self.states[device]
            if status in ("pv", "foreign", "orphan"):
                return 0, "TYPE=LVM2_member\n"
            if status == "part":
                return 0, "PTTYPE=gpt\n"
            if status == "unknown":
                return 0, "TYPE=ext4\n"
            return 2, ""
        if tool == "wipefs":
            device = argv[-1]
            if device in self.wipefs_override:
                return 0, self.wipefs_override[device]
            if device == localdisk.LV_PATH:
                return 0, "xfs\n" if self.fs == "xfs" else ""
            if device.endswith("p1"):
                return 0, "xfs\n" if self.partition_fs.get(device) == "xfs" else ""
            status = self.states[device]
            return 0, {"pv": "LVM2_member\n", "foreign": "LVM2_member\n",
                       "orphan": "LVM2_member\n", "part": "gpt\nPMBR\n",
                       "unknown": "ext4\n"}.get(status, "")
        if tool == "vgcreate":
            self.vg = True
            for disk in argv[4:]:
                self.states[disk] = "pv"
            return 0, ""
        if tool == "vgextend":
            for disk in argv[2:]:
                self.states[disk] = "pv"
            return 0, ""
        if tool == "lvcreate":
            self.lv = True
            self.lv_type = argv[argv.index("--type") + 1]
            return 0, ""
        if tool == "mkfs.xfs":
            device = argv[-1]
            if device == localdisk.LV_PATH:
                self.fs = "xfs"
            else:
                self.partition_fs[device] = "xfs"
            return 0, ""
        if tool == "parted":
            self.states[argv[4]] = "part"
            return 0, ""
        if tool == "udevadm":
            return 0, ""
        raise AssertionError("unexpected command: " + repr(argv))


class LocaldiskTests(unittest.TestCase):
    def setup_disks(self, count, state="blank", **kwargs):
        return FakeRunner({"/dev/nvme{}n1".format(index): state for index in range(count)}, **kwargs)

    def apply(self, runner, log_vol=True, redundancy=True, nosync=True):
        names = [disk.split("/dev/")[1] for disk in runner.states]
        return localdisk.configure(runner, names, MOUNT, log_vol, redundancy, nosync,
                                   block_check=lambda path: True)

    def commands(self, runner, name):
        return [argv for argv in runner.calls if argv[0] == name]

    def test_zero_nvme_does_not_probe_or_write(self):
        runner = self.setup_disks(0)
        result = self.apply(runner)
        self.assertEqual(result["mounts"], [])
        self.assertEqual(runner.calls, [])

    def test_one_nvme_preserves_individual_partition_label_and_mount(self):
        runner = self.setup_disks(1)
        result = self.apply(runner)
        self.assertEqual(result["mounts"], [{"path": MOUNT, "src": "LABEL=locscratch0", "opts": "defaults,noatime"}])
        self.assertEqual(len(self.commands(runner, "parted")), 1)
        self.assertEqual(len(self.commands(runner, "mkfs.xfs")), 1)
        self.assertFalse(self.commands(runner, "vgcreate"))

    def test_two_disk_raid1_size_and_nosync(self):
        runner = self.setup_disks(2)
        result = self.apply(runner)
        self.assertEqual(result["mounts"][0]["src"], localdisk.LV_PATH)
        self.assertFalse(self.commands(runner, "parted"))
        self.assertEqual(self.commands(runner, "vgcreate")[0][-2:], ["/dev/nvme0n1", "/dev/nvme1n1"])
        lv = self.commands(runner, "lvcreate")[0]
        self.assertEqual(lv[lv.index("--size") + 1], "937480192k")
        self.assertIn("--nosync", lv)
        self.assertEqual(lv[lv.index("--type") + 1], "raid1")

    def test_false_nosync_uses_normal_initial_sync(self):
        runner = self.setup_disks(2)
        self.apply(runner, nosync=False)
        self.assertNotIn("--nosync", self.commands(runner, "lvcreate")[0])

    def test_four_disk_raid10_and_odd_spare(self):
        for count in (4, 5):
            with self.subTest(count=count):
                runner = self.setup_disks(count)
                result = self.apply(runner)
                lv = self.commands(runner, "lvcreate")[0]
                self.assertEqual(lv[lv.index("--type") + 1], "raid10")
                self.assertEqual(lv[lv.index("--stripes") + 1], "2")
                self.assertEqual(lv[lv.index("--size") + 1], "1874960384k")
                self.assertEqual(result["unused"], ["/dev/nvme4n1"] if count == 5 else [])
                if count == 5:
                    self.assertEqual(runner.states["/dev/nvme4n1"], "blank")

    def test_three_disk_raid_uses_two_and_leaves_one_unused(self):
        runner = self.setup_disks(3)
        result = self.apply(runner)
        self.assertEqual(result["unused"], ["/dev/nvme2n1"])
        self.assertEqual(self.commands(runner, "lvcreate")[0][-2:], ["/dev/nvme0n1", "/dev/nvme1n1"])

    def test_nonredundant_striping_and_odd_disk(self):
        runner = self.setup_disks(5)
        result = self.apply(runner, redundancy=False)
        lv = self.commands(runner, "lvcreate")[0]
        self.assertEqual(lv[lv.index("--type") + 1], "striped")
        self.assertEqual(lv[lv.index("--stripes") + 1], "4")
        self.assertEqual(lv[lv.index("--size") + 1], "3749920768k")
        self.assertNotIn("--nosync", lv)
        self.assertEqual(result["unused"], ["/dev/nvme4n1"])

    def test_log_vol_false_keeps_individual_mounts(self):
        runner = self.setup_disks(2)
        result = self.apply(runner, log_vol=False, redundancy=True)
        self.assertEqual([item["path"] for item in result["mounts"]], [MOUNT, MOUNT + "1"])
        self.assertEqual(len(self.commands(runner, "parted")), 2)
        self.assertFalse(self.commands(runner, "vgcreate"))

    def test_existing_lv_is_reused_without_resize_or_resync_change(self):
        runner = self.setup_disks(2, state="pv", vg=True, lv=True, fs="xfs")
        result = self.apply(runner, nosync=True)
        self.assertFalse(result["changed"])
        self.assertFalse(self.commands(runner, "lvcreate"))
        self.assertFalse(self.commands(runner, "mkfs.xfs"))

    def test_partial_vg_without_lv_resumes(self):
        runner = self.setup_disks(2, state="pv", vg=True)
        self.apply(runner)
        self.assertEqual(len(self.commands(runner, "lvcreate")), 1)
        self.assertFalse(self.commands(runner, "vgcreate"))

    def test_partial_vg_can_add_only_proven_blank_disk(self):
        runner = FakeRunner({"/dev/nvme0n1": "pv", "/dev/nvme1n1": "blank"}, vg=True)
        self.apply(runner)
        self.assertEqual(self.commands(runner, "vgextend")[0], ["vgextend", "vg_nvmes", "/dev/nvme1n1"])

    def test_existing_blank_lv_is_formatted_without_recreating_it(self):
        runner = self.setup_disks(2, state="pv", vg=True, lv=True, fs=None)
        self.apply(runner)
        self.assertEqual(len(self.commands(runner, "mkfs.xfs")), 1)
        self.assertFalse(self.commands(runner, "lvcreate"))

    def test_existing_correct_individual_partition_is_reused(self):
        runner = self.setup_disks(1, state="part")
        runner.partition_fs["/dev/nvme0n1p1"] = "xfs"
        result = self.apply(runner)
        self.assertFalse(result["changed"])
        self.assertFalse(self.commands(runner, "parted"))
        self.assertFalse(self.commands(runner, "mkfs.xfs"))

    def test_existing_blank_partition_is_not_autoformatted(self):
        runner = self.setup_disks(1, state="part")
        with self.assertRaisesRegex(localdisk.ScratchError, "existing partition"):
            self.apply(runner)
        self.assertFalse(self.commands(runner, "mkfs.xfs"))

    def test_partitioned_disk_is_not_used_as_whole_pv(self):
        runner = self.setup_disks(2)
        runner.states["/dev/nvme0n1"] = "part"
        with self.assertRaisesRegex(localdisk.ScratchError, "partition exists"):
            self.apply(runner)
        self.assertFalse(self.commands(runner, "vgcreate"))

    def test_in_use_disk_other_vg_and_orphan_pv_are_rejected(self):
        for state, expected in (("foreign", "another VG"), ("orphan", "orphan PV")):
            with self.subTest(state=state):
                runner = self.setup_disks(2)
                runner.states["/dev/nvme0n1"] = state
                with self.assertRaisesRegex(localdisk.ScratchError, expected):
                    self.apply(runner)
                self.assertFalse(self.commands(runner, "vgcreate"))
        runner = self.setup_disks(2)
        runner.disk_mount["/dev/nvme0n1"] = "/"
        with self.assertRaises(localdisk.ScratchError):
            self.apply(runner)
        self.assertFalse(self.commands(runner, "vgcreate"))

    def test_existing_wrong_lv_type_is_rejected_without_changes(self):
        runner = self.setup_disks(2, state="pv", vg=True, lv=True, lv_type="striped", fs="xfs")
        with self.assertRaisesRegex(localdisk.ScratchError, "type mismatch"):
            self.apply(runner)
        self.assertFalse(self.commands(runner, "mkfs.xfs"))

    def test_missing_pv_report_is_rejected_before_extension(self):
        runner = FakeRunner({"/dev/nvme0n1": "pv", "/dev/nvme1n1": "blank"}, vg=True)
        original = runner.run

        def inconsistent(argv, allowed=(0,)):
            rc, raw = original(argv, allowed)
            if argv[0] == "vgs":
                value = json.loads(raw)
                value["report"][0]["vg"][0]["pv_count"] = "2"
                return rc, json.dumps(value)
            return rc, raw

        runner.run = inconsistent
        with self.assertRaisesRegex(localdisk.ScratchError, "missing or unreported PV"):
            self.apply(runner)
        self.assertFalse(self.commands(runner, "vgextend"))

    def test_unsafe_blkid_results_do_not_authorize_writes(self):
        device = "/dev/nvme0n1"
        for response in ((2, "TYPE=xfs\n"), (0, ""), (8, ""), (0, "TYPE=xfs\nTYPE=ext4\n")):
            with self.subTest(response=response):
                runner = self.setup_disks(2)
                runner.blkid_override[device] = response
                with self.assertRaises(localdisk.ScratchError):
                    self.apply(runner)
                self.assertFalse(self.commands(runner, "vgcreate"))
        runner = self.setup_disks(2)
        runner.wipefs_override[device] = "gpt\n"
        with self.assertRaisesRegex(localdisk.ScratchError, "not a proven empty disk"):
            self.apply(runner)

    def test_corrupt_probe_json_and_io_errors_are_not_empty_disks(self):
        for tool in ("lsblk", "findmnt", "pvs", "vgs", "lvs"):
            with self.subTest(tool=tool):
                runner = self.setup_disks(2)
                runner.bad_json = tool
                with self.assertRaisesRegex(localdisk.ScratchError, "invalid JSON"):
                    self.apply(runner)
                self.assertFalse(self.commands(runner, "vgcreate"))
        for tool in ("lsblk", "blkid", "wipefs", "pvs"):
            with self.subTest(tool=tool):
                runner = self.setup_disks(2)
                runner.fail_tool = tool
                with self.assertRaisesRegex(localdisk.ScratchError, "I/O error"):
                    self.apply(runner)
                self.assertFalse(self.commands(runner, "vgcreate"))

    def test_create_and_format_failures_report_stage_without_force_retry(self):
        for tool in ("vgcreate", "lvcreate", "mkfs.xfs"):
            with self.subTest(tool=tool):
                runner = self.setup_disks(2)
                runner.fail_tool = tool
                with self.assertRaisesRegex(localdisk.ScratchError, tool):
                    self.apply(runner)
                self.assertFalse(any("--force" in call or "-f" in call for call in runner.calls))

    def test_mount_and_host_failure_policy_stays_inside_role(self):
        text = (ROLE / "tasks" / "common.yml").read_text()
        self.assertIn("localdisk_optional_compute", text)
        self.assertIn("(role | default('')) == 'compute'", text)
        self.assertIn("not (initial | default(false) | bool)", text)
        self.assertIn("Warn about optional Autoscaling compute scratch failure", text)
        self.assertIn("Fail required local scratch configuration", text)
        self.assertIn("Mount local NVMe scratch", text)
        self.assertIn("Set local NVMe scratch permissions", text)
        self.assertIn("rescue:", text)
        self.assertNotIn("use_local_block_volume", text)

    def test_runner_rejects_stderr_even_with_blkid_exit_two(self):
        result = mock.Mock(returncode=2, stdout="", stderr="Input/output error")
        with mock.patch.object(localdisk.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(localdisk.ScratchError, "Input/output error"):
                localdisk.blkid(localdisk.Runner(), "/dev/nvme0n1")


if __name__ == "__main__":
    unittest.main()
