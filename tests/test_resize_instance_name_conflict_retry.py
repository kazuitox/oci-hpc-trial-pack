import importlib.util
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUPPORT_PATH = os.path.join(
    REPOSITORY_ROOT, "tests", "test_resize_instance_pool_hostname_sync.py"
)
SPEC = importlib.util.spec_from_file_location("hostname_sync_retry_support", SUPPORT_PATH)
SUPPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPPORT)


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.delays = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay


def service_error(status=409, code="Conflict", message=None):
    error = SUPPORT.FakeServiceError(status)
    error.code = code
    error.message = message if message is not None else (
        "instance ocid1.instance.one is currently being modified, try again later"
    )
    return error


class InstanceNameConflictRetryTests(unittest.TestCase):
    def setUp(self):
        self.namespace = SUPPORT.load_resize_functions()
        self.clock = FakeClock()
        self.namespace["time"] = SimpleNamespace(
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            time=mock.Mock(side_effect=AssertionError("retry must use monotonic time")),
        )
        self.strategy = object()
        self.namespace["oci"].retry = SimpleNamespace(DEFAULT_RETRY_STRATEGY=self.strategy)
        self.update = mock.Mock(return_value="updated")
        self.namespace["computeClient"] = SimpleNamespace(update_instance=self.update)

    def invoke(self, max_wait_seconds=60):
        return self.namespace["update_instance_display_name_with_conflict_retry"](
            "ocid1.instance.one", "worker-alpha", max_wait_seconds=max_wait_seconds
        )

    def test_success_preserves_sdk_strategy_and_only_updates_display_name(self):
        self.assertEqual(self.invoke(), "updated")
        self.update.assert_called_once()
        args, kwargs = self.update.call_args
        self.assertEqual(args[0], "ocid1.instance.one")
        self.assertEqual(vars(args[1]), {"display_name": "worker-alpha"})
        self.assertIs(kwargs["retry_strategy"], self.strategy)
        self.assertRegex(kwargs["opc_retry_token"], r"^[0-9a-f-]{36}$")
        self.assertEqual(self.clock.delays, [])

    def test_busy_conflict_retries_same_request_and_token_then_succeeds(self):
        self.update.side_effect = [service_error(), service_error(), "updated"]

        self.assertEqual(self.invoke(), "updated")

        self.assertEqual(self.clock.delays, [2, 4])
        calls = self.update.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({call.kwargs["opc_retry_token"] for call in calls}), 1)
        self.assertTrue(all(call.args[1] is calls[0].args[1] for call in calls))
        self.assertTrue(all(call.kwargs["retry_strategy"] is self.strategy for call in calls))

    def test_unrelated_service_errors_are_not_retried(self):
        cases = [
            service_error(status=400),
            service_error(status=429),
            service_error(status=500),
            service_error(code="IncorrectState"),
            service_error(code="LockConflict"),
            service_error(message="Another resource conflicts with this request"),
            service_error(message="instance is not in a valid state"),
            SUPPORT.FakeServiceError(409),
        ]
        for error in cases:
            with self.subTest(status=error.status, code=getattr(error, "code", None)):
                self.update.reset_mock()
                self.update.side_effect = error
                with self.assertRaises(SUPPORT.FakeServiceError) as raised:
                    self.invoke()
                self.assertIs(raised.exception, error)
                self.update.assert_called_once()
                self.assertEqual(self.clock.delays, [])

    def test_non_service_exception_is_not_retried(self):
        error = RuntimeError("connection failed")
        self.update.side_effect = error

        with self.assertRaises(RuntimeError) as raised:
            self.invoke()

        self.assertIs(raised.exception, error)
        self.update.assert_called_once()
        self.assertEqual(self.clock.delays, [])

    def test_attempt_cap_applies_even_when_clock_does_not_advance(self):
        error = service_error()
        self.update.side_effect = error
        self.namespace["time"].sleep = self.clock.delays.append

        with self.assertRaises(SUPPORT.FakeServiceError) as raised:
            self.invoke()

        self.assertIs(raised.exception, error)
        self.assertEqual(self.update.call_count, 8)
        self.assertEqual(self.clock.delays, [2, 4, 8, 10, 10, 10, 10])

    def test_deadline_truncates_wait_and_prevents_another_attempt(self):
        error = service_error()
        self.update.side_effect = error

        with self.assertRaises(SUPPORT.FakeServiceError) as raised:
            self.invoke(max_wait_seconds=5)

        self.assertIs(raised.exception, error)
        self.assertEqual(self.update.call_count, 2)
        self.assertEqual(self.clock.delays, [2, 3])
        self.assertEqual(self.clock.now, 105)

    def test_sdk_call_time_counts_toward_additional_retry_deadline(self):
        error = service_error()

        def slow_update(*args, **kwargs):
            self.clock.now += 61
            raise error

        self.update.side_effect = slow_update
        with self.assertRaises(SUPPORT.FakeServiceError) as raised:
            self.invoke()

        self.assertIs(raised.exception, error)
        self.update.assert_called_once()
        self.assertEqual(self.clock.delays, [])

    def test_oversleep_does_not_start_attempt_after_deadline(self):
        error = service_error()
        self.update.side_effect = error

        def oversleep(delay):
            self.clock.now += 61

        self.namespace["time"].sleep = oversleep
        with self.assertRaises(SUPPORT.FakeServiceError) as raised:
            self.invoke()

        self.assertIs(raised.exception, error)
        self.update.assert_called_once()

    def test_zero_retry_budget_still_allows_one_update_attempt(self):
        error = service_error()
        self.update.side_effect = error

        with self.assertRaises(SUPPORT.FakeServiceError) as raised:
            self.invoke(max_wait_seconds=0)

        self.assertIs(raised.exception, error)
        self.update.assert_called_once()
        self.assertEqual(self.clock.delays, [])

    def test_exhausted_conflict_retains_sync_journal_and_resumes_partial_vnic_update(self):
        fixture = SUPPORT.InstancePoolDisplayNameApiTests()
        fixture.namespace = self.namespace
        fixture.compartment_id = "ocid1.compartment.test"
        fixture.pool_id = "ocid1.instancepool.test"
        fixture.cluster_name = "batch-1-standard"
        instance = SUPPORT.make_instance("ocid1.instance.one", "generated-one")
        fixture.configure_clients([instance])
        original_update = self.namespace["computeClient"].update_instance
        error = service_error()
        update = self.namespace["computeClient"].update_instance = mock.Mock(side_effect=error)
        self.namespace["time"].time = self.clock.monotonic

        def current_snapshot(*args, **kwargs):
            member = {
                "ocid": instance.id,
                "display_name": instance.display_name,
                "ip": "10.0.0.10",
            }
            return [member], {instance.id: member}

        self.namespace["get_complete_instance_pool_instances"] = current_snapshot
        self.namespace["load_pending_local_block_volume_deletions"] = mock.Mock(return_value=[])
        reconcile_dns = self.namespace["reconcile_instance_pool_name_dns"] = mock.Mock()
        self.namespace["refresh_instance_pool_hosts"] = mock.Mock()
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock(
            side_effect=AssertionError("pending journal must replace fact collection")
        )
        observed = {instance.id: SUPPORT.observed_names()[instance.id]}
        recovery_fixture = SUPPORT.InstancePoolHostnameSyncPlanRecoveryTests()

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = recovery_fixture.write_inventory(directory, observed)
            with self.assertRaises(SUPPORT.FakeServiceError) as raised:
                self.namespace["synchronize_instance_pool_names"](
                    fixture.compartment_id,
                    fixture.pool_id,
                    inventory_path,
                    fixture.cluster_name,
                    observed_hostnames_by_instance_id=observed,
                )
            self.assertIs(raised.exception, error)
            self.assertEqual(update.call_count, 8)
            self.assertEqual(instance.display_name, "generated-one")
            self.assertEqual(fixture.vnic_state[instance.id].display_name, "worker-alpha")
            reconcile_dns.assert_not_called()
            journal_path = self.namespace["get_instance_pool_hostname_sync_plan_path"](
                inventory_path
            )
            with open(journal_path, encoding="utf-8") as source:
                journal = json.load(source)
            self.assertEqual(journal["status"], "pending")
            self.assertEqual(journal["members"][instance.id]["previous_display_name"], "generated-one")
            self.assertEqual(journal["members"][instance.id]["hostname"], "worker-alpha")
            self.assertEqual(os.stat(journal_path).st_mode & 0o777, 0o600)

            update.side_effect = original_update
            synchronized, _ = self.namespace["synchronize_instance_pool_names"](
                fixture.compartment_id,
                fixture.pool_id,
                inventory_path,
                fixture.cluster_name,
            )

            self.assertFalse(os.path.exists(journal_path))
            self.assertEqual(synchronized[0]["display_name"], "worker-alpha")
            self.assertEqual(instance.display_name, "worker-alpha")
            self.assertEqual(len(fixture.vnic_update_calls), 1)
            collector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
