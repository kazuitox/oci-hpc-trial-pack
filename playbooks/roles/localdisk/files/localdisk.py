#!/usr/bin/env python3
"""Inspect and prepare disposable local NVMe scratch without force options.

The Ansible role mounts and sets permissions after this script returns.  Keeping
the probes and the first write in one process makes the safety decision testable
without a block device.  Python 3.6 and util-linux 2.32 are sufficient.
"""

import json
import os
import re
import stat
import subprocess
import sys
from decimal import Decimal, InvalidOperation


VG = "vg_nvmes"
LV = "lv_nvmes"
LV_PATH = "/dev/{}/{}".format(VG, LV)
RESERVED_PE = 4
NVME_RE = re.compile(r"^nvme([0-9]+)n1$")


class ScratchError(Exception):
    pass


class Runner:
    def run(self, argv, allowed=(0,)):
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True)
        except OSError as exc:
            raise ScratchError("{}: {}".format(" ".join(argv), exc))
        if result.returncode not in allowed or result.stderr.strip():
            raise ScratchError("{}: rc={} stdout={!r} stderr={!r}".format(
                " ".join(argv), result.returncode, result.stdout, result.stderr))
        return result.returncode, result.stdout


def parse_json(raw, label):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ScratchError("{}: invalid JSON: {}".format(label, exc))
    if not isinstance(value, dict):
        raise ScratchError("{}: JSON object expected".format(label))
    return value


def report(runner, tool, columns, extra=()):
    argv = [tool, "--reportformat", "json", "--units", "b", "--nosuffix"]
    argv.extend(extra)
    argv.extend(["-o", columns])
    _, raw = runner.run(argv)
    data = parse_json(raw, tool)
    groups = data.get("report")
    if not isinstance(groups, list) or len(groups) != 1:
        raise ScratchError("{}: ambiguous report".format(tool))
    rows = groups[0].get({"pvs": "pv", "vgs": "vg", "lvs": "lv"}[tool])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ScratchError("{}: invalid report rows".format(tool))
    return rows


def lsblk(runner, devices):
    argv = ["lsblk", "--json", "--list", "--paths", "--bytes", "--output",
            "NAME,TYPE,FSTYPE,LABEL,MOUNTPOINT,SIZE,RO,MAJ:MIN"] + devices
    _, raw = runner.run(argv)
    rows = parse_json(raw, "lsblk").get("blockdevices")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ScratchError("lsblk: invalid blockdevices")
    return rows


def findmnt(runner):
    _, raw = runner.run(["findmnt", "--json", "--output", "SOURCE,TARGET,FSTYPE"])
    roots = parse_json(raw, "findmnt").get("filesystems")
    if not isinstance(roots, list):
        raise ScratchError("findmnt: invalid filesystems")
    mounts = []

    def visit(item):
        if not isinstance(item, dict) or not all(key in item for key in ("source", "target", "fstype")):
            raise ScratchError("findmnt: invalid mount entry")
        mounts.append(item)
        children = item.get("children", [])
        if not isinstance(children, list):
            raise ScratchError("findmnt: invalid mount children")
        for child in children:
            visit(child)

    for root in roots:
        visit(root)
    return mounts


def blkid(runner, device):
    rc, raw = runner.run(["blkid", "-p", "-o", "export", device], allowed=(0, 2))
    if rc == 2:
        if raw.strip():
            raise ScratchError("{}: blkid rc=2 with output".format(device))
        return None
    if not raw.strip():
        raise ScratchError("{}: blkid rc=0 without output".format(device))
    fields = {}
    for line in raw.splitlines():
        if "=" not in line:
            raise ScratchError("{}: malformed blkid output".format(device))
        key, value = line.split("=", 1)
        if not key or not value or key in fields:
            raise ScratchError("{}: ambiguous blkid output".format(device))
        fields[key] = value
    return fields


def signatures(runner, device):
    # Read-only.  Even an empty blkid result is not enough to authorize a write.
    _, raw = runner.run(["wipefs", "--no-act", "--noheadings", "--output", "TYPE", device])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def integer(value, label):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        raise ScratchError("{}: invalid number {!r}".format(label, value))
    if number != number.to_integral_value() or number < 0:
        raise ScratchError("{}: invalid integer {!r}".format(label, value))
    return int(number)


def unique(rows, key, value, label):
    found = [row for row in rows if str(row.get(key, "")).strip() == value]
    if len(found) > 1:
        raise ScratchError("{}: duplicate {}".format(label, value))
    return found[0] if found else None


