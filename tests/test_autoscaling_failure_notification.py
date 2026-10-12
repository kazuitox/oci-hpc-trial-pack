import importlib.util
import json
import os
import re
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
    def test_home_region_discovery_includes_failure_notifications_alone(self):
        local = (ROOT / "locals.tf").read_text(encoding="utf-8")
        expression = re.search(r"(?m)^\s*needs_home_region\s*=\s*(.+)$", local).group(1)
        # Each feature independently requires IAM writes in the home region.
        # In particular, failure notifications must work with all other flags off.
        self.assertEqual(set(expression.split(" || ")), {
            "var.cost_tags_enabled",
            "var.create_iam_policy_dynamic_group",
            "var.slurm_job_notifications_enabled",
            "var.autoscaling_failure_notifications_enabled",
        })
        provider = (ROOT / "provider.tf").read_text(encoding="utf-8")
        self.assertRegex(provider, r'alias\s*=\s*"home"\s+region\s*=\s*local\.home_region')
        resources = (ROOT / "notifications.tf").read_text(encoding="utf-8")
        for kind in ("oci_identity_dynamic_group", "oci_identity_policy"):
            block = resources.split('resource "{}" "autoscaling_failure_controllers" {{'.format(kind), 1)[1].split("\n}", 1)[0]
            self.assertRegex(block, r"provider\s*=\s*oci\.home")
            self.assertRegex(block, r"count\s*=\s*var\.autoscaling_failure_notifications_enabled \? 1 : 0")

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

    def test_terraform_prefixed_ansible_failure_includes_task_and_avoids_footer_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "create.log"
            log.write_text(
                "null_resource.configure (local-exec): TASK [TEST - autoscaling setup script failure] ***\n"
                'null_resource.configure (local-exec): fatal: [node-1]: FAILED! => {"stderr": "AUTOSCALING_SETUP_FAILURE_TEST", "rc": 1}\n'
                "│ TASK [TEST - autoscaling setup script failure]\n"
                '│ fatal: [node-1]: FAILED! => {"stderr":\n'
                '│ "AUTOSCALING_SETUP_FAILURE_TEST", "rc": 1}\n', encoding="utf-8")
            diagnostic = notify.log_diagnostic(log)
            self.assertIn("TASK [TEST - autoscaling setup script failure]", diagnostic)
            self.assertIn("AUTOSCALING_SETUP_FAILURE_TEST", diagnostic)
            self.assertEqual(diagnostic.count("fatal: [node-1]"), 1)
            self.assertNotIn("null_resource.configure", diagnostic)

    def test_terraform_footer_only_keeps_multiline_failure_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "create.log"
            log.write_text(
                "│ TASK [setup : Run setup script]\n"
                '│ fatal: [node-1]: FAILED! => {"msg":\n'
                '│ "setup script failed", "stderr": "failure marker"}\n'
                "│\n│ PLAY RECAP\n", encoding="utf-8")
            diagnostic = notify.log_diagnostic(log)
            self.assertIn("TASK [setup : Run setup script]", diagnostic)
            self.assertIn("setup script failed", diagnostic)
            self.assertIn("failure marker", diagnostic)
            self.assertNotIn("PLAY RECAP", diagnostic)

    def test_notification_report_and_retry_find_user_cli_with_restricted_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            cli = home / ".local/bin/oci"
            cli.parent.mkdir(parents=True)
            cli.write_text("#!" + sys.executable + "\n" +
                           "import json,sys\n"
                           "print(json.dumps({'data': []}))\n", encoding="utf-8")
            cli.chmod(0o700)
            config = {"region": "us-ashburn-1", "instance_principal": True,
                      "topic_id": "ocid1.onstopic.test", "spool": str(root)}
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}), \
                    mock.patch.object(notify.Path, "home", return_value=home):
                self.assertEqual(notify.oci_json(config, "compute-management", "instance-pool", "list"), [])
                message = {"id": "attempt", "topic_id": config["topic_id"],
                           "title": "failed", "body": "failure detail"}
                self.assertTrue(notify.publish(config, message))
                notify.save_message(root, message)
                notify.retry(config, root)
                self.assertFalse((root / "attempt.json").exists())

    def test_cli_fallback_uses_system_installation_and_preserves_path_precedence(self):
        with mock.patch.object(notify.shutil, "which", return_value=None), \
                mock.patch.object(notify.Path, "is_file", return_value=True), \
                mock.patch.object(notify.os, "access", side_effect=lambda path, mode: str(path) == "/usr/local/bin/oci"):
            self.assertEqual(notify.oci_executable(), "/usr/local/bin/oci")
        with mock.patch.object(notify.shutil, "which", return_value="/custom/bin/oci"):
            self.assertEqual(notify.oci_executable(), "/custom/bin/oci")

    def test_ansible_report_omits_work_requests_and_never_queries_compute_apis(self):
        for mode in ["pool", "network", "compute"]:
            for failure in ["FAILED!", "UNREACHABLE!"]:
                with self.subTest(mode=mode, failure=failure), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = root / "config.json"
                    config.write_text(json.dumps({
                        "enabled": True, "topic_id": "ocid1.onstopic.test", "spool": str(root / "spool")
                    }), encoding="utf-8")
                    log = root / "create.log"
                    log.write_text(
                        "null_resource.configure (local-exec): TASK [setup : Run script] ***\n"
                        'null_resource.configure (local-exec): fatal: [node-1]: ' + failure +
                        ' => {"msg": "SETUP_FAILURE_TEST"}\n', encoding="utf-8")
                    delivered = []
                    with mock.patch.object(notify, "work_request_details") as work_requests, \
                            mock.patch.object(notify, "oci_json") as lookup, \
                            mock.patch.object(notify, "publish", side_effect=lambda _, message: delivered.append(message) or True):
                        self.assertEqual(notify.main([
                            "report", "--config", str(config), "--cluster", "cluster-a",
                            "--queue", "compute", "--instance-type", "default", "--nodes", "1",
                            "--job-id", "4", "--mode", mode, "--started", "1791724742",
                            "--log", str(log), "--workspace", str(root),
                        ]), 0)
                    work_requests.assert_not_called()
                    lookup.assert_not_called()
                    self.assertEqual(len(delivered), 1)
                    body = delivered[0]["body"]
                    self.assertIn("TASK [setup : Run script]", body)
                    self.assertIn("node-1", body)
                    self.assertIn("SETUP_FAILURE_TEST", body)
                    self.assertIn("Failure phase: セットアップ\n", body)
                    self.assertLess(body.index("Failure phase:"), body.index("Creation log:"))
                    self.assertNotIn("OCI Work Request", body)
                    self.assertNotIn("特定できませんでした", body)

    def test_failure_phase_uses_stage_evidence_and_keeps_unknown_errors_unknown(self):
        cases = [
            ("TASK [setup]\nfatal: [node]: FAILED! => {}\n", "セットアップ"),
            ("Error: local-exec provisioner error\n│ with null_resource.configure,\n", "セットアップ"),
            ("Failed to prepare autoscaling OS hostnames for cluster-a\n", "セットアップ"),
            ("Error: QuotaExceeded\n│ with oci_core_cluster_network.cluster_network[0],\n", "インスタンス作成"),
            ("Error: QuotaExceeded\n│ with oci_core_instance.compute_cluster_instances[0],\n", "インスタンス作成"),
            ("Error: timeout while waiting for state (last state: 'PROVISIONING')\n", "インスタンス作成"),
            ("Error: unrelated failure\n", "判定できませんでした"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "create.log"
            for source, expected in cases:
                with self.subTest(source=source):
                    log.write_text(source, encoding="utf-8")
                    self.assertEqual(notify.failure_phase(log, notify.log_diagnostic(log)), expected)
            missing = Path(directory) / "missing.log"
            self.assertEqual(notify.failure_phase(missing, notify.log_diagnostic(missing)), "判定できませんでした")

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

    def test_network_timeout_email_includes_linked_pool_quota_error(self):
        # The pool is nested in the CN API response, not an independent TF resource.
        for state_present in [True, False]:
            with self.subTest(state_present=state_present), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                network_id = "ocid1.clusternetwork.current"
                pool_id = "ocid1.instancepool.linked"
                request_id = "ocid1.workrequest.poolfailure"
                if state_present:
                    (root / "terraform.tfstate").write_text(json.dumps({"resources": [{
                        "name": "cluster_network", "instances": [{"attributes": {"id": network_id}}]
                    }]}), encoding="utf-8")
                config = {"enabled": True, "topic_id": "ocid1.onstopic.test",
                          "compartment_id": "ocid1.compartment.test", "region": "ap-osaka-1",
                          "instance_principal": True, "spool": str(root / "spool")}
                config_path = root / "config.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                log = root / "create.log"
                log.write_text("Error: timeout while waiting for state to become 'RUNNING' "
                               "(last state: 'PROVISIONING', timeout: 30m0s)\n", encoding="utf-8")
                calls = []
                quota = ("Instance launch failed. (400, QuotaExceeded, false) "
                         "The following compartment quotas were exceeded: "
                         "optimized3-core-count in policy 'ocid1.quota.test' by 36.")

                def response(_, *args):
                    calls.append(args)
                    if args[:3] == ("compute-management", "cluster-network", "list"):
                        return [
                            {"id": "ocid1.clusternetwork.old", "display-name": "compute-1-opt",
                             "time-created": "2020-01-01T00:00:00Z"},
                            {"id": network_id, "display-name": "compute-1-opt",
                             "time-created": "2026-10-11T13:19:02Z"},
                            {"id": "ocid1.clusternetwork.neighbor", "display-name": "compute-1-opt-lite",
                             "time-created": "2026-10-11T13:19:02Z"},
                        ]
                    if args[:3] == ("compute-management", "cluster-network", "get"):
                        self.assertIn(network_id, args)
                        return {"id": network_id, "instance-pools": [
                            None, {}, {"id": "not-an-ocid"}, {"id": "ocid1.instance.unrelated"},
                            {"id": pool_id}, {"id": pool_id},
                        ]}
                    if args[:3] == ("work-requests", "work-request", "list"):
                        resource = args[args.index("--resource-id") + 1]
                        if resource == network_id:
                            return [{"id": "ocid1.workrequest.network", "status": "IN_PROGRESS",
                                     "time-accepted": "2026-10-11T13:19:02Z"}]
                        self.assertEqual(resource, pool_id)
                        return [
                            {"id": "ocid1.workrequest.old", "status": "FAILED",
                             "time-accepted": "2020-01-01T00:00:00Z"},
                            {"id": "ocid1.workrequest.success", "status": "SUCCEEDED",
                             "time-accepted": "2026-10-11T13:19:02Z"},
                            {"id": request_id, "status": "FAILED", "operation-type": "LaunchInstancesInPool",
                             "time-accepted": "2026-10-11T13:19:02Z"},
                        ]
                    self.assertIn(request_id, args)
                    if args[:3] == ("work-requests", "work-request-error", "list"):
                        return [{"message": quota}]
                    if args[:3] == ("work-requests", "work-request-log-entry", "list"):
                        return [{"message": "Failed to launch instances in the pool"}]
                    self.fail("Unexpected OCI command: " + repr(args))

                delivered = []
                with mock.patch.object(notify, "oci_json", side_effect=response), \
                        mock.patch.object(notify, "publish", side_effect=lambda _, message: delivered.append(message) or True):
                    self.assertEqual(notify.main([
                        "report", "--config", str(config_path), "--cluster", "compute-1-opt",
                        "--queue", "compute", "--instance-type", "opt-default", "--nodes", "2",
                        "--job-id", "2", "--mode", "network", "--started", "1791724742",
                        "--log", str(log), "--workspace", str(root),
                    ]), 0)
                self.assertEqual(len(delivered), 1)
                body = delivered[0]["body"]
                self.assertIn("Failure phase: インスタンス作成\n", body)
                for value in ["PROVISIONING", "LaunchInstancesInPool", "QuotaExceeded",
                              "optimized3-core-count", "by 36", pool_id, request_id]:
                    self.assertIn(value, body)
                self.assertNotIn("ocid1.workrequest.old", body)
                self.assertNotIn("ocid1.workrequest.success", body)
                queried = [args[args.index("--resource-id") + 1] for args in calls
                           if args[:3] == ("work-requests", "work-request", "list")]
                self.assertEqual(queried.count(pool_id), 1)
                self.assertEqual(set(queried), {network_id, pool_id})

    def test_network_pool_lookup_failure_retains_parent_work_request_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "terraform.tfstate").write_text(json.dumps({"resources": [{
                "name": "cluster_network", "instances": [{"attributes": {
                    "id": "ocid1.clusternetwork.current"}}]}]}), encoding="utf-8")
            config = {"compartment_id": "ocid1.compartment.test"}

            def response(config, *args):
                if args[:3] == ("compute-management", "cluster-network", "list"):
                    return []
                if args[:3] == ("compute-management", "cluster-network", "get"):
                    config.setdefault("_lookup_errors", []).append("cluster-network get: permission denied")
                    return None
                if args[:3] == ("work-requests", "work-request", "list"):
                    return [{"id": "ocid1.workrequest.parent", "status": "FAILED",
                             "operation-type": "CreateClusterNetworkReservation",
                             "time-accepted": "2026-10-11T13:19:02Z"}]
                if args[:3] == ("work-requests", "work-request-error", "list"):
                    return [{"message": "Parent reservation failed"}]
                return []

            with mock.patch.object(notify, "oci_json", side_effect=response):
                result = notify.work_request_details(config, root, "network", "compute-1-opt", 1791724742)
            self.assertIn("Parent reservation failed", result)
            self.assertIn("permission denied", result)

    def test_ambiguous_network_discovery_never_queries_child_pools(self):
        config = {"compartment_id": "ocid1.compartment.test"}
        networks = [{"id": "ocid1.clusternetwork." + name, "display-name": "compute-1-opt",
                     "time-created": "2026-10-11T13:19:02Z"} for name in ["one", "two"]]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(notify, "oci_json", return_value=networks) as lookup:
            result = notify.work_request_details(config, Path(directory), "network", "compute-1-opt", 1791724742)
        self.assertIn("特定できませんでした", result)
        self.assertEqual(lookup.call_count, 1)

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
                self.assertIn("Failure phase: インスタンス作成\n", message["body"])
                self.assertIn("PROVISIONING", message["body"])
                self.assertIn("30m0s", message["body"])
                self.assertIn("失敗 Work Request を特定できませんでした", message["body"])
                self.assertIn("Job ID: 123", message["body"])
                self.assertIn("OCI 情報取得エラー", message["body"])
                self.assertIn("lookup denied", message["body"])
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
