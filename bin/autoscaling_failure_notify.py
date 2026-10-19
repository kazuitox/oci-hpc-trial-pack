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


def oci_executable():
    executable = shutil.which("oci")
    if executable:
        return executable
    # Cron and interactive shells may expose different PATH values.
    candidates = [Path.home() / ".local/bin/oci", Path.home() / "bin/oci",
                  Path("/usr/local/bin/oci"), Path("/usr/bin/oci")]
    for candidate in candidates:
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return "oci"  # Let the existing error handling report a missing installation.


def oci_environment():
    # Click on Python 3.6 refuses an ASCII locale, including in cron jobs.
    env = os.environ.copy()
    env.update(LC_ALL="C.UTF-8", LANG="C.UTF-8")
    return env


def oci_json(config, *args):
    command = [oci_executable(), *args, "--region", config["region"],
               "--max-retries", "0", "--connection-timeout", "5", "--read-timeout", "12"]
    if config["instance_principal"]:
        command += ["--auth", "instance_principal"]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, encoding="utf-8", errors="replace",
                                timeout=20, check=True, env=oci_environment())
        return json.loads(result.stdout).get("data", [])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        detail = getattr(error, "stderr", None) or error
        diagnostic = "{}: {}".format(args[:3], clean(detail, 300))
        config.setdefault("_lookup_errors", []).append(diagnostic)
        print("OCI lookup failed: " + diagnostic, file=sys.stderr)
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
    config = dict(config)
    config["_lookup_errors"] = []
    allowed = {
        "pool": {"instance_pool"},
        "network": {"cluster_network", "instance_pool"},
        "compute": {"compute_cluster", "compute_cluster_instances"},
    }[mode]
    resources = [(kind, resource_id) for kind, resource_id in state_resources(workspace)
                 if kind in allowed]
    discovered = created_resources(config, mode, cluster, started)
    resources = list(dict.fromkeys(resources + discovered))
    if mode == "network":
        # OCI creates the pool inside the CN. Terraform may only track the CN,
        # and a timed-out CN can coexist with a FAILED pool work request.
        linked_pools = []
        for kind, network_id in resources:
            if kind != "cluster_network":
                continue
            network = oci_json(config, "compute-management", "cluster-network", "get",
                               "--cluster-network-id", network_id)
            if not isinstance(network, dict):
                continue
            pools = network.get("instance-pools")
            if not isinstance(pools, list):
                continue
            for pool in pools:
                pool_id = pool.get("id") if isinstance(pool, dict) else None
                if isinstance(pool_id, str) and pool_id.startswith("ocid1.instancepool."):
                    linked_pools.append(("instance_pool", pool_id))
        resources = list(dict.fromkeys(resources + linked_pools))
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
            details.append("{} / {} / {}\nResource: {}\nStatus: {}\nerror: {}\nlog: {}".format(
                kind, clean(request.get("operation-type", "unknown"), 100), request_id,
                resource_id, clean(request.get("status", "unknown"), 50),
                clean("\n".join(error_text), 1800) or "取得できませんでした",
                clean("\n".join(log_text[-5:]), 1000) or "取得できませんでした"))
    result = "\n\n".join(details) or "該当する失敗 Work Request を特定できませんでした。"
    if config["_lookup_errors"]:
        result += "\nOCI 情報取得エラー:\n" + clean("\n".join(config["_lookup_errors"]), 1200)
    return result


