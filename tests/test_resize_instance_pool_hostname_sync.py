import ast
import json
import os
import re
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESIZE_PATH = os.path.join(REPOSITORY_ROOT, "bin", "resize.py")


class FakeServiceError(Exception):
    def __init__(self, status):
        super().__init__("OCI service error "+str(status))
        self.status = status


class FakeUpdateInstanceDetails:
    def __init__(self, display_name=None):
        self.display_name = display_name


class FakeUpdateVnicDetails:
    def __init__(self, display_name=None):
        self.display_name = display_name


class FakeRecordDetails:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeUpdateRRSetDetails:
    def __init__(self, items=None):
        self.items = items or []


def load_resize_functions():
    fake_oci = types.ModuleType("oci")
    fake_oci.pagination = SimpleNamespace(
        list_call_get_all_results=lambda function, *args, **kwargs: function(
            *args, **kwargs
        )
    )
    fake_oci.exceptions = SimpleNamespace(ServiceError=FakeServiceError)
    fake_oci.core = SimpleNamespace(
        models=SimpleNamespace(
            UpdateInstanceDetails=FakeUpdateInstanceDetails,
            UpdateVnicDetails=FakeUpdateVnicDetails,
        )
    )
    fake_oci.dns = SimpleNamespace(
        models=SimpleNamespace(
            RecordDetails=FakeRecordDetails,
            UpdateRRSetDetails=FakeUpdateRRSetDetails,
        )
    )
    fake_requests = types.ModuleType("requests")
    with open(RESIZE_PATH, encoding="utf-8") as source_file:
        function_source = source_file.read().split("batchsize=12", 1)[0]
    namespace = {"__name__": "resize_instance_pool_hostname_test"}
    with mock.patch.dict(sys.modules, {"oci": fake_oci, "requests": fake_requests}):
        exec(compile(function_source, RESIZE_PATH, "exec"), namespace)
    return namespace


def read_repository_file(*path_parts):
    with open(os.path.join(REPOSITORY_ROOT, *path_parts), encoding="utf-8") as source:
        return source.read()


def make_instance(
    instance_id,
    display_name,
    cluster_name="batch-1-standard",
    compartment_id="ocid1.compartment.test",
    lifecycle_state="RUNNING",
):
    return SimpleNamespace(
        id=instance_id,
        display_name=display_name,
        compartment_id=compartment_id,
        lifecycle_state=lifecycle_state,
        freeform_tags={"parent_cluster": cluster_name},
    )


def make_inventory_text(
    compute_configured="",
    compute_to_add="",
    nfs="",
    controller="controller ansible_host=10.0.0.2\n",
    dns_entries="false",
):
    return (
        "[controller]\n"+controller+
        "[slurm_backup]\n"
        "[login]\n"
        "[compute_to_add]\n"+compute_to_add+
        "[compute_configured]\n"+compute_configured+
        "[compute_to_destroy]\n"
        "[nfs]\n"+nfs+
        "[all:vars]\n"
        "cluster_name=batch-1-standard\n"
        "queue=batch\n"
        "private_subnet=10.0.0.0/24\n"
        "zone_name=batch-1-standard.local\n"
        "dns_entries="+dns_entries+"\n"
    )


def observed_names(one="worker-alpha", two="worker-beta"):
    return {
        "ocid1.instance.one": {
            "hostname": one,
            "private_ip": "10.0.0.10",
            "inventory_hostname": "generated-one",
        },
        "ocid1.instance.two": {
            "hostname": two,
            "private_ip": "10.0.0.11",
            "inventory_hostname": "generated-two",
        },
    }


def invoke_update_callback(kwargs, previous_names):
    callback = kwargs.get("before_updates")
    if callback is not None:
        callback(previous_names)


def write_instance_pool_state(directory, instance_pool_id):
    with open(
        os.path.join(directory, "terraform.tfstate"),
        "w",
        encoding="utf-8",
    ) as state_file:
        json.dump(
            {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "oci_core_instance_pool",
                        "name": "instance_pool",
                        "instances": [
                            {"attributes": {"id": instance_pool_id}}
                        ],
                    }
                ]
            },
            state_file,
        )


class InstancePoolHostnameCollectionTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def write_inventory(self, directory):
        inventory_path = os.path.join(directory, "inventory")
        with open(inventory_path, "w", encoding="utf-8") as inventory_file:
            inventory_file.write(
                make_inventory_text(
                    compute_configured=(
                        "generated-one ansible_host=10.0.0.10 ansible_user=opc "
                        "role=compute oci_instance_id=ocid1.instance.one\n"
                    ),
                    compute_to_add=(
                        "generated-two ansible_host=10.0.0.11 ansible_user=opc "
                        "role=compute oci_instance_id=ocid1.instance.two\n"
                    ),
                )
            )
        return inventory_path

    def test_collects_final_ansible_hostname_and_keeps_ocid_ip_association(self):
        commands = []

        def run_ansible(command, **kwargs):
            commands.append((command, kwargs))
            tree_path = command[command.index("--tree")+1]
            for alias, hostname in {
                "generated-one": "render-node-a",
                "generated-two": "render-node-b",
            }.items():
                with open(os.path.join(tree_path, alias), "w", encoding="utf-8") as output:
                    json.dump(
                        {"ansible_facts": {"ansible_hostname": hostname}, "changed": False},
                        output,
                    )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_inventory(directory)
            with mock.patch.object(self.namespace["subprocess"], "run", run_ansible):
                result = self.namespace["collect_instance_pool_os_hostnames"](
                    inventory_path
                )

        self.assertEqual(
            result,
            {
                "ocid1.instance.one": {
                    "hostname": "render-node-a",
                    "private_ip": "10.0.0.10",
                    "inventory_hostname": "generated-one",
                },
                "ocid1.instance.two": {
                    "hostname": "render-node-b",
                    "private_ip": "10.0.0.11",
                    "inventory_hostname": "generated-two",
                },
            },
        )
        command, kwargs = commands[0]
        self.assertIn("compute", command)
        self.assertIn("ansible_hostname", " ".join(command))
        self.assertEqual(kwargs["env"]["ANSIBLE_HOST_KEY_CHECKING"], "False")

    def test_fails_if_any_compute_host_did_not_return_final_facts(self):
        def run_ansible(command, **kwargs):
            tree_path = command[command.index("--tree")+1]
            with open(os.path.join(tree_path, "generated-one"), "w", encoding="utf-8") as output:
                json.dump(
                    {"ansible_facts": {"ansible_hostname": "render-node-a"}}, output
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_inventory(directory)
            with mock.patch.object(self.namespace["subprocess"], "run", run_ansible):
                with self.assertRaisesRegex(RuntimeError, r"generated-two|all compute"):
                    self.namespace["collect_instance_pool_os_hostnames"](inventory_path)

    def test_fails_when_ansible_fact_collection_fails(self):
        failed = SimpleNamespace(returncode=2, stdout="", stderr="unreachable")
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_inventory(directory)
            with mock.patch.object(
                self.namespace["subprocess"], "run", return_value=failed
            ):
                with self.assertRaisesRegex(RuntimeError, r"Ansible|ansible|unreachable"):
                    self.namespace["collect_instance_pool_os_hostnames"](inventory_path)


class InstancePoolDisplayNameApiTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()
        self.compartment_id = "ocid1.compartment.test"
        self.pool_id = "ocid1.instancepool.test"
        self.cluster_name = "batch-1-standard"

    def configure_clients(self, instances):
        state = {instance.id: instance for instance in instances}
        update_calls = []
        vnic_state = {}
        vnic_update_calls = []
        for index, instance in enumerate(instances, start=10):
            vnic_id = "ocid1.vnic."+instance.id.rsplit(".", 1)[-1]
            vnic_state[instance.id] = SimpleNamespace(
                id=vnic_id,
                compartment_id=self.compartment_id,
                lifecycle_state="AVAILABLE",
                is_primary=True,
                display_name=instance.display_name,
                hostname_label="generated-label-"+str(index),
                private_ip="10.0.0."+str(index),
            )

        def get_instance(instance_id, **kwargs):
            return SimpleNamespace(data=state[instance_id])

        def update_instance(instance_id, details, **kwargs):
            update_calls.append((instance_id, details, kwargs))
            state[instance_id].display_name = details.display_name
            return SimpleNamespace(data=state[instance_id])

        def list_vnic_attachments(*args, **kwargs):
            instance_id = kwargs["instance_id"]
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        lifecycle_state="ATTACHED",
                        vnic_id=vnic_state[instance_id].id,
                    )
                ]
            )

        def get_vnic(vnic_id, **kwargs):
            vnic = next(
                vnic for vnic in vnic_state.values()
                if vnic.id == vnic_id
            )
            return SimpleNamespace(
                data=vnic,
                headers={"etag": "etag-"+vnic_id},
            )

        def update_vnic(vnic_id, details, **kwargs):
            vnic_update_calls.append((vnic_id, details, kwargs))
            vnic = next(
                vnic for vnic in vnic_state.values()
                if vnic.id == vnic_id
            )
            vnic.display_name = details.display_name
            return SimpleNamespace(data=vnic)

        self.namespace["computeManagementClient"] = SimpleNamespace(
            list_instance_pool_instances=lambda **kwargs: SimpleNamespace(
                data=[SimpleNamespace(id=instance.id) for instance in instances]
            ),
            get_instance_pool_instance=lambda *args, **kwargs: SimpleNamespace(
                data=SimpleNamespace(lifecycle_state="ACTIVE")
            ),
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=get_instance,
            update_instance=update_instance,
            list_vnic_attachments=list_vnic_attachments,
        )
        self.namespace["virtualNetworkClient"] = SimpleNamespace(
            get_vnic=get_vnic,
            update_vnic=update_vnic,
        )
        self.vnic_state = vnic_state
        self.vnic_update_calls = vnic_update_calls
        return state, update_calls

    def test_updates_only_mismatched_instances_and_is_idempotent(self):
        first = make_instance("ocid1.instance.one", "generated-one")
        second = make_instance("ocid1.instance.two", "render-node-b")
        state, calls = self.configure_clients([first, second])
        desired = {first.id: "render-node-a", second.id: "render-node-b"}

        previous = self.namespace["update_instance_pool_display_names"](
            self.compartment_id, self.pool_id, self.cluster_name, desired,
            max_wait_seconds=0,
        )
        self.namespace["update_instance_pool_display_names"](
            self.compartment_id, self.pool_id, self.cluster_name, desired,
            max_wait_seconds=0,
        )

        self.assertEqual(previous[first.id], "generated-one")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], first.id)
        self.assertEqual(calls[0][1].display_name, "render-node-a")
        self.assertRegex(calls[0][2]["opc_retry_token"], r"^[0-9a-f-]{36}$")
        self.assertEqual(state[first.id].display_name, "render-node-a")
        self.assertEqual(len(self.vnic_update_calls), 1)
        vnic_id, details, kwargs = self.vnic_update_calls[0]
        self.assertEqual(vnic_id, self.vnic_state[first.id].id)
        self.assertEqual(details.display_name, "render-node-a")
        self.assertFalse(hasattr(details, "hostname_label"))
        self.assertNotIn("opc_retry_token", kwargs)
        self.assertEqual(kwargs["if_match"], "etag-"+vnic_id)
        self.assertEqual(
            self.vnic_state[first.id].hostname_label,
            "generated-label-10",
        )

    def test_updates_only_the_explicit_primary_vnic(self):
        instance = make_instance("ocid1.instance.one", "generated-one")
        _, calls = self.configure_clients([instance])
        primary_vnic = self.vnic_state[instance.id]
        secondary_vnic = SimpleNamespace(
            id="ocid1.vnic.secondary",
            compartment_id=self.compartment_id,
            lifecycle_state="AVAILABLE",
            is_primary=False,
            display_name="weka",
            hostname_label="weka",
            private_ip="10.0.1.10",
        )
        original_get_vnic = self.namespace["virtualNetworkClient"].get_vnic
        self.namespace["virtualNetworkClient"].get_vnic = lambda vnic_id, **kwargs: (
            SimpleNamespace(
                data=secondary_vnic,
                headers={"etag": "etag-"+vnic_id},
            )
            if vnic_id == secondary_vnic.id
            else original_get_vnic(vnic_id, **kwargs)
        )
        self.namespace["computeClient"].list_vnic_attachments = lambda *args, **kwargs: SimpleNamespace(
            data=[
                SimpleNamespace(
                    lifecycle_state="ATTACHED",
                    nic_index=0,
                    vnic_id=secondary_vnic.id,
                ),
                SimpleNamespace(
                    lifecycle_state="ATTACHED",
                    nic_index=0,
                    vnic_id=primary_vnic.id,
                ),
            ]
        )

        self.namespace["update_instance_pool_display_names"](
            self.compartment_id,
            self.pool_id,
            self.cluster_name,
            {instance.id: "render-node-a"},
            max_wait_seconds=0,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.vnic_update_calls), 1)
        self.assertEqual(self.vnic_update_calls[0][0], primary_vnic.id)
        self.assertEqual(primary_vnic.display_name, "render-node-a")
        self.assertEqual(secondary_vnic.display_name, "weka")
        self.assertEqual(secondary_vnic.hostname_label, "weka")

    def test_updates_stale_vnic_when_instance_name_already_matches(self):
        instance = make_instance("ocid1.instance.one", "render-node-a")
        _, calls = self.configure_clients([instance])
        self.vnic_state[instance.id].display_name = "generated-one"

        self.namespace["update_instance_pool_display_names"](
            self.compartment_id,
            self.pool_id,
            self.cluster_name,
            {instance.id: "render-node-a"},
            max_wait_seconds=0,
        )

        self.assertEqual(calls, [])
        self.assertEqual(len(self.vnic_update_calls), 1)
        self.assertEqual(
            self.vnic_state[instance.id].display_name,
            "render-node-a",
        )

    def test_accepts_primary_vnic_in_shared_subnet_compartment(self):
        instance = make_instance("ocid1.instance.one", "generated-one")
        _, calls = self.configure_clients([instance])
        self.vnic_state[instance.id].compartment_id = (
            "ocid1.compartment.shared-network"
        )

        self.namespace["update_instance_pool_display_names"](
            self.compartment_id,
            self.pool_id,
            self.cluster_name,
            {instance.id: "render-node-a"},
            max_wait_seconds=0,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.vnic_update_calls), 1)
        self.assertEqual(
            self.vnic_state[instance.id].display_name,
            "render-node-a",
        )

    def test_validates_all_primary_vnics_before_first_mutation(self):
        first = make_instance("ocid1.instance.one", "generated-one")
        second = make_instance("ocid1.instance.two", "generated-two")
        _, calls = self.configure_clients([first, second])
        self.vnic_state[second.id].is_primary = False

        with self.assertRaisesRegex(RuntimeError, "exactly one primary VNIC"):
            self.namespace["update_instance_pool_display_names"](
                self.compartment_id,
                self.pool_id,
                self.cluster_name,
                {first.id: "render-node-a", second.id: "render-node-b"},
                max_wait_seconds=0,
            )

        self.assertEqual(calls, [])
        self.assertEqual(self.vnic_update_calls, [])

    def test_mutation_requires_explicit_attached_available_primary_vnic(self):
        cases = [
            ("attachment", None, r"exactly one primary VNIC"),
            ("is_primary", None, r"exactly one primary VNIC"),
            ("lifecycle_state", None, r"not AVAILABLE"),
        ]
        for target, value, expected_message in cases:
            with self.subTest(target=target):
                instance = make_instance(
                    "ocid1.instance.one",
                    "generated-one",
                )
                _, calls = self.configure_clients([instance])
                before_updates = mock.Mock()
                if target == "attachment":
                    vnic_id = self.vnic_state[instance.id].id
                    self.namespace[
                        "computeClient"
                    ].list_vnic_attachments = lambda *args, **kwargs: SimpleNamespace(
                        data=[SimpleNamespace(vnic_id=vnic_id)]
                    )
                else:
                    setattr(self.vnic_state[instance.id], target, value)

                with self.assertRaisesRegex(RuntimeError, expected_message):
                    self.namespace["update_instance_pool_display_names"](
                        self.compartment_id,
                        self.pool_id,
                        self.cluster_name,
                        {instance.id: "render-node-a"},
                        max_wait_seconds=0,
                        before_updates=before_updates,
                    )

                before_updates.assert_not_called()
                self.assertEqual(calls, [])
                self.assertEqual(self.vnic_update_calls, [])

    def test_vnic_failure_keeps_instance_name_unchanged(self):
        instance = make_instance("ocid1.instance.one", "generated-one")
        state, calls = self.configure_clients([instance])
        before_updates = mock.Mock()
        self.namespace["virtualNetworkClient"].update_vnic = mock.Mock(
            side_effect=RuntimeError("VNIC update failed")
        )

        with self.assertRaisesRegex(RuntimeError, "VNIC update failed"):
            self.namespace["update_instance_pool_display_names"](
                self.compartment_id,
                self.pool_id,
                self.cluster_name,
                {instance.id: "render-node-a"},
                max_wait_seconds=0,
                before_updates=before_updates,
            )

        before_updates.assert_called_once_with({instance.id: "generated-one"})
        self.assertEqual(calls, [])
        self.assertEqual(state[instance.id].display_name, "generated-one")

    def test_rejects_primary_vnic_ip_change_before_first_mutation(self):
        instance = make_instance("ocid1.instance.one", "generated-one")
        _, calls = self.configure_clients([instance])

        with self.assertRaisesRegex(RuntimeError, "private IP changed"):
            self.namespace["update_instance_pool_display_names"](
                self.compartment_id,
                self.pool_id,
                self.cluster_name,
                {instance.id: "render-node-a"},
                max_wait_seconds=0,
                expected_private_ips_by_instance_id={
                    instance.id: "10.0.0.99",
                },
            )

        self.assertEqual(calls, [])
        self.assertEqual(self.vnic_update_calls, [])

    def test_accepts_legacy_member_state_during_update_preflight(self):
        instance = make_instance("ocid1.instance.legacy", "generated-legacy")
        instance.lifecycle_state = "Running"
        _, calls = self.configure_clients([instance])
        self.namespace[
            "computeManagementClient"
        ].get_instance_pool_instance = mock.Mock(
            return_value=SimpleNamespace(
                data=SimpleNamespace(
                    lifecycle_state=None,
                    state="Running",
                )
            )
        )

        self.namespace["update_instance_pool_display_names"](
            self.compartment_id,
            self.pool_id,
            self.cluster_name,
            {instance.id: "render-node-legacy"},
            max_wait_seconds=0,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], instance.id)

    def test_rejects_update_when_all_membership_state_is_missing(self):
        instance = make_instance("ocid1.instance.unknown-state", "generated-one")
        _, calls = self.configure_clients([instance])
        self.namespace[
            "computeManagementClient"
        ].get_instance_pool_instance = mock.Mock(
            return_value=SimpleNamespace(
                data=SimpleNamespace(
                    lifecycle_state=None,
                    state=None,
                )
            )
        )

        with self.assertRaisesRegex(RuntimeError, "not an ACTIVE"):
            self.namespace["update_instance_pool_display_names"](
                self.compartment_id,
                self.pool_id,
                self.cluster_name,
                {instance.id: "render-node-a"},
                max_wait_seconds=0,
            )

        self.assertEqual(calls, [])

    def test_validates_all_instances_before_first_mutation(self):
        first = make_instance("ocid1.instance.one", "generated-one")
        second = make_instance(
            "ocid1.instance.two", "generated-two", cluster_name="another-cluster"
        )
        _, calls = self.configure_clients([first, second])
        with self.assertRaisesRegex(RuntimeError, "does not belong to cluster"):
            self.namespace["update_instance_pool_display_names"](
                self.compartment_id,
                self.pool_id,
                self.cluster_name,
                {first.id: "render-node-a", second.id: "render-node-b"},
                max_wait_seconds=0,
            )
        self.assertEqual(calls, [])

    def test_rejects_nonmember_and_detaching_instances_before_mutation(self):
        instance = make_instance("ocid1.instance.one", "generated-one")
        _, calls = self.configure_clients([instance])
        with self.assertRaisesRegex(RuntimeError, "not active members"):
            self.namespace["update_instance_pool_display_names"](
                self.compartment_id,
                self.pool_id,
                self.cluster_name,
                {instance.id: "render-node-a", "ocid1.instance.unknown": "other"},
                max_wait_seconds=0,
            )
        self.namespace["computeManagementClient"].get_instance_pool_instance = mock.Mock(
            return_value=SimpleNamespace(
                data=SimpleNamespace(lifecycle_state="DETACHING")
            )
        )
        with self.assertRaisesRegex(RuntimeError, r"DETACHING|not an ACTIVE"):
            self.namespace["update_instance_pool_display_names"](
                self.compartment_id,
                self.pool_id,
                self.cluster_name,
                {instance.id: "render-node-a"},
                max_wait_seconds=0,
            )
        self.assertEqual(calls, [])


class InstancePoolInventoryRewriteTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def test_rewrites_by_ocid_updates_nfs_and_removes_stale_desired_hostname(self):
        configured = (
            "  generated-one ansible_host=10.0.0.10 ansible_user=opc role=compute "
            "oci_instance_id=ocid1.instance.one desired_hostname=predicted-name "
            "use_local_block_volume=true # keep me\n"
        )
        to_add = (
            "generated-two ansible_host=10.0.0.11 ansible_user=opc role=compute "
            "oci_instance_id=ocid1.instance.two\n"
        )
        untouched = (
            "unrelated ansible_host=10.0.0.20 role=compute "
            "oci_instance_id=ocid1.instance.other\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "inventory")
            with open(path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(
                    make_inventory_text(
                        compute_configured=configured+untouched,
                        compute_to_add=to_add,
                        nfs="generated-one ansible_user=opc role=nfs\n",
                    )
                )
            parsed = self.namespace["parse_inventory"](path)
            rewritten = self.namespace["rewrite_instance_pool_inventory_names"](
                path,
                parsed,
                {
                    "ocid1.instance.one": "render-node-a",
                    "ocid1.instance.two": "render-node-b",
                },
            )
            with open(path, encoding="utf-8") as inventory_file:
                persisted = inventory_file.read()

        self.assertIn("render-node-a ansible_host=10.0.0.10", persisted)
        self.assertIn("render-node-b ansible_host=10.0.0.11", persisted)
        self.assertIn("use_local_block_volume=true # keep me", persisted)
        self.assertIn(untouched.strip(), persisted)
        self.assertNotIn("desired_hostname=", persisted)
        self.assertEqual(
            rewritten["nfs"], ["render-node-a ansible_user=opc role=nfs\n"]
        )

    def test_duplicate_ocid_is_rejected_without_writing(self):
        original = make_inventory_text(
            compute_configured=(
                "old-one ansible_host=10.0.0.10 oci_instance_id=ocid1.instance.one\n"
            ),
            compute_to_add=(
                "old-copy ansible_host=10.0.0.11 oci_instance_id=ocid1.instance.one\n"
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "inventory")
            with open(path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(original)
            parsed = self.namespace["parse_inventory"](path)
            with self.assertRaisesRegex(RuntimeError, "occurs more than once"):
                self.namespace["rewrite_instance_pool_inventory_names"](
                    path, parsed, {"ocid1.instance.one": "render-node-a"}
                )
            with open(path, encoding="utf-8") as inventory_file:
                self.assertEqual(inventory_file.read(), original)

    def test_plan_rejects_duplicate_or_reserved_actual_hostnames(self):
        validate = self.namespace["validate_instance_pool_name_plan"]
        inventory = {
            "controller": ["controller ansible_host=10.0.0.2\n"],
            "login": ["reserved-name ansible_host=10.0.0.3\n"],
            "compute_configured": [
                "generated-one ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n"
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "not unique"):
            validate(
                inventory,
                {
                    "ocid1.instance.one": "same-name",
                    "ocid1.instance.two": "same-name",
                },
            )
        with self.assertRaisesRegex(RuntimeError, "conflicts with inventory"):
            validate(inventory, {"ocid1.instance.one": "reserved-name"})

    def test_repeated_sync_allows_nfs_alias_that_is_the_same_compute_host(self):
        inventory = {
            "controller": [],
            "slurm_backup": [],
            "login": [],
            "compute_configured": [
                "final-worker ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n"
            ],
            "compute_to_add": [],
            "compute_to_destroy": [],
            "nfs": ["final-worker ansible_user=opc role=nfs\n"],
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "dns_entries=false\n",
            ],
        }

        self.namespace["validate_instance_pool_name_plan"](
            inventory,
            {"ocid1.instance.one": "final-worker"},
            {"ocid1.instance.one": "10.0.0.10"},
        )


class InstancePoolNameDnsTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()
        self.zone_id = "ocid1.dns-zone.test"

    def test_slurm_dns_alias_is_not_managed_when_slurm_is_disabled(self):
        inventory = {
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "slurm=false\n",
                "queue=batch\n",
                "instance_type=standard\n",
                "private_subnet=10.0.0.0/24\n",
            ],
        }

        self.assertEqual(
            self.namespace["get_instance_pool_node_dns_domains"](
                inventory,
                ["worker-alpha"],
                "10.0.0.10",
            ),
            ["worker-alpha.batch-1-standard.local"],
        )

    def test_case_only_rename_does_not_delete_canonical_rrset(self):
        inventory = {
            "compute_configured": [
                "worker-alpha ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n"
            ],
            "compute_to_add": [],
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "dns_entries=true\n",
            ],
        }
        dns_client = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id=self.zone_id)]
                )
            ),
            get_rr_set=mock.Mock(
                return_value=SimpleNamespace(data=SimpleNamespace(items=[]))
            ),
            update_rr_set=mock.Mock(),
        )
        delete_rrset = mock.Mock()
        self.namespace["dns_client"] = dns_client
        self.namespace["delete_private_dns_rrset_if_present"] = delete_rrset

        self.namespace["reconcile_instance_pool_name_dns"](
            "ocid1.compartment.test",
            inventory,
            {"ocid1.instance.one": {"ip": "10.0.0.10"}},
            {"ocid1.instance.one": "Worker-Alpha"},
            {"ocid1.instance.one": "worker-alpha"},
        )

        dns_client.update_rr_set.assert_called_once()
        self.assertEqual(
            dns_client.update_rr_set.call_args.kwargs["domain"],
            "Worker-Alpha.batch-1-standard.local",
        )
        delete_rrset.assert_not_called()

    def test_previous_display_name_cannot_delete_active_slurm_dns_alias(self):
        inventory = {
            "controller": [],
            "slurm_backup": [],
            "login": [],
            "compute_configured": [
                "generated-one ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n"
            ],
            "compute_to_add": [],
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "dns_entries=true\n",
                "slurm=true\n",
                "queue=batch\n",
                "instance_type=standard\n",
                "private_subnet=10.0.0.0/24\n",
            ],
        }
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id=self.zone_id)]
                )
            ),
            get_rr_set=mock.Mock(
                side_effect=lambda **kwargs: SimpleNamespace(
                    data=SimpleNamespace(
                        items=(
                            [SimpleNamespace(rtype="A", rdata="10.0.0.10")]
                            if kwargs["domain"].startswith("generated-one.")
                            else []
                        )
                    )
                )
            ),
            update_rr_set=mock.Mock(),
        )
        delete_rrset = self.namespace[
            "delete_private_dns_rrset_if_present"
        ] = mock.Mock()

        self.namespace["reconcile_instance_pool_name_dns"](
            "ocid1.compartment.test",
            inventory,
            {"ocid1.instance.one": {"ip": "10.0.0.10"}},
            {"ocid1.instance.one": "final-worker"},
            {"ocid1.instance.one": "batch-standard-11"},
        )

        delete_rrset.assert_called_once_with(
            self.zone_id,
            "generated-one.batch-1-standard.local",
        )

    def test_foreign_existing_rrset_blocks_all_updates(self):
        inventory = {
            "compute_configured": [
                "generated-one ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n"
            ],
            "compute_to_add": [],
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "dns_entries=true\n",
            ],
        }
        update_rrset = mock.Mock()
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id=self.zone_id)]
                )
            ),
            get_rr_set=mock.Mock(
                return_value=SimpleNamespace(
                    data=SimpleNamespace(
                        items=[SimpleNamespace(rtype="A", rdata="10.9.9.9")]
                    )
                )
            ),
            update_rr_set=update_rrset,
        )

        with self.assertRaisesRegex(RuntimeError, "not owned"):
            self.namespace["reconcile_instance_pool_name_dns"](
                "ocid1.compartment.test",
                inventory,
                {
                    "ocid1.instance.one": {
                        "display_name": "generated-one",
                        "ip": "10.0.0.10",
                    }
                },
                {"ocid1.instance.one": "final-worker"},
                {"ocid1.instance.one": "generated-one"},
            )

        update_rrset.assert_not_called()

    def test_pending_old_ip_cannot_delete_reused_hostname_dns(self):
        domain = "worker-alpha.batch-1-standard.local"
        record = {
            "cluster_name": "batch-1-standard",
            "compartment_id": "ocid1.compartment.test",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_id": "ocid1.instance.old",
            "instance_display_name": "worker-alpha",
            "instance_names": ["worker-alpha"],
            "private_ip": "10.0.0.10",
            "zone_name": "batch-1-standard.local",
            "dns_domains": [domain],
        }
        ownership = {
            "version": 1,
            "cluster_name": "batch-1-standard",
            "instance_pool_id": "ocid1.instancepool.test",
            "rrsets": [
                {
                    "zone_id": self.zone_id,
                    "zone_name": "batch-1-standard.local",
                    "domain": domain,
                    "private_ips": ["10.0.0.20"],
                }
            ],
        }
        self.namespace["load_instance_pool_name_dns_ownership"] = mock.Mock(
            return_value=ownership
        )
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id=self.zone_id)]
                )
            )
        )
        delete_owned = self.namespace[
            "delete_private_dns_a_rrset_if_owned"
        ] = mock.Mock(side_effect=RuntimeError("RRset is not owned by the old IP"))
        write_ownership = self.namespace[
            "write_instance_pool_name_dns_ownership"
        ] = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "not owned"):
            self.namespace["delete_pending_instance_pool_node_dns_records"](
                record,
                "/tmp/inventory",
            )

        delete_owned.assert_called_once_with(
            self.zone_id,
            domain,
            {"10.0.0.10"},
        )
        write_ownership.assert_not_called()

    def test_pending_matching_ip_deletes_and_removes_dns_ownership(self):
        domain = "worker-alpha.batch-1-standard.local"
        record = {
            "cluster_name": "batch-1-standard",
            "compartment_id": "ocid1.compartment.test",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_id": "ocid1.instance.old",
            "instance_display_name": "worker-alpha",
            "instance_names": ["worker-alpha"],
            "private_ip": "10.0.0.10",
            "zone_name": "batch-1-standard.local",
            "dns_domains": [domain],
        }
        ownership = {
            "version": 1,
            "cluster_name": "batch-1-standard",
            "instance_pool_id": "ocid1.instancepool.test",
            "rrsets": [
                {
                    "zone_id": self.zone_id,
                    "zone_name": "batch-1-standard.local",
                    "domain": domain,
                    "private_ips": ["10.0.0.10"],
                }
            ],
        }
        self.namespace["load_instance_pool_name_dns_ownership"] = mock.Mock(
            return_value=ownership
        )
        self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock()
        delete_owned = self.namespace[
            "delete_private_dns_a_rrset_if_owned"
        ] = mock.Mock()
        write_ownership = self.namespace[
            "write_instance_pool_name_dns_ownership"
        ] = mock.Mock()

        self.namespace["delete_pending_instance_pool_node_dns_records"](
            record,
            "/tmp/inventory",
        )

        delete_owned.assert_called_once_with(
            self.zone_id,
            domain,
            ["10.0.0.10"],
        )
        self.assertEqual(
            write_ownership.call_args.args[1]["rrsets"],
            [],
        )

    def test_persisted_dns_ownership_is_cleaned_when_dns_is_disabled(self):
        rrsets = {}

        def get_rr_set(**kwargs):
            private_ip = rrsets.get((kwargs["zone_name_or_id"], kwargs["domain"]))
            items = (
                [SimpleNamespace(rtype="A", rdata=private_ip)]
                if private_ip is not None
                else []
            )
            return SimpleNamespace(data=SimpleNamespace(items=items))

        def update_rr_set(**kwargs):
            rrsets[(kwargs["zone_name_or_id"], kwargs["domain"])] = (
                kwargs["update_rr_set_details"].items[0].rdata
            )

        def delete_rrset(zone_id, domain):
            rrsets.pop((zone_id, domain), None)

        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id=self.zone_id)]
                )
            ),
            get_rr_set=get_rr_set,
            update_rr_set=update_rr_set,
        )
        self.namespace["delete_private_dns_rrset_if_present"] = delete_rrset
        enabled_inventory = {
            "controller": [],
            "slurm_backup": [],
            "login": [],
            "compute_configured": [
                "generated-one ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n"
            ],
            "compute_to_add": [],
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "dns_entries=true\n",
            ],
        }
        disabled_inventory = {
            **enabled_inventory,
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "dns_entries=false\n",
            ],
        }
        instances = {
            "ocid1.instance.one": {
                "display_name": "generated-one",
                "ip": "10.0.0.10",
            }
        }
        desired = {"ocid1.instance.one": "final-worker"}

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = os.path.join(directory, "inventory")
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(make_inventory_text())
            self.namespace["reconcile_instance_pool_name_dns"](
                "ocid1.compartment.test",
                enabled_inventory,
                instances,
                desired,
                {"ocid1.instance.one": "generated-one"},
                inventory_path=inventory_path,
                instance_pool_id="ocid1.instancepool.test",
            )
            ownership_path = self.namespace[
                "get_instance_pool_name_dns_ownership_path"
            ](inventory_path)
            self.assertTrue(os.path.isfile(ownership_path))
            self.assertEqual(
                rrsets,
                {(self.zone_id, "final-worker.batch-1-standard.local"): "10.0.0.10"},
            )

            self.namespace["reconcile_instance_pool_name_dns"](
                "ocid1.compartment.test",
                disabled_inventory,
                instances,
                desired,
                {"ocid1.instance.one": "final-worker"},
                inventory_path=inventory_path,
                instance_pool_id="ocid1.instancepool.test",
            )

            self.assertFalse(os.path.exists(ownership_path))
            self.assertEqual(rrsets, {})

    def test_cleanup_includes_current_pool_display_names(self):
        inventory = {
            "compute_configured": [
                "inventory-alias ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n"
            ],
            "compute_to_add": [],
            "compute_to_destroy": [],
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "dns_entries=true\n",
            ],
        }
        pool = SimpleNamespace(
            id="ocid1.instancepool.test",
            display_name="batch-1-standard",
            compartment_id="ocid1.compartment.test",
            lifecycle_state="RUNNING",
        )
        member = make_instance("ocid1.instance.one", "final-os-hostname")
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id=self.zone_id)]
                )
            ),
            get_rr_set=mock.Mock(
                return_value=SimpleNamespace(
                    data=SimpleNamespace(
                        items=[SimpleNamespace(rtype="A", rdata="10.0.0.10")]
                    )
                )
            ),
        )
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=mock.Mock(return_value=SimpleNamespace(data=pool)),
            list_instance_pool_instances=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id=member.id)]
                )
            ),
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=mock.Mock(return_value=SimpleNamespace(data=member))
        )
        deleted = []
        self.namespace["delete_private_dns_rrset_if_present"] = (
            lambda zone_id, domain: deleted.append((zone_id, domain))
        )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = os.path.join(directory, "inventory")
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(make_inventory_text(dns_entries="true"))
            write_instance_pool_state(directory, pool.id)
            self.namespace["cleanup_instance_pool_name_dns_records"](
                "ocid1.compartment.test",
                inventory,
                inventory_path=inventory_path,
            )

        self.assertEqual(
            set(deleted),
            {
                (self.zone_id, "inventory-alias.batch-1-standard.local"),
                (self.zone_id, "final-os-hostname.batch-1-standard.local"),
            },
        )

    def test_cleanup_treats_missing_private_zone_as_success(self):
        inventory = {
            "compute_configured": [],
            "compute_to_add": [],
            "compute_to_destroy": [],
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "dns_entries=true\n",
            ],
        }
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(return_value=SimpleNamespace(data=[]))
        )
        list_pools = mock.Mock(side_effect=AssertionError("pool lookup is unnecessary"))
        self.namespace["computeManagementClient"] = SimpleNamespace(
            list_instance_pools=list_pools
        )
        delete_rrset = mock.Mock()
        self.namespace["delete_private_dns_rrset_if_present"] = delete_rrset

        result = self.namespace["cleanup_instance_pool_name_dns_records"](
            "ocid1.compartment.test", inventory
        )

        self.assertIsNone(result)
        list_pools.assert_not_called()
        delete_rrset.assert_not_called()

    def test_cleanup_includes_all_pending_sync_plan_aliases(self):
        inventory_text = make_inventory_text(dns_entries="true")
        pool = SimpleNamespace(
            id="ocid1.instancepool.test",
            display_name="batch-1-standard",
            compartment_id="ocid1.compartment.test",
            lifecycle_state="RUNNING",
        )
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id=self.zone_id)]
                )
            ),
            get_rr_set=mock.Mock(
                return_value=SimpleNamespace(
                    data=SimpleNamespace(
                        items=[SimpleNamespace(rtype="A", rdata="10.0.0.10")]
                    )
                )
            ),
        )
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=mock.Mock(return_value=SimpleNamespace(data=pool)),
            list_instance_pool_instances=mock.Mock(
                return_value=SimpleNamespace(data=[])
            ),
        )
        deleted = []
        self.namespace["delete_private_dns_rrset_if_present"] = (
            lambda zone_id, domain: deleted.append((zone_id, domain))
        )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = os.path.join(directory, "inventory")
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(inventory_text)
            inventory = self.namespace["parse_inventory"](inventory_path)
            self.namespace["write_instance_pool_hostname_sync_plan"](
                inventory_path,
                {
                    "version": 1,
                    "status": "pending",
                    "cluster_name": "batch-1-standard",
                    "instance_pool_id": pool.id,
                    "members": {
                        "ocid1.instance.one": {
                            "hostname": "final-os-hostname",
                            "private_ip": "10.0.0.10",
                            "inventory_hostname": "pre-ansible-alias",
                            "previous_display_name": "generated-one",
                        }
                    },
                },
            )

            self.namespace["cleanup_instance_pool_name_dns_records"](
                "ocid1.compartment.test",
                inventory,
                inventory_path=inventory_path,
            )

        self.assertEqual(
            set(deleted),
            {
                (self.zone_id, "final-os-hostname.batch-1-standard.local"),
                (self.zone_id, "generated-one.batch-1-standard.local"),
                (self.zone_id, "pre-ansible-alias.batch-1-standard.local"),
            },
        )

    def test_cleanup_rejects_plan_for_other_cluster_or_pool_before_delete(self):
        active_pool = SimpleNamespace(
            id="ocid1.instancepool.test",
            display_name="batch-1-standard",
            compartment_id="ocid1.compartment.test",
            lifecycle_state="RUNNING",
        )
        cases = (
            ("another-cluster", active_pool.id, "another cluster"),
            ("batch-1-standard", "ocid1.instancepool.other", "another pool"),
        )
        for plan_cluster, plan_pool, expected_error in cases:
            with self.subTest(plan_cluster=plan_cluster, plan_pool=plan_pool):
                self.namespace["dns_client"] = SimpleNamespace(
                    list_zones=mock.Mock(
                        return_value=SimpleNamespace(
                            data=[SimpleNamespace(id=self.zone_id)]
                        )
                    )
                )
                self.namespace["computeManagementClient"] = SimpleNamespace(
                    get_instance_pool=mock.Mock(
                        side_effect=AssertionError(
                            "an invalid plan must be rejected before member cleanup"
                        )
                    ),
                )
                delete_rrset = mock.Mock()
                self.namespace["delete_private_dns_rrset_if_present"] = delete_rrset
                with tempfile.TemporaryDirectory() as directory:
                    inventory_path = os.path.join(directory, "inventory")
                    with open(
                        inventory_path, "w", encoding="utf-8"
                    ) as inventory_file:
                        inventory_file.write(make_inventory_text(dns_entries="true"))
                    write_instance_pool_state(directory, active_pool.id)
                    inventory = self.namespace["parse_inventory"](inventory_path)
                    self.namespace["write_instance_pool_hostname_sync_plan"](
                        inventory_path,
                        {
                            "version": 1,
                            "status": "pending",
                            "cluster_name": plan_cluster,
                            "instance_pool_id": plan_pool,
                            "members": {
                                "ocid1.instance.one": {
                                    "hostname": "final-os-hostname",
                                    "private_ip": "10.0.0.10",
                                    "inventory_hostname": "pre-ansible-alias",
                                    "previous_display_name": "generated-one",
                                }
                            },
                        },
                    )

                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        self.namespace["cleanup_instance_pool_name_dns_records"](
                            "ocid1.compartment.test",
                            inventory,
                            inventory_path=inventory_path,
                        )

                delete_rrset.assert_not_called()


class InstancePoolDnsOwnershipMigrationTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def write_cluster_files(self, directory, for_each_value):
        inventory_path = os.path.join(directory, "inventory")
        with open(inventory_path, "w", encoding="utf-8") as inventory_file:
            inventory_file.write(
                make_inventory_text(dns_entries="true")+
                "cluster_network=false\n"
            )
        with open(os.path.join(directory, "variables.tf"), "w", encoding="utf-8") as variables:
            variables.write('variable "compute_cluster" { default = false }\n')
        with open(os.path.join(directory, "network.tf"), "w", encoding="utf-8") as network:
            network.write(
                'resource "oci_dns_rrset" "rrset-cluster-network-OCI" {\n'
                '  for_each = '+for_each_value+'\n'
                '}\n'
                'resource "next" "sentinel" {}\n'
            )
        return inventory_path

    def configure_dns(self):
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=SimpleNamespace(
                    data=[SimpleNamespace(id="ocid1.dns-zone.test")]
                )
            ),
            get_rr_set=mock.Mock(
                return_value=SimpleNamespace(
                    data=SimpleNamespace(
                        items=[SimpleNamespace(rtype="A", rdata="10.0.0.10")]
                    )
                )
            ),
        )

    def test_legacy_state_is_released_only_after_dns_ownership_is_persisted(self):
        legacy = (
            "var.dns_entries ? toset([for v in range(var.node_count) : "
            "tostring(v)]) : []"
        )
        calls = []

        def run_terraform(command, **kwargs):
            calls.append(list(command))
            if command[:3] == ["terraform", "state", "list"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        'oci_dns_rrset.rrset-cluster-network-OCI["0"]\n'
                    ),
                    stderr="",
                )
            self.assertTrue(
                os.path.isfile(
                    self.namespace["get_instance_pool_name_dns_ownership_path"](
                        inventory_path
                    )
                ),
                "DNS ownership must be durable before terraform state rm",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_cluster_files(directory, legacy)
            with open(
                os.path.join(directory, "terraform.tfstate"),
                "w",
                encoding="utf-8",
            ) as state_file:
                json.dump(
                    {
                        "resources": [{
                            "mode": "managed",
                            "type": "oci_dns_rrset",
                            "name": "rrset-cluster-network-OCI",
                            "instances": [{
                                "index_key": "0",
                                "attributes": {
                                    "zone_name_or_id": "ocid1.dns-zone.test",
                                    "domain": (
                                        "generated-one."
                                        "batch-1-standard.local"
                                    ),
                                    "rtype": "A",
                                    "scope": "PRIVATE",
                                    "items": [{
                                        "domain": (
                                            "generated-one."
                                            "batch-1-standard.local"
                                        ),
                                        "rdata": "10.0.0.10",
                                        "rtype": "A",
                                        "ttl": 3600,
                                    }],
                                },
                            }],
                        }],
                    },
                    state_file,
                )
            self.configure_dns()
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=run_terraform,
            ):
                changed = self.namespace[
                    "migrate_instance_pool_oci_dns_ownership"
                ](
                    inventory_path,
                    compartment_id="ocid1.compartment.test",
                    instance_pool_id="ocid1.instancepool.test",
                    instances_by_id={
                        "ocid1.instance.one": {
                            "display_name": "generated-one",
                            "ip": "10.0.0.10",
                        }
                    },
                )
            with open(os.path.join(directory, "network.tf"), encoding="utf-8") as network:
                migrated = network.read()
            ownership = self.namespace[
                "load_instance_pool_name_dns_ownership"
            ](inventory_path)

        self.assertTrue(changed)
        self.assertIn("var.dns_entries && var.compute_cluster", migrated)
        self.assertEqual(calls[1][:3], ["terraform", "state", "rm"])
        self.assertEqual(
            ownership["rrsets"][0]["domain"],
            "generated-one.batch-1-standard.local",
        )

    def test_v1_instance_pool_marker_upgrades_without_nested_terraform_state_commands(self):
        current = (
            "var.dns_entries && (var.cluster_network || var.compute_cluster) ? "
            "toset([for v in range(var.node_count) : tostring(v)]) : []"
        )
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_cluster_files(directory, current)
            marker_path = os.path.join(
                directory,
                self.namespace["INSTANCE_POOL_DNS_OWNERSHIP_MARKER_FILENAME"],
            )
            with open(marker_path, "w", encoding="utf-8") as marker:
                marker.write("python-owned-canonical-dns-v1\n")
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=AssertionError("new clusters must not nest terraform state"),
            ):
                changed = self.namespace[
                    "migrate_instance_pool_oci_dns_ownership"
                ](inventory_path)
            with open(
                os.path.join(directory, "network.tf"),
                encoding="utf-8",
            ) as network_file:
                migrated = network_file.read()
            version_two_marker = os.path.join(
                directory,
                self.namespace["MANAGED_POOL_DNS_OWNERSHIP_MARKER_FILENAME"],
            )
            self.assertTrue(os.path.isfile(version_two_marker))
        self.assertTrue(changed)
        self.assertIn("var.dns_entries && var.compute_cluster", migrated)


class InstancePoolSynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()
        self.compartment_id = "ocid1.compartment.test"
        self.pool_id = "ocid1.instancepool.test"
        self.cluster_name = "batch-1-standard"

    def write_inventory(self, directory, dns_entries="false"):
        path = os.path.join(directory, "inventory")
        with open(path, "w", encoding="utf-8") as inventory_file:
            inventory_file.write(
                make_inventory_text(
                    compute_configured=(
                        "generated-one ansible_host=10.0.0.10 ansible_user=opc "
                        "role=compute oci_instance_id=ocid1.instance.one\n"
                        "generated-two ansible_host=10.0.0.11 ansible_user=opc "
                        "role=compute oci_instance_id=ocid1.instance.two\n"
                    ),
                    nfs="generated-one ansible_user=opc role=nfs\n",
                    dns_entries=dns_entries,
                )
            )
        return path

    def configure_pool(self):
        instances = [
            {"display_name": "generated-one", "ip": "10.0.0.10", "ocid": "ocid1.instance.one"},
            {"display_name": "generated-two", "ip": "10.0.0.11", "ocid": "ocid1.instance.two"},
        ]
        self.namespace["get_complete_instance_pool_instances"] = mock.Mock(
            return_value=(instances, {instance["ocid"]: instance for instance in instances})
        )
        self.namespace["load_pending_local_block_volume_deletions"] = mock.Mock(
            return_value=[]
        )
        self.namespace["reconcile_instance_pool_name_dns"] = mock.Mock()
        self.namespace["refresh_instance_pool_hosts"] = mock.Mock()
        return instances

    def test_actual_post_ansible_hostname_is_source_of_truth_not_ip_formula(self):
        self.configure_pool()
        previous_names = {
            "ocid1.instance.one": "generated-one",
            "ocid1.instance.two": "generated-two",
        }
        update_names = self.namespace["update_instance_pool_display_names"] = mock.Mock(
            side_effect=lambda *args, **kwargs: (
                invoke_update_callback(kwargs, previous_names) or previous_names
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory)
            synchronized, _ = self.namespace["synchronize_instance_pool_names"](
                self.compartment_id,
                self.pool_id,
                path,
                self.cluster_name,
                observed_hostnames_by_instance_id=observed_names(
                    "custom-render-a", "custom-render-b"
                ),
                max_wait_seconds=0,
            )
            with open(path, encoding="utf-8") as inventory_file:
                persisted = inventory_file.read()

        desired = update_names.call_args.args[3]
        self.assertEqual(
            desired,
            {
                "ocid1.instance.one": "custom-render-a",
                "ocid1.instance.two": "custom-render-b",
            },
        )
        self.assertNotIn("batch-standard-node-11", str(desired))
        self.assertIn("custom-render-a ansible_host=10.0.0.10", persisted)
        self.assertEqual(
            {instance["display_name"] for instance in synchronized},
            {"custom-render-a", "custom-render-b"},
        )
        self.namespace["refresh_instance_pool_hosts"].assert_called_once()

    def test_collects_facts_only_when_observed_map_was_not_supplied(self):
        self.configure_pool()
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock(
            return_value=observed_names()
        )
        previous_names = {
            "ocid1.instance.one": "generated-one",
            "ocid1.instance.two": "generated-two",
        }
        self.namespace["update_instance_pool_display_names"] = mock.Mock(
            side_effect=lambda *args, **kwargs: (
                invoke_update_callback(kwargs, previous_names) or previous_names
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory)
            self.namespace["synchronize_instance_pool_names"](
                self.compartment_id,
                self.pool_id,
                path,
                self.cluster_name,
                max_wait_seconds=0,
            )
        collector.assert_called_once_with(path)

    def test_id_ip_or_duplicate_hostname_mismatch_fails_before_oci_mutation(self):
        self.configure_pool()
        update_names = self.namespace["update_instance_pool_display_names"] = mock.Mock()
        base = observed_names()
        bad_cases = (
            ({"ocid1.instance.one": base["ocid1.instance.one"]}, r"OCID|[Ii]nstance"),
            (
                {
                    **base,
                    "ocid1.instance.one": {
                        **base["ocid1.instance.one"],
                        "private_ip": "10.0.0.99",
                    },
                },
                r"IP|ip|identity",
            ),
            (observed_names("Same-Name", "same-name"), r"unique|duplicate"),
        )
        for observations, message in bad_cases:
            with self.subTest(observations=observations):
                with tempfile.TemporaryDirectory() as directory:
                    path = self.write_inventory(directory)
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.namespace["synchronize_instance_pool_names"](
                            self.compartment_id,
                            self.pool_id,
                            path,
                            self.cluster_name,
                            observed_hostnames_by_instance_id=observations,
                            max_wait_seconds=0,
                        )
        update_names.assert_not_called()

    def test_invalid_dns_label_is_rejected_before_oci_mutation(self):
        self.configure_pool()
        update_names = self.namespace["update_instance_pool_display_names"] = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory)
            with self.assertRaisesRegex(RuntimeError, r"hostname|DNS"):
                self.namespace["synchronize_instance_pool_names"](
                    self.compartment_id,
                    self.pool_id,
                    path,
                    self.cluster_name,
                    observed_hostnames_by_instance_id=observed_names("bad_name"),
                    max_wait_seconds=0,
                )
        update_names.assert_not_called()

    def test_inventory_cluster_mismatch_fails_before_facts_or_oci_calls(self):
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock()
        get_pool = self.namespace["get_complete_instance_pool_instances"] = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory)
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                self.namespace["synchronize_instance_pool_names"](
                    self.compartment_id,
                    self.pool_id,
                    path,
                    "another-cluster",
                    max_wait_seconds=0,
                )
        collector.assert_not_called()
        get_pool.assert_not_called()

    def test_pending_volume_deletion_blocks_rename(self):
        self.configure_pool()
        self.namespace["load_pending_local_block_volume_deletions"] = mock.Mock(
            return_value=[
                {
                    "instance_id": "ocid1.instance.one",
                    "instance_display_name": "generated-one",
                }
            ]
        )
        update_names = self.namespace["update_instance_pool_display_names"] = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory)
            with self.assertRaisesRegex(RuntimeError, "pending local Block Volume"):
                self.namespace["synchronize_instance_pool_names"](
                    self.compartment_id,
                    self.pool_id,
                    path,
                    self.cluster_name,
                    observed_hostnames_by_instance_id=observed_names(),
                    max_wait_seconds=0,
                )
        update_names.assert_not_called()

    def test_pending_generic_node_removal_blocks_sync_before_mutation(self):
        get_pool = self.namespace[
            "get_complete_instance_pool_instances"
        ] = mock.Mock()
        update_names = self.namespace[
            "update_instance_pool_display_names"
        ] = mock.Mock()
        self.namespace["load_pending_instance_pool_node_removals"] = mock.Mock(
            return_value=[
                {
                    "cluster_name": self.cluster_name,
                    "compartment_id": self.compartment_id,
                    "instance_pool_id": self.pool_id,
                }
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory)
            with self.assertRaisesRegex(RuntimeError, "node removal is pending"):
                self.namespace["synchronize_instance_pool_names"](
                    self.compartment_id,
                    self.pool_id,
                    path,
                    self.cluster_name,
                    observed_hostnames_by_instance_id=observed_names(),
                    max_wait_seconds=0,
                )

        get_pool.assert_not_called()
        update_names.assert_not_called()


class InstancePoolHostnameSyncPlanRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()
        self.compartment_id = "ocid1.compartment.test"
        self.pool_id = "ocid1.instancepool.test"
        self.cluster_name = "batch-1-standard"

    def write_inventory(self, directory, members):
        path = os.path.join(directory, "inventory")
        lines = []
        for instance_id, member in members.items():
            lines.append(
                member["inventory_hostname"]+
                " ansible_host="+member["private_ip"]+
                " ansible_user=opc role=compute oci_instance_id="+
                instance_id+"\n"
            )
        with open(path, "w", encoding="utf-8") as inventory_file:
            inventory_file.write(
                make_inventory_text(compute_configured="".join(lines))
            )
        return path

    def configure_pool_snapshot(self, state):
        def get_complete(*args, **kwargs):
            instances = [dict(member) for member in state.values()]
            return (
                instances,
                {member["ocid"]: member for member in instances},
            )

        self.namespace["get_complete_instance_pool_instances"] = mock.Mock(
            side_effect=get_complete
        )
        self.namespace["load_pending_local_block_volume_deletions"] = mock.Mock(
            return_value=[]
        )
        self.namespace["reconcile_instance_pool_name_dns"] = mock.Mock()
        self.namespace["refresh_instance_pool_hosts"] = mock.Mock()

    def test_partial_rename_persists_private_plan_and_retry_reuses_it(self):
        inventory_members = observed_names()
        state = {
            "ocid1.instance.one": {
                "display_name": "generated-one",
                "ip": "10.0.0.10",
                "ocid": "ocid1.instance.one",
            },
            "ocid1.instance.two": {
                "display_name": "generated-two",
                "ip": "10.0.0.11",
                "ocid": "ocid1.instance.two",
            },
        }
        self.configure_pool_snapshot(state)
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock(
            side_effect=AssertionError("a pending plan must replace fact collection")
        )
        update_attempts = []

        def update_names(*args, **kwargs):
            desired = args[3]
            update_attempts.append(dict(desired))
            previous = {
                instance_id: member["display_name"]
                for instance_id, member in state.items()
            }
            invoke_update_callback(kwargs, previous)
            if len(update_attempts) == 1:
                state["ocid1.instance.one"]["display_name"] = desired[
                    "ocid1.instance.one"
                ]
                raise RuntimeError("failed after first OCI rename")
            for instance_id, desired_name in desired.items():
                state[instance_id]["display_name"] = desired_name
            return previous

        self.namespace["update_instance_pool_display_names"] = update_names

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory, inventory_members)
            with self.assertRaisesRegex(RuntimeError, "first OCI rename"):
                self.namespace["synchronize_instance_pool_names"](
                    self.compartment_id,
                    self.pool_id,
                    path,
                    self.cluster_name,
                    observed_hostnames_by_instance_id=inventory_members,
                    max_wait_seconds=0,
                )

            plan_path = self.namespace[
                "get_instance_pool_hostname_sync_plan_path"
            ](path)
            self.assertTrue(os.path.isfile(plan_path))
            self.assertEqual(os.stat(plan_path).st_mode & 0o777, 0o600)
            with open(plan_path, encoding="utf-8") as plan_file:
                persisted_plan = json.load(plan_file)
            self.assertEqual(
                persisted_plan["members"]["ocid1.instance.one"][
                    "previous_display_name"
                ],
                "generated-one",
            )
            self.assertEqual(
                state["ocid1.instance.one"]["display_name"], "worker-alpha"
            )
            self.assertEqual(
                state["ocid1.instance.two"]["display_name"], "generated-two"
            )

            synchronized, _ = self.namespace["synchronize_instance_pool_names"](
                self.compartment_id,
                self.pool_id,
                path,
                self.cluster_name,
                max_wait_seconds=0,
            )
            with open(path, encoding="utf-8") as inventory_file:
                rewritten_inventory = inventory_file.read()

            self.assertFalse(os.path.exists(plan_path))

        collector.assert_not_called()
        self.assertEqual(len(update_attempts), 2)
        self.assertEqual(
            {instance["display_name"] for instance in synchronized},
            {"worker-alpha", "worker-beta"},
        )
        self.assertIn("worker-alpha ansible_host=10.0.0.10", rewritten_inventory)
        self.assertIn("worker-beta ansible_host=10.0.0.11", rewritten_inventory)

    def test_changed_membership_against_pending_plan_stops_before_mutation(self):
        current_members = {
            "ocid1.instance.one": observed_names()["ocid1.instance.one"]
        }
        state = {
            "ocid1.instance.one": {
                "display_name": "generated-one",
                "ip": "10.0.0.10",
                "ocid": "ocid1.instance.one",
            }
        }
        self.configure_pool_snapshot(state)
        update_names = self.namespace["update_instance_pool_display_names"] = mock.Mock()
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory, current_members)
            self.namespace["write_instance_pool_hostname_sync_plan"](
                path,
                {
                    "version": 1,
                    "status": "pending",
                    "cluster_name": self.cluster_name,
                    "instance_pool_id": self.pool_id,
                    "members": {
                        instance_id: {
                            **member,
                            "previous_display_name": (
                                "generated-one"
                                if instance_id.endswith("one")
                                else "generated-two"
                            ),
                        }
                        for instance_id, member in observed_names().items()
                    },
                },
            )
            with self.assertRaisesRegex(RuntimeError, "membership changed"):
                self.namespace["synchronize_instance_pool_names"](
                    self.compartment_id,
                    self.pool_id,
                    path,
                    self.cluster_name,
                    max_wait_seconds=0,
                )

        update_names.assert_not_called()
        collector.assert_not_called()

    def test_changed_ip_against_pending_plan_stops_before_mutation(self):
        current_members = {
            "ocid1.instance.one": {
                **observed_names()["ocid1.instance.one"],
                "private_ip": "10.0.0.99",
            }
        }
        state = {
            "ocid1.instance.one": {
                "display_name": "generated-one",
                "ip": "10.0.0.99",
                "ocid": "ocid1.instance.one",
            }
        }
        self.configure_pool_snapshot(state)
        update_names = self.namespace["update_instance_pool_display_names"] = mock.Mock()
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory, current_members)
            original_member = observed_names()["ocid1.instance.one"]
            self.namespace["write_instance_pool_hostname_sync_plan"](
                path,
                {
                    "version": 1,
                    "status": "pending",
                    "cluster_name": self.cluster_name,
                    "instance_pool_id": self.pool_id,
                    "members": {
                        "ocid1.instance.one": {
                            **original_member,
                            "previous_display_name": "generated-one",
                        }
                    },
                },
            )
            with self.assertRaisesRegex(RuntimeError, r"identity|IP"):
                self.namespace["synchronize_instance_pool_names"](
                    self.compartment_id,
                    self.pool_id,
                    path,
                    self.cluster_name,
                    max_wait_seconds=0,
                )

        update_names.assert_not_called()
        collector.assert_not_called()

    def test_hosts_refresh_failure_keeps_plan_until_successful_retry(self):
        inventory_members = observed_names()
        state = {
            "ocid1.instance.one": {
                "display_name": "generated-one",
                "ip": "10.0.0.10",
                "ocid": "ocid1.instance.one",
            },
            "ocid1.instance.two": {
                "display_name": "generated-two",
                "ip": "10.0.0.11",
                "ocid": "ocid1.instance.two",
            },
        }
        self.configure_pool_snapshot(state)

        def update_names(*args, **kwargs):
            desired = args[3]
            previous = {
                instance_id: member["display_name"]
                for instance_id, member in state.items()
            }
            invoke_update_callback(kwargs, previous)
            for instance_id, desired_name in desired.items():
                state[instance_id]["display_name"] = desired_name
            return previous

        self.namespace["update_instance_pool_display_names"] = update_names
        refresh = self.namespace["refresh_instance_pool_hosts"] = mock.Mock(
            side_effect=RuntimeError("unreachable")
        )

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_inventory(directory, inventory_members)
            plan_path = self.namespace[
                "get_instance_pool_hostname_sync_plan_path"
            ](path)
            with self.assertRaisesRegex(RuntimeError, "/etc/hosts refresh"):
                self.namespace["synchronize_instance_pool_names"](
                    self.compartment_id,
                    self.pool_id,
                    path,
                    self.cluster_name,
                    observed_hostnames_by_instance_id=inventory_members,
                    max_wait_seconds=0,
                )
            self.assertTrue(os.path.isfile(plan_path))

            refresh.side_effect = None
            self.namespace["synchronize_instance_pool_names"](
                self.compartment_id,
                self.pool_id,
                path,
                self.cluster_name,
                max_wait_seconds=0,
            )
            self.assertFalse(os.path.exists(plan_path))