def disk_row(rows, path):
    row = unique(rows, "name", path, "lsblk")
    if row is None or row.get("type") != "disk":
        raise ScratchError("{}: expected one disk row".format(path))
    if str(row.get("ro")) != "0" or integer(row.get("size"), path + " size") == 0:
        raise ScratchError("{}: read-only or zero size".format(path))
    if not re.fullmatch(r"[0-9]+:[0-9]+", str(row.get("maj:min", ""))):
        raise ScratchError("{}: invalid major:minor".format(path))
    return row


def validate_disk_rows(rows, paths):
    identifiers = [disk_row(rows, path)["maj:min"] for path in paths]
    if len(set(identifiers)) != len(identifiers):
        raise ScratchError("lsblk: duplicate underlying disk")
    if any(row.get("type") == "disk" and row.get("name") not in paths for row in rows):
        raise ScratchError("lsblk: unexpected disk row")


def empty_field(value):
    return value is None or value == ""


def ensure_mount_path(path, sources, mounts):
    if not path.startswith("/") or path == "/" or os.path.islink(path):
        raise ScratchError("{}: unsafe mount path".format(path))
    found = [mount for mount in mounts if mount["target"] == path]
    if len(found) > 1:
        raise ScratchError("{}: ambiguous mount".format(path))
    if found:
        if found[0]["source"] not in sources or found[0]["fstype"] != "xfs":
            raise ScratchError("{}: mounted from another source".format(path))
    elif os.path.exists(path):
        if not os.path.isdir(path) or os.listdir(path):
            raise ScratchError("{}: unmounted non-empty path".format(path))


def check_lsblk_mounts(rows, desired_path):
    for row in rows:
        mountpoint = row.get("mountpoint")
        if not empty_field(mountpoint) and mountpoint != desired_path:
            raise ScratchError("{}: in use at {}".format(row.get("name"), mountpoint))


def check_disk_source(mounts, disk, allowed_source=None, allowed_target=None):
    for mount in mounts:
        source = mount["source"]
        if source == disk or source.startswith(disk + "p"):
            if source != allowed_source or mount["target"] != allowed_target:
                raise ScratchError("{}: in use at {}".format(source, mount["target"]))


def lvm_rows(runner):
    return (report(runner, "pvs", "pv_name,vg_name,pv_pe_count,pv_pe_alloc_count"),
            report(runner, "vgs", "vg_name,vg_extent_size,pv_count"),
            report(runner, "lvs", "vg_name,lv_name,segtype,devices", extra=("-a",)))


def classify_lvm_disk(runner, rows, mounts, pvs, path, desired_path):
    row = disk_row(rows, path)
    if not empty_field(row.get("mountpoint")):
        raise ScratchError("{}: disk is mounted".format(path))
    if any(str(child.get("name", "")).startswith(path + "p") for child in rows):
        raise ScratchError("{}: partition exists; refusing whole-disk PV".format(path))
    check_disk_source(mounts, path)
    pv = unique(pvs, "pv_name", path, "pvs")
    identity = blkid(runner, path)
    found = signatures(runner, path)
    if pv:
        if str(pv.get("vg_name", "")).strip() != VG:
            raise ScratchError("{}: belongs to another VG or orphan PV".format(path))
        if identity is None or identity.get("TYPE") != "LVM2_member":
            raise ScratchError("{}: inconsistent LVM signature".format(path))
        if row.get("fstype") != "LVM2_member" or not empty_field(row.get("label")):
            raise ScratchError("{}: inconsistent lsblk LVM data".format(path))
        if found != ["LVM2_member"]:
            raise ScratchError("{}: unknown additional signature".format(path))
        return "pv"
    if identity is not None or found or not empty_field(row.get("fstype")) or not empty_field(row.get("label")):
        raise ScratchError("{}: not a proven empty disk".format(path))
    return "blank"


