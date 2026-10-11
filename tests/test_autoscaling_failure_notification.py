import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "autoscaling_failure_notify", ROOT / "bin/autoscaling_failure_notify.py"
)
notify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notify)


class AutoscalingFailureNotificationTests(unittest.TestCase):
    def test_ansible_failure_reports_task_host_and_message_without_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "create.log"
            log.write_text(
                "TASK [localdisk : Create volume group] ***\n"
                "fatal: [node-1]: FAILED! => {\"msg\": \"device is partitioned\"}\n"
                "password=should-not-be-mailed\n", encoding="utf-8"
            )
            result = notify.log_diagnostic(log)
            self.assertIn("localdisk : Create volume group", result)
            self.assertIn("node-1", result)
            self.assertIn("device is partitioned", result)
            self.assertNotIn("should-not-be-mailed", result)

    def test_work_request_uses_failed_operation_for_current_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "terraform.tfstate").write_text(json.dumps({"resources": [{
                "name": "instance_pool", "instances": [{"attributes": {
                    "id": "ocid1.instancepool.test"}}]}]}), encoding="utf-8")
            config = {"region": "us-ashburn-1", "compartment_id": "ocid1.compartment.test",
                      "instance_principal": True}

            def response(_, *args):
                if args[1:3] == ("work-request", "list"):
                    self.assertIn("ocid1.instancepool.test", args)
                    return [
                        {"id": "ocid1.workrequest.old", "status": "FAILED",
                         "time-accepted": "2020-01-01T00:00:00Z"},
                        {"id": "ocid1.workrequest.ok", "status": "SUCCEEDED",
                         "time-accepted": "2026-10-01T00:00:00Z"},
                        {"id": "ocid1.workrequest.fail", "status": "FAILED",
                         "operation-type": "LaunchInstancesInPool",
                         "time-accepted": "2026-10-01T00:00:00Z"},
                    ]
                if args[1:3] == ("work-request-error", "list"):
                    return [{"message": "Out of host capacity"}]
                return [{"message": "Failed to launch instances"}]

            with mock.patch.object(notify, "oci_json", side_effect=response):
                detail = notify.work_request_details(
                    config, workspace, "pool", "cluster-a", 1790812800)
            self.assertIn("LaunchInstancesInPool", detail)
            self.assertIn("Out of host capacity", detail)
            self.assertNotIn("ocid1.workrequest.old", detail)
            self.assertNotIn("ocid1.workrequest.ok", detail)

    def test_compute_cluster_instance_failure_is_queried_by_instance_id(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "terraform.tfstate").write_text(json.dumps({"resources": [{
                "name": "compute_cluster_instances", "instances": [{"attributes": {
                    "id": "ocid1.instance.test"}}]}]}), encoding="utf-8")
            config = {"region": "us-ashburn-1", "compartment_id": "ocid1.compartment.test",
                      "instance_principal": True}
            calls = []

            def response(_, *args):
                calls.append(args)
                if args[:3] == ("compute", "compute-cluster", "list"):
                    return []
                if args[1:3] == ("work-request", "list"):
                    return [{"id": "ocid1.workrequest.compute", "status": "FAILED",
                             "operation-type": "LaunchInstance",
                             "time-accepted": "2026-10-01T00:00:01Z"}]
                if args[1:3] == ("work-request-error", "list"):
                    return [{"message": "Out of capacity"}]
                return []

            with mock.patch.object(notify, "oci_json", side_effect=response):
                detail = notify.work_request_details(
                    config, workspace, "compute", "cluster-a", 1790812800)
            self.assertIn("LaunchInstance", detail)
            self.assertIn("Out of capacity", detail)
            self.assertTrue(any("ocid1.instance.test" in call for call in calls))

    def test_resource_discovery_rejects_old_and_ambiguous_names(self):
        config = {"compartment_id": "ocid1.compartment.test"}
        resources = [
            {"display-name": "cluster-a", "id": "ocid1.clusternetwork.old",
             "time-created": "2020-01-01T00:00:00Z"},
            {"display-name": "cluster-b", "id": "ocid1.clusternetwork.other",
             "time-created": "2026-10-01T00:00:00Z"},
            {"display-name": "cluster-a", "id": "ocid1.clusternetwork.current",
             "time-created": "2026-10-01T00:00:00Z"},
        ]
        with mock.patch.object(notify, "oci_json", return_value=resources):
            self.assertEqual(notify.created_resources(config, "network", "cluster-a", 1790812800),
                             [("cluster_network", "ocid1.clusternetwork.current")])
            resources.append({"display-name": "cluster-a", "id": "ocid1.clusternetwork.second",
                              "time-created": "2026-10-01T00:00:01Z"})
            self.assertEqual(notify.created_resources(config, "network", "cluster-a", 1790812800), [])

    def test_failed_publish_retains_spool_then_retry_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = Path(directory) / "spool"
            message = {"id": "attempt-1", "topic_id": "ocid1.onstopic.test",
                       "title": "failed", "body": "details"}
            notify.save_message(spool, message)
            config = {"topic_id": message["topic_id"]}
            with mock.patch.object(notify, "publish", side_effect=[False, True]) as publish:
                notify.retry(config, spool)
                self.assertTrue((spool / "attempt-1.json").exists())
                notify.retry(config, spool)
                self.assertFalse((spool / "attempt-1.json").exists())
            self.assertEqual(publish.call_count, 2)

    def test_publish_passes_body_in_private_file_not_process_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"topic_id": "ocid1.onstopic.test", "region": "us-ashburn-1",
                      "instance_principal": True, "spool": directory}
            message = {"title": "failure", "body": "node detail"}

            def inspect(command, **_kwargs):
                self.assertNotIn("node detail", command)
                self.assertIn("--auth", command)
                input_path = Path(command[command.index("--from-json") + 1][7:])
                self.assertEqual(json.loads(input_path.read_text())["body"], "node detail")

            with mock.patch.object(notify.subprocess, "run", side_effect=inspect):
                self.assertTrue(notify.publish(config, message))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_disabled_feature_neither_looks_up_oci_nor_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"enabled": false, "topic_id": ""}', encoding="utf-8")
            with mock.patch.object(notify, "oci_json") as lookup:
                self.assertEqual(notify.main(["report", "--config", str(config)]), 0)
            lookup.assert_not_called()

    def test_oci_lookup_accepts_python36_subprocess_arguments(self):
        real_run = subprocess.run

        # Explicit Python 3.6-compatible signature rejects capture_output/text.
        def run36(command, *, stdout=None, stderr=None, universal_newlines=False,
                  timeout=None, check=False):
            return real_run(command, stdout=stdout, stderr=stderr,
                            universal_newlines=universal_newlines,
                            timeout=timeout, check=check)

        config = {"region": "us-ashburn-1", "instance_principal": False}
        with mock.patch.object(notify.shutil, "which", return_value=sys.executable), \
                mock.patch.object(notify.subprocess, "run", side_effect=run36):
            result = notify.oci_json(
                config, "-c", 'import json; print(json.dumps({"data": [{"message": "失敗原因"}]}))')
        self.assertEqual(result, [{"message": "失敗原因"}])

    def test_report_with_lookup_error_preserves_timeout_and_retries_publish_on_python36(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spool = root / "spool"
            config_path = root / "config.json"
            config = {"enabled": True, "topic_id": "ocid1.onstopic.test",
                      "region": "us-ashburn-1", "instance_principal": True,
                      "compartment_id": "ocid1.compartment.test", "spool": str(spool)}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            log = root / "create.log"
            log.write_text("Error: timeout while waiting for state to become 'RUNNING' "
                           "(last state: 'PROVISIONING', timeout: 30m0s)\n", encoding="utf-8")
            sent = []

            def run36(command, *, stdout=None, stderr=None, universal_newlines=False,
                      timeout=None, check=False):
                self.assertIs(stdout, subprocess.PIPE)
                self.assertIs(stderr, subprocess.PIPE)
                self.assertTrue(universal_newlines)
                self.assertTrue(check)
                self.assertEqual(timeout, 20)
                if command[1:4] != ["ons", "message", "publish"]:
                    raise subprocess.CalledProcessError(1, command, stderr="lookup denied")
                path = Path(command[command.index("--from-json") + 1][7:])
                sent.append(json.loads(path.read_text(encoding="utf-8")))
                if len(sent) == 1:
                    raise subprocess.CalledProcessError(1, command, stderr="temporary publish error")
                return subprocess.CompletedProcess(command, 0, "{}", "")

            with mock.patch.object(notify.subprocess, "run", side_effect=run36):
                self.assertEqual(notify.main([
                    "report", "--config", str(config_path), "--cluster", "compute-1-opt",
                    "--queue", "compute", "--instance-type", "opt", "--nodes", "1",
                    "--job-id", "123", "--mode", "network", "--started", "1791721740",
                    "--log", str(log), "--workspace", str(root),
                ]), 0)
                messages = list(spool.glob("*.json"))
                self.assertEqual(len(messages), 1)
                message = json.loads(messages[0].read_text(encoding="utf-8"))
                self.assertIn("PROVISIONING", message["body"])
                self.assertIn("30m0s", message["body"])
                self.assertIn("失敗 Work Request を特定できませんでした", message["body"])
                self.assertIn("Job ID: 123", message["body"])
                self.assertEqual(notify.main(["retry", "--config", str(config_path)]), 0)
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[0], sent[1])
            self.assertEqual(list(spool.glob("*.json")), [])
            self.assertEqual(list(spool.glob(".publish-*")), [])

    def test_configuration_route_is_separate_from_slurm(self):
        resource = (ROOT / "notifications.tf").read_text(encoding="utf-8")
        deploy = (ROOT / "autoscaling-failure-notifications.tf").read_text(encoding="utf-8")
        shell = (ROOT / "bin/create_cluster.sh").read_text(encoding="utf-8")
        self.assertIn('resource "oci_ons_notification_topic" "autoscaling_failure"', resource)
        self.assertIn('resource "oci_ons_subscription" "autoscaling_failure_email"', resource)
        self.assertIn('null_resource.controller', deploy)
        self.assertNotIn('    null_resource.cluster,', deploy)
        self.assertLess(shell.index('autoscaling_failure_notify.py" report'),
                        shell.index('$folder/delete_cluster.sh $2 FORCE'))


if __name__ == "__main__":
    unittest.main()