class InstancePoolListingTests(unittest.TestCase):
    def test_exact_pool_size_refresh_returns_post_detach_size(self):
        namespace = load_resize_functions()
        get_instance_pool = mock.Mock(
            return_value=SimpleNamespace(
                data=SimpleNamespace(
                    id="ocid1.instancepool.test",
                    compartment_id="ocid1.compartment.test",
                    display_name="batch-1-standard",
                    size=1,
                )
            )
        )
        namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=get_instance_pool
        )

        # The earlier pool summary may still say 2; finalization must use this
        # exact-ID refresh after the pending detach and therefore observe 1.
        self.assertEqual(
            namespace["get_exact_instance_pool_size"](
                "ocid1.instancepool.test",
                "ocid1.compartment.test",
                expected_display_name="batch-1-standard",
            ),
            1,
        )
        get_instance_pool.assert_called_once_with("ocid1.instancepool.test")

    def test_get_instances_uses_full_instance_name_not_stale_pool_summary(self):
        namespace = load_resize_functions()
        summary = SimpleNamespace(id="ocid1.instance.one", display_name="stale-summary")
        full = SimpleNamespace(id=summary.id, display_name="final-os-hostname")
        namespace["computeManagementClient"] = SimpleNamespace(
            list_instance_pool_instances=lambda *args, **kwargs: SimpleNamespace(
                data=[summary]
            )
        )
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id: SimpleNamespace(data=full),
            list_vnic_attachments=lambda **kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        display_name="Primary VNIC attachment",
                        lifecycle_state="ATTACHED",
                        nic_index=0,
                        vnic_id="ocid1.vnic.one",
                    )
                ]
            ),
        )
        namespace["virtualNetworkClient"] = SimpleNamespace(
            get_vnic=lambda vnic_id: SimpleNamespace(
                data=SimpleNamespace(
                    is_primary=True,
                    private_ip="10.0.0.10",
                )
            )
        )
        self.assertEqual(
            namespace["get_instances"](
                "ocid1.compartment.test", "ocid1.instancepool.test", "IP"
            )[0]["display_name"],
            "final-os-hostname",
        )

    def test_complete_pool_accepts_named_primary_vnic_attachment(self):
        namespace = load_resize_functions()
        instance_id = "ocid1.instance.one"
        pool_id = "ocid1.instancepool.test"
        compartment_id = "ocid1.compartment.test"
        full_instance = make_instance(instance_id, "generated-one")
        primary_attachment = SimpleNamespace(
            display_name="Primary VNIC attachment",
            lifecycle_state="ATTACHED",
            nic_index=0,
            vnic_id="ocid1.vnic.primary",
        )
        secondary_attachment = SimpleNamespace(
            display_name="Secondary VNIC attachment",
            lifecycle_state="ATTACHED",
            nic_index=0,
            vnic_id="ocid1.vnic.secondary",
        )
        vnics = {
            primary_attachment.vnic_id: SimpleNamespace(
                is_primary=True,
                private_ip="10.0.0.10",
            ),
            secondary_attachment.vnic_id: SimpleNamespace(
                is_primary=False,
                private_ip="10.0.0.20",
            ),
        }
        namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=lambda *args, **kwargs: SimpleNamespace(
                data=SimpleNamespace(lifecycle_state="RUNNING", size=1)
            ),
            list_instance_pool_instances=lambda *args, **kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id=instance_id,
                        lifecycle_state="ACTIVE",
                        state="RUNNING",
                    )
                ]
            ),
        )
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda *args, **kwargs: SimpleNamespace(
                data=full_instance
            ),
            list_vnic_attachments=lambda *args, **kwargs: SimpleNamespace(
                data=[secondary_attachment, primary_attachment]
            ),
        )
        namespace["virtualNetworkClient"] = SimpleNamespace(
            get_vnic=lambda vnic_id: SimpleNamespace(data=vnics[vnic_id])
        )

        instances, instances_by_id = namespace[
            "get_complete_instance_pool_instances"
        ](
            compartment_id,
            pool_id,
            max_wait_seconds=0,
        )

        self.assertEqual(
            instances,
            [
                {
                    "display_name": "generated-one",
                    "ip": "10.0.0.10",
                    "ocid": instance_id,
                }
            ],
        )
        self.assertEqual(instances_by_id[instance_id], instances[0])

    def test_complete_pool_accepts_instance_summary_state_shape(self):
        namespace = load_resize_functions()
        instance_id = "ocid1.instance.legacy"
        pool_id = "ocid1.instancepool.test"
        compartment_id = "ocid1.compartment.test"
        full_instance = make_instance(instance_id, "generated-legacy")
        namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=lambda *args, **kwargs: SimpleNamespace(
                data=SimpleNamespace(lifecycle_state="RUNNING", size=1)
            ),
            list_instance_pool_instances=lambda *args, **kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id=instance_id,
                        lifecycle_state=None,
                        state="Running",
                    )
                ]
            ),
        )
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda *args, **kwargs: SimpleNamespace(
                data=full_instance
            ),
            list_vnic_attachments=lambda *args, **kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        lifecycle_state="ATTACHED",
                        vnic_id="ocid1.vnic.legacy",
                    )
                ]
            ),
        )
        namespace["virtualNetworkClient"] = SimpleNamespace(
            get_vnic=lambda vnic_id: SimpleNamespace(
                data=SimpleNamespace(
                    is_primary=True,
                    private_ip="10.0.0.10",
                )
            )
        )

        instances, instances_by_id = namespace[
            "get_complete_instance_pool_instances"
        ](
            compartment_id,
            pool_id,
            max_wait_seconds=0,
        )

        self.assertEqual(
            instances,
            [
                {
                    "display_name": "generated-legacy",
                    "ip": "10.0.0.10",
                    "ocid": instance_id,
                }
            ],
        )
        self.assertEqual(instances_by_id[instance_id], instances[0])

    def test_primary_vnic_lookup_rejects_a_lone_explicit_secondary(self):
        namespace = load_resize_functions()
        namespace["computeClient"] = SimpleNamespace(
            list_vnic_attachments=lambda *args, **kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        lifecycle_state="ATTACHED",
                        nic_index=0,
                        vnic_id="ocid1.vnic.secondary",
                    )
                ]
            )
        )
        namespace["virtualNetworkClient"] = SimpleNamespace(
            get_vnic=lambda vnic_id: SimpleNamespace(
                data=SimpleNamespace(
                    is_primary=False,
                    private_ip="10.0.0.20",
                )
            )
        )

        with self.assertRaisesRegex(RuntimeError, "exactly one primary VNIC"):
            namespace["get_instance_primary_private_ip"](
                "ocid1.compartment.test",
                "ocid1.instance.one",
            )

    def test_complete_pool_rejects_transitional_member_even_when_count_matches(self):
        namespace = load_resize_functions()
        namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=mock.Mock(
                return_value=SimpleNamespace(
                    data=SimpleNamespace(lifecycle_state="RUNNING", size=1)
                )
            ),
            list_instance_pool_instances=mock.Mock(
                return_value=SimpleNamespace(
                    data=[
                        SimpleNamespace(
                            id="ocid1.instance.one",
                            lifecycle_state="DETACHING",
                            state="RUNNING",
                        )
                    ]
                )
            ),
        )
        namespace["computeClient"] = SimpleNamespace(
            get_instance=mock.Mock(
                return_value=SimpleNamespace(
                    data=make_instance("ocid1.instance.one", "worker-one")
                )
            )
        )
        namespace["get_instances"] = mock.Mock(
            return_value=[
                {
                    "display_name": "worker-one",
                    "ip": "10.0.0.10",
                    "ocid": "ocid1.instance.one",
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            namespace["get_complete_instance_pool_instances"](
                "ocid1.compartment.test",
                "ocid1.instancepool.test",
                max_wait_seconds=0,
            )


class InstancePoolIdentitySelectionTests(unittest.TestCase):
    def test_hostname_sync_uses_terraform_tracked_pool_ocid(self):
        namespace = load_resize_functions()
        compartment_id = "ocid1.compartment.test"
        pool_id = "ocid1.instancepool.tracked"
        cluster_name = "batch-1-standard"
        tracked_pool = SimpleNamespace(
            id=pool_id,
            compartment_id=compartment_id,
            display_name=cluster_name,
            lifecycle_state="RUNNING",
            size=1,
        )
        get_instance_pool = mock.Mock(
            return_value=SimpleNamespace(data=tracked_pool)
        )
        namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=get_instance_pool
        )
        namespace["get_summary"] = mock.Mock(
            side_effect=AssertionError("display-name lookup must not run")
        )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = os.path.join(directory, "inventory")
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write("[all:vars]\ncluster_network = false\n")
            write_instance_pool_state(directory, pool_id)

            summary, pool_summary, cluster_type = namespace[
                "get_summary_for_operation"
            ](
                compartment_id,
                cluster_name,
                inventory_path,
                {"all:vars": ["cluster_network = false\n"]},
                "sync_instance_pool_names",
                True,
            )

        self.assertIs(summary, tracked_pool)
        self.assertIs(pool_summary, tracked_pool)
        self.assertEqual(cluster_type, "IP")
        get_instance_pool.assert_called_once_with(pool_id)


class InstancePoolPostResizeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()
        self.pool_id = "ocid1.instancepool.test"
        self.compartment_id = "ocid1.compartment.test"
        self.cluster_name = "batch-1-standard"

    def recovery_document(self, action, source_size, target_size):
        return {
            "version": 2,
            "status": "state_only",
            "cluster_name": self.cluster_name,
            "instance_pool_id": self.pool_id,
            "action": action,
            "source_size": source_size,
            "target_size": target_size,
        }

    def configure_live_size(self, size):
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=mock.Mock(
                return_value=SimpleNamespace(
                    data=SimpleNamespace(
                        id=self.pool_id,
                        compartment_id=self.compartment_id,
                        display_name=self.cluster_name,
                        size=size,
                    )
                )
            )
        )

    def test_partial_remove_size_reconciles_exact_live_state(self):
        self.configure_live_size(3)
        update_state = self.namespace["updateTFState"] = mock.Mock()

        recovered_size = self.namespace[
            "reconcile_instance_pool_post_resize_state"
        ](
            "/tmp/inventory",
            self.compartment_id,
            self.recovery_document("remove", 5, 2),
        )

        self.assertEqual(recovered_size, 3)
        update_state.assert_called_once_with(
            "/tmp/inventory",
            self.cluster_name,
            3,
        )

    def test_unexpected_intermediate_add_size_fails_closed(self):
        self.configure_live_size(3)
        update_state = self.namespace["updateTFState"] = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "resize boundary"):
            self.namespace["reconcile_instance_pool_post_resize_state"](
                "/tmp/inventory",
                self.compartment_id,
                self.recovery_document("add", 2, 4),
            )

        update_state.assert_not_called()

    def test_recovery_marker_rejects_reversed_action_boundary(self):
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            self.namespace["validate_instance_pool_post_resize_recovery"](
                self.recovery_document("remove", 2, 3)
            )

    def test_state_only_marker_round_trips_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = os.path.join(directory, "inventory")
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(make_inventory_text())
            self.namespace["write_instance_pool_post_resize_recovery"](
                inventory_path,
                self.cluster_name,
                self.pool_id,
                status="state_only",
                action="add",
                source_size=2,
                target_size=4,
            )

            marker = self.namespace["load_instance_pool_post_resize_recovery"](
                inventory_path
            )
            marker_path = self.namespace[
                "get_instance_pool_post_resize_recovery_path"
            ](inventory_path)
            marker_mode = os.stat(marker_path).st_mode & 0o777

        self.assertEqual(marker["status"], "state_only")
        self.assertEqual(marker["target_size"], 4)
        self.assertEqual(marker_mode, 0o600)


