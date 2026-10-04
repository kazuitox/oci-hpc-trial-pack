#!/usr/bin/python3
"""Bounded, read-only Slurm readiness checks (Slurm 23.02 text output)."""
import argparse
import os
import re
import signal
import subprocess
import sys
import time


def valid_tres(output):
    rows = [line.strip().split("|") for line in output.splitlines() if line.strip()]
    if not rows or any(len(row) != 3 or not row[2].isdigit() or int(row[2]) <= 0 for row in rows):
        return False
    return all(any(row[0] == kind and row[1] == "" for row in rows) for kind in ("cpu", "mem"))


def valid_ping(output, controller):
    return re.search(r"^Slurmctld\(" + re.escape(controller) + r"\) at \S+ is UP\s*$", output, re.M) is not None


def run_command(argv, env, timeout):
    # A separate process group also bounds children holding stdout/stderr open.
    with subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, start_new_session=True) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            return 124, stdout, "command timed out; " + stderr


def wait_ready(argv, validate, total_timeout=180, command_timeout=15, interval=5):
    env = os.environ.copy()
    env.pop("SLURM_CLUSTERS", None)
    deadline = time.monotonic() + total_timeout
    attempts = 0
    last = "no attempt completed"
    while time.monotonic() < deadline:
        attempts += 1
        try:
            rc, stdout, stderr = run_command(argv, env, min(command_timeout, deadline - time.monotonic()))
        except OSError as exc:
            rc, stdout, stderr = 127, "", str(exc)
        if rc == 0 and validate(stdout):
            return attempts
        last = "rc={}; stdout={!r}; stderr={!r}".format(rc, stdout[-2000:], stderr[-2000:])
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    raise RuntimeError("readiness deadline {}s exceeded after {} attempts: {}: {}".format(
        total_timeout, attempts, " ".join(argv), last))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dbd", "controller", "reconfigure"))
    parser.add_argument("--bin-dir", required=True)
    parser.add_argument("--controller", choices=("primary", "backup"), default="primary")
    parser.add_argument("--total-timeout", type=float, default=180)
    parser.add_argument("--command-timeout", type=float, default=15)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if min(args.total_timeout, args.command_timeout, args.interval) <= 0:
        parser.error("timeouts and interval must be positive")
    if args.mode == "dbd":
        argv = [os.path.join(args.bin_dir, "sacctmgr"), "-nP", "show", "tres", "format=Type,Name,ID"]
        validate = valid_tres
    else:
        argv = [os.path.join(args.bin_dir, "scontrol"), "ping"]
        validate = lambda output: valid_ping(output, args.controller)
    try:
        attempts = wait_ready(argv, validate, args.total_timeout, args.command_timeout, args.interval)
        print("{} ready after {} attempts".format(args.mode, attempts))
        if args.mode == "reconfigure":
            attempts = wait_ready([os.path.join(args.bin_dir, "scontrol"), "reconfigure"],
                                  lambda output: True, 45, args.command_timeout, args.interval)
            print("reconfigure succeeded after {} attempts".format(attempts))
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
