import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("autoscaling_guard", ROOT / "bin/autoscaling_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
ARGS = ["1", "compute-1-hpc", "HPC", "compute", "123", ""]


class AutoscalingGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "guard"
        self.config = {"failure_threshold": 3, "state_dir": str(self.directory), "admin_user": "opc"}
        self.notification = mock.patch.object(guard, "notify_stop")
        self.notify = self.notification.start()
        self.addCleanup(self.notification.stop)
        success = mock.patch.object(guard, "notify_success")
        self.success_notice = success.start()
        self.addCleanup(success.stop)

    def run_result(self, code):
        return guard.run_create(self.config, ARGS,
                                [sys.executable, "-c", "import sys; sys.exit({})".format(code)])

    def test_threshold_latches_and_blocks_actual_child_launch(self):
        for unused in range(3):
            self.assertEqual(self.run_result(1), 1)
        self.assertTrue(guard.read_state(self.directory)["stopped"])
        output = io.StringIO()
        with mock.patch.object(guard.subprocess, "Popen") as launch, contextlib.redirect_stdout(output):
            self.assertEqual(self.run_result(0), 0)
            launch.assert_not_called()
        self.assertIn('Automatic cluster creation is stopped', output.getvalue())
        self.assertTrue(output.getvalue().isascii())
        self.notify.assert_called_once()

    def test_success_resets_consecutive_count_and_other_queue_shares_counter(self):
        self.run_result(1)
        self.run_result(1)
        self.run_result(0)
        self.assertEqual(guard.read_state(self.directory)["consecutive_failures"], 0)
        guard.run_create(self.config, ["1", "vnc-1", "desktop", "vnc", "124"],
                         [sys.executable, "-c", "raise SystemExit(1)"])
        self.assertEqual(guard.read_state(self.directory)["consecutive_failures"], 1)
        self.assertFalse(guard.read_state(self.directory)["stopped"])
        self.success_notice.assert_called_once_with(ARGS, recovered=True, stopped=False,
                                                   previous_failures=2, failures_before_resume=None)

    def test_disabled_and_missing_config_pass_through_without_state(self):
        self.config["failure_threshold"] = 0
        self.assertEqual(self.run_result(9), 9)
        self.assertFalse(self.directory.exists())
        self.assertEqual(guard.load_config(Path(self.temp.name) / "absent.json"),
                         {"failure_threshold": 0})

    def test_launch_failure_counts(self):
        self.assertEqual(guard.run_create(self.config, ARGS, ["/nonexistent/create"]), 127)
        self.assertEqual(guard.read_state(self.directory)["consecutive_failures"], 1)

    def test_normal_success_is_quiet_even_when_guard_disabled(self):
        self.run_result(0)
        self.run_result(0)
        self.config["failure_threshold"] = 0
        self.run_result(0)
        self.run_result(1)
        self.success_notice.assert_not_called()
        self.notify.assert_not_called()

    def test_resume_recovers_latch_and_preserves_old_generation_isolation(self):
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps(self.config))
        self.run_result(1)
        old = guard.read_state(self.directory)["generation"]
        def complete_after_resume():
            self.assertEqual(guard.main(["--config", str(config), "resume"]), 0)
            return 1
        process = mock.Mock()
        process.wait.side_effect = complete_after_resume
        with mock.patch.object(guard.subprocess, "Popen", return_value=process):
            self.assertEqual(guard.run_create(self.config, ARGS), 1)
        state = guard.read_state(self.directory)
        self.assertNotEqual(state["generation"], old)
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertFalse(state["stopped"])

    def test_late_inflight_success_cannot_clear_stop(self):
        self.config["failure_threshold"] = 1
        def late_success():
            state = guard.read_state(self.directory)
            state.update(stopped=True, consecutive_failures=1, threshold=1)
            guard.save_state(self.directory, state)
            return 0
        process = mock.Mock()
        process.wait.side_effect = late_success
        with mock.patch.object(guard.subprocess, "Popen", return_value=process):
            guard.run_create(self.config, ARGS)
        self.assertTrue(guard.read_state(self.directory)["stopped"])
        self.assertEqual(guard.read_state(self.directory)["consecutive_failures"], 1)
        self.success_notice.assert_not_called()

    def test_event_notifications_share_admin_config_and_retry(self):
        spec = importlib.util.spec_from_file_location(
            "autoscaling_failure_notify", ROOT / "bin/autoscaling_failure_notify.py")
        notify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(notify)
        path = Path(self.temp.name) / "admin.json"
        spool = Path(self.temp.name) / "spool"
        config = {"enabled": True, "topic_id": "dummy-admin-topic", "spool": str(spool)}
        path.write_text(json.dumps(config))
        with mock.patch.dict(sys.modules, autoscaling_failure_notify=notify), \
                mock.patch.object(notify, "DEFAULT_CONFIG", path):
            # Both events use the very same production queue function.
            guard.queue_notification("Autoscaling 自動作成に成功しました", "Cluster: c\nJob ID: 123")
            guard.queue_notification("Autoscaling 自動作成を停止しました", "Threshold: 3")
            messages = [json.loads(p.read_text()) for p in spool.glob("*.json")]
            self.assertEqual(len(messages), 2)
            self.assertEqual({m["topic_id"] for m in messages}, {"dummy-admin-topic"})
            with mock.patch.object(notify, "publish", return_value=False):
                notify.retry(config, spool)
            self.assertEqual(len(list(spool.glob("*.json"))), 2)
            with mock.patch.object(notify, "publish", return_value=True):
                notify.retry(config, spool)
            self.assertEqual(list(spool.glob("*.json")), [])
            config["enabled"] = False
            path.write_text(json.dumps(config))
            with mock.patch.object(notify, "save_message") as save:
                guard.queue_notification("success", "body")
                guard.queue_notification("stop", "body")
                save.assert_not_called()
            path.unlink()
            guard.queue_notification("success", "missing config is optional")

    def test_success_notice_body_has_creation_details(self):
        # Load a fresh module because setUp mocks success delivery.
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        with mock.patch.object(module, "queue_notification") as queue:
            module.notify_success(ARGS, recovered=True)
        title, body = queue.call_args[0]
        self.assertIn("復旧", title)
        for detail in ("compute-1-hpc", "Queue: compute", "Instance type: HPC", "Nodes: 1", "Job ID: 123"):
            self.assertIn(detail, body)

    def test_corrupt_state_and_config_prevent_launch(self):
        self.directory.mkdir()
        (self.directory / "state.json").write_text("{bad")
        with mock.patch.object(guard.subprocess, "Popen") as launch:
            with self.assertRaises(ValueError):
                guard.run_create(self.config, ARGS)
            launch.assert_not_called()
        path = Path(self.temp.name) / "config.json"
        for value in (-1, True, 1.5, "3", None):
            path.write_text(json.dumps({"failure_threshold": value}))
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard.load_config(path)

    def test_parallel_completions_do_not_lose_failures(self):
        self.config["failure_threshold"] = 100
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps(self.config))
        # Use independent interpreter processes to exercise the real flock,
        # atomic replacement, and imports without multiprocessing fork state.
        harness = ("import importlib.util,json,sys; "
                   "s=importlib.util.spec_from_file_location('g',sys.argv[1]); "
                   "g=importlib.util.module_from_spec(s); s.loader.exec_module(g); "
                   "g.run_create(json.load(open(sys.argv[2])),sys.argv[3:],"
                   "[sys.executable,'-c','raise SystemExit(1)'])")
        processes = [subprocess.Popen([sys.executable, "-c", harness,
                                      str(ROOT / "bin/autoscaling_guard.py"), str(config)] + ARGS)
                     for unused in range(8)]
        self.assertEqual([p.wait(timeout=20) for p in processes], [0] * 8)
        self.assertEqual(guard.read_state(self.directory)["consecutive_failures"], 8)

    def test_bash_syntax_failure_counts(self):
        script = Path(self.temp.name) / "bad.sh"
        script.write_text("#!/bin/bash\nif then broken\n")
        self.assertNotEqual(guard.run_create(self.config, ARGS, ["bash", str(script)]), 0)
        self.assertEqual(guard.read_state(self.directory)["consecutive_failures"], 1)

    def test_stop_notice_uses_existing_spool_without_oci(self):
        notify_spec = importlib.util.spec_from_file_location(
            "autoscaling_failure_notify", ROOT / "bin/autoscaling_failure_notify.py")
        notify = importlib.util.module_from_spec(notify_spec)
        notify_spec.loader.exec_module(notify)
        config = Path(self.temp.name) / "notification.json"
        spool = Path(self.temp.name) / "spool"
        config.write_text(json.dumps({"enabled": True, "topic_id": "dummy", "spool": str(spool)}))
        state = dict(consecutive_failures=3, threshold=3, cluster="c", queue="q", instance_type="t")
        with mock.patch.dict(sys.modules, autoscaling_failure_notify=notify), \
                mock.patch.object(notify, "DEFAULT_CONFIG", config):
            self.notification.stop()
            guard.notify_stop(state)
            self.notification.start()
        messages = list(spool.glob("*.json"))
        self.assertEqual(len(messages), 1)
        body = json.loads(messages[0].read_text())["body"]
        self.assertIn("resume", body)
        self.assertIn("Consecutive failures: 3", body)

    def test_failed_cleanup_does_not_mask_create_failure(self):
        # Execute the actual final shell branch with notifications and cleanup
        # stubbed; both normal and DEBUG paths must propagate the create result.
        source = (ROOT / "bin/create_cluster.sh").read_text()
        tail = source[source.index('if [ $status -ne 0 ]\nthen'):]
        folder = Path(self.temp.name) / "bin"
        folder.mkdir()
        (folder / "delete_cluster.sh").write_text("#!/bin/bash\nexit 0\n")
        (folder / "delete_cluster.sh").chmod(0o700)
        harness = ('status=1; compute_cluster=false; cluster_network=false; start=1; date=test\n'
                   'folder="$GUARD_TEST_FOLDER"; logs_folder="$GUARD_TEST_LOGS"; '
                   'autoscaling_folder="$GUARD_TEST_LOGS"; debug="$GUARD_TEST_DEBUG"\n'
                   'python3() { return 0; }\n') + tail
        for debug in (0, 1):
            env = dict(os.environ, GUARD_TEST_FOLDER=str(folder), GUARD_TEST_LOGS=self.temp.name,
                       GUARD_TEST_DEBUG=str(debug))
            result = subprocess.run(["bash", "-c", harness, "test"] + ARGS, env=env)
            self.assertEqual(result.returncode, 1)

    def test_deploy_and_both_schedulers_use_guard(self):
        for name in ("autoscale_slurm.sh", "autoscale_slurm_disable-resize.sh"):
            source = (ROOT / "autoscaling/crontab" / name).read_text()
            self.assertIn("/autoscaling_guard.py", source)
            self.assertNotIn("/create_cluster.sh", source)
            compile(source, name, "exec")
        deploy = (ROOT / "autoscaling-guard.tf").read_text()
        self.assertIn("null_resource.cluster_backup", deploy)
        self.assertIn("local.host_backup", deploy)
        self.assertIn("var.autoscaling_failure_threshold", deploy)
        self.assertNotIn("oci_identity_policy", deploy)

    def test_cli_retains_stop_across_processes_and_resume_allows_next_create(self):
        # A copied helper resolves its production-relative paths in the isolated
        # layout. Exercise the actual CLI rather than replacing main/run_create.
        root = Path(self.temp.name)
        (root / "bin").mkdir()
        (root / "conf").mkdir()
        script = root / "bin/autoscaling_guard.py"
        script.write_text((ROOT / "bin/autoscaling_guard.py").read_text())
        script.chmod(0o755)
        create = root / "bin/create_cluster.sh"
        create.write_text('#!/bin/bash\necho attempt >> "$GUARD_TEST_ATTEMPTS"\nexit 1\n')
        create.chmod(0o700)
        (root / "conf/autoscaling_guard.json").write_text(json.dumps(self.config))
        attempts = root / "attempts"
        env = dict(os.environ, GUARD_TEST_ATTEMPTS=str(attempts))
        for unused in range(3):
            self.assertEqual(subprocess.run([sys.executable, str(script), "run"] + ARGS,
                                            env=env, capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run([sys.executable, str(script), "run"] + ARGS,
                                        env=env, capture_output=True).returncode, 0)
        self.assertEqual(len(attempts.read_text().splitlines()), 3)
        status = subprocess.run([str(script), "status", "--debug"],
                                env=env, capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(status.stdout)["effective_stopped"])
        subprocess.run([sys.executable, str(script), "resume"], env=env,
                       capture_output=True, check=True)
        subprocess.run([sys.executable, str(script), "run"] + ARGS,
                       env=env, capture_output=True)
        self.assertEqual(len(attempts.read_text().splitlines()), 4)

    def test_notice_queue_failure_keeps_stop(self):
        notify_spec = importlib.util.spec_from_file_location(
            "autoscaling_failure_notify", ROOT / "bin/autoscaling_failure_notify.py")
        notify = importlib.util.module_from_spec(notify_spec)
        notify_spec.loader.exec_module(notify)
        config = Path(self.temp.name) / "notification.json"
        config.write_text(json.dumps({"enabled": True, "topic_id": "dummy", "spool": self.temp.name}))
        self.config["failure_threshold"] = 1
        self.notification.stop()
        with mock.patch.dict(sys.modules, autoscaling_failure_notify=notify), \
                mock.patch.object(notify, "DEFAULT_CONFIG", config), \
                mock.patch.object(notify, "save_message", side_effect=OSError("disk error")):
            self.assertEqual(self.run_result(1), 1)
        self.notification.start()
        self.assertTrue(guard.read_state(self.directory)["stopped"])

    def test_resource_manager_setting_matches_runtime_config(self):
        import yaml
        schema = yaml.safe_load((ROOT / "schema.yaml").read_text())
        setting = schema["variables"]["autoscaling_failure_threshold"]
        self.assertEqual(setting["type"], "integer")
        self.assertEqual(setting["default"], 3)
        self.assertEqual(setting["minimum"], 0)
        self.assertEqual(setting["visible"], "${autoscaling}")
        self.assertTrue(any("${autoscaling_failure_threshold}" in group.get("variables", [])
                            for group in schema["variableGroups"]))
        deploy = (ROOT / "autoscaling-guard.tf").read_text()
        self.assertIn("failure_threshold = var.autoscaling_failure_threshold", deploy)
        self.assertIn('floor(var.autoscaling_failure_threshold)', deploy)
        self.assertRegex(deploy, r'variable "autoscaling_failure_threshold"\s*\{\s*type\s*=\s*number\s+default\s*=\s*3\b')


    def test_status_is_japanese_and_debug_retains_json_in_both_positions(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        self.run_result(1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(guard.main(['--config', str(config), 'status']), 0)
        self.assertRegex(output.getvalue(), '連続失敗回数 +:\t1回')
        self.assertIn('全キュー・全インスタンスタイプ共通', output.getvalue())
        self.assertRegex(output.getvalue(), 'クラスター名 +:\tcompute-1-hpc')
        self.assertIn('現在の稼働状況ではありません', output.getvalue())
        self.assertNotIn('generation', output.getvalue())
        for options in (['status', '--debug'], ['--debug', 'status']):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(guard.main(['--config', str(config)] + options), 0)
            self.assertEqual(json.loads(output.getvalue())['consecutive_failures'], 1)

    def test_stop_resume_then_success_reports_recovery_once(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        for unused in range(3):
            self.run_result(1)
        with contextlib.redirect_stdout(io.StringIO()):
            guard.main(['--config', str(config), 'resume'])
            guard.main(['--config', str(config), 'resume'])
        self.assertTrue(guard.read_state(self.directory)['recovery_pending'])
        self.success_notice.assert_not_called()
        self.run_result(1)
        self.run_result(0)
        self.run_result(0)
        self.assertEqual(self.success_notice.call_args_list,
                         [mock.call(ARGS, recovered=True, stopped=False,
                                    previous_failures=1, failures_before_resume=3)])
        self.assertFalse(guard.read_state(self.directory)['recovery_pending'])

    def test_legacy_state_is_readable_and_stopped_display_explains_resume(self):
        self.run_result(1)
        state = guard.read_state(self.directory)
        state.pop('recovery_pending', None)
        state.update(stopped=True, consecutive_failures=3)
        guard.save_state(self.directory, state)
        loaded = guard.read_state(self.directory)
        loaded.update(configured_threshold=3, effective_stopped=True)
        output = guard.format_status(loaded)
        self.assertIn('停止中', output)
        self.assertIn('autoscaling_guard.py resume', output)
        loaded.update(configured_threshold=0, effective_stopped=False)
        output = guard.format_status(loaded)
        self.assertIn('自動停止は無効', output)
        self.assertIn('保存済みの停止記録', output)

    def test_notice_distinguishes_recovery_from_completion_and_late_completion(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        with mock.patch.object(module, 'queue_notification') as queue:
            module.notify_success(ARGS, recovered=True)
            self.assertEqual(queue.call_args[0][0], 'Autoscaling の自動作成が復旧しました')
            self.assertIn('連続失敗回数が0', queue.call_args[0][1])
            queue.reset_mock()
            module.notify_success(ARGS, stopped=True)
            module.notify_success(ARGS)
            queue.assert_not_called()

    def test_old_generation_success_does_not_report_recovery(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        self.run_result(1)
        def complete():
            with contextlib.redirect_stdout(io.StringIO()):
                guard.main(['--config', str(config), 'resume'])
            return 0
        process = mock.Mock()
        process.wait.side_effect = complete
        with mock.patch.object(guard.subprocess, 'Popen', return_value=process):
            guard.run_create(self.config, ARGS)
        self.success_notice.assert_not_called()
        self.assertTrue(guard.read_state(self.directory)['recovery_pending'])

    def test_deploy_grants_execute_and_supplies_actual_admin_user(self):
        source = (ROOT / 'autoscaling-guard.tf').read_text()
        self.assertIn('admin_user        = local.controller_username', source)
        self.assertIn('chmod 755 /opt/oci-hpc/bin/autoscaling_guard.py', source)
        self.assertTrue(os.access(str(ROOT / 'bin/autoscaling_guard.py'), os.X_OK))

    def test_opc_and_ubuntu_appear_in_stop_mail_and_status_instructions(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        state = dict(consecutive_failures=3, threshold=3, cluster='c', queue='q',
                     instance_type='t', stopped=True, effective_stopped=True, configured_threshold=3)
        for user in ('opc', 'ubuntu'):
            with self.subTest(user=user), mock.patch.object(module, 'queue_notification') as queue:
                module.notify_stop(state, admin_user=user)
                body = queue.call_args[0][1]
                self.assertIn(user + ' ユーザーにて実行してください', body)
                self.assertIn('/opt/oci-hpc/bin/autoscaling_guard.py resume', body)
                text = module.format_status(dict(state, admin_user=user))
                self.assertIn(user + ' ユーザーにて実行してください', text)
                self.assertEqual(module.management_user({'admin_user': user}), user)

    def test_legacy_config_uses_deployed_file_owner_instead_of_current_caller(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text('{}')
        with mock.patch.object(guard, 'DEFAULT_CONFIG', config), \
                mock.patch.object(guard.pwd, 'getpwuid', return_value=mock.Mock(pw_name='ubuntu')):
            self.assertEqual(guard.management_user({}), 'ubuntu')

    def execute_scheduler(self, name):
        path = ROOT / 'autoscaling/crontab' / name
        definitions, main = path.read_text().split('\nautoscaling = getAutoscaling()\n', 1)
        module = types.ModuleType('scheduler_under_test')
        exec(compile(definitions, str(path), 'exec'), module.__dict__)
        module.lockfile = str(Path(self.temp.name) / 'scheduler.lock')
        output = io.StringIO()
        with mock.patch.dict(sys.modules, autoscaling_guard=guard), \
                mock.patch.object(module.sys, 'path', list(sys.path)), \
                mock.patch.object(module.sys, 'argv', [str(path)]), \
                mock.patch.object(module, 'getAutoscaling', return_value='true'), \
                mock.patch.object(module, 'getQueueConf', return_value=[]), \
                mock.patch.object(module, 'getstatus_slurm', return_value=(
                    [[1, 'hpc-default', 'compute', 1, 'opc']], [['idle-cluster']],
                    {}, [], [], {}, {}, {})), \
                mock.patch.object(module, 'getJobConfig', return_value={'instance_keyword': 'hpc'}), \
                mock.patch.object(module, 'getQueueLimits', return_value={
                    'max_cluster_count': 10, 'max_cluster_size': 10, 'max_number_nodes': 10}), \
                mock.patch.object(module.time, 'sleep'), \
                mock.patch.object(module.subprocess, 'Popen') as launch, \
                mock.patch.object(module, 'deleteClusterSafely', return_value=True,
                                  create=True) as delete, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            exec(compile('\nautoscaling = getAutoscaling()\n' + main, str(path), 'exec'), module.__dict__)
        self.assertFalse(Path(module.lockfile).exists())
        self.assertNotIn('Traceback', output.getvalue())
        return output.getvalue(), launch, delete

    def test_stopped_scheduler_omits_creation_plan_and_keeps_deletion(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        self.directory.mkdir()
        state = guard.initial_state()
        state.update(stopped=True, consecutive_failures=3)
        guard.save_state(self.directory, state)
        for name in ('autoscale_slurm.sh', 'autoscale_slurm_disable-resize.sh'):
            with self.subTest(script=name), mock.patch.object(guard, 'DEFAULT_CONFIG', config):
                output, launch, delete = self.execute_scheduler(name)
            self.assertIn('Automatic cluster creation is stopped; skipped 1 creation request(s).', output)
            self.assertNotIn('Run the following command', output)
            self.assertNotIn('/opt/oci-hpc/bin/autoscaling_guard.py status', output)
            self.assertTrue(output.isascii())
            self.assertNotIn('cluster_to_build', output)
            self.assertNotIn('Creating cluster', output)
            self.assertNotIn('autoscaling_guard.py', str(launch.call_args_list))
            if name.endswith('disable-resize.sh'):
                delete.assert_called_once_with('idle-cluster')
            else:
                launch.assert_called_once_with(['/opt/oci-hpc/bin/delete_cluster.sh', 'idle-cluster'])
        self.assertEqual(guard.read_state(self.directory)['consecutive_failures'], 3)
        self.notify.assert_not_called()

    def test_resumed_scheduler_launches_guard_and_corruption_only_blocks_creation(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        self.directory.mkdir()
        for corrupt in (False, True):
            if corrupt:
                (self.directory / 'state.json').write_text('{broken')
            else:
                guard.save_state(self.directory, guard.initial_state())
            with self.subTest(corrupt=corrupt), mock.patch.object(guard, 'DEFAULT_CONFIG', config):
                output, launch, delete = self.execute_scheduler('autoscale_slurm_disable-resize.sh')
            delete.assert_called_once_with('idle-cluster')
            if corrupt:
                launch.assert_not_called()
                self.assertIn('Could not verify the autoscaling guard state', output)
                self.assertTrue(output.isascii())
                self.assertNotIn('cluster_to_build', output)
            else:
                self.assertIn('cluster_to_build', output)
                self.assertEqual(launch.call_args[0][0][1:3],
                                 ['/opt/oci-hpc/bin/autoscaling_guard.py', 'run'])

    def test_cron_error_fallback_is_english_without_a_known_admin_user(self):
        config = Path(self.temp.name) / 'broken.json'
        config.write_text('{broken')
        output = io.StringIO()
        with mock.patch.object(guard, 'DEFAULT_CONFIG', config), \
                mock.patch.object(guard.pwd, 'getpwuid', return_value=mock.Mock(pw_name='root')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(guard.scheduler_creation_requests([ARGS]), [])
            self.assertIn('管理ユーザー', guard.management_user({}))
        self.assertTrue(output.getvalue().isascii())
        self.assertNotIn('Run the following command', output.getvalue())
        self.assertNotIn('autoscaling_guard.py status', output.getvalue())

    def test_running_create_preserves_two_failures_until_process_success(self):
        self.run_result(1)
        self.run_result(1)
        root = Path(self.temp.name)
        started, release = root / 'started', root / 'release'
        child = ('import sys,time;from pathlib import Path;Path(sys.argv[1]).touch();'
                 '\nwhile not Path(sys.argv[2]).exists(): time.sleep(.02)\n')
        harness = ('import importlib.util,json,sys;'
                   's=importlib.util.spec_from_file_location("g",sys.argv[1]);'
                   'g=importlib.util.module_from_spec(s);s.loader.exec_module(g);'
                   'g.notify_success=lambda *a,**k:None;'
                   'g.run_create(json.loads(sys.argv[2]),sys.argv[6:],'
                   'command=[sys.executable,"-c",sys.argv[3],sys.argv[4],sys.argv[5]])')
        process = subprocess.Popen([sys.executable, '-c', harness,
                                    str(ROOT / 'bin/autoscaling_guard.py'), json.dumps(self.config),
                                    child, str(started), str(release)] + ARGS,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(started.exists())
            self.assertIsNone(process.poll())
            self.assertEqual(guard.read_state(self.directory)['consecutive_failures'], 2)
            release.touch()
            stdout, stderr = process.communicate(timeout=5)
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        state = guard.read_state(self.directory)
        self.assertEqual(state['consecutive_failures'], 0)
        self.assertEqual(state['last_reset']['reason'], 'creation_succeeded')
        self.assertEqual(state['last_reset']['previous_failures'], 2)
        self.assertEqual(state['last_reset']['attempt_id'], state['last_attempt_id'])
        self.assertIn('failures=2->0', stdout)
        self.assertIn('Autoscaling guard attempt:', stdout)

    def test_initialization_and_manual_resume_reset_are_identifiable(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.run_result(1)
        self.assertIn('reason=missing_state_file', output.getvalue())
        self.assertIn('failures=0->1', output.getvalue())
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        with contextlib.redirect_stdout(io.StringIO()):
            guard.main(['--config', str(config), 'resume'])
        state = guard.read_state(self.directory)
        self.assertEqual(state['last_reset']['reason'], 'manual_resume')
        self.assertEqual(state['last_reset']['previous_failures'], 1)

    def test_recovery_mail_includes_reset_count_and_separate_resume_count(self):
        module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(module)
        with mock.patch.object(module, 'queue_notification') as queue:
            module.notify_success(ARGS, recovered=True, previous_failures=2)
            self.assertIn('連続失敗回数: 2回 → 0回', queue.call_args[0][1])
            module.notify_success(ARGS, recovered=True, previous_failures=0, failures_before_resume=3)
            self.assertIn('手動再開前の連続失敗回数: 3回', queue.call_args[0][1])
            self.assertIn('手動再開後の追加失敗なし', queue.call_args[0][1])
            module.notify_success(ARGS, recovered=True, previous_failures=1, failures_before_resume=3)
            self.assertIn('連続失敗回数: 1回 → 0回', queue.call_args[0][1])
            self.assertIn('手動再開前の連続失敗回数: 3回', queue.call_args[0][1])

    def test_resume_count_survives_repeated_resume_and_is_consumed_on_recovery(self):
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps(self.config))
        self.run_result(1)
        self.run_result(1)
        with contextlib.redirect_stdout(io.StringIO()):
            guard.main(['--config', str(config), 'resume'])
            guard.main(['--config', str(config), 'resume'])
        self.run_result(0)
        self.success_notice.assert_called_once_with(ARGS, recovered=True, stopped=False,
                                                   previous_failures=0, failures_before_resume=2)
        self.assertNotIn('failures_before_resume', guard.read_state(self.directory))


if __name__ == "__main__":
    unittest.main()