def validate_lv(rows, pv_paths, expected_type):
    own = [row for row in rows if str(row.get("vg_name", "")).strip() == VG]
    names = [str(row.get("lv_name", "")).strip().strip("[]") for row in own]
    top = [row for row, name in zip(own, names) if name == LV]
    if not top:
        if own:
            raise ScratchError("{}: unexpected or partial LV metadata".format(VG))
        return False
    if len(top) != 1 or str(top[0].get("segtype", "")).strip() != expected_type:
        raise ScratchError("{}: LV type mismatch".format(LV_PATH))
    hidden = [(row, name) for row, name in zip(own, names) if name != LV]
    used = set()
    if expected_type in ("raid1", "raid10"):
        images = [(row, name) for row, name in hidden if re.fullmatch(LV + r"_rimage_[0-9]+", name)]
        metadata = [(row, name) for row, name in hidden if re.fullmatch(LV + r"_rmeta_[0-9]+", name)]
        if len(images) != len(pv_paths) or len(metadata) != len(pv_paths) or len(hidden) != 2 * len(pv_paths):
            raise ScratchError("{}: incomplete RAID images".format(LV_PATH))
        for rows_for_kind, suffix in ((images, "rimage"), (metadata, "rmeta")):
            names_for_kind = set()
            disks_for_kind = set()
            for row, name in rows_for_kind:
                names_for_kind.add(name)
                devices = re.findall(r"(/dev/nvme[0-9]+n1)\([0-9]+\)", str(row.get("devices", "")))
                if len(devices) != 1 or devices[0] not in pv_paths:
                    raise ScratchError("{}: RAID image on unexpected PV".format(LV_PATH))
                disks_for_kind.add(devices[0])
            expected_names = {"{}_{}_{}".format(LV, suffix, index) for index in range(len(pv_paths))}
            if names_for_kind != expected_names or disks_for_kind != set(pv_paths):
                raise ScratchError("{}: incomplete RAID device placement".format(LV_PATH))
            used.update(disks_for_kind)
    else:
        if hidden:
            raise ScratchError("{}: unexpected hidden LV".format(LV_PATH))
        used.update(re.findall(r"(/dev/nvme[0-9]+n1)\([0-9]+\)", str(top[0].get("devices", ""))))
    if used != set(pv_paths):
        raise ScratchError("{}: LV does not use the expected PVs".format(LV_PATH))
    return True


