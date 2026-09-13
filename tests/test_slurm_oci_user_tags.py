import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "playbooks", "roles", "slurm", "files", "slurm_oci_user_tags.py")


class UserTagTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("oci_user_tags_test", HELPER)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {"enabled": True, "state_dir": self.directory.name, "node_name": "compute-1",
                       "reconcile_grace_seconds": 0}
        self.identity = {"instance_id": "ocid1.instance.oc1.ap-tokyo-1.example", "region": "ap-tokyo-1"}
        self.real_identity = self.module.instance_identity
        for patcher in (mock.patch.object(self.module, "ROOT_UID", os.geteuid()),
                        mock.patch.object(self.module, "instance_identity", return_value=self.identity),
                        mock.patch.object(self.module, "find_oci", return_value="/usr/bin/oci"),
                        mock.patch.object(self.module, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.module.hook(self.config, "register", {})
        self.path = self.module.record_path(self.config, self.identity)

    def event(self, mode, job="42", user="alice", **extra):
        environment = dict(SLURM_JOB_ID=job, SLURM_JOB_USER=user, **extra)
        self.module.hook(self.config, mode, environment)

    def record(self):
        return self.module.read_json(self.path)

    def get_response(self, user="Management", **tags):
        return json.dumps({"data": {"id": self.identity["instance_id"],
                                    "freeform-tags": dict(tags, user=user)}, "etag": "version-1"})

    def updated_tag(self, command):
        return json.loads(command[command.index("--freeform-tags") + 1])["user"]

    def test_hooks_record_owner_and_utc_history_without_external_commands(self):
        with mock.patch.object(self.module, "run") as run:
            self.event("prolog")
            self.assertEqual(self.module.desired_owner(self.record()), "alice")
            self.event("epilog")
            self.assertEqual(self.module.desired_owner(self.record()), "Management")
            run.assert_not_called()
        with open(self.path[:-5] + ".events.jsonl") as stream:
            events = [json.loads(line) for line in stream]
        self.assertEqual([event["event"] for event in events], ["register", "prolog", "epilog"])
        self.assertTrue(all(event["timestamp"].endswith("+00:00") for event in events))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_duplicate_events_and_late_other_job_epilog_preserve_new_owner(self):
        self.event("prolog")
        self.event("prolog")
        self.assertEqual(list(self.record()["jobs"]), ["42"])
        self.event("epilog")
        self.event("prolog", job="43", user="bob")
        self.event("epilog", job="42")
        self.event("epilog", job="42")
        self.assertEqual(self.module.desired_owner(self.record()), "bob")

    def test_requeue_generations_reject_old_epilog_when_exported(self):
        self.event("prolog", SLURM_JOB_RESTART_COUNT="0")
        self.event("epilog", SLURM_JOB_RESTART_COUNT="0")
        self.event("prolog", SLURM_JOB_RESTART_COUNT="1")
        self.event("epilog", SLURM_JOB_RESTART_COUNT="0")
        self.assertEqual(self.module.desired_owner(self.record()), "alice")
        self.event("epilog", SLURM_JOB_RESTART_COUNT="1")
        self.assertEqual(self.module.desired_owner(self.record()), "Management")

    def test_same_user_jobs_share_owner_and_ambiguous_users_remove_user_tag(self):
        self.event("prolog")
        self.event("prolog", job="43")
        self.assertEqual(self.module.desired_owner(self.record()), "alice")
        self.event("prolog", job="44", user="bob")
        self.assertIsNone(self.module.desired_owner(self.record()))
        with mock.patch.object(self.module, "run", side_effect=[self.get_response(user="alice", cluster_name="trial"), "{}"]) as run:
            self.module.apply_latest(self.config, self.path)
        command = run.call_args.args[0]
        tags = json.loads(command[command.index("--freeform-tags") + 1])
        self.assertEqual(tags, {"cluster_name": "trial"})
        self.assertIsNone(self.record()["applied_owner"])
        self.event("epilog", job="44", user="bob")
        self.assertEqual(self.module.desired_owner(self.record()), "alice")

    def test_scheduler_uses_raw_array_ids_and_expands_hostlist_once(self):
        response = "42|alice|RUNNING|compute-[1-2]\n43|alice|SUSPENDED|compute-[1-2]\n44|bob|PENDING|(null)\n"
        with mock.patch.object(self.module, "run", side_effect=[response, "compute-1\ncompute-2\n"]) as run:
            result = self.module.scheduler_allocations(self.config)
        command = run.call_args_list[0].args[0]
        self.assertIn("--format=%A|%u|%T|%N", command)
        self.assertIn("--states=all", command)
        self.assertIn("--array", command)
        self.assertIn("--local", command)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["compute-1"], {"42": {"user": "alice"}, "43": {"user": "alice"}})
        self.assertEqual(result["compute-2"], result["compute-1"])

    def test_malformed_scheduler_responses_never_become_empty_allocation(self):
        for response in ("garbage\n", "42|alice|UNKNOWN|compute-1\n", "42|alice|RUNNING|(null)\n",
                         "bad-id|alice|RUNNING|compute-1\n", "42||RUNNING|compute-1\n"):
            with self.subTest(response=response):
                with mock.patch.object(self.module, "run", return_value=response):
                    with self.assertRaises(ValueError):
                        self.module.scheduler_allocations(self.config)
        with mock.patch.object(self.module, "run", side_effect=["42|alice|RUNNING|compute-1\n", "bad host\n"]):
            with self.assertRaises(ValueError):
                self.module.scheduler_allocations(self.config)

    def test_scheduler_recovers_missed_epilog_and_missed_prolog(self):
        self.event("prolog")
        self.module.correct_allocation(self.config, self.path, self.record(), {})
        self.assertEqual(self.module.desired_owner(self.record()), "Management")
        self.module.correct_allocation(self.config, self.path, self.record(), {"compute-1": {"43": {"user": "bob"}}})
        self.assertEqual(self.module.desired_owner(self.record()), "bob")

    def test_hook_during_scheduler_query_wins_over_older_idle_snapshot(self):
        snapshot = self.record()
        self.event("prolog")
        self.module.correct_allocation(self.config, self.path, snapshot, {})
        self.assertEqual(self.module.desired_owner(self.record()), "alice")

    def test_grace_period_preserves_recent_hook_state(self):
        self.config["reconcile_grace_seconds"] = 10
        self.event("prolog")
        self.module.correct_allocation(self.config, self.path, self.record(), {})
        self.assertEqual(self.module.desired_owner(self.record()), "alice")
        with mock.patch.object(self.module.time, "time", return_value=self.record()["last_hook_at"] + 11):
            self.module.correct_allocation(self.config, self.path, self.record(), {})
        self.assertEqual(self.module.desired_owner(self.record()), "Management")

    def test_scheduler_unavailability_preserves_record_and_applies_latest_hook(self):
        self.event("prolog")
        with mock.patch.object(self.module, "scheduler_allocations", side_effect=RuntimeError("controller down")):
            with mock.patch.object(self.module, "apply_latest") as apply:
                self.assertEqual(self.module.reconcile(self.config), 1)
                apply.assert_called_once_with(self.config, self.path)
        self.assertEqual(self.module.desired_owner(self.record()), "alice")

    def test_oci_update_preserves_unrelated_tags_uses_etag_and_no_job_tag(self):
        self.event("prolog")
        with mock.patch.object(self.module, "run", side_effect=[self.get_response(cluster_name="trial", custom="keep"), "{}"]) as run:
            self.module.apply_latest(self.config, self.path)
        command, timeout = run.call_args.args
        tags = json.loads(command[command.index("--freeform-tags") + 1])
        self.assertEqual(tags, {"user": "alice", "cluster_name": "trial", "custom": "keep"})
        self.assertEqual(command[command.index("--if-match") + 1], "version-1")
        self.assertEqual(command[command.index("--auth") + 1], "instance_principal")
        self.assertNotIn("--defined-tags", command)
        self.assertLessEqual(timeout, 10)
        self.assertEqual(self.record()["applied_owner"], "alice")

    def test_revision_changed_during_get_never_writes_old_owner(self):
        self.event("prolog")
        updates = []
        changed = [False]
        def run(command, timeout):
            if command[3] == "get":
                if not changed[0]:
                    changed[0] = True
                    self.event("epilog")
                    self.event("prolog", job="43", user="bob")
                return self.get_response()
            updates.append(self.updated_tag(command))
            return "{}"
        with mock.patch.object(self.module, "run", side_effect=run):
            self.module.apply_latest(self.config, self.path)
        self.assertEqual(updates, ["bob"])
        self.assertEqual(self.record()["applied_owner"], "bob")

    def test_hook_during_update_is_not_blocked_and_latest_owner_is_repaired(self):
        self.event("prolog")
        updates = []
        def run(command, timeout):
            if command[3] == "get":
                return self.get_response()
            updates.append(self.updated_tag(command))
            if len(updates) == 1:
                self.event("epilog")
                self.event("prolog", job="43", user="bob")
            return "{}"
        with mock.patch.object(self.module, "run", side_effect=run):
            self.module.apply_latest(self.config, self.path)
        self.assertEqual(updates, ["alice", "bob"])
        self.assertEqual(self.record()["applied_revision"], self.record()["revision"])
        self.assertEqual(self.record()["applied_owner"], "bob")

    def test_failed_management_update_is_not_replayed_after_new_job(self):
        self.event("prolog")
        self.event("epilog")
        with mock.patch.object(self.module, "run", side_effect=[self.get_response(user="alice"), RuntimeError("ETag conflict")]):
            with self.assertRaises(RuntimeError):
                self.module.apply_latest(self.config, self.path)
        self.event("prolog", job="43", user="bob")
        with mock.patch.object(self.module, "run", side_effect=[self.get_response(user="alice"), "{}"]) as run:
            self.module.apply_latest(self.config, self.path)
        self.assertEqual(self.updated_tag(run.call_args.args[0]), "bob")

    def test_unchanged_state_skips_oci_until_periodic_drift_check(self):
        with mock.patch.object(self.module, "run", return_value=self.get_response()) as run:
            self.module.apply_latest(self.config, self.path)
            self.module.apply_latest(self.config, self.path)
            self.assertEqual(run.call_count, 1)
            with mock.patch.object(self.module.time, "time", return_value=self.record()["checked_at"] + 301):
                self.module.apply_latest(self.config, self.path)
            self.assertEqual(run.call_count, 2)

    def test_missing_etag_or_wrong_instance_never_updates(self):
        for response in ({"data": {"id": self.identity["instance_id"], "freeform-tags": {}}},
                         {"data": {"id": "ocid1.instance.wrong", "freeform-tags": {}}, "etag": "v1"}):
            with self.subTest(response=response):
                with mock.patch.object(self.module, "run", return_value=json.dumps(response)) as run:
                    with self.assertRaises(ValueError):
                        self.module.apply_latest(self.config, self.path)
                    self.assertEqual(run.call_count, 1)

    def test_only_valid_trusted_compute_records_are_registered(self):
        bad = dict(self.record(), instance_id="ocid1.volume.oc1.example")
        bad_path = os.path.join(self.directory.name, "a" * 64 + ".json")
        self.module.atomic_json(bad_path, bad)
        symlink = os.path.join(self.directory.name, "b" * 64 + ".json")
        os.symlink(self.path, symlink)
        self.assertEqual(list(self.module.snapshot_records(self.config)), [self.path])
        os.chmod(self.path, 0o666)
        self.assertEqual(self.module.snapshot_records(self.config), {})

    def test_history_rotation_is_bounded(self):
        self.config.update(history_max_bytes=500, history_backups=2)
        for number in range(20):
            self.event("prolog", job=str(number))
        files = [name for name in os.listdir(self.directory.name) if ".events.jsonl" in name]
        self.assertEqual(len(files), 3)
        for name in files:
            self.assertLessEqual(os.path.getsize(os.path.join(self.directory.name, name)), 500)

    def test_second_worker_cannot_overlap(self):
        with self.module.locked(os.path.join(self.directory.name, ".worker.lock")):
            with mock.patch.object(self.module, "scheduler_allocations") as scheduler:
                with self.assertRaises(TimeoutError):
                    self.module.reconcile(self.config)
                scheduler.assert_not_called()

    def test_hook_error_always_returns_zero(self):
        config_path = os.path.join(self.directory.name, "config.json")
        self.module.atomic_json(config_path, dict(self.config))
        with mock.patch.object(self.module, "hook", side_effect=RuntimeError("metadata unreachable")):
            self.assertEqual(self.module.main(["prolog", "--config", config_path]), 0)
        self.module.log.assert_called()

    def test_command_budget_is_bounded_and_exhaustion_does_not_run(self):
        with mock.patch.object(self.module.time, "monotonic", return_value=100):
            self.config["_deadline"] = 101
            self.assertEqual(self.module.command_timeout(self.config, "oci_timeout", 10), 1)
            self.config["_deadline"] = 99
            with self.assertRaises(TimeoutError):
                self.module.command_timeout(self.config, "oci_timeout", 10)

    def test_stale_nfs_lock_is_reclaimed_and_release_keeps_successor(self):
        path = os.path.join(self.directory.name, "stale.lock")
        os.mkdir(path, 0o700)
        marker = os.path.join(path, "a" * 32 + ".owner")
        with open(marker, "w"):
            pass
        os.chmod(marker, 0o600)
        os.utime(marker, (0, 0))
        with self.module.locked(path):
            markers = os.listdir(path)
            self.assertEqual(len(markers), 1)
            self.assertNotEqual(markers[0], os.path.basename(marker))
            os.unlink(os.path.join(path, markers[0]))
            successor = os.path.join(path, "b" * 32 + ".owner")
            with open(successor, "w"):
                pass
            os.chmod(successor, 0o600)
            with self.assertRaises(RuntimeError):
                self.module.atomic_json(self.path, self.record())
        self.assertTrue(os.path.exists(successor))

    def test_fresh_nfs_lock_cannot_be_reclaimed(self):
        path = os.path.join(self.directory.name, "fresh.lock")
        with self.module.locked(path):
            token = os.listdir(path)
            self.module.remove_abandoned_lock(path)
            self.assertEqual(os.listdir(path), token)
            with self.assertRaises(TimeoutError):
                with self.module.locked(path, wait=0):
                    self.fail("concurrent lock acquisition succeeded")

    def test_cached_metadata_is_bound_to_current_linux_boot(self):
        local = os.path.join(self.directory.name, "local")
        os.mkdir(local, 0o700)
        cache = os.path.join(local, "instance.json")
        self.module.atomic_json(cache, dict(self.identity, boot_id="old-boot"))
        config = dict(self.config, local_state_dir=local)
        metadata = {"id": "ocid1.instance.oc1.ap-tokyo-1.new", "canonicalRegionName": "ap-tokyo-1"}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(metadata).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(self.module, "boot_identity", return_value="new-boot"):
            with mock.patch.object(self.module.urllib.request, "build_opener", return_value=opener):
                result = self.real_identity(config)
        self.assertEqual(result["instance_id"], metadata["id"])
        self.assertEqual(self.module.read_json(cache)["boot_id"], "new-boot")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.module.IMDS_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer Oracle")
        with mock.patch.object(self.module, "boot_identity", return_value="third-boot"):
            with mock.patch.object(self.module.urllib.request, "build_opener", side_effect=OSError("IMDS unavailable")):
                with self.assertRaises(OSError):
                    self.real_identity(config)

    def test_terminated_instance_is_retired_without_further_api_requests(self):
        response = json.loads(self.get_response(user="alice"))
        response["data"]["lifecycle-state"] = "TERMINATED"
        with mock.patch.object(self.module, "run", return_value=json.dumps(response)) as run:
            self.module.apply_latest(self.config, self.path)
            self.assertEqual(run.call_count, 1)
        self.assertTrue(self.record()["retired"])
        self.assertEqual(self.module.snapshot_records(self.config), {})
        self.assertTrue(os.path.exists(self.path[:-5] + ".events.jsonl"))

    def test_failed_old_records_back_off_and_do_not_starve_new_instance(self):
        old_paths = []
        for number in range(5):
            identity = dict(self.identity, instance_id="ocid1.instance.oc1.ap-tokyo-1.old{}".format(number))
            path = self.module.record_path(self.config, identity)
            record = dict(self.record(), **identity)
            record.update(attempted_at="2000-01-01", next_attempt_at=self.module.time.time() + 300)
            self.module.atomic_json(path, record)
            old_paths.append(path)
        with mock.patch.object(self.module, "scheduler_allocations", return_value={}):
            with mock.patch.object(self.module, "apply_latest") as apply:
                self.assertEqual(self.module.reconcile(self.config), 0)
        apply.assert_called_once_with(self.config, self.path)
        with mock.patch.object(self.module, "scheduler_allocations", return_value={}):
            with mock.patch.object(self.module, "apply_latest", side_effect=RuntimeError("NotAuthorizedOrNotFound")):
                self.assertEqual(self.module.reconcile(self.config), 1)
        self.assertGreater(self.record()["next_attempt_at"], self.module.time.time())
        self.assertFalse(self.record().get("retired", False))
        self.event("prolog")
        self.assertNotIn("next_attempt_at", self.record())

    def test_register_failure_is_visible_but_slurm_hook_failure_is_not_fatal(self):
        config_path = os.path.join(self.directory.name, "config.json")
        self.module.atomic_json(config_path, dict(self.config))
        with mock.patch.object(self.module, "hook", side_effect=RuntimeError("metadata unreachable")):
            self.assertEqual(self.module.main(["register", "--config", config_path]), 1)
            self.assertEqual(self.module.main(["epilog", "--config", config_path]), 0)

    def test_stale_empty_reaper_cannot_remove_new_atomically_published_lock(self):
        path = os.path.join(self.directory.name, "empty.lock")
        os.mkdir(path, 0o700)
        os.utime(path, (0, 0))
        old_stat = os.stat(path)
        real_stat = self.module.os.stat
        successor = self.module.locked(path)
        interleaved = [False]
        def stat_during_reap(target, *args, **kwargs):
            if target == path and not interleaved[0]:
                interleaved[0] = True
                successor.__enter__()
                return old_stat
            return real_stat(target, *args, **kwargs)
        with mock.patch.object(self.module.os, "stat", side_effect=stat_during_reap):
            self.module.remove_abandoned_lock(path)
        try:
            self.assertEqual(len(os.listdir(path)), 1)
            self.module.assert_locks_owned()
            with self.assertRaises(TimeoutError):
                with self.module.locked(path, wait=0):
                    self.fail("second owner replaced a published lock")
        finally:
            successor.__exit__(None, None, None)

    def test_independent_processes_serialize_shared_record_updates(self):
        counter = os.path.join(self.directory.name, "counter.json")
        self.module.atomic_json(counter, 0)
        program = """import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('worker', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.ROOT_UID = os.geteuid()
for unused in range(10):
    with m.locked(sys.argv[2] + '.lock', wait=5):
        value = m.read_json(sys.argv[2])
        m.atomic_json(sys.argv[2], value + 1)
"""
        processes = [subprocess.Popen([sys.executable, "-c", program, HELPER, counter],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for unused in range(4)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr.decode("utf-8"))
        self.assertEqual(self.module.read_json(counter), 40)

    def test_empty_registry_is_healthy_when_cluster_has_no_allocations(self):
        os.unlink(self.path)
        with mock.patch.object(self.module, "scheduler_allocations", return_value={}):
            with mock.patch.object(self.module, "apply_latest") as apply:
                self.assertEqual(self.module.reconcile(self.config), 0)
                apply.assert_not_called()
        self.module.log.assert_not_called()

    def test_allocated_nodes_without_registration_report_shared_path_and_failure(self):
        os.unlink(self.path)
        allocations = {"compute-1": {"42": {"user": "alice"}}}
        with mock.patch.object(self.module, "scheduler_allocations", return_value=allocations):
            with mock.patch.object(self.module, "apply_latest") as apply:
                self.assertEqual(self.module.reconcile(self.config), 1)
                apply.assert_not_called()
        message = self.module.log.call_args.args[0]
        self.assertIn("1 allocated Slurm node(s) have no compute registration", message)
        self.assertIn(self.config["state_dir"], message)
        self.assertIn("compute-1", message)

    def test_unregistered_allocations_do_not_block_valid_registered_nodes(self):
        allocations = {"compute-1": {"42": {"user": "alice"}}}
        allocations.update({"missing-{:02d}".format(number): {"43": {"user": "bob"}}
                            for number in range(20)})
        with mock.patch.object(self.module, "scheduler_allocations", return_value=allocations):
            with mock.patch.object(self.module, "apply_latest") as apply:
                self.assertEqual(self.module.reconcile(self.config), 1)
                apply.assert_called_once_with(self.config, self.path)
        self.assertEqual(self.module.desired_owner(self.record()), "alice")
        message = self.module.log.call_args.args[0]
        self.assertIn("20 allocated Slurm node(s) have no compute registration", message)
        self.assertIn("missing-04", message)
        self.assertNotIn("missing-05", message)
        self.assertIn(", ...", message)


if __name__ == "__main__":
    unittest.main()
