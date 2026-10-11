#!/usr/bin/env python3
"""Save and deliver autoscaling creation failures independently of Slurm mail."""

import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


DEFAULT_CONFIG = Path("/opt/oci-hpc/conf/autoscaling_failure_notification.json")
SENSITIVE = re.compile(r"password|passphrase|private.?key|secret|authorization|token", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
MAX_BODY = 12000


def clean(value, limit=1200):
    lines = []
    for line in ANSI.sub("", str(value)).splitlines():
        if SENSITIVE.search(line):
            lines.append("[機密情報を含む行を省略]")
        else:
            lines.append("".join(c for c in line if c.isprintable() or c == "\t"))
    return "\n".join(lines)[:limit]


def oci_json(config, *args):
    command = [shutil.which("oci") or "oci", *args, "--region", config["region"],
               "--max-retries", "0", "--connection-timeout", "5", "--read-timeout", "12"]
    if config["instance_principal"]:
        command += ["--auth", "instance_principal"]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=20, check=True)
        return json.loads(result.stdout).get("data", [])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        detail = getattr(error, "stderr", None) or error
        print("OCI lookup failed: {}: {}".format(args[:3], clean(detail, 300)), file=sys.stderr)
        return None


def state_resources(workspace):
    """Read IDs only; never print or attach Terraform state to a message."""
    try:
        state = json.loads((workspace / "terraform.tfstate").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = []
    names = {"instance_pool", "cluster_network", "compute_cluster", "compute_cluster_instances"}
    for resource in state.get("resources", []):
        if resource.get("name") not in names:
            continue
        for instance in resource.get("instances", []):
            resource_id = instance.get("attributes", {}).get("id")
            if isinstance(resource_id, str) and resource_id.startswith("ocid1."):
                found.append((resource["name"], resource_id))
    return found


def created_resources(config, mode, cluster, started):
    # A failed create may leave no state. Accept only a unique resource with the
    # exact display name created during this attempt; do not use the newest name.
    commands = {
        "pool": (("compute-management", "instance-pool", "list"), "instance_pool"),
        "network": (("compute-management", "cluster-network", "list"), "cluster_network"),
        "compute": (("compute", "compute-cluster", "list"), "compute_cluster"),
    }
    command, name = commands[mode]
    resources = oci_json(config, *command, "--compartment-id", config["compartment_id"], "--all")
    if not isinstance(resources, list):
        return []
    matches = []
    for resource in resources:
        if resource.get("display-name") != cluster:
            continue
        created = parse_time(resource.get("time-created"))
        if created is not None and created >= started - 60:
            matches.append((name, resource.get("id")))
    return matches if len(matches) == 1 else []


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        normalized = re.sub(r"\.\d+", "", value.replace("Z", "+00:00"))
        if normalized.endswith("+00:00"):
            normalized = normalized[:-6]
        return datetime.datetime.strptime(normalized[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def work_request_details(config, workspace, mode, cluster, started):
    allowed = {
        "pool": {"instance_pool"},
        "network": {"cluster_network", "instance_pool"},
        "compute": {"compute_cluster", "compute_cluster_instances"},
    }[mode]
    resources = [(kind, resource_id) for kind, resource_id in state_resources(workspace)
                 if kind in allowed]
    discovered = created_resources(config, mode, cluster, started)
    resources = list(dict.fromkeys(resources + discovered))
    details = []
    for kind, resource_id in resources:
        if kind not in allowed:
            continue
        requests = oci_json(config, "work-requests", "work-request", "list",
                            "--compartment-id", config["compartment_id"],
                            "--resource-id", resource_id, "--all")
        if not isinstance(requests, list):
            continue
        for request in requests:
            accepted = parse_time(request.get("time-accepted"))
            if request.get("status") != "FAILED" or accepted is None or accepted < started - 60:
                continue
            request_id = request.get("id")
            if not isinstance(request_id, str) or not request_id.startswith("ocid1."):
                continue
            errors = oci_json(config, "work-requests", "work-request-error", "list",
                              "--work-request-id", request_id, "--all")
            logs = oci_json(config, "work-requests", "work-request-log-entry", "list",
                            "--work-request-id", request_id, "--all")
            error_text = [item.get("message", "") for item in errors or [] if isinstance(item, dict)]
            log_text = [item.get("message", "") for item in logs or [] if isinstance(item, dict)]
            details.append("{} / {} / {}\nerror: {}\nlog: {}".format(
                kind, clean(request.get("operation-type", "unknown"), 100), request_id,
                clean("\n".join(error_text), 1800) or "取得できませんでした",
                clean("\n".join(log_text[-5:]), 1000) or "取得できませんでした"))
    return "\n\n".join(details) or "該当する失敗 Work Request を特定できませんでした。"


def log_diagnostic(log_path):
    try:
        with open(log_path, encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()[-1500:]
    except OSError:
        return "作成ログを読み取れませんでした。"
    task = ""
    failures = []
    errors = []
    for line in lines:
        if line.startswith("TASK ["):
            task = line.strip()
        if "FAILED!" in line or "UNREACHABLE!" in line:
            failures.append("{}\n{}".format(task, line.strip()))
        if "Error:" in line or "Failed to " in line:
            errors.append(line.strip())
    if failures:
        return "Ansible: " + clean("\n".join(failures[-3:]), 4000)
    if errors:
        return "Terraform/構築: " + clean("\n".join(errors[-8:]), 4000)
    return "ログに具体的なエラー行がありません。終了コードと作成ログを確認してください。"


def publish(config, message):
    descriptor, input_path = tempfile.mkstemp(prefix=".publish-", dir=config["spool"])
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"topicId": config["topic_id"], "title": message["title"],
                       "body": message["body"]}, stream, ensure_ascii=False)
        command = [shutil.which("oci") or "oci", "ons", "message", "publish",
                   "--from-json", "file://" + input_path, "--region", config["region"],
                   "--max-retries", "0", "--connection-timeout", "5", "--read-timeout", "12"]
        if config["instance_principal"]:
            command += ["--auth", "instance_principal"]
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=20, check=True)
        return True
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", None) or error
        print("Autoscaling failure notification delivery failed: {}".format(clean(detail, 300)), file=sys.stderr)
        return False
    finally:
        os.unlink(input_path)


def save_message(spool, message):
    spool.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".failure-", dir=str(spool))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(message, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, spool / (message["id"] + ".json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def retry(config, spool):
    if not spool.is_dir():
        return
    with open(spool / ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in sorted(spool.glob("*.json")):
            try:
                message = json.loads(path.read_text(encoding="utf-8"))
                if message["topic_id"] != config["topic_id"]:
                    print("Notification topic changed; retained {} for manual review".format(path), file=sys.stderr)
                    continue
                if publish(config, message):
                    path.unlink()
            except (OSError, ValueError, KeyError) as error:
                print("Notification retry failed for {}: {}".format(path, clean(error, 300)), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("report", "retry"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cluster")
    parser.add_argument("--queue")
    parser.add_argument("--instance-type")
    parser.add_argument("--nodes")
    parser.add_argument("--job-id")
    parser.add_argument("--mode", choices=("pool", "network", "compute"))
    parser.add_argument("--started", type=int)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0  # Existing installations without the optional feature.
    if not config.get("enabled") or not config.get("topic_id"):
        return 0
    spool = Path(config["spool"])
    if args.action == "retry":
        retry(config, spool)
        return 0
    if not all((args.cluster, args.queue, args.instance_type, args.nodes, args.mode,
                args.started, args.log, args.workspace)):
        parser.error("report requires cluster, queue, instance-type, nodes, mode, started, log and workspace")
    body = ("Cluster: {}\nQueue: {}\nInstance type: {}\nNodes: {}\nJob ID: {}\n"
            "Started (UTC): {}\nMode: {}\nCreation log: {}\n\n"
            "Failure:\n{}\n\nOCI Work Request:\n{}").format(
        clean(args.cluster, 150), clean(args.queue, 150), clean(args.instance_type, 150),
        clean(args.nodes, 30), clean(args.job_id or "-", 100),
        datetime.datetime.fromtimestamp(args.started, datetime.timezone.utc).isoformat(),
        args.mode, clean(args.log, 300), log_diagnostic(args.log),
        work_request_details(config, args.workspace, args.mode, args.cluster, args.started))
    message = {
        "id": "{}-{}".format(args.started, re.sub(r"[^A-Za-z0-9_-]", "_", args.cluster)),
        "topic_id": config["topic_id"],
        "title": "Autoscaling 作成失敗: {}".format(clean(args.cluster, 150)),
        "body": body[:MAX_BODY],
    }
    save_message(spool, message)
    retry(config, spool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