def log_diagnostic(log_path):
    try:
        with open(log_path, encoding="utf-8", errors="replace") as stream:
            lines = stream.readlines()[-1500:]
    except OSError:
        return "作成ログを読み取れませんでした。"
    # Terraform prefixes streamed output and repeats it in a wrapped footer.
    streamed = []
    normalized = []
    for line in lines:
        line = ANSI.sub("", line).rstrip("\n")
        match = re.match(r"^\S+ \(local-exec\):\s?(.*)$", line)
        if match:
            streamed.append(match.group(1))
            normalized.append(match.group(1))
        else:
            normalized.append(re.sub(r"^\s*│\s?", "", line))
    # Prefer the unwrapped stream when it contains the Ansible failure.
    diagnostic_lines = streamed if any(
        "FAILED!" in line or "UNREACHABLE!" in line for line in streamed) else normalized
    task = ""
    failures = []
    errors = [line.strip() for line in normalized
              if "Error:" in line or "Failed to " in line]
    for index, line in enumerate(diagnostic_lines):
        if line.startswith("TASK ["):
            task = line.strip()
        if "FAILED!" in line or "UNREACHABLE!" in line:
            block = [task, line.strip()]
            if "=>" in line and not line.rstrip().endswith("}"):
                for following in diagnostic_lines[index + 1:index + 31]:
                    if not following.strip() or following.startswith(("TASK [", "PLAY ", "fatal:")):
                        break
                    block.append(following.strip())
                    if following.rstrip().endswith("}"):
                        break
            failure = "\n".join(block)
            if failure not in failures:
                failures.append(failure)
    if failures:
        return "Ansible: " + clean("\n".join(failures[-3:]), 4000)
    if errors:
        return "Terraform/構築: " + clean("\n".join(errors[-8:]), 4000)
    return "ログに具体的なエラー行がありません。終了コードと作成ログを確認してください。"


def failure_phase(log_path, diagnostic):
    if diagnostic.startswith("Ansible: ") or any(message in diagnostic for message in (
            "Failed to configure autoscaling nodes", "Failed to configure nodes",
            "Failed to prepare autoscaling OS hostnames")):
        return "セットアップ"
    try:
        with open(log_path, encoding="utf-8", errors="replace") as stream:
            evidence = ANSI.sub("", "".join(stream.readlines()[-1500:]))
    except OSError:
        return "判定できませんでした"
    # Terraform's error resource identifies the stage, unlike its generic
    # 'local-exec provisioner error' or 'Error:' heading alone.
    if re.search(r"\bwith null_resource\.configure\b", evidence):
        return "セットアップ"
    if re.search(r"\bwith oci_core_(?:instance|instance_pool|cluster_network|compute_cluster)\.", evidence):
        return "インスタンス作成"
    if "timeout while waiting for state" in diagnostic and "'PROVISIONING'" in diagnostic:
        return "インスタンス作成"
    return "判定できませんでした"


def publish(config, message):
    descriptor, input_path = tempfile.mkstemp(prefix=".publish-", dir=config["spool"])
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"topicId": config["topic_id"], "title": message["title"],
                       "body": message["body"]}, stream, ensure_ascii=False)
        command = [oci_executable(), "ons", "message", "publish",
                   "--from-json", "file://" + input_path, "--region", config["region"],
                   "--max-retries", "0", "--connection-timeout", "5", "--read-timeout", "12"]
        if config["instance_principal"]:
            command += ["--auth", "instance_principal"]
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, encoding="utf-8", errors="replace",
                                timeout=20, check=True, env=oci_environment())
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
    failure = log_diagnostic(args.log)
    phase = failure_phase(args.log, failure)
    body = ("Cluster: {}\nQueue: {}\nInstance type: {}\nNodes: {}\nJob ID: {}\n"
            "Started (UTC): {}\nMode: {}\nFailure phase: {}\nCreation log: {}\n\n"
            "Failure:\n{}").format(
        clean(args.cluster, 150), clean(args.queue, 150), clean(args.instance_type, 150),
        clean(args.nodes, 30), clean(args.job_id or "-", 100),
        datetime.datetime.fromtimestamp(args.started, datetime.timezone.utc).isoformat(),
        args.mode, phase, clean(args.log, 300), failure)
    # Ansible setup runs after instance provisioning; its failure has no OCI
    # creation work request to diagnose. Keep OCI diagnostics for other failures.
    if phase != "セットアップ":
        body += "\n\nOCI Work Request:\n" + work_request_details(
            config, args.workspace, args.mode, args.cluster, args.started)
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
