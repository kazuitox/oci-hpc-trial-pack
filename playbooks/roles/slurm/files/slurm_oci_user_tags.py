#!/usr/bin/env python3
"""Record Slurm ownership locally; reconcile the existing OCI user tag centrally."""

import argparse
from contextlib import contextmanager
import datetime
import errno
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import syslog
import tempfile
import time
import urllib.request
import uuid


ROOT_UID = 0
LOCK_STALE_SECONDS = 300
_HELD_LOCKS = {}
IMDS_URL = "http://169.254.169.254/opc/v2/instance/"
VALID_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}$")
VALID_JOB = re.compile(r"^[0-9]+(?:[_.+][0-9]+)*$")
VALID_REGION = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
ACTIVE_STATES = {"RUNNING", "SUSPENDED", "COMPLETING", "CONFIGURING", "RESIZING", "SIGNALING", "STAGE_OUT", "STOPPED"}
INACTIVE_STATES = {"PENDING", "COMPLETED", "CANCELLED", "FAILED", "TIMEOUT", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "OUT_OF_MEMORY", "REVOKED", "REQUEUED", "REQUEUE_FED", "REQUEUE_HOLD", "SPECIAL_EXIT"}
EMPTY_NODES = {"", "(null)", "N/A", "None"}


def log(message):
    syslog.openlog("slurm-oci-user-tags", syslog.LOG_PID, syslog.LOG_DAEMON)
    syslog.syslog(syslog.LOG_WARNING, str(message)[:800])


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def trusted(path, directory=False):
    details = os.lstat(path)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(details.st_mode) or details.st_uid != ROOT_UID or details.st_mode & 0o077:
        raise ValueError("expected root-only {}: {}".format("directory" if directory else "file", path))