def logical_size_kib(pvs, paths, extent_bytes, redundancy):
    if extent_bytes < 1024 or extent_bytes % 1024:
        raise ScratchError("{}: invalid PE size".format(VG))
    free = []
    for path in paths:
        pv = unique(pvs, "pv_name", path, "pvs")
        if pv is None or str(pv.get("vg_name", "")).strip() != VG:
            raise ScratchError("{}: missing expected PV".format(path))
        count = integer(pv.get("pv_pe_count"), path + " PE count")
        allocated = integer(pv.get("pv_pe_alloc_count"), path + " allocated PE")
        if allocated > count or allocated != 0:
            raise ScratchError("{}: unexpected allocated extents".format(path))
        free.append(count - allocated)
    usable = min(free) - RESERVED_PE
    if usable <= 0:
        raise ScratchError("{}: insufficient free PEs".format(VG))
    multiplier = len(paths) // 2 if redundancy else len(paths)
    return usable * (extent_bytes // 1024) * multiplier


def expected_type(redundancy, count):
    return "raid1" if redundancy and count == 2 else ("raid10" if redundancy else "striped")


def lvcreate_command(paths, size_kib, redundancy, nosync):
    kind = expected_type(redundancy, len(paths))
    argv = ["lvcreate", "--type", kind]
    if redundancy:
        argv.extend(["--mirrors", "1"])
        if kind == "raid10":
            argv.extend(["--stripes", str(len(paths) // 2)])
        if nosync:
            argv.append("--nosync")
    else:
        argv.extend(["--stripes", str(len(paths))])
    return argv + ["--name", LV, "--size", "{}k".format(size_kib), VG] + paths


def filesystem_state(runner, device, expected_label=None):
    identity = blkid(runner, device)
    found = signatures(runner, device)
    if identity is None and not found:
        return "blank"
    if identity and identity.get("TYPE") == "xfs" and found == ["xfs"]:
        if expected_label is None or identity.get("LABEL") == expected_label:
            return "xfs"
    raise ScratchError("{}: existing or ambiguous filesystem signature".format(device))


def inspect_lvm(runner, paths, mount_path, redundancy):
    rows = lsblk(runner, paths)
    validate_disk_rows(rows, paths)
    if any(row.get("type") not in ("disk", "lvm") for row in rows):
        raise ScratchError("lsblk: partition exists or unexpected descendant of an LVM disk")
    mounts = findmnt(runner)
    check_lsblk_mounts(rows, mount_path)
    ensure_mount_path(mount_path, {LV_PATH, "/dev/mapper/vg_nvmes-lv_nvmes"}, mounts)
    pvs, vgs, lvs = lvm_rows(runner)
    kinds = {path: classify_lvm_disk(runner, rows, mounts, pvs, path, mount_path) for path in paths}
    vg = unique(vgs, "vg_name", VG, "vgs")
    current = {str(row.get("pv_name", "")).strip() for row in pvs
               if str(row.get("vg_name", "")).strip() == VG}
    if current - set(paths):
        raise ScratchError("{}: contains PV outside the selected disks".format(VG))
    if bool(vg) != bool(current):
        raise ScratchError("{}: inconsistent VG/PV report".format(VG))
    if vg and integer(vg.get("pv_count"), VG + " PV count") != len(current):
        raise ScratchError("{}: missing or unreported PV".format(VG))
    present = validate_lv(lvs, paths, expected_type(redundancy, len(paths)))
    if present and current != set(paths):
        raise ScratchError("{}: existing LV with missing PV".format(LV_PATH))
    return pvs, vg, present, kinds


def configure_lvm(runner, paths, mount_path, redundancy, nosync, actions):
    pvs, vg, present, kinds = inspect_lvm(runner, paths, mount_path, redundancy)
    blanks = [path for path in paths if kinds[path] == "blank"]
    if vg is None:
        if len(blanks) != len(paths):
            raise ScratchError("{}: incomplete VG state".format(VG))
        argv = ["vgcreate", "--physicalextentsize", "4M", VG] + paths
        runner.run(argv)
        actions.append("vgcreate " + ",".join(paths))
    elif blanks:
        if present:
            raise ScratchError("{}: cannot extend an existing LV automatically".format(LV_PATH))
        runner.run(["vgextend", VG] + blanks)
        actions.append("vgextend " + ",".join(blanks))
    if vg is None or blanks:
        pvs, vg, present, kinds = inspect_lvm(runner, paths, mount_path, redundancy)
        if vg is None or any(kind != "pv" for kind in kinds.values()):
            raise ScratchError("{}: VG creation could not be verified".format(VG))
    if not present:
        extent = integer(vg.get("vg_extent_size"), VG + " PE size")
        size_kib = logical_size_kib(pvs, paths, extent, redundancy)
        argv = lvcreate_command(paths, size_kib, redundancy, nosync)
        runner.run(argv)
        actions.append("lvcreate {} {}k".format(expected_type(redundancy, len(paths)), size_kib))
        _, _, present, _ = inspect_lvm(runner, paths, mount_path, redundancy)
        if not present:
            raise ScratchError("{}: LV creation could not be verified".format(LV_PATH))
    state = filesystem_state(runner, LV_PATH)
    if state == "blank":
        runner.run(["mkfs.xfs", LV_PATH])
        actions.append("mkfs.xfs " + LV_PATH)
        if filesystem_state(runner, LV_PATH) != "xfs":
            raise ScratchError("{}: XFS creation could not be verified".format(LV_PATH))
    return [{"path": mount_path, "src": LV_PATH, "opts": "defaults,noatime,nofail"}]


def inspect_individual(runner, paths, mount_path):
    rows = lsblk(runner, paths)
    validate_disk_rows(rows, paths)
    expected_rows = set(paths) | {path + "p1" for path in paths}
    if any(row.get("type") not in ("disk", "part") or row.get("name") not in expected_rows
           for row in rows):
        raise ScratchError("lsblk: unexpected descendant of an individual disk")
    mounts = findmnt(runner)
    plans = []
    for disk in paths:
        row = disk_row(rows, disk)
        number = NVME_RE.fullmatch(os.path.basename(disk)).group(1)
        target = mount_path if number == "0" else mount_path + number
        part = disk + "p1"
        label = "locscratch" + number
        ensure_mount_path(target, {part, "LABEL=" + label}, mounts)
        check_disk_source(mounts, disk, allowed_source=part, allowed_target=target)
        if not empty_field(row.get("mountpoint")):
            raise ScratchError("{}: disk mounted directly".format(disk))
        children = [child for child in rows if str(child.get("name", "")).startswith(disk + "p")]
        identity = blkid(runner, disk)
        found = signatures(runner, disk)
        if not children:
            if identity is not None or found or not empty_field(row.get("fstype")) or not empty_field(row.get("label")):
                raise ScratchError("{}: not a proven empty disk".format(disk))
            plans.append((disk, part, target, label, "new"))
            continue
        if len(children) != 1 or children[0].get("name") != part or children[0].get("type") != "part":
            raise ScratchError("{}: unexpected partitions".format(disk))
        if identity is None or identity.get("PTTYPE") != "gpt" or not set(found).issubset({"gpt", "PMBR"}) or "gpt" not in found:
            raise ScratchError("{}: unrecognized partition table".format(disk))
        if not empty_field(children[0].get("mountpoint")) and children[0]["mountpoint"] != target:
            raise ScratchError("{}: partition used elsewhere".format(part))
        if filesystem_state(runner, part, label) != "xfs" or children[0].get("fstype") != "xfs" or children[0].get("label") != label:
            raise ScratchError("{}: existing partition is not the expected scratch filesystem".format(part))
        plans.append((disk, part, target, label, "existing"))
    return plans


def configure_individual(runner, paths, mount_path, actions):
    plans = inspect_individual(runner, paths, mount_path)
    mounts = []
    for disk, part, target, label, state in plans:
        if state == "new":
            runner.run(["parted", "--script", "--align", "optimal", disk,
                        "mklabel", "gpt", "mkpart", "primary", "1MiB", "100%"])
            actions.append("parted " + disk)
            runner.run(["udevadm", "settle"])
            rows = lsblk(runner, [disk])
            child = unique(rows, "name", part, "lsblk")
            disk_identity = blkid(runner, disk)
            disk_signatures = signatures(runner, disk)
            if (child is None or child.get("type") != "part" or str(child.get("ro")) != "0"
                    or disk_identity is None or disk_identity.get("PTTYPE") != "gpt"
                    or "gpt" not in disk_signatures
                    or not set(disk_signatures).issubset({"gpt", "PMBR"})
                    or filesystem_state(runner, part) != "blank"):
                raise ScratchError("{}: new partition could not be verified".format(part))
            runner.run(["mkfs.xfs", "-L", label, part])
            actions.append("mkfs.xfs " + part)
            if filesystem_state(runner, part, label) != "xfs":
                raise ScratchError("{}: XFS creation could not be verified".format(part))
        mounts.append({"path": target, "src": "LABEL=" + label, "opts": "defaults,noatime"})
    return mounts


def is_block_device(path):
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise ScratchError("{}: {}".format(path, exc))
    return stat.S_ISBLK(mode)


def configure(runner, devices, mount_path, log_vol, redundancy, nosync,
              block_check=is_block_device):
    if not mount_path.startswith("/") or mount_path.rstrip("/") in ("", "/"):
        raise ScratchError("invalid scratch mount path")
    names = devices[:]
    if len(set(names)) != len(names) or any(NVME_RE.fullmatch(name) is None for name in names):
        raise ScratchError("invalid NVMe device inventory")
    names.sort(key=lambda name: int(NVME_RE.fullmatch(name).group(1)))
    paths = ["/dev/" + name for name in names]
    # The script is run on the target node; refuse a stale or forged inventory.
    for path in paths:
        if not block_check(path):
            raise ScratchError("{}: not a block device".format(path))
    actions = []
    unused = []
    mount_path = mount_path.rstrip("/")
    if log_vol and len(paths) > 1:
        if len(paths) % 2:
            unused = paths[-1:]
            paths = paths[:-1]
        mounts = configure_lvm(runner, paths, mount_path, redundancy, nosync, actions)
    elif paths:
        mounts = configure_individual(runner, paths, mount_path, actions)
    else:
        mounts = []
    return {"changed": bool(actions), "actions": actions, "unused": unused, "mounts": mounts}


def env_bool(name):
    value = os.environ.get(name, "").lower()
    if value not in ("true", "false"):
        raise ScratchError("{}: expected true or false".format(name))
    return value == "true"


if __name__ == "__main__":
    try:
        result = configure(
            Runner(), [name for name in os.environ.get("LOCALDISK_DEVICES", "").split(",") if name],
            os.environ["LOCALDISK_MOUNT"], env_bool("LOCALDISK_LOG_VOL"),
            env_bool("LOCALDISK_REDUNDANCY"), env_bool("LOCALDISK_RAID_NOSYNC"))
        print(json.dumps(result, sort_keys=True))
    except (ScratchError, KeyError) as exc:
        print("localdisk: {}".format(exc), file=sys.stderr)
        sys.exit(1)
