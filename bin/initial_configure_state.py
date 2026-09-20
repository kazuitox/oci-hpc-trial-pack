#!/usr/bin/env python3
"""Persist initial Autoscaling configuration progress for one exact membership."""

import argparse
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile


STATE_FILENAME = ".initial-configure-stage"
STAGES = ("sync", "configure", "monitoring", "legacy-sync")
INSTANCE_ID = re.compile(r"ocid1\.instance\.[A-Za-z0-9.-]+")


def state_path(inventory_path):
    return os.path.join(os.path.dirname(os.path.abspath(inventory_path)), STATE_FILENAME)


def uncomment(line):
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return ""
    return re.split(r"\s[#;]", stripped, maxsplit=1)[0].rstrip()


def validate_identity(identity):
    if not isinstance(identity, dict) or set(identity) != {"cluster_name", "members"}:
        raise ValueError("Initial configuration state has an invalid identity")
    cluster_name = identity["cluster_name"]
    if not isinstance(cluster_name, str) or not cluster_name or any(
        character.isspace() or ord(character) < 32 for character in cluster_name
    ):
        raise ValueError("Initial configuration identity has an invalid cluster_name")
    members = identity["members"]
    if not isinstance(members, dict) or not members:
        raise ValueError("Initial configuration identity must contain compute members")
    normalized = {}
    for instance_id, private_ip in members.items():
        if not isinstance(instance_id, str) or INSTANCE_ID.fullmatch(instance_id) is None:
            raise ValueError("Initial configuration identity has an invalid instance OCID")
        if not isinstance(private_ip, str):
            raise ValueError("Initial configuration identity has an invalid private IP")
        normalized[instance_id] = str(ipaddress.ip_address(private_ip))
    if len(set(normalized.values())) != len(normalized):
        raise ValueError("Initial configuration identity contains duplicate private IPs")
    return {"cluster_name": cluster_name, "members": normalized}


def inventory_identity(inventory_path):
    # Match resize.py's section/token representation, without importing its CLI
    # or OCI dependencies. Inventory aliases intentionally are not identities:
    # the name synchronization step rewrites them between configuration phases.
    sections = {}
    current_section = None
    with open(inventory_path, "r", encoding="utf-8") as inventory_file:
        for raw_line in inventory_file:
            line = uncomment(raw_line)
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                sections.setdefault(current_section, [])
            elif current_section is not None:
                sections[current_section].append(line)
    cluster_names = []
    for line in sections.get("all:vars", []):
        key, separator, value = line.partition("=")
        if separator and key.strip() == "cluster_name":
            cluster_names.append(value.strip())
    if len(cluster_names) != 1:
        raise ValueError("Inventory must contain exactly one cluster_name")
    members = {}
    aliases = set()
    for section in ("compute_configured", "compute_to_add"):
        if section not in sections:
            raise ValueError("Inventory does not contain [" + section + "]")
        for line in sections[section]:
            tokens = line.split()
            alias = tokens[0]
            if alias in aliases or any(character in alias for character in "/\\\x00"):
                raise ValueError("Inventory contains an invalid or duplicate compute alias")
            aliases.add(alias)
            values = {}
            for key in ("oci_instance_id", "ansible_host"):
                matches = [token.split("=", 1)[1] for token in tokens[1:] if token.startswith(key + "=")]
                if len(matches) != 1 or not matches[0]:
                    raise ValueError("Inventory compute member must have exactly one " + key)
                values[key] = matches[0]
            instance_id = values["oci_instance_id"]
            if instance_id in members:
                raise ValueError("Inventory contains duplicate compute instance OCIDs")
            members[instance_id] = values["ansible_host"]
    return validate_identity({"cluster_name": cluster_names[0], "members": members})


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Initial configuration state contains duplicate JSON keys")
        result[key] = value
    return result


def read_state(inventory_path):
    filename = state_path(inventory_path)
    try:
        file_stat = os.lstat(filename)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("Initial configuration state is not a regular non-symlink file")
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as state_file:
        opened_stat = os.fstat(state_file.fileno())
        inventory_stat = os.stat(inventory_path)
        if not stat.S_ISREG(opened_stat.st_mode) or (
            opened_stat.st_uid != inventory_stat.st_uid
            or opened_stat.st_gid != inventory_stat.st_gid
            or stat.S_IMODE(opened_stat.st_mode) & 0o077
        ):
            raise ValueError("Initial configuration state has unsafe ownership or permissions")
        document = json.load(state_file, object_pairs_hook=unique_json_object)
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "stage", "identity"}
        or type(document["version"]) is not int
        or document["version"] != 1
        or document["stage"] not in STAGES
    ):
        raise ValueError("Initial configuration state has an invalid format or stage")
    identity = validate_identity(document["identity"])
    if identity != inventory_identity(inventory_path):
        raise ValueError(
            "Initial configuration identity does not match the current inventory; "
            "cluster, instance OCIDs, or private IPs changed. Refusing to resume."
        )
    document["identity"] = identity
    return document


def fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_state(inventory_path, stage):
    if stage not in STAGES:
        raise ValueError("Unknown initial configuration stage")
    # Never overwrite stale or malformed progress with a new membership.
    previous = read_state(inventory_path)
    identity = inventory_identity(inventory_path)
    if previous is not None and identity != previous["identity"]:
        raise ValueError("Inventory changed while saving initial configuration state")
    document = {"version": 1, "stage": stage, "identity": identity}
    filename = state_path(inventory_path)
    directory = os.path.dirname(filename)
    descriptor, temporary_path = tempfile.mkstemp(prefix=STATE_FILENAME + ".", dir=directory, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            inventory_stat = os.stat(inventory_path)
            os.fchmod(state_file.fileno(), 0o600)
            current_stat = os.fstat(state_file.fileno())
            if (current_stat.st_uid, current_stat.st_gid) != (inventory_stat.st_uid, inventory_stat.st_gid):
                os.fchown(state_file.fileno(), inventory_stat.st_uid, inventory_stat.st_gid)
            json.dump(document, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, filename)
        temporary_path = None
        fsync_directory(directory)
    finally:
        if temporary_path is not None:
            os.unlink(temporary_path)


def clear_state(inventory_path):
    if read_state(inventory_path) is None:
        return
    filename = state_path(inventory_path)
    os.unlink(filename)
    fsync_directory(os.path.dirname(filename))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("read", "write", "clear"))
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--stage", choices=STAGES)
    args = parser.parse_args(argv)
    if (args.operation == "write") != (args.stage is not None):
        parser.error("--stage is required only for write")
    try:
        if args.operation == "read":
            document = read_state(args.inventory)
            if document is not None:
                print(document["stage"])
        elif args.operation == "write":
            write_state(args.inventory, args.stage)
        else:
            clear_state(args.inventory)
    except (OSError, ValueError) as error:
        print("Failed to access initial configuration state: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