def private_directory(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    trusted(path, directory=True)


def read_json(path):
    trusted(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        return json.load(stream)


def atomic_json(path, value):
    assert_locks_owned()
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        assert_locks_owned()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def assert_locks_owned():
    for directory, token in _HELD_LOCKS.items():
        if not os.path.isfile(os.path.join(directory, token)):
            raise RuntimeError("ownership lock expired; refusing stale operation")


def remove_abandoned_lock(path):
    trusted(path, directory=True)
    markers = os.listdir(path)
    if not markers:
        if time.time() - os.stat(path).st_mtime > LOCK_STALE_SECONDS:
            # rmdir is atomic and cannot remove a successor's nonempty lock.
            try:
                os.rmdir(path)
            except OSError:
                pass
        return
    if len(markers) != 1 or not re.match(r"^[0-9a-f]{32}\.owner$", markers[0]):
        raise ValueError("unexpected ownership lock contents")
    marker = os.path.join(path, markers[0])
    try:
        trusted(marker)
        if time.time() - os.stat(marker).st_mtime <= LOCK_STALE_SECONDS:
            return
        # Delete the unique observed token, never a replacement owner's token.
        # Only the reaper that actually removes it may remove this directory.
        os.unlink(marker)
    except FileNotFoundError:
        return
    try:
        os.rmdir(path)
    except OSError:
        pass


@contextmanager
def locked(path, wait=0.5):
    # NFS local_lock=all makes flock host-local. Publish a nonempty lock directory
    # atomically so a stale-lock reaper can never remove a successor's empty lock.
    token = uuid.uuid4().hex + ".owner"
    staging = tempfile.mkdtemp(prefix=".lock-", dir=os.path.dirname(path))
    staged_marker = os.path.join(staging, token)
    descriptor = os.open(staged_marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + wait
    acquired = False
    try:
        while True:
            try:
                # POSIX rename cannot replace a nonempty directory. Even an
                # interrupted release leaves either our successor or an empty
                # directory that can safely be replaced with this prepared lock.
                os.rename(staging, path)
                acquired = True
                break
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                try:
                    remove_abandoned_lock(path)
                except FileNotFoundError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("ownership record is busy")
                time.sleep(0.01)
        _HELD_LOCKS[path] = token
        try:
            yield
        finally:
            _HELD_LOCKS.pop(path, None)
            try:
                os.unlink(os.path.join(path, token))
            except FileNotFoundError:
                pass
            else:
                try:
                    os.rmdir(path)
                except OSError:
                    pass
    finally:
        if not acquired:
            try:
                os.unlink(staged_marker)
                os.rmdir(staging)
            except FileNotFoundError:
                pass


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict) or not isinstance(config.get("enabled"), bool):
        raise ValueError("invalid ownership configuration")
    if not config["enabled"]:
        return config
    if not os.path.isabs(config.get("state_dir", "")):
        raise ValueError("state_dir must be an absolute path")
    if config.get("tag_key", "user") != "user":
        raise ValueError("only the existing user tag is supported")
    if config.get("management_value", "Management") != "Management":
        raise ValueError("management_value must be Management")
    for field, default, low, high in (("oci_timeout", 10, 1, 60), ("slurm_timeout", 5, 1, 30),
                                      ("run_timeout", 45, 1, 180), ("history_max_bytes", 1048576, 1024, 10485760),
                                      ("history_backups", 3, 0, 10), ("reconcile_grace_seconds", 10, 0, 300),
                                      ("drift_check_seconds", 300, 5, 3600)):
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("invalid {}".format(field))
        config[field] = value
    return config


def validate_identity(identity):
    if not isinstance(identity, dict):
        raise ValueError("invalid instance metadata")
    instance_id, region = identity.get("instance_id", ""), identity.get("region", "")
    if not isinstance(instance_id, str) or not re.match(r"^ocid1\.instance\.[A-Za-z0-9.-]+$", instance_id):
        raise ValueError("invalid compute instance OCID")
    if not isinstance(region, str) or not VALID_REGION.match(region):
        raise ValueError("invalid OCI region")
    return identity


def boot_identity():
    with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as stream:
        return stream.read(128).strip()


def instance_identity(config, refresh=False):
    directory = config.get("local_state_dir", "/var/lib/slurm-oci-user-tags")
    private_directory(directory)
    cache_path = os.path.join(directory, "instance.json")
    boot_id = boot_identity()
    if not refresh:
        try:
            cached = validate_identity(read_json(cache_path))
            if cached.get("boot_id") == boot_id:
                return {"instance_id": cached["instance_id"], "region": cached["region"]}
        except FileNotFoundError:
            pass
    request = urllib.request.Request(IMDS_URL, headers={"Authorization": "Bearer Oracle"})
    # Never route instance metadata through proxy configuration from the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=1) as response:
        metadata = json.loads(response.read(65537).decode("utf-8"))
    region_info = metadata.get("regionInfo") or {}
    identity = validate_identity({
        "instance_id": metadata.get("id"),
        "region": metadata.get("canonicalRegionName") or region_info.get("regionIdentifier") or metadata.get("region"),
    })
    atomic_json(cache_path, dict(identity, boot_id=boot_id))
    return identity


def node_name(config, environment):
    name = config.get("node_name") or environment.get("SLURMD_NODENAME") or socket.gethostname().split(".")[0]
    if not isinstance(name, str) or not VALID_NODE.match(name):
        raise ValueError("invalid Slurm node name")
    return name


def record_path(config, identity):
    digest = hashlib.sha256(identity["instance_id"].encode("utf-8")).hexdigest()
    return os.path.join(config["state_dir"], digest + ".json")


def validate_jobs(jobs):
    if not isinstance(jobs, dict):
        raise ValueError("invalid allocation records")
    for job_id, details in jobs.items():
        if not isinstance(job_id, str) or not VALID_JOB.match(job_id) or not isinstance(details, dict):
            raise ValueError("invalid job record")
        user = details.get("user")
        if not isinstance(user, str) or not user or user in {"(null)", "N/A", "Unknown", "unknown", "None"} or len(user.encode("utf-8")) > 256 or any(c.isspace() or ord(c) < 32 for c in user):
            raise ValueError("invalid allocation user")
    return jobs


def validate_record(record):
    validate_identity(record)
    if record.get("version") != 1 or not VALID_NODE.match(record.get("node_name", "")):
        raise ValueError("invalid node record")
    if not isinstance(record.get("revision"), str) or not record["revision"]:
        raise ValueError("invalid record revision")
    validate_jobs(record.get("jobs"))
    return record


def history(config, path, event):
    history_path = path[:-5] + ".events.jsonl"
    maximum = config.get("history_max_bytes", 1048576)
    backups = config.get("history_backups", 3)
    line = json.dumps(dict(event, timestamp=utc_now()), sort_keys=True, separators=(",", ":")) + "\n"
    if os.path.exists(history_path):
        trusted(history_path)
        if os.path.getsize(history_path) + len(line.encode("utf-8")) > maximum:
            if backups:
                for number in range(backups, 0, -1):
                    source = history_path if number == 1 else history_path + "." + str(number - 1)
                    if os.path.exists(source):
                        os.replace(source, history_path + "." + str(number))
            else:
                os.unlink(history_path)
    descriptor = os.open(history_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(line)


def save_record(config, path, record, event):
    record["revision"] = uuid.uuid4().hex
    record["updated_at"] = utc_now()
    record.pop("next_attempt_at", None)
    record.pop("failures", None)
    atomic_json(path, record)
    history(config, path, dict(event, revision=record["revision"], node_name=record["node_name"], instance_id=record["instance_id"]))


def hook(config, mode, environment=None):
    environment = os.environ if environment is None else environment
    private_directory(config["state_dir"])
    identity = instance_identity(config, refresh=(mode == "register"))
    path = record_path(config, identity)
    name = node_name(config, environment)
    if mode != "register":
        job_id = environment.get("SLURM_JOB_ID") or environment.get("SLURM_JOBID", "")
        user = environment.get("SLURM_JOB_USER", "")
        details = {"user": user}
        # Slurm versions differ in which run identifiers they export to hooks.
        generation = environment.get("SLURM_JOB_START_TIME") or environment.get("SLURM_JOB_RESTART_COUNT") or environment.get("SLURM_RESTART_COUNT")
        if generation:
            details["generation"] = generation
        validate_jobs({job_id: details})
    with locked(path + ".lock"):
        try:
            record = validate_record(read_json(path))
            if record["instance_id"] != identity["instance_id"] or record["node_name"] != name:
                raise ValueError("registered node identity does not match")
        except FileNotFoundError:
            record = dict(identity, version=1, node_name=name, jobs={}, revision="new")
        if mode == "register":
            if record["revision"] == "new":
                save_record(config, path, record, {"event": "register", "owner": "Management"})
            return
        jobs = record["jobs"]
        if mode == "prolog":
            jobs[job_id] = details
        elif mode == "epilog":
            existing = jobs.get(job_id)
            if existing and existing["user"] == user:
                if not generation or not existing.get("generation") or existing["generation"] == generation:
                    del jobs[job_id]
        record["last_hook_at"] = time.time()
        save_record(config, path, record, {"event": mode, "job_id": job_id, "user": user})


def run(command, timeout):
    assert_locks_owned()
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("{} failed: {}".format(os.path.basename(command[0]), (result.stderr or result.stdout)[:500]))
    return result.stdout


def command_timeout(config, key, default):
    remaining = config.get("_deadline", time.monotonic() + 3600) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("ownership reconciliation time budget exhausted")
    return min(config.get(key, default), remaining)


def scheduler_allocations(config):
    directory = config.get("slurm_bin_dir", "/usr/bin")
    output = run([os.path.join(directory, "squeue"), "--local", "--noheader", "--all", "--array",
                  "--states=all", "--format=%A|%u|%T|%N"], command_timeout(config, "slurm_timeout", 5))
    allocations, expanded = {}, {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split("|")
        if len(fields) != 4:
            raise ValueError("malformed squeue response")
        job_id, user, state, hostlist = (value.strip() for value in fields)
        validate_jobs({job_id: {"user": user}})
        if state not in ACTIVE_STATES | INACTIVE_STATES:
            raise ValueError("unknown Slurm allocation state: {}".format(state))
        if state in INACTIVE_STATES:
            continue
        if hostlist in EMPTY_NODES:
            raise ValueError("active job has no node allocation")
        if hostlist not in expanded:
            nodes = run([os.path.join(directory, "scontrol"), "show", "hostnames", hostlist], command_timeout(config, "slurm_timeout", 5)).splitlines()
            if not nodes or any(not VALID_NODE.match(node) for node in nodes):
                raise ValueError("malformed Slurm hostlist expansion")
            expanded[hostlist] = nodes
        for name in expanded[hostlist]:
            jobs = allocations.setdefault(name, {})
            if job_id in jobs and jobs[job_id]["user"] != user:
                raise ValueError("conflicting Slurm job ownership")
            jobs[job_id] = {"user": user}
    return allocations


def snapshot_records(config):
    snapshots = {}
    for filename in sorted(os.listdir(config["state_dir"])):
        if filename.startswith(".lock-"):
            try:
                remove_abandoned_lock(os.path.join(config["state_dir"], filename))
            except (OSError, ValueError) as error:
                log("could not clean abandoned lock staging: {}".format(error))
            continue
        if not re.match(r"^[0-9a-f]{64}\.json$", filename):
            continue
        path = os.path.join(config["state_dir"], filename)
        try:
            record = validate_record(read_json(path))
            if record_path(config, record) != path:
                raise ValueError("instance record filename does not match its OCID")
            if not record.get("retired"):
                snapshots[path] = record
        except Exception as error:
            log("skipping invalid compute registration: {}".format(error))
    return snapshots


def correct_allocation(config, path, snapshot, allocations):
    with locked(path + ".lock"):
        record = validate_record(read_json(path))
        if record["revision"] != snapshot["revision"]:
            return
        if time.time() - record.get("last_hook_at", 0) < config.get("reconcile_grace_seconds", 10):
            return
        expected = allocations.get(record["node_name"], {})
        # Keep the optional hook generation when scheduler ownership is unchanged.
        current = {job_id: {"user": details["user"]} for job_id, details in record["jobs"].items()}
        if current != expected:
            record["jobs"] = expected
            save_record(config, path, record, {"event": "scheduler-correction", "jobs": expected})


def desired_owner(record):
    users = {details["user"] for details in record["jobs"].values()}
    if len(users) > 1:
        return None
    return next(iter(users)) if users else "Management"


def trusted_executable(path):
    path = os.path.realpath(path)
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    while True:
        details = os.stat(path)
        if details.st_uid != ROOT_UID or details.st_mode & 0o022:
            return False
        parent = os.path.dirname(path)
        if parent == path:
            return True
        path = parent


def find_oci(config):
    configured = config.get("oci_cli", "auto")
    candidates = (configured,) if configured != "auto" else ("/usr/local/bin/oci", "/usr/bin/oci", "/opt/oci-cli/bin/oci")
    for candidate in candidates:
        if os.path.isabs(candidate) and trusted_executable(candidate):
            return candidate
    raise RuntimeError("root-owned OCI CLI executable was not found")


def oci_command(config, record, operation):
    timeout = config.get("oci_timeout", 10)
    return [find_oci(config), "compute", "instance", operation,
            "--instance-id", record["instance_id"], "--auth", "instance_principal",
            "--region", record["region"], "--max-retries", "0", "--connection-timeout", str(min(3, timeout)),
            "--read-timeout", str(timeout), "--output", "json"]


def apply_latest(config, path):
    # Hook locks are never held across network calls. If an ownership event lands
    # during the API request, do not acknowledge that revision; repair it promptly.
    for attempt in range(2):
        record = validate_record(read_json(path))
        owner = desired_owner(record)
        if (record.get("applied_revision") == record["revision"] and
                time.time() - record.get("checked_at", 0) < config.get("drift_check_seconds", 300)):
            return
        response = json.loads(run(oci_command(config, record, "get"), command_timeout(config, "oci_timeout", 10)))
        data = response.get("data")
        etag = response.get("etag")
        if not isinstance(data, dict) or not isinstance(data.get("freeform-tags"), dict) or not etag:
            raise ValueError("OCI instance response is missing tags or ETag")
        if data.get("id") != record["instance_id"]:
            raise ValueError("OCI instance response has an unexpected OCID")
        if data.get("lifecycle-state") in {"TERMINATED", "TERMINATING"}:
            with locked(path + ".lock"):
                latest = validate_record(read_json(path))
                latest["retired"] = True
                latest["retired_at"] = utc_now()
                atomic_json(path, latest)
                history(config, path, {"event": "instance-retired", "instance_id": record["instance_id"],
                                       "lifecycle_state": data["lifecycle-state"]})
            return
        tags = dict(data["freeform-tags"])
        latest = validate_record(read_json(path))
        if latest["revision"] != record["revision"]:
            continue
        changed = ("user" in tags) if owner is None else tags.get("user") != owner
        if changed:
            if owner is None:
                tags.pop("user", None)
                log("multiple users share {}; removing ambiguous user tag".format(record["node_name"]))
            else:
                tags["user"] = owner
            command = oci_command(config, record, "update") + ["--freeform-tags", json.dumps(tags), "--if-match", str(etag), "--force"]
            run(command, command_timeout(config, "oci_timeout", 10))
        with locked(path + ".lock"):
            latest = validate_record(read_json(path))
            if latest["revision"] != record["revision"]:
                continue
            latest["applied_revision"] = record["revision"]
            latest["applied_owner"] = owner
            latest["applied_at"] = utc_now()
            latest["checked_at"] = time.time()
            latest.pop("next_attempt_at", None)
            latest.pop("failures", None)
            atomic_json(path, latest)
            history(config, path, {"event": "tag-updated" if changed else "tag-confirmed", "owner": owner,
                                   "revision": record["revision"], "instance_id": record["instance_id"]})
            return


def needs_attempt(config, record):
    now = time.time()
    if record.get("retired") or record.get("next_attempt_at", 0) > now:
        return False
    return (record.get("applied_revision") != record["revision"] or
            now - record.get("checked_at", 0) >= config.get("drift_check_seconds", 300))


def defer_attempt(config, path, revision):
    with locked(path + ".lock"):
        record = validate_record(read_json(path))
        if record["revision"] != revision:
            return
        record["failures"] = min(record.get("failures", 0) + 1, 10)
        record["next_attempt_at"] = time.time() + min(300, 5 * (2 ** (record["failures"] - 1)))
        atomic_json(path, record)


def reconcile(config):
    private_directory(config["state_dir"])
    deadline = time.monotonic() + min(config.get("run_timeout", 45), 180)
    config["_deadline"] = deadline
    with locked(os.path.join(config["state_dir"], ".worker.lock"), wait=0):
        # Capture revisions before reading Slurm; hooks newer than this snapshot win.
        snapshots = snapshot_records(config)
        failures = 0
        try:
            allocations = scheduler_allocations(config)
        except Exception as error:
            failures += 1
            log("scheduler reconciliation deferred: {}".format(error))
        else:
            for path, snapshot in snapshots.items():
                try:
                    correct_allocation(config, path, snapshot, allocations)
                except Exception as error:
                    failures += 1
                    log("allocation reconciliation failed: {}".format(error))
        # Retry only the newest state, never historical events. Old acknowledged
        # entries come last so an API failure cannot starve subsequent nodes.
        paths = sorted(snapshots, key=lambda path: snapshots[path].get("attempted_at", ""))
        for path in paths:
            if time.monotonic() >= deadline:
                break
            revision = None
            try:
                with locked(path + ".lock"):
                    record = validate_record(read_json(path))
                    if not needs_attempt(config, record):
                        continue
                    revision = record["revision"]
                    record["attempted_at"] = utc_now()
                    atomic_json(path, record)
                apply_latest(config, path)
            except Exception as error:
                failures += 1
                try:
                    defer_attempt(config, path, revision)
                except Exception as retry_error:
                    log("could not save retry delay: {}".format(retry_error))
                log("OCI ownership update deferred for {}: {}".format(os.path.basename(path), error))
        return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("register", "prolog", "epilog", "reconcile"))
    parser.add_argument("--config", default="/etc/slurm/oci-user-tags.json")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != ROOT_UID:
            raise PermissionError("ownership helper must run as root")
        config = load_config(args.config)
        if config["enabled"]:
            if args.mode == "reconcile":
                return reconcile(config)
            else:
                hook(config, args.mode)
    except Exception as error:
        # Billing visibility must never cause Slurm to drain a healthy node.
        log("{} failed: {}".format(args.mode, error))
        return 0 if args.mode in {"prolog", "epilog"} else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