class InstancePoolRemovalResolutionTests(unittest.TestCase):
    def test_inventory_alias_resolves_ocid_after_oci_display_name_changed(self):
        namespace = load_resize_functions()
        renamed_instance = make_instance(
            "ocid1.instance.one", "final-os-hostname"
        )
        get_instance = mock.Mock(
            return_value=SimpleNamespace(data=renamed_instance)
        )
        list_instances = mock.Mock(
            side_effect=AssertionError("display-name lookup must not be used")
        )
        namespace["computeClient"] = SimpleNamespace(
            get_instance=get_instance,
            list_instances=list_instances,
        )
        inventory = {
            "compute_configured": [
                "pre-rename-alias ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n"
            ],
            "compute_to_add": [],
            "compute_to_destroy": [],
        }

        resolved = namespace["find_cluster_instance_by_name"](
            "ocid1.compartment.test",
            "pre-rename-alias",
            "batch-1-standard",
            inventory_for_resolution=inventory,
        )

        self.assertIs(resolved, renamed_instance)
        get_instance.assert_called_once_with("ocid1.instance.one")
        list_instances.assert_not_called()

    def test_removal_plan_rejects_unrelated_missing_pool_member(self):
        namespace = load_resize_functions()
        inventory = {
            "compute_configured": [
                "worker-a ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.a\n"
            ],
            "compute_to_add": [],
        }
        current_instances = [
            {"display_name": "worker-a", "ocid": "ocid1.instance.a"},
            {"display_name": "worker-b", "ocid": "ocid1.instance.b"},
        ]

        with self.assertRaisesRegex(RuntimeError, "reconfigure"):
            namespace["validate_instance_pool_removal_inventory_plan"](
                inventory,
                current_instances,
                ["worker-a"],
            )

    def test_removal_plan_allows_selected_stale_inventory_cleanup(self):
        namespace = load_resize_functions()
        inventory = {
            "compute_configured": [
                "worker-a ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.a\n",
                "stale-worker ansible_host=10.0.0.12 "
                "oci_instance_id=ocid1.instance.stale\n",
            ],
            "compute_to_add": [],
        }
        current_instances = [
            {"display_name": "worker-a", "ocid": "ocid1.instance.a"}
        ]

        namespace["validate_instance_pool_removal_inventory_plan"](
            inventory,
            current_instances,
            ["stale-worker"],
        )


class PendingLocalBlockVolumeReplayTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()
        self.records = [
            {
                "instance_id": "ocid1.instance.detached",
                "instance_pool_id": "ocid1.instancepool.test",
                "instance_display_name": "worker-detached",
                "volume_id": "ocid1.volume.detached",
            },
            {
                "instance_id": "ocid1.instance.active",
                "instance_pool_id": "ocid1.instancepool.test",
                "instance_display_name": "worker-active",
                "volume_id": "ocid1.volume.active",
            },
        ]
        self.namespace["load_pending_local_block_volume_deletions"] = mock.Mock(
            return_value=self.records
        )

    def test_mixed_detached_and_authorized_active_records_replay_together(self):
        self.namespace["inspect_pending_local_block_volume_deletion"] = mock.Mock(
            side_effect=[("TERMINATED", None), ("RUNNING", object())]
        )
        self.namespace["instance_is_pool_member"] = mock.Mock(return_value=True)
        complete = self.namespace[
            "complete_pending_local_block_volume_deletion"
        ] = mock.Mock()

        ready = self.namespace["retry_pending_local_block_volume_deletions"](
            "/tmp/inventory",
            "batch-1-standard",
            "ocid1.compartment.test",
            resume_pool_member_instance_ids={"ocid1.instance.active"},
        )

        self.assertEqual(ready, self.records)
        self.assertFalse(complete.call_args_list[0].kwargs["resume_pool_member"])
        self.assertTrue(complete.call_args_list[1].kwargs["resume_pool_member"])

    def test_unrequested_active_record_blocks_before_any_completion(self):
        self.namespace["inspect_pending_local_block_volume_deletion"] = mock.Mock(
            side_effect=[("RUNNING", None), ("RUNNING", None)]
        )
        self.namespace["instance_is_pool_member"] = mock.Mock(
            side_effect=[False, True]
        )
        complete = self.namespace[
            "complete_pending_local_block_volume_deletion"
        ] = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "worker-active"):
            self.namespace["retry_pending_local_block_volume_deletions"](
                "/tmp/inventory",
                "batch-1-standard",
                "ocid1.compartment.test",
                resume_pool_member_instance_ids=set(),
            )

        complete.assert_not_called()

    def test_generic_replay_passes_exact_authorized_ocid(self):
        retry = self.namespace[
            "retry_pending_local_block_volume_deletions"
        ] = mock.Mock(return_value=self.records)

        self.namespace[
            "process_pending_local_block_volume_deletions_for_operation"
        ](
            "/tmp/inventory",
            "batch-1-standard",
            "ocid1.compartment.test",
            "remove",
            ["worker-active"],
            authorized_instance_ids={"ocid1.instance.active"},
            expected_instance_pool_id="ocid1.instancepool.test",
        )

        self.assertEqual(
            retry.call_args.kwargs["resume_pool_member_instance_ids"],
            {"ocid1.instance.active"},
        )
        self.assertFalse(retry.call_args.kwargs["resume_pool_members"])
        self.assertEqual(
            retry.call_args.kwargs["expected_instance_pool_id"],
            "ocid1.instancepool.test",
        )

    def test_duplicate_display_names_are_rejected_before_any_completion(self):
        self.records[0]["instance_display_name"] = "same-name"
        self.records[1]["instance_display_name"] = "same-name"
        inspect = self.namespace[
            "inspect_pending_local_block_volume_deletion"
        ] = mock.Mock()
        complete = self.namespace[
            "complete_pending_local_block_volume_deletion"
        ] = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "duplicate instance names"):
            self.namespace["retry_pending_local_block_volume_deletions"](
                "/tmp/inventory",
                "batch-1-standard",
                "ocid1.compartment.test",
                resume_pool_member_instance_ids={"ocid1.instance.active"},
                expected_instance_pool_id="ocid1.instancepool.test",
            )

        inspect.assert_not_called()
        complete.assert_not_called()

    def test_foreign_pool_record_is_rejected_before_any_completion(self):
        self.records[1]["instance_pool_id"] = "ocid1.instancepool.foreign"
        inspect = self.namespace[
            "inspect_pending_local_block_volume_deletion"
        ] = mock.Mock(return_value=("TERMINATED", None))
        complete = self.namespace[
            "complete_pending_local_block_volume_deletion"
        ] = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "different instance pool"):
            self.namespace["retry_pending_local_block_volume_deletions"](
                "/tmp/inventory",
                "batch-1-standard",
                "ocid1.compartment.test",
                resume_pool_members=True,
                expected_instance_pool_id="ocid1.instancepool.test",
            )

        inspect.assert_not_called()
        complete.assert_not_called()


class InstancePoolNodeRemovalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()
        self.cluster_name = "batch-1-standard"
        self.compartment_id = "ocid1.compartment.test"
        self.pool_id = "ocid1.instancepool.test"

    def removal_record(self, instance_id, name, private_ip):
        return {
            "cluster_name": self.cluster_name,
            "compartment_id": self.compartment_id,
            "instance_pool_id": self.pool_id,
            "instance_id": instance_id,
            "instance_display_name": name,
            "instance_names": [name],
            "private_ip": private_ip,
            "zone_name": self.cluster_name+".local",
            "dns_domains": [],
        }

    def configure_common_clients(self, instances):
        instances_by_id = {instance.id: instance for instance in instances}
        pool = SimpleNamespace(
            id=self.pool_id,
            display_name=self.cluster_name,
            compartment_id=self.compartment_id,
            lifecycle_state="RUNNING",
            size=0,
        )
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=mock.Mock(return_value=SimpleNamespace(data=pool))
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=mock.Mock(
                side_effect=lambda instance_id: SimpleNamespace(
                    data=instances_by_id[instance_id]
                )
            )
        )
        self.namespace["get_tracked_instance_pool_id"] = mock.Mock(
            return_value=self.pool_id
        )
        self.namespace["instance_is_pool_member"] = mock.Mock(return_value=False)
        self.namespace["load_pending_local_block_volume_deletions"] = mock.Mock(
            return_value=[]
        )
        self.namespace["delete_pending_instance_pool_node_dns_records"] = mock.Mock()

    def test_state_failure_keeps_completed_node_removal_journal_for_retry(self):
        instance = make_instance(
            "ocid1.instance.one",
            "final-worker",
            lifecycle_state="TERMINATED",
        )
        self.configure_common_clients([instance])
        update_state = self.namespace["updateTFState"] = mock.Mock(
            side_effect=RuntimeError("state push failed")
        )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = os.path.join(directory, "inventory")
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(make_inventory_text())
            self.namespace["write_pending_instance_pool_node_removals"](
                inventory_path,
                [self.removal_record(instance.id, instance.display_name, "10.0.0.10")],
            )
            journal_path = self.namespace[
                "get_pending_instance_pool_node_removals_path"
            ](inventory_path)

            with self.assertRaisesRegex(RuntimeError, "state push failed"):
                self.namespace["resume_pending_instance_pool_node_removals"](
                    inventory_path,
                    self.cluster_name,
                    self.compartment_id,
                    self.pool_id,
                    "remove",
                )
            self.assertTrue(os.path.isfile(journal_path))

            update_state.side_effect = None
            self.namespace["resume_pending_instance_pool_node_removals"](
                inventory_path,
                self.cluster_name,
                self.compartment_id,
                self.pool_id,
                "remove",
            )
            self.assertFalse(os.path.exists(journal_path))

    def test_every_node_is_preflighted_before_first_termination(self):
        first = make_instance("ocid1.instance.one", "worker-one")
        second = make_instance("ocid1.instance.two", "worker-two")
        self.configure_common_clients([first, second])
        self.namespace["get_instance_primary_private_ip"] = mock.Mock(
            side_effect=lambda compartment_id, instance_id: (
                "10.0.0.10" if instance_id == first.id else "10.0.0.99"
            )
        )
        remove_member = self.namespace[
            "remove_instance_pool_member_and_managed_local_block_volume"
        ] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = os.path.join(directory, "inventory")
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(make_inventory_text())
            self.namespace["write_pending_instance_pool_node_removals"](
                inventory_path,
                [
                    self.removal_record(first.id, first.display_name, "10.0.0.10"),
                    self.removal_record(second.id, second.display_name, "10.0.0.11"),
                ],
            )

            with self.assertRaisesRegex(RuntimeError, "changed private IP"):
                self.namespace["resume_pending_instance_pool_node_removals"](
                    inventory_path,
                    self.cluster_name,
                    self.compartment_id,
                    self.pool_id,
                    "cleanup_compute_cluster",
                )

        remove_member.assert_not_called()

    def test_multi_node_removal_plan_freezes_every_exact_ocid(self):
        first = make_instance("ocid1.instance.one", "worker-one")
        second = make_instance("ocid1.instance.two", "worker-two")
        instances = {first.id: first, second.id: second}
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=mock.Mock(
                side_effect=lambda instance_id: SimpleNamespace(
                    data=instances[instance_id]
                )
            )
        )
        self.namespace["get_instance_primary_private_ip"] = mock.Mock(
            side_effect=lambda compartment_id, instance_id: (
                "10.0.0.10" if instance_id == first.id else "10.0.0.11"
            )
        )
        inventory = {
            "compute_configured": [
                "worker-one ansible_host=10.0.0.10 "
                "oci_instance_id=ocid1.instance.one\n",
                "worker-two ansible_host=10.0.0.11 "
                "oci_instance_id=ocid1.instance.two\n",
            ],
            "compute_to_add": [],
            "all:vars": [
                "cluster_name=batch-1-standard\n",
                "zone_name=batch-1-standard.local\n",
                "dns_entries=false\n",
            ],
        }

        records = self.namespace["build_instance_pool_removal_journal_plan"](
            inventory,
            self.compartment_id,
            self.pool_id,
            [
                {"ocid": first.id, "display_name": first.display_name, "ip": "10.0.0.10"},
                {"ocid": second.id, "display_name": second.display_name, "ip": "10.0.0.11"},
            ],
            [first.display_name, second.display_name],
            {first.id, second.id},
        )

        self.assertEqual(
            {record["instance_id"] for record in records},
            {first.id, second.id},
        )
        self.assertEqual(
            {record["private_ip"] for record in records},
            {"10.0.0.10", "10.0.0.11"},
        )


class InstancePoolNameWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.locals = read_repository_file("autoscaling", "tf_init", "locals.tf")
        cls.controller_update = read_repository_file(
            "autoscaling", "tf_init", "controller_update.tf"
        )
        cls.network = read_repository_file("autoscaling", "tf_init", "network.tf")
        cls.inventory_template = read_repository_file(
            "autoscaling", "tf_init", "inventory.tpl"
        )
        cls.configure_autoscaling = read_repository_file("bin", "configure_as.sh")
        cls.create_cluster = read_repository_file("bin", "create_cluster.sh")
        cls.delete_cluster = read_repository_file("bin", "delete_cluster.sh")
        cls.autoscale_slurm = read_repository_file(
            "autoscaling", "crontab", "autoscale_slurm.sh"
        )
        cls.resize_shell = read_repository_file("bin", "resize.sh")
        cls.resize = read_repository_file("bin", "resize.py")

    def test_production_hostname_sync_calls_use_exact_cluster_identity_arguments(self):
        source = read_repository_file("bin", "resize.py")
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "synchronize_instance_pool_names"
            )
        ]

        self.assertGreaterEqual(len(calls), 1)
        for call in calls:
            self.assertEqual(len(call.args), 4)
            first, second, third, fourth = call.args
            self.assertIn(first.id, ["comp_ocid", "compartment_id"])
            self.assertIn(third.id, ["inventory", "inventory_path"])
            self.assertIn(fourth.id, ["cluster_name", "expected_cluster_name"])
            self.assertIn(
                getattr(second, "id", getattr(second, "attr", None)),
                ["current_instance_pool_id", "instance_pool_id", "id"],
            )

    def test_terraform_does_not_predict_final_os_hostname(self):
        combined = self.locals+self.controller_update+self.inventory_template
        self.assertNotIn("instance_pool_hostnames", combined)
        self.assertNotIn("cluster_inventory_names", combined)
        self.assertNotIn("desired_hostname=", combined)
        self.assertNotIn("instance_keyword=", self.inventory_template)

    def test_initial_sync_runs_only_after_successful_ansible_configuration(self):
        prepare = self.configure_autoscaling.index("prepare_local_block_volume")
        playbook = self.configure_autoscaling.index(
            "ansible-playbook $playbooks_path/new_nodes.yml"
        )
        sync = self.configure_autoscaling.index(
            "synchronize_managed_pool_names_and_monitoring", playbook
        )
        sync_helper = self.configure_autoscaling.split(
            "synchronize_managed_pool_names_and_monitoring()", 1
        )[1].split("\n}\n", 1)[0]
        self.assertLess(prepare, playbook)
        self.assertLess(playbook, sync)
        self.assertLess(
            sync_helper.index("sync_instance_pool_names"),
            sync_helper.index("--reconcile-monitoring"),
        )

    def test_initial_sync_is_gated_to_managed_pool_before_oci_lookup(self):
        sync = self.configure_autoscaling.index("sync_instance_pool_names")
        gating = self.configure_autoscaling[:sync]
        self.assertIn("cluster_network", gating)
        self.assertIn('variable "compute_cluster"', gating)
        self.assertIn("is_autoscaling_managed_pool_deployment", gating)

    def test_initial_retry_resumes_pending_plan_before_ansible(self):
        pending = self.configure_autoscaling.index(
            ".instance-pool-hostname-sync.json"
        )
        resume = self.configure_autoscaling.index(
            "synchronize_managed_pool_names_and_monitoring", pending
        )
        prepare = self.configure_autoscaling.index("prepare_local_block_volume")
        playbook = self.configure_autoscaling.index(
            "ansible-playbook $playbooks_path/new_nodes.yml"
        )
        self.assertLess(pending, resume)
        self.assertLess(resume, prepare)
        self.assertLess(resume, playbook)

    def test_initial_sync_waits_for_terraform_dns_records(self):
        configure_resource = self.controller_update.split(
            'resource "null_resource" "configure"', 1
        )[1]
        self.assertIn(
            "oci_dns_rrset.rrset-cluster-network-OCI",
            configure_resource,
        )
        self.assertIn(
            "oci_dns_rrset.rrset-cluster-network-SLURM",
            configure_resource,
        )

    def test_instance_pool_canonical_dns_is_not_dual_owned_by_terraform(self):
        oci_rrset = self.network.split(
            'resource "oci_dns_rrset" "rrset-cluster-network-OCI"', 1
        )[1].split(
            'resource "oci_dns_rrset" "rrset-cluster-network-SLURM"', 1
        )[0]
        self.assertIn(
            "var.dns_entries && var.compute_cluster",
            oci_rrset,
        )

    def test_runtime_sync_uses_post_ansible_facts_and_not_ip_arithmetic(self):
        sync_body = self.resize.split("def synchronize_instance_pool_names", 1)[1].split(
            "def ", 1
        )[0]
        self.assertIn("collect_instance_pool_os_hostnames", sync_body)
        self.assertIn("observed_hostnames_by_instance_id", sync_body)
        self.assertNotIn("instance_keyword", sync_body)
        self.assertNotIn("desired_instance_pool_hostname", self.resize)

    def test_reconfigure_paths_sync_after_ansible_succeeds(self):
        add = self.resize.split("def add_reconfigure", 1)[1].split("def reconfigure", 1)[0]
        reconfigure = self.resize.split("def reconfigure", 1)[1].split("def getreachable", 1)[0]
        for body in (add, reconfigure):
            self.assertIn("synchronize_autoscaling_managed_pool_names", body)
            self.assertLess(body.index("update_cluster("), body.index("synchronize_autoscaling_managed_pool_names"))
            self.assertRegex(
                body,
                r"(?s)if update_flag\s*==\s*0:.*?synchronize_autoscaling_managed_pool_names",
            )

    def test_cli_is_gated_to_autoscaling_managed_pools_only(self):
        cli = self.resize.split("if args.mode == 'sync_instance_pool_names':", 1)[1].split(
            "if CN != \"CC\"", 1
        )[0]
        self.assertIn('if CN not in ["IP", "CN"] or not autoscaling:', cli)
        self.assertIn("synchronize_autoscaling_managed_pool_names(", cli)

    def test_name_sync_updates_primary_vnic_display_name_only(self):
        update_function = next(
            node
            for node in ast.parse(self.resize).body
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "update_instance_pool_display_names"
            )
        )
        update_vnic_call = next(
            node
            for node in ast.walk(update_function)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update_vnic"
            )
        )
        update_details_call = update_vnic_call.args[1]
        self.assertIsInstance(update_details_call, ast.Call)
        self.assertEqual(update_details_call.func.attr, "UpdateVnicDetails")
        self.assertEqual(
            [keyword.arg for keyword in update_details_call.keywords],
            ["display_name"],
        )
        self.assertNotIn(
            "opc_retry_token",
            [keyword.arg for keyword in update_vnic_call.keywords],
        )

    def test_inventory_refresh_runs_after_name_rewrite(self):
        sync_body = self.resize.split("def synchronize_instance_pool_names", 1)[1].split(
            "\ndef prepare_local_block_volume_inventory", 1
        )[0]
        self.assertIn("rewrite_instance_pool_inventory_names", sync_body)
        self.assertIn("refresh_instance_pool_hosts", sync_body)
        self.assertLess(
            sync_body.index("rewrite_instance_pool_inventory_names"),
            sync_body.index("refresh_instance_pool_hosts"),
        )
        refresh_playbook = read_repository_file(
            "playbooks", "refresh_instance_pool_hosts.yml"
        )
        self.assertIn("- hosts: controller", refresh_playbook)
        self.assertIn("gather_facts: false", refresh_playbook)
        self.assertNotIn("controller,slurm_backup,login,compute", refresh_playbook)
        self.assertIn("delegate_to: 127.0.0.1", refresh_playbook)
        self.assertIn("groups.get('compute', []) | length == 0", refresh_playbook)
        self.assertIn("state: absent", refresh_playbook)
        refresh_template = read_repository_file(
            "playbooks", "templates", "instance-pool-hosts.j2"
        )
        self.assertIn("{{ item }}.local.vcn {{ item }}", refresh_template)

    def test_managed_pool_dns_cleanup_blocks_terraform_destroy_on_failure(self):
        cleanup_guard = self.delete_cluster.index(
            "Managed pool DNS cleanup failed; Terraform destroy was not started"
        )
        terraform_destroy = self.delete_cluster.index(
            "terraform destroy -auto-approve -parallelism 1"
        )
        self.assertLess(cleanup_guard, terraform_destroy)

    def test_monitoring_rows_are_reconciled_by_ocid(self):
        function = self.resize_shell.split(
            "reconcile_managed_pool_monitoring()", 1
        )[1].split("\n}\n", 1)[0]
        self.assertIn("list --monitoring-output", function)
        self.assertIn("EXPECTED_SIZE", function)
        self.assertRegex(function, r"UPDATE cluster_log\.nodes SET .*?WHERE node_OCID='\$\{ocid\}'")
        self.assertNotIn("INSERT IGNORE", function)
        self.assertNotRegex(function, r"\|\s*grep\s+inst\b")

    def test_monitoring_checks_existing_ocid_before_claiming_placeholder(self):
        function = self.resize_shell.split(
            "reconcile_managed_pool_monitoring()", 1
        )[1].split("\n}\n", 1)[0]
        guard = "SET @oci_hpc_node_exists = (SELECT COUNT(*) FROM cluster_log.nodes WHERE node_OCID='${ocid}');"
        claim = (
            "WHERE cluster_id='$cluster_id' AND node_OCID IS NULL AND "
            "state='provisioning' AND @oci_hpc_node_exists=0"
        )
        self.assertIn(guard, function)
        self.assertIn(claim, function)
        self.assertLess(function.index(guard), function.index(claim))
        self.assertEqual(
            function.count(
                "state='provisioning' AND hostname='${hostname}' AND "
                "@oci_hpc_node_exists=0"
            ),
            1,
        )
        self.assertEqual(
            function.count(
                "state='provisioning' AND @oci_hpc_node_exists=0"
            ),
            1,
        )

    def test_monitoring_releases_names_before_atomic_reassignment(self):
        function = self.resize_shell.split(
            "reconcile_managed_pool_monitoring()", 1
        )[1].split("\n}\n", 1)[0]
        release = 'UPDATE cluster_log.nodes SET hostname=NULL WHERE node_OCID='
        assign = "UPDATE cluster_log.nodes SET cluster_id='$cluster_id',hostname='${hostname}'"
        self.assertIn("START TRANSACTION", function)
        self.assertIn("COMMIT", function)
        self.assertLess(function.index(release), function.index(assign))

    def test_monitoring_failure_does_not_turn_completed_resize_into_retry(self):
        success_branch = self.resize_shell.split(
            "if [ $status -eq 0 ]", 1
        )[1].split("else\n    echo \"Could not resize cluster", 1)[0]
        monitoring_failure = success_branch.split(
            'if ! reconcile_managed_pool_monitoring "$cluster_name"', 1
        )[1].split("fi", 1)[0]
        self.assertNotIn("status=1", monitoring_failure)
        self.assertIn("--reconcile-monitoring", monitoring_failure)

    def test_quiet_remove_does_not_enter_confirmation_prompt(self):
        prompt_condition = self.resize_shell.split(
            'if { [ "$resize_type" = "remove" ]', 1
        )[1].split("then", 1)[0]
        self.assertIn('&& [ "$quietMode" = "False" ]', prompt_condition)

    def test_pre_add_guard_requires_exact_inventory_pool_ocid_set(self):
        add_body = self.resize.split("if args.mode == 'add':", 1)[1]
        pool_ids = "previous_instance_ids = {instance['ocid'] for instance in cn_instances}"
        inventory_ids = (
            "inventory_instance_ids = get_compute_inventory_instance_ids("
        )
        exact_guard = "if inventory_instance_ids != previous_instance_ids:"
        pool_mutation = "update_instance_pool_and_wait_for_state("
        for fragment in (pool_ids, inventory_ids, exact_guard, pool_mutation):
            self.assertIn(fragment, add_body)
        self.assertLess(add_body.index(pool_ids), add_body.index(exact_guard))
        self.assertLess(add_body.index(inventory_ids), add_body.index(exact_guard))
        self.assertLess(add_body.index(exact_guard), add_body.index(pool_mutation))

    def test_mutating_modes_resume_pending_hostname_plan_first(self):
        resume_start = self.resize.index("# A failed synchronization")
        resume_end = self.resize.index(
            "if args.mode == 'sync_instance_pool_names':", resume_start
        )
        resume_block = self.resize[resume_start:resume_end]
        for mode in ("add", "remove", "remove_unreachable", "reconfigure"):
            self.assertIn('"'+mode+'"', resume_block)
        self.assertIn("load_instance_pool_hostname_sync_plan(inventory)", resume_block)
        self.assertIn("synchronize_instance_pool_names(", resume_block)
        resume_call = self.resize.index(
            "synchronize_instance_pool_names(", resume_start, resume_end
        )
        for normal_operation in (
            "args.mode == 'reconfigure'",
            "args.mode == 'remove_unreachable'",
            "args.mode == 'remove'",
            "args.mode == 'add'",
        ):
            self.assertGreater(
                self.resize.index(normal_operation, resume_end),
                resume_call,
            )

    def test_post_resize_recovery_boundary_precedes_durable_followup_work(self):
        removal_body = self.resize.split(
            "hostnames_to_remove_len=len(hostnames_to_remove)", 1
        )[1].split("if args.mode == 'add':", 1)[0]
        self.assertLess(
            removal_body.index("write_instance_pool_post_resize_recovery("),
            removal_body.index("destroy_unreachable_reconfigure("),
        )
        self.assertLess(
            removal_body.index("write_pending_instance_pool_node_removals("),
            removal_body.index("destroy_unreachable_reconfigure("),
        )
        self.assertLess(
            removal_body.index("write_pending_instance_pool_node_removals("),
            removal_body.index(
                "remove_instance_pool_member_and_managed_local_block_volume("
            ),
        )
        add_body = self.resize.split("if args.mode == 'add':", 1)[1]
        self.assertLess(
            add_body.rindex("write_instance_pool_post_resize_recovery("),
            add_body.rindex("update_instance_pool_and_wait_for_state("),
        )
        self.assertLess(
            add_body.rindex("write_instance_pool_post_resize_recovery("),
            add_body.rindex("add_reconfigure("),
        )
        self.assertIn('"state_only" if no_reconfigure', removal_body)
        self.assertIn('"state_only"', add_body)

    def test_post_resize_recovery_updates_state_before_ansible_or_marker_clear(self):
        recovery = self.resize.split(
            "pending_post_resize_recovery is not None", 2
        )[2].split("if completed_pending_node_removals_to_report:", 1)[0]
        reconcile = recovery.index("reconcile_instance_pool_post_resize_state(")
        clear = recovery.index("clear_instance_pool_post_resize_recovery(")
        ansible = recovery.index("reconfigure(")
        self.assertLess(reconcile, clear)
        self.assertLess(reconcile, ansible)

    def test_resize_uses_unambiguous_private_dns_zone_before_mutation(self):
        resize_body = self.resize.split("else:\n    wait_for_running_status", 1)[1]
        zone_lookup = resize_body.index("get_single_private_dns_zone_id(")
        pool_update = resize_body.index("update_instance_pool_and_wait_for_state(")
        self.assertLess(zone_lookup, pool_update)

    def test_pending_node_removal_retry_cannot_expand_selection(self):
        selection_body = self.resize.split(
            "for line in inventory_dict['compute_to_add']:", 1
        )[1].split("hostnames_to_remove_len=len(hostnames_to_remove)", 1)[0]
        committed_branch = selection_body.split(
            "if resuming_pending_node_removals:", 1
        )[1].split("elif args.mode == 'remove_unreachable':", 1)[0]
        self.assertIn("hostnames_to_remove = list(hostnames)", committed_branch)
        self.assertNotIn("getreachable", committed_branch)
        count_branch = selection_body.split("if args.mode == 'remove'", 1)[1]
        self.assertIn("not resuming_pending_node_removals", count_branch.split(":", 1)[0])

    def test_pending_node_removal_retry_reuses_frozen_records(self):
        removal_body = self.resize.split(
            "hostnames_to_remove_len=len(hostnames_to_remove)", 1
        )[1].split("if args.mode == 'add':", 1)[0]
        resume_branch = removal_body.split(
            "if resuming_pending_node_removals:", 1
        )[1].split("else:", 1)[0]
        fresh_branch = removal_body.split(
            "if resuming_pending_node_removals:", 1
        )[1].split("else:", 1)[1].split("pool_members_to_remove_len", 1)[0]
        self.assertIn("resuming_pending_node_removal_records", resume_branch)
        self.assertNotIn("validate_instance_pool_removal_inventory_plan", resume_branch)
        self.assertNotIn("build_instance_pool_removal_journal_plan", resume_branch)
        self.assertIn("validate_instance_pool_removal_inventory_plan", fresh_branch)
        marker_condition = removal_body.split(
            "write_instance_pool_post_resize_recovery(", 1
        )[0].rsplit("if (", 1)[1]
        self.assertIn("not resuming_pending_node_removals", marker_condition)

    def test_pending_volume_retry_refreshes_exact_pool_size_before_finalization(self):
        processing = self.resize.rindex(
            "ready_pending_deletions = process_pending_local_block_volume_deletions_for_operation("
        )
        refresh = self.resize.index(
            "current_size = get_exact_instance_pool_size(", processing
        )
        finalization = self.resize.index(
            "finalize_pending_local_block_volume_deletions(", refresh
        )
        refresh_block = self.resize[refresh:finalization]
        self.assertLess(processing, refresh)
        self.assertLess(refresh, finalization)
        self.assertIn("ipa_ocid", refresh_block)
        self.assertIn(
            'expected_display_name=cluster_name',
            refresh_block,
        )

    def test_new_instance_pool_creation_reconciles_after_cluster_is_running(self):
        self.assertIn("--reconcile-monitoring", self.create_cluster)
        state_running = self.create_cluster.index("state='running'")
        reconcile = self.create_cluster.index("--reconcile-monitoring", state_running)
        self.assertLess(state_running, reconcile)

    def test_autoscaler_uses_refreshed_fourth_hosts_alias_for_resize(self):
        function_match = re.search(
            r"(?ms)^def getResizeNodeName\(node,hosts_entry\):.*?(?=^def )",
            self.autoscale_slurm,
        )
        self.assertIsNotNone(function_match)
        namespace = {}
        exec(function_match.group(0), namespace)
        resolver = namespace["getResizeNodeName"]
        self.assertEqual(
            resolver(
                "final-worker-a",
                "10.0.0.10 final-worker-a final-worker-a.local final-worker-a\n",
            ),
            "final-worker-a",
        )
        self.assertEqual(self.autoscale_slurm.count("+['--quiet']"), 3)

    def test_inventory_host_matching_is_exact(self):
        namespace = load_resize_functions()
        line = (
            "node-40 ansible_host=10.0.0.40 "
            "oci_instance_id=ocid1.instance.forty\n"
        )
        self.assertFalse(namespace["inventory_host_line_matches"](line, "node-4"))
        self.assertTrue(namespace["inventory_host_line_matches"](line, "node-40"))


if __name__ == "__main__":
    unittest.main()
