import importlib.util
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_POOL_TEST_PATH = os.path.join(
    REPOSITORY_ROOT,
    "tests",
    "test_resize_instance_pool_hostname_sync.py",
)


def load_instance_pool_test_support():
    spec = importlib.util.spec_from_file_location(
        "resize_instance_pool_hostname_test_support",
        INSTANCE_POOL_TEST_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SUPPORT = load_instance_pool_test_support()
load_resize_functions = SUPPORT.load_resize_functions
make_instance = SUPPORT.make_instance


COMPARTMENT_ID = "ocid1.compartment.test"
CLUSTER_NAME = "batch-1-hpc"
CLUSTER_NETWORK_ID = "ocid1.clusternetwork.tracked"
INSTANCE_POOL_ID = "ocid1.instancepool.embedded"
INSTANCE_ID_ONE = "ocid1.instance.one"
INSTANCE_ID_TWO = "ocid1.instance.two"
DNS_ZONE_ID = "ocid1.dns-zone.test"


def response(data, headers=None):
    return SimpleNamespace(data=data, headers=headers or {})


def write_cluster_network_state(
    directory,
    cluster_network_ids=(CLUSTER_NETWORK_ID,),
    instance_pool_id=INSTANCE_POOL_ID,
    dns_rrsets=None,
    slurm_rrsets=None,
):
    resources = []
    for cluster_network_id in cluster_network_ids:
        resources.append({
            "mode": "managed",
            "type": "oci_core_cluster_network",
            "name": "cluster_network",
            "instances": [{
                "attributes": {
                    "id": cluster_network_id,
                    "instance_pools": [{"id": instance_pool_id}],
                }
            }],
        })
    # Same-type resources with a different Terraform resource name must never
    # become candidates for this autoscaling cluster.
    resources.append({
        "mode": "managed",
        "type": "oci_core_cluster_network",
        "name": "unrelated",
        "instances": [{"attributes": {"id": "ocid1.clusternetwork.other"}}],
    })
    if dns_rrsets is not None:
        resources.append({
            "mode": "managed",
            "type": "oci_dns_rrset",
            "name": "rrset-cluster-network-OCI",
            "instances": [
                {
                    "index_key": rrset["index_key"],
                    "schema_version": 0,
                    "attributes": {
                        "zone_name_or_id": rrset.get(
                            "zone_id",
                            DNS_ZONE_ID,
                        ),
                        "domain": rrset["domain"],
                        "rtype": "A",
                        "scope": "PRIVATE",
                        "items": [{
                            "domain": rrset["domain"],
                            "rdata": rrset["private_ip"],
                            "rtype": "A",
                            "ttl": 3600,
                        }],
                    },
                }
                for rrset in dns_rrsets
            ],
        })
    if slurm_rrsets is not None:
        resources.append({
            "mode": "managed",
            "type": "oci_dns_rrset",
            "name": "rrset-cluster-network-SLURM",
            "instances": [
                {
                    "index_key": rrset["index_key"],
                    "schema_version": 0,
                    "attributes": {
                        "zone_name_or_id": rrset.get(
                            "zone_id",
                            DNS_ZONE_ID,
                        ),
                        "domain": rrset["domain"],
                        "rtype": "A",
                        "scope": "PRIVATE",
                        "items": [{
                            "domain": rrset["domain"],
                            "rdata": rrset["private_ip"],
                            "rtype": "A",
                            "ttl": 3600,
                        }],
                    },
                }
                for rrset in slurm_rrsets
            ],
        })
    with open(
        os.path.join(directory, "terraform.tfstate"),
        "w",
        encoding="utf-8",
    ) as state_file:
        json.dump({"resources": resources}, state_file)


def write_cluster_network_inventory(
    directory,
    hostname="generated-one",
    second_hostname=None,
):
    inventory_path = os.path.join(directory, "inventory")
    compute_lines = (
        hostname+" ansible_host=10.0.0.10 ansible_user=opc role=compute "
        "oci_instance_id="+INSTANCE_ID_ONE+"\n"
    )
    if second_hostname is not None:
        compute_lines += (
            second_hostname+
            " ansible_host=10.0.0.11 ansible_user=opc role=compute "
            "oci_instance_id="+INSTANCE_ID_TWO+"\n"
        )
    with open(inventory_path, "w", encoding="utf-8") as inventory_file:
        inventory_file.write(
            "[controller]\n"
            "controller ansible_host=10.0.0.2\n"
            "[slurm_backup]\n"
            "[login]\n"
            "[compute_to_add]\n"
            "[compute_configured]\n"
            +compute_lines+
            "[compute_to_destroy]\n"
            "[nfs]\n"
            +hostname+" ansible_user=opc role=nfs\n"
            "[all:vars]\n"
            "cluster_name="+CLUSTER_NAME+"\n"
            "cluster_network=true\n"
            "queue=batch\n"
            "instance_type=HPC_instance\n"
            "private_subnet=10.0.0.0/24\n"
            "zone_name="+CLUSTER_NAME+".local\n"
            "dns_entries=false\n"
            "slurm=true\n"
        )
    return inventory_path


def cluster_network(pool=None, **overrides):
    values = {
        "id": CLUSTER_NETWORK_ID,
        "compartment_id": COMPARTMENT_ID,
        "display_name": CLUSTER_NAME,
        "lifecycle_state": "RUNNING",
        "instance_pools": [pool or instance_pool()],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def instance_pool(**overrides):
    values = {
        "id": INSTANCE_POOL_ID,
        "compartment_id": COMPARTMENT_ID,
        "display_name": CLUSTER_NAME,
        "lifecycle_state": "RUNNING",
        "size": 1,
        "current_size": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def member_summary(instance_id=INSTANCE_ID_ONE, display_name="generated-one"):
    # OCI's Cluster Network list API returns core InstanceSummary. Its member
    # state field is `state`, not `lifecycle_state`, and real responses use
    # title-case values such as `Running`.
    return SimpleNamespace(
        id=instance_id,
        display_name=display_name,
        compartment_id=COMPARTMENT_ID,
        state="Running",
    )


def configure_primary_vnics(namespace, instances_by_id):
    primary_vnics = {}
    secondary_vnics = {}
    for index, instance_id in enumerate(instances_by_id, start=10):
        primary_vnics[instance_id] = SimpleNamespace(
            id="ocid1.vnic.primary."+str(index),
            compartment_id=COMPARTMENT_ID,
            lifecycle_state="AVAILABLE",
            is_primary=True,
            display_name=instances_by_id[instance_id].display_name,
            hostname_label="immutable-label-"+str(index),
            private_ip="10.0.0."+str(index),
        )
        secondary_vnics[instance_id] = SimpleNamespace(
            id="ocid1.vnic.rdma-or-secondary."+str(index),
            compartment_id=COMPARTMENT_ID,
            lifecycle_state="AVAILABLE",
            is_primary=False,
            display_name="rdma-or-secondary-"+str(index),
            hostname_label="secondary-label-"+str(index),
            private_ip="10.1.0."+str(index),
        )

    def list_vnic_attachments(*args, **kwargs):
        instance_id = kwargs["instance_id"]
        return response([
            SimpleNamespace(
                vnic_id=secondary_vnics[instance_id].id,
                lifecycle_state="ATTACHED",
            ),
            SimpleNamespace(
                vnic_id=primary_vnics[instance_id].id,
                lifecycle_state="ATTACHED",
            ),
        ])

    def get_vnic(vnic_id, **kwargs):
        for vnic_by_instance in (primary_vnics, secondary_vnics):
            for vnic in vnic_by_instance.values():
                if vnic.id == vnic_id:
                    return response(vnic, {"etag": "etag-"+vnic_id})
        raise AssertionError("unexpected VNIC "+vnic_id)

    namespace["computeClient"].list_vnic_attachments = list_vnic_attachments
    namespace["virtualNetworkClient"] = SimpleNamespace(get_vnic=get_vnic)
    return primary_vnics, secondary_vnics


class ClusterNetworkTerraformIdentityTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def test_reads_only_the_exact_managed_cluster_network_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = write_cluster_network_inventory(directory)
            self.assertEqual(
                self.namespace["get_tracked_cluster_network_id"](inventory_path),
                CLUSTER_NETWORK_ID,
            )

    def test_rejects_multiple_tracked_cluster_network_ocids(self):
        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(
                directory,
                cluster_network_ids=(
                    CLUSTER_NETWORK_ID,
                    "ocid1.clusternetwork.duplicate",
                ),
            )
            inventory_path = write_cluster_network_inventory(directory)
            with self.assertRaisesRegex(RuntimeError, "multiple|Multiple"):
                self.namespace["get_tracked_cluster_network_id"](inventory_path)

    def test_partial_destroy_state_keeps_parent_but_not_syncable_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory, instance_pool_id=None)
            inventory_path = write_cluster_network_inventory(directory)

            self.assertEqual(
                self.namespace["get_tracked_cluster_network_id"](inventory_path),
                CLUSTER_NETWORK_ID,
            )
            self.assertIsNone(
                self.namespace["get_tracked_managed_instance_pool_id"](
                    inventory_path
                )
            )
            with self.assertRaisesRegex(RuntimeError, "complete Cluster Network"):
                self.namespace[
                    "get_tracked_cluster_network_for_hostname_sync"
                ](
                    inventory_path,
                    COMPARTMENT_ID,
                    CLUSTER_NAME,
                )

    def test_rejects_cn_mixed_with_compute_cluster_child_state(self):
        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            state_path = os.path.join(directory, "terraform.tfstate")
            with open(state_path, encoding="utf-8") as state_file:
                state = json.load(state_file)
            state["resources"].append({
                "mode": "managed",
                "type": "oci_core_instance",
                "name": "compute_cluster_instances",
                "instances": [{
                    "attributes": {"id": "ocid1.instance.compute-cluster"}
                }],
            })
            with open(state_path, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file)
            inventory_path = write_cluster_network_inventory(directory)
            inventory = self.namespace["parse_inventory"](inventory_path)

            calls = [
                lambda: self.namespace["get_tracked_managed_instance_pool_id"](
                    inventory_path
                ),
                lambda: self.namespace[
                    "synchronize_autoscaling_compute_names"
                ](
                    COMPARTMENT_ID,
                    inventory_path,
                    CLUSTER_NAME,
                ),
                lambda: self.namespace["get_summary_for_operation"](
                    COMPARTMENT_ID,
                    CLUSTER_NAME,
                    inventory_path,
                    inventory,
                    "add",
                    True,
                ),
            ]
            for call in calls:
                with self.subTest(call=call):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "multiple compute deployment types",
                    ):
                        call()

    def test_ignores_same_named_resources_in_child_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            state_path = os.path.join(directory, "terraform.tfstate")
            with open(state_path, encoding="utf-8") as state_file:
                state = json.load(state_file)
            state["resources"].extend([
                {
                    "module": "module.unrelated",
                    "mode": "managed",
                    "type": "oci_core_cluster_network",
                    "name": "cluster_network",
                    "instances": [{
                        "attributes": {
                            "id": "ocid1.clusternetwork.module",
                            "instance_pools": [{"id": "ocid1.pool.module"}],
                        }
                    }],
                },
                {
                    "module": "module.unrelated",
                    "mode": "managed",
                    "type": "oci_core_instance",
                    "name": "compute_cluster_instances",
                    "instances": [{
                        "attributes": {"id": "ocid1.instance.module"}
                    }],
                },
            ])
            with open(state_path, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file)
            inventory_path = write_cluster_network_inventory(directory)

            self.assertEqual(
                self.namespace["get_tracked_managed_instance_pool_id"](
                    inventory_path
                ),
                INSTANCE_POOL_ID,
            )

    def test_resolver_uses_tracked_ocid_and_one_embedded_pool(self):
        tracked_pool = instance_pool()
        tracked_network = cluster_network(tracked_pool)
        get_cluster_network = mock.Mock(return_value=response(tracked_network))
        list_cluster_networks = mock.Mock(
            side_effect=AssertionError("display-name lookup must not run")
        )
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_cluster_network=get_cluster_network,
            get_instance_pool=mock.Mock(return_value=response(tracked_pool)),
            list_cluster_networks=list_cluster_networks,
        )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = write_cluster_network_inventory(directory)
            resolved_network, resolved_pool = self.namespace[
                "get_tracked_cluster_network_for_hostname_sync"
            ](inventory_path, COMPARTMENT_ID, CLUSTER_NAME)

        self.assertIs(resolved_network, tracked_network)
        self.assertIs(resolved_pool, tracked_pool)
        get_cluster_network.assert_called_once_with(
            CLUSTER_NETWORK_ID,
            **self.namespace["get_oci_retry_kwargs"](),
        )
        list_cluster_networks.assert_not_called()

    def test_resolver_rejects_non_running_or_ambiguous_parent(self):
        cases = [
            cluster_network(lifecycle_state="SCALING"),
            cluster_network(instance_pools=[]),
            cluster_network(instance_pools=[instance_pool(), instance_pool(id="pool-two")]),
            cluster_network(compartment_id="ocid1.compartment.other"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = write_cluster_network_inventory(directory)
            for invalid_network in cases:
                with self.subTest(invalid_network=invalid_network.__dict__):
                    self.namespace["computeManagementClient"] = SimpleNamespace(
                        get_cluster_network=lambda *args, value=invalid_network, **kwargs: response(value),
                        get_instance_pool=lambda *args, **kwargs: response(instance_pool()),
                    )
                    with self.assertRaises(RuntimeError):
                        self.namespace[
                            "get_tracked_cluster_network_for_hostname_sync"
                        ](inventory_path, COMPARTMENT_ID, CLUSTER_NAME)

    def test_resolver_rejects_live_embedded_pool_that_differs_from_state(self):
        live_pool = instance_pool(id="ocid1.instancepool.different")
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_cluster_network=lambda *args, **kwargs: response(
                cluster_network(live_pool)
            ),
            get_instance_pool=lambda *args, **kwargs: response(live_pool),
        )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = write_cluster_network_inventory(directory)
            with self.assertRaisesRegex(RuntimeError, "Instance Pool|expected"):
                self.namespace[
                    "get_tracked_cluster_network_for_hostname_sync"
                ](inventory_path, COMPARTMENT_ID, CLUSTER_NAME)

    def test_sync_waits_for_transiently_scaling_tracked_parent(self):
        tracked_pool = instance_pool()
        tracked_network = cluster_network(
            tracked_pool,
            lifecycle_state="SCALING",
        )
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_cluster_network=lambda *args, **kwargs: response(
                tracked_network
            )
        )
        synchronize = self.namespace[
            "synchronize_instance_pool_names"
        ] = mock.Mock(return_value=([], {}))

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = write_cluster_network_inventory(directory)
            result = self.namespace[
                "synchronize_autoscaling_compute_names"
            ](
                COMPARTMENT_ID,
                inventory_path,
                CLUSTER_NAME,
                expected_instance_pool_id=INSTANCE_POOL_ID,
                expected_cluster_network_id=CLUSTER_NETWORK_ID,
            )

        self.assertEqual(result, ([], {}))
        self.assertEqual(
            synchronize.call_args.kwargs["cluster_network_id"],
            CLUSTER_NETWORK_ID,
        )


class ClusterNetworkMembershipSafetyTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def configure_complete_membership(self, summaries):
        instances = {
            summary.id: make_instance(
                summary.id,
                summary.display_name,
                cluster_name=CLUSTER_NAME,
                compartment_id=COMPARTMENT_ID,
            )
            for summary in summaries
        }
        pool = instance_pool(size=len(summaries), current_size=len(summaries))
        network = cluster_network(pool)
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_cluster_network=lambda *args, **kwargs: response(network),
            get_instance_pool=lambda *args, **kwargs: response(pool),
            list_cluster_network_instances=lambda *args, **kwargs: response(summaries),
            list_instance_pool_instances=lambda *args, **kwargs: response(summaries),
            get_instance_pool_instance=lambda *args, **kwargs: response(
                SimpleNamespace(state="Running")
            ),
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id, **kwargs: response(instances[instance_id])
        )
        primary_vnics, secondary_vnics = configure_primary_vnics(
            self.namespace,
            instances,
        )
        return instances, primary_vnics, secondary_vnics

    def test_complete_snapshot_accepts_instance_summary_state_running(self):
        summaries = [member_summary()]
        self.configure_complete_membership(summaries)

        instances, instances_by_id = self.namespace[
            "get_complete_cluster_network_instances"
        ](
            COMPARTMENT_ID,
            CLUSTER_NETWORK_ID,
            INSTANCE_POOL_ID,
            CLUSTER_NAME,
            max_wait_seconds=0,
        )

        self.assertEqual(set(instances_by_id), {INSTANCE_ID_ONE})
        self.assertEqual(instances_by_id[INSTANCE_ID_ONE]["ip"], "10.0.0.10")
        self.assertEqual(instances, list(instances_by_id.values()))

    def test_cluster_listing_uses_full_instance_name_and_explicit_primary_vnic(self):
        summary = member_summary(display_name="stale-cluster-summary")
        full_instance = make_instance(
            INSTANCE_ID_ONE,
            "final-os-hostname",
            cluster_name=CLUSTER_NAME,
            compartment_id=COMPARTMENT_ID,
        )
        primary = SimpleNamespace(
            id="ocid1.vnic.primary",
            is_primary=True,
            private_ip="10.0.0.10",
        )
        secondary = SimpleNamespace(
            id="ocid1.vnic.rdma",
            is_primary=False,
            private_ip="10.1.0.10",
        )
        self.namespace["computeManagementClient"] = SimpleNamespace(
            list_cluster_network_instances=lambda *args, **kwargs: response(
                [summary]
            )
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda *args, **kwargs: response(full_instance),
            list_vnic_attachments=lambda *args, **kwargs: response([
                SimpleNamespace(
                    display_name="named-rdma-attachment",
                    lifecycle_state="ATTACHED",
                    vnic_id=secondary.id,
                ),
                SimpleNamespace(
                    display_name="named-primary-attachment",
                    lifecycle_state="ATTACHED",
                    vnic_id=primary.id,
                ),
            ]),
        )
        self.namespace["virtualNetworkClient"] = SimpleNamespace(
            get_vnic=lambda vnic_id, **kwargs: response(
                primary if vnic_id == primary.id else secondary
            )
        )

        listed = self.namespace["get_instances"](
            COMPARTMENT_ID,
            CLUSTER_NETWORK_ID,
            "CN",
        )

        self.assertEqual(listed, [{
            "display_name": "final-os-hostname",
            "ip": "10.0.0.10",
            "ocid": INSTANCE_ID_ONE,
        }])

    def test_parent_and_embedded_pool_membership_mismatch_is_rejected_before_update(self):
        one = member_summary()
        two = member_summary(INSTANCE_ID_TWO, "generated-two")
        instances, _, _ = self.configure_complete_membership([one, two])
        self.namespace[
            "computeManagementClient"
        ].list_instance_pool_instances = lambda *args, **kwargs: response([one])
        instance_updates = mock.Mock()
        vnic_updates = mock.Mock()
        self.namespace["computeClient"].update_instance = instance_updates
        self.namespace["virtualNetworkClient"].update_vnic = vnic_updates

        with self.assertRaisesRegex(RuntimeError, "membership|member|incomplete"):
            self.namespace["update_instance_pool_display_names"](
                COMPARTMENT_ID,
                INSTANCE_POOL_ID,
                CLUSTER_NAME,
                {
                    INSTANCE_ID_ONE: "worker-one",
                    INSTANCE_ID_TWO: "worker-two",
                },
                max_wait_seconds=0,
                cluster_network_id=CLUSTER_NETWORK_ID,
            )

        instance_updates.assert_not_called()
        vnic_updates.assert_not_called()

    def test_cluster_network_updates_only_instance_and_explicit_primary_vnic_names(self):
        summaries = [member_summary()]
        instances, primary_vnics, secondary_vnics = self.configure_complete_membership(
            summaries
        )
        primary = primary_vnics[INSTANCE_ID_ONE]
        secondary = secondary_vnics[INSTANCE_ID_ONE]
        original_primary_label = primary.hostname_label
        original_secondary = dict(secondary.__dict__)
        instance_update_calls = []
        vnic_update_calls = []

        def update_instance(instance_id, details, **kwargs):
            instance_update_calls.append((instance_id, details, kwargs))
            instances[instance_id].display_name = details.display_name
            return response(instances[instance_id])

        def update_vnic(vnic_id, details, **kwargs):
            vnic_update_calls.append((vnic_id, details, kwargs))
            self.assertEqual(vnic_id, primary.id)
            primary.display_name = details.display_name
            return response(primary)

        self.namespace["computeClient"].update_instance = update_instance
        self.namespace["virtualNetworkClient"].update_vnic = update_vnic

        self.namespace["update_instance_pool_display_names"](
            COMPARTMENT_ID,
            INSTANCE_POOL_ID,
            CLUSTER_NAME,
            {INSTANCE_ID_ONE: "worker-one"},
            max_wait_seconds=0,
            cluster_network_id=CLUSTER_NETWORK_ID,
            expected_private_ips_by_instance_id={INSTANCE_ID_ONE: "10.0.0.10"},
        )

        self.assertEqual(instances[INSTANCE_ID_ONE].display_name, "worker-one")
        self.assertEqual(primary.display_name, "worker-one")
        self.assertEqual(primary.hostname_label, original_primary_label)
        self.assertEqual(secondary.__dict__, original_secondary)
        self.assertEqual(len(instance_update_calls), 1)
        self.assertEqual(len(vnic_update_calls), 1)
        self.assertFalse(hasattr(vnic_update_calls[0][1], "hostname_label"))

    def test_membership_change_after_updates_is_rejected_before_caller_commit(self):
        summaries = [member_summary()]
        instances, primary_vnics, _ = self.configure_complete_membership(
            summaries
        )
        primary = primary_vnics[INSTANCE_ID_ONE]
        original_complete_snapshot = self.namespace[
            "get_complete_cluster_network_instances"
        ]
        snapshot_calls = []

        def complete_snapshot(*args, **kwargs):
            snapshot_calls.append(True)
            members, members_by_id = original_complete_snapshot(*args, **kwargs)
            if len(snapshot_calls) == 3:
                unexpected_member = {
                    "display_name": "external-scale-member",
                    "ip": "10.0.0.99",
                    "ocid": "ocid1.instance.external",
                }
                members = members+[unexpected_member]
                members_by_id = dict(members_by_id)
                members_by_id[unexpected_member["ocid"]] = unexpected_member
            return members, members_by_id

        self.namespace[
            "get_complete_cluster_network_instances"
        ] = complete_snapshot

        def update_instance(instance_id, details, **kwargs):
            instances[instance_id].display_name = details.display_name
            return response(instances[instance_id])

        def update_vnic(vnic_id, details, **kwargs):
            primary.display_name = details.display_name
            return response(primary)

        self.namespace["computeClient"].update_instance = update_instance
        self.namespace["virtualNetworkClient"].update_vnic = update_vnic

        with self.assertRaisesRegex(RuntimeError, "membership changed"):
            self.namespace["update_instance_pool_display_names"](
                COMPARTMENT_ID,
                INSTANCE_POOL_ID,
                CLUSTER_NAME,
                {INSTANCE_ID_ONE: "worker-one"},
                max_wait_seconds=0,
                cluster_network_id=CLUSTER_NETWORK_ID,
                expected_private_ips_by_instance_id={
                    INSTANCE_ID_ONE: "10.0.0.10"
                },
            )

        self.assertEqual(len(snapshot_calls), 3)

    def test_no_member_is_updated_when_later_primary_vnic_preflight_fails(self):
        one = member_summary()
        two = member_summary(INSTANCE_ID_TWO, "generated-two")
        instances, primary_vnics, _ = self.configure_complete_membership([one, two])
        primary_vnics[INSTANCE_ID_TWO].is_primary = False
        instance_updates = mock.Mock()
        vnic_updates = mock.Mock()
        self.namespace["computeClient"].update_instance = instance_updates
        self.namespace["virtualNetworkClient"].update_vnic = vnic_updates

        with self.assertRaisesRegex(RuntimeError, "primary VNIC"):
            self.namespace["update_instance_pool_display_names"](
                COMPARTMENT_ID,
                INSTANCE_POOL_ID,
                CLUSTER_NAME,
                {
                    INSTANCE_ID_ONE: "worker-one",
                    INSTANCE_ID_TWO: "worker-two",
                },
                max_wait_seconds=0,
                cluster_network_id=CLUSTER_NETWORK_ID,
            )

        instance_updates.assert_not_called()
        vnic_updates.assert_not_called()


class ClusterNetworkDnsMigrationSafetyTests(unittest.TestCase):
    LEGACY_CN_DNS_EXPRESSION = (
        "var.dns_entries && (var.cluster_network || var.compute_cluster) ? "
        "toset([for v in range(var.node_count) : tostring(v)]) : []"
    )
    def setUp(self):
        self.namespace = load_resize_functions()

    def write_legacy_cn_files(self, directory, dns_rrsets=None):
        if dns_rrsets is None:
            dns_rrsets = [{
                "index_key": "0",
                "domain": "generated-one."+CLUSTER_NAME+".local",
                "private_ip": "10.0.0.10",
            }]
        write_cluster_network_state(
            directory,
            dns_rrsets=dns_rrsets,
        )
        inventory_path = write_cluster_network_inventory(directory)
        with open(inventory_path, encoding="utf-8") as inventory_file:
            inventory = inventory_file.read().replace(
                "dns_entries=false",
                "dns_entries=true",
            )
        with open(inventory_path, "w", encoding="utf-8") as inventory_file:
            inventory_file.write(inventory)
        with open(
            os.path.join(directory, "variables.tf"),
            "w",
            encoding="utf-8",
        ) as variables_file:
            variables_file.write(
                'variable "compute_cluster" { default = false }\n'
            )
        with open(
            os.path.join(directory, "network.tf"),
            "w",
            encoding="utf-8",
        ) as network_file:
            network_file.write(
                'resource "oci_dns_rrset" "rrset-cluster-network-OCI" {\n'
                "  for_each = "+self.LEGACY_CN_DNS_EXPRESSION+"\n"
                "}\n"
                'resource "next" "sentinel" {}\n'
            )
        with open(
            os.path.join(
                directory,
                self.namespace["INSTANCE_POOL_DNS_OWNERSHIP_MARKER_FILENAME"],
            ),
            "w",
            encoding="utf-8",
        ) as marker_file:
            marker_file.write("python-owned-canonical-dns-v1\n")
        return inventory_path

    def configure_migration_dns(self):
        self.namespace["get_single_private_dns_zone_id"] = mock.Mock(
            return_value=DNS_ZONE_ID
        )
        self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock(
            return_value={"10.0.0.10"}
        )

    def test_existing_cn_migration_orders_ledger_state_rm_network_v2_marker(self):
        events = []
        self.configure_migration_dns()
        self.namespace["load_instance_pool_name_dns_ownership"] = mock.Mock(
            return_value=None
        )

        def write_ledger(inventory_path, ownership):
            self.assertEqual(ownership["cluster_name"], CLUSTER_NAME)
            self.assertEqual(ownership["instance_pool_id"], INSTANCE_POOL_ID)
            self.assertEqual(
                ownership["rrsets"][0]["domain"],
                "generated-one."+CLUSTER_NAME+".local",
            )
            events.append("ledger")

        def run_terraform(command, **kwargs):
            if command[:3] == ["terraform", "state", "list"]:
                events.append("state-list")
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        'oci_dns_rrset.rrset-cluster-network-OCI["0"]\n'
                    ),
                    stderr="",
                )
            self.assertEqual(command[:3], ["terraform", "state", "rm"])
            events.append("state-rm")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def write_network(path, contents):
            self.assertRegex(
                contents,
                r"(?m)^\s*for_each\s*=\s*toset\(\[\]\)\s*$",
            )
            self.assertNotIn(self.LEGACY_CN_DNS_EXPRESSION, contents)
            self.assertNotIn("var.compute_cluster ?", contents)
            events.append("network-v2")

        self.namespace["write_instance_pool_name_dns_ownership"] = write_ledger
        self.namespace["write_text_atomic_preserving_metadata"] = write_network
        self.namespace["fsync_directory"] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_legacy_cn_files(directory)
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=run_terraform,
            ), mock.patch.object(
                self.namespace["os"],
                "replace",
                side_effect=lambda *args: events.append("marker-v2"),
            ):
                changed = self.namespace[
                    "migrate_instance_pool_oci_dns_ownership"
                ](
                    inventory_path,
                    compartment_id=COMPARTMENT_ID,
                    instance_pool_id=INSTANCE_POOL_ID,
                    instances_by_id={
                        INSTANCE_ID_ONE: {
                            "display_name": "generated-one",
                            "ip": "10.0.0.10",
                        }
                    },
                )

        self.assertTrue(changed)
        self.assertLess(events.index("ledger"), events.index("state-rm"))
        self.assertLess(events.index("state-rm"), events.index("network-v2"))
        self.assertLess(events.index("network-v2"), events.index("marker-v2"))

    def test_cn_migration_fails_fast_when_terraform_state_is_locked(self):
        self.configure_migration_dns()
        state_address = (
            'oci_dns_rrset.rrset-cluster-network-OCI["0"]'
        )
        commands = []

        def run_terraform(command, **kwargs):
            commands.append(command)
            self.assertEqual(command[:3], ["terraform", "state", "list"])
            return SimpleNamespace(
                returncode=0,
                stdout=state_address+"\n",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_legacy_cn_files(directory)
            with open(
                os.path.join(directory, ".terraform.tfstate.lock.info"),
                "w",
                encoding="utf-8",
            ) as lock_file:
                lock_file.write("{}\n")
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=run_terraform,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Terraform state is locked.*reconfigure",
                ):
                    self.namespace[
                        "migrate_instance_pool_oci_dns_ownership"
                    ](
                        inventory_path,
                        compartment_id=COMPARTMENT_ID,
                        instance_pool_id=INSTANCE_POOL_ID,
                        instances_by_id={
                            INSTANCE_ID_ONE: {
                                "display_name": "generated-one",
                                "ip": "10.0.0.10",
                            }
                        },
                    )
            ownership = self.namespace[
                "load_instance_pool_name_dns_ownership"
            ](inventory_path)
            with open(
                os.path.join(directory, "network.tf"),
                encoding="utf-8",
            ) as network_file:
                network_after_failure = network_file.read()

        # The checked-in state JSON is sufficient to discover that this
        # legacy address still needs ownership transfer.  Refuse the lock
        # before spawning any Terraform subprocess.
        self.assertEqual(commands, [])
        self.assertIsNone(ownership)
        self.assertIn(self.LEGACY_CN_DNS_EXPRESSION, network_after_failure)

    def test_cn_migration_ledgers_every_state_rrset_including_stale_members(self):
        self.configure_migration_dns()
        ownership_writes = []
        state_rm_commands = []
        live_domain = "generated-one."+CLUSTER_NAME+".local"
        stale_domain = "retired-worker."+CLUSTER_NAME+".local"
        state_addresses = [
            'oci_dns_rrset.rrset-cluster-network-OCI["0"]',
            'oci_dns_rrset.rrset-cluster-network-OCI["retired"]',
        ]

        def write_ownership(inventory_path, ownership):
            ownership_writes.append(json.loads(json.dumps(ownership)))

        def run_terraform(command, **kwargs):
            if command[:3] == ["terraform", "state", "list"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="\n".join(state_addresses)+"\n",
                    stderr="",
                )
            self.assertEqual(command[:3], ["terraform", "state", "rm"])
            state_rm_commands.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.namespace[
            "write_instance_pool_name_dns_ownership"
        ] = write_ownership
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_legacy_cn_files(
                directory,
                dns_rrsets=[
                    {
                        "index_key": "0",
                        "domain": live_domain,
                        "private_ip": "10.0.0.10",
                    },
                    {
                        "index_key": "retired",
                        "domain": stale_domain,
                        "private_ip": "10.0.0.99",
                    },
                ],
            )
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=run_terraform,
            ):
                changed = self.namespace[
                    "migrate_instance_pool_oci_dns_ownership"
                ](
                    inventory_path,
                    compartment_id=COMPARTMENT_ID,
                    instance_pool_id=INSTANCE_POOL_ID,
                    instances_by_id={
                        INSTANCE_ID_ONE: {
                            "display_name": "generated-one",
                            "ip": "10.0.0.10",
                        }
                    },
                )

        self.assertTrue(changed)
        self.assertEqual(len(ownership_writes), 2)
        pending_ownership = ownership_writes[0]
        self.assertIs(pending_ownership["terraform_state_released"], False)
        ledger_records = {
            (rrset["domain"], tuple(rrset["private_ips"]))
            for rrset in pending_ownership["rrsets"]
        }
        self.assertEqual(
            ledger_records,
            {
                (live_domain, ("10.0.0.10",)),
                (stale_domain, ("10.0.0.99",)),
            },
        )
        self.assertIn(
            (live_domain, ("10.0.0.10",)),
            ledger_records,
        )
        self.assertEqual(
            state_rm_commands,
            [[
                "terraform",
                "state",
                "rm",
                "-lock-timeout=60s",
            ]+state_addresses],
        )
        self.assertEqual(
            self.namespace[
                "verify_private_dns_a_rrset_ownership"
            ].call_args_list,
            [
                mock.call(DNS_ZONE_ID, live_domain, ["10.0.0.10"]),
                mock.call(DNS_ZONE_ID, stale_domain, ["10.0.0.99"]),
            ],
        )

    def test_cn_migration_adopts_live_resize_member_absent_from_state(self):
        ownership_writes = []
        state_address = (
            'oci_dns_rrset.rrset-cluster-network-OCI["0"]'
        )
        first_domain = "generated-one."+CLUSTER_NAME+".local"
        added_domain = "generated-two."+CLUSTER_NAME+".local"
        self.namespace["get_single_private_dns_zone_id"] = mock.Mock(
            return_value=DNS_ZONE_ID
        )
        self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock(
            side_effect=lambda zone_id, domain, private_ips: set(private_ips)
        )
        self.namespace[
            "write_instance_pool_name_dns_ownership"
        ] = lambda inventory_path, ownership: ownership_writes.append(
            json.loads(json.dumps(ownership))
        )

        def run_terraform(command, **kwargs):
            if command[:3] == ["terraform", "state", "list"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=state_address+"\n",
                    stderr="",
                )
            self.assertEqual(command[:3], ["terraform", "state", "rm"])
            self.assertEqual(command[4:], [state_address])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_legacy_cn_files(
                directory,
                dns_rrsets=[{
                    "index_key": "0",
                    "domain": first_domain,
                    "private_ip": "10.0.0.10",
                }],
            )
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=run_terraform,
            ):
                changed = self.namespace[
                    "migrate_instance_pool_oci_dns_ownership"
                ](
                    inventory_path,
                    compartment_id=COMPARTMENT_ID,
                    instance_pool_id=INSTANCE_POOL_ID,
                    instances_by_id={
                        INSTANCE_ID_ONE: {
                            "display_name": "generated-one",
                            "ip": "10.0.0.10",
                        },
                        INSTANCE_ID_TWO: {
                            "display_name": "generated-two",
                            "ip": "10.0.0.11",
                        },
                    },
                )

        self.assertTrue(changed)
        pending_ownership = ownership_writes[0]
        self.assertEqual(
            {
                (rrset["domain"], tuple(rrset["private_ips"]))
                for rrset in pending_ownership["rrsets"]
            },
            {
                (first_domain, ("10.0.0.10",)),
                (added_domain, ("10.0.0.11",)),
            },
        )
        self.namespace[
            "verify_private_dns_a_rrset_ownership"
        ].assert_any_call(
            DNS_ZONE_ID,
            added_domain,
            {"10.0.0.11"},
        )

    def test_cn_migration_refuses_state_list_json_address_mismatch(self):
        self.configure_migration_dns()
        live_rrset = {
            "index_key": "0",
            "domain": "generated-one."+CLUSTER_NAME+".local",
            "private_ip": "10.0.0.10",
        }
        stale_rrset = {
            "index_key": "retired",
            "domain": "retired-worker."+CLUSTER_NAME+".local",
            "private_ip": "10.0.0.99",
        }
        live_address = (
            'oci_dns_rrset.rrset-cluster-network-OCI["0"]'
        )
        stale_address = (
            'oci_dns_rrset.rrset-cluster-network-OCI["retired"]'
        )
        cases = [
            (
                "state-list-address-missing-from-json",
                [live_rrset],
                [live_address, stale_address],
            ),
            (
                "json-address-missing-from-state-list",
                [live_rrset, stale_rrset],
                [live_address],
            ),
        ]
        for label, dns_rrsets, listed_addresses in cases:
            with self.subTest(label=label):
                commands = []

                def run_terraform(command, **kwargs):
                    commands.append(command)
                    self.assertEqual(
                        command[:3],
                        ["terraform", "state", "list"],
                    )
                    return SimpleNamespace(
                        returncode=0,
                        stdout="\n".join(listed_addresses)+"\n",
                        stderr="",
                    )

                with tempfile.TemporaryDirectory() as directory:
                    inventory_path = self.write_legacy_cn_files(
                        directory,
                        dns_rrsets=dns_rrsets,
                    )
                    with mock.patch.object(
                        self.namespace["subprocess"],
                        "run",
                        side_effect=run_terraform,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "state addresses.*recovered exactly",
                        ):
                            self.namespace[
                                "migrate_instance_pool_oci_dns_ownership"
                            ](
                                inventory_path,
                                compartment_id=COMPARTMENT_ID,
                                instance_pool_id=INSTANCE_POOL_ID,
                                instances_by_id={
                                    INSTANCE_ID_ONE: {
                                        "display_name": "generated-one",
                                        "ip": "10.0.0.10",
                                    }
                                },
                            )
                    ownership_path = self.namespace[
                        "get_instance_pool_name_dns_ownership_path"
                    ](inventory_path)
                    v2_marker = os.path.join(
                        directory,
                        self.namespace[
                            "MANAGED_POOL_DNS_OWNERSHIP_MARKER_FILENAME"
                        ],
                    )
                    with open(
                        os.path.join(directory, "network.tf"),
                        encoding="utf-8",
                    ) as network_file:
                        network = network_file.read()
                    self.assertFalse(os.path.exists(ownership_path))
                    self.assertFalse(os.path.exists(v2_marker))

                self.assertEqual(len(commands), 1)
                self.assertIn(self.LEGACY_CN_DNS_EXPRESSION, network)

    def test_failed_cn_state_release_leaves_ledger_and_v1_configuration(self):
        self.configure_migration_dns()

        def run_terraform(command, **kwargs):
            if command[:3] == ["terraform", "state", "list"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        'oci_dns_rrset.rrset-cluster-network-OCI["0"]\n'
                    ),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="simulated state rm failure",
            )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = self.write_legacy_cn_files(directory)
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=run_terraform,
            ):
                with self.assertRaisesRegex(RuntimeError, "state|ownership"):
                    self.namespace["migrate_instance_pool_oci_dns_ownership"](
                        inventory_path,
                        compartment_id=COMPARTMENT_ID,
                        instance_pool_id=INSTANCE_POOL_ID,
                        instances_by_id={
                            INSTANCE_ID_ONE: {
                                "display_name": "generated-one",
                                "ip": "10.0.0.10",
                            }
                        },
                    )
            ownership = self.namespace[
                "load_instance_pool_name_dns_ownership"
            ](inventory_path)
            with open(
                os.path.join(directory, "network.tf"),
                encoding="utf-8",
            ) as network_file:
                network = network_file.read()
            v2_marker = os.path.join(
                directory,
                self.namespace["MANAGED_POOL_DNS_OWNERSHIP_MARKER_FILENAME"],
            )

        self.assertEqual(ownership["instance_pool_id"], INSTANCE_POOL_ID)
        self.assertIn(self.LEGACY_CN_DNS_EXPRESSION, network)
        self.assertFalse(os.path.exists(v2_marker))

    def test_cleanup_does_not_delete_cn_rrset_when_state_release_failed(self):
        self.configure_migration_dns()

        def failed_release(command, **kwargs):
            if command[:3] == ["terraform", "state", "list"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        'oci_dns_rrset.rrset-cluster-network-OCI["0"]\n'
                    ),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="simulated state rm failure",
            )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = self.write_legacy_cn_files(directory)
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=failed_release,
            ):
                with self.assertRaisesRegex(RuntimeError, "state|ownership"):
                    self.namespace["migrate_instance_pool_oci_dns_ownership"](
                        inventory_path,
                        compartment_id=COMPARTMENT_ID,
                        instance_pool_id=INSTANCE_POOL_ID,
                        instances_by_id={
                            INSTANCE_ID_ONE: {
                                "display_name": "generated-one",
                                "ip": "10.0.0.10",
                            }
                        },
                    )

            delete_rrset = self.namespace[
                "delete_private_dns_a_rrset_if_owned"
            ] = mock.Mock()
            self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock(
                return_value={"10.0.0.10"}
            )
            self.namespace["dns_client"] = SimpleNamespace(
                list_zones=mock.Mock(
                    return_value=response([SimpleNamespace(id="ocid1.zone.test")])
                )
            )
            self.namespace["computeManagementClient"] = SimpleNamespace(
                get_instance_pool=lambda *args, **kwargs: response(
                    instance_pool()
                ),
                list_instance_pool_instances=lambda *args, **kwargs: response(
                    []
                ),
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            def still_failed_release(command, **kwargs):
                if command[:3] == ["terraform", "state", "list"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(
                            'oci_dns_rrset.rrset-cluster-network-OCI["0"]\n'
                        ),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="state rm is still failing",
                )

            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=still_failed_release,
            ):
                try:
                    self.namespace["cleanup_instance_pool_name_dns_records"](
                        COMPARTMENT_ID,
                        inventory,
                        inventory_path=inventory_path,
                    )
                except RuntimeError:
                    # Blocking destroy is also safe while Terraform still owns
                    # the RRset.  The invariant under test is no DNS mutation.
                    pass

        delete_rrset.assert_not_called()

    def test_cleanup_resumes_pending_cn_state_release_before_dns_delete(self):
        self.configure_migration_dns()

        def failed_release(command, **kwargs):
            if command[:3] == ["terraform", "state", "list"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        'oci_dns_rrset.rrset-cluster-network-OCI["0"]\n'
                    ),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="simulated initial state rm failure",
            )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = self.write_legacy_cn_files(directory)
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=failed_release,
            ):
                with self.assertRaisesRegex(RuntimeError, "state|ownership"):
                    self.namespace["migrate_instance_pool_oci_dns_ownership"](
                        inventory_path,
                        compartment_id=COMPARTMENT_ID,
                        instance_pool_id=INSTANCE_POOL_ID,
                        instances_by_id={
                            INSTANCE_ID_ONE: {
                                "display_name": "generated-one",
                                "ip": "10.0.0.10",
                            }
                        },
                    )
            pending_ownership = self.namespace[
                "load_instance_pool_name_dns_ownership"
            ](inventory_path)
            self.assertIs(
                pending_ownership.get("terraform_state_released"),
                False,
            )

            events = []
            original_write_ownership = self.namespace[
                "write_instance_pool_name_dns_ownership"
            ]

            def track_ownership(inventory, document):
                if document is None or not document.get("rrsets"):
                    events.append("ledger-clear")
                elif document.get("terraform_state_released") is True:
                    events.append("ledger-released")
                return original_write_ownership(inventory, document)

            def successful_release(command, **kwargs):
                if command[:3] == ["terraform", "state", "list"]:
                    events.append("state-list")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(
                            'oci_dns_rrset.rrset-cluster-network-OCI["0"]\n'
                        ),
                        stderr="",
                    )
                self.assertEqual(
                    command[:3],
                    ["terraform", "state", "rm"],
                )
                events.append("state-rm")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def delete_rrset(zone_id, domain, private_ips):
                self.assertIn("ledger-released", events)
                events.append("dns-delete")
                return True

            self.namespace[
                "write_instance_pool_name_dns_ownership"
            ] = track_ownership
            self.namespace[
                "delete_private_dns_a_rrset_if_owned"
            ] = delete_rrset
            self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock(
                return_value={"10.0.0.10"}
            )
            self.namespace["dns_client"] = SimpleNamespace(
                list_zones=mock.Mock(
                    return_value=response([SimpleNamespace(id="ocid1.zone.test")])
                )
            )
            self.namespace["computeManagementClient"] = SimpleNamespace(
                get_instance_pool=lambda *args, **kwargs: response(
                    instance_pool()
                ),
                list_instance_pool_instances=lambda *args, **kwargs: response(
                    []
                ),
            )
            inventory = self.namespace["parse_inventory"](inventory_path)
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=successful_release,
            ):
                self.namespace["cleanup_instance_pool_name_dns_records"](
                    COMPARTMENT_ID,
                    inventory,
                    inventory_path=inventory_path,
                )

            ownership_path = self.namespace[
                "get_instance_pool_name_dns_ownership_path"
            ](inventory_path)
            self.assertFalse(os.path.exists(ownership_path))

        self.assertLess(events.index("state-list"), events.index("state-rm"))
        self.assertLess(events.index("state-rm"), events.index("ledger-released"))
        self.assertLess(events.index("ledger-released"), events.index("dns-delete"))

    def test_pending_cn_transfer_refuses_state_json_ledger_mismatch(self):
        state_address = (
            'oci_dns_rrset.rrset-cluster-network-OCI["0"]'
        )
        expected_domain = "generated-one."+CLUSTER_NAME+".local"
        pending_ownership = {
            "version": 1,
            "cluster_name": CLUSTER_NAME,
            "instance_pool_id": INSTANCE_POOL_ID,
            "terraform_state_released": False,
            "rrsets": [{
                "zone_id": DNS_ZONE_ID,
                "zone_name": CLUSTER_NAME+".local",
                "domain": expected_domain,
                "private_ips": ["10.0.0.10"],
            }],
        }
        commands = []

        def run_terraform(command, **kwargs):
            commands.append(command)
            self.assertEqual(
                command[:3],
                ["terraform", "state", "list"],
            )
            return SimpleNamespace(
                returncode=0,
                stdout=state_address+"\n",
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(
                directory,
                dns_rrsets=[{
                    "index_key": "0",
                    "domain": "unexpected."+CLUSTER_NAME+".local",
                    "private_ip": "10.0.0.99",
                }],
            )
            inventory_path = write_cluster_network_inventory(directory)
            self.namespace["write_instance_pool_name_dns_ownership"](
                inventory_path,
                pending_ownership,
            )
            persisted_pending_ownership = self.namespace[
                "load_instance_pool_name_dns_ownership"
            ](inventory_path)
            with mock.patch.object(
                self.namespace["subprocess"],
                "run",
                side_effect=run_terraform,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "no longer matches Terraform state",
                ):
                    self.namespace[
                        "finish_pending_managed_pool_dns_ownership_transfer"
                    ](
                        inventory_path,
                        persisted_pending_ownership,
                        CLUSTER_NAME,
                        INSTANCE_POOL_ID,
                    )
            ownership_after_failure = self.namespace[
                "load_instance_pool_name_dns_ownership"
            ](inventory_path)

        self.assertEqual(
            commands,
            [["terraform", "state", "list"]],
        )
        self.assertIs(
            ownership_after_failure["terraform_state_released"],
            False,
        )

    def test_cn_cleanup_leaves_terraform_owned_slurm_rrset_for_destroy(self):
        canonical_domain = "worker-one."+CLUSTER_NAME+".local"
        slurm_domain = "batch-HPC_instance-11."+CLUSTER_NAME+".local"
        deleted_domains = []
        full_instance = make_instance(
            INSTANCE_ID_ONE,
            "worker-one",
            cluster_name=CLUSTER_NAME,
            compartment_id=COMPARTMENT_ID,
        )
        live_pool = instance_pool()
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=lambda *args, **kwargs: response(live_pool),
            list_instance_pool_instances=lambda *args, **kwargs: response([
                member_summary()
            ]),
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda *args, **kwargs: response(full_instance)
        )
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=response([SimpleNamespace(id=DNS_ZONE_ID)])
            )
        )
        self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock(
            return_value={"10.0.0.10"}
        )
        self.namespace["delete_private_dns_a_rrset_if_owned"] = (
            lambda zone_id, domain, private_ips: deleted_domains.append(domain)
        )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(
                directory,
                slurm_rrsets=[{
                    "index_key": "0",
                    "domain": slurm_domain,
                    "private_ip": "10.0.0.10",
                }],
            )
            inventory_path = write_cluster_network_inventory(directory)
            with open(inventory_path, encoding="utf-8") as inventory_file:
                inventory_text = inventory_file.read().replace(
                    "dns_entries=false",
                    "dns_entries=true",
                )
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(inventory_text)
            with open(
                os.path.join(
                    directory,
                    self.namespace[
                        "MANAGED_POOL_DNS_OWNERSHIP_MARKER_FILENAME"
                    ],
                ),
                "w",
                encoding="utf-8",
            ) as marker_file:
                marker_file.write("python-owned-canonical-dns-v2\n")
            self.namespace["write_instance_pool_name_dns_ownership"](
                inventory_path,
                {
                    "version": 1,
                    "cluster_name": CLUSTER_NAME,
                    "instance_pool_id": INSTANCE_POOL_ID,
                    "rrsets": [{
                        "zone_id": DNS_ZONE_ID,
                        "zone_name": CLUSTER_NAME+".local",
                        "domain": canonical_domain,
                        "private_ips": ["10.0.0.10"],
                    }],
                },
            )
            inventory = self.namespace["parse_inventory"](inventory_path)
            self.assertEqual(
                self.namespace[
                    "get_instance_pool_slurm_dns_domain"
                ](inventory, "10.0.0.10", CLUSTER_NAME+".local"),
                slurm_domain,
            )
            self.namespace["cleanup_instance_pool_name_dns_records"](
                COMPARTMENT_ID,
                inventory,
                inventory_path=inventory_path,
            )

        self.assertEqual(deleted_domains, [canonical_domain])
        self.assertNotIn(slurm_domain, deleted_domains)

    def test_cn_cleanup_deletes_resize_added_slurm_rrset_outside_state(self):
        canonical_domains = {
            "worker-one."+CLUSTER_NAME+".local",
            "worker-two."+CLUSTER_NAME+".local",
        }
        terraform_slurm_domain = (
            "batch-HPC_instance-11."+CLUSTER_NAME+".local"
        )
        added_slurm_domain = (
            "batch-HPC_instance-12."+CLUSTER_NAME+".local"
        )
        deleted_rrsets = []
        full_instances = {
            INSTANCE_ID_ONE: make_instance(
                INSTANCE_ID_ONE,
                "worker-one",
                cluster_name=CLUSTER_NAME,
                compartment_id=COMPARTMENT_ID,
            ),
            INSTANCE_ID_TWO: make_instance(
                INSTANCE_ID_TWO,
                "worker-two",
                cluster_name=CLUSTER_NAME,
                compartment_id=COMPARTMENT_ID,
            ),
        }
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=lambda *args, **kwargs: response(
                instance_pool(size=2, current_size=2)
            ),
            list_instance_pool_instances=lambda *args, **kwargs: response([
                member_summary(INSTANCE_ID_ONE, "worker-one"),
                member_summary(INSTANCE_ID_TWO, "worker-two"),
            ]),
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id, *args, **kwargs: response(
                full_instances[instance_id]
            )
        )
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=response([SimpleNamespace(id=DNS_ZONE_ID)])
            )
        )
        self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock(
            side_effect=lambda zone_id, domain, private_ips: set(private_ips)
        )
        self.namespace["delete_private_dns_a_rrset_if_owned"] = (
            lambda zone_id, domain, private_ips: deleted_rrsets.append(
                (zone_id, domain, frozenset(private_ips))
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(
                directory,
                slurm_rrsets=[{
                    "index_key": "0",
                    "domain": terraform_slurm_domain,
                    "private_ip": "10.0.0.10",
                }],
            )
            inventory_path = write_cluster_network_inventory(
                directory,
                hostname="worker-one",
                second_hostname="worker-two",
            )
            with open(inventory_path, encoding="utf-8") as inventory_file:
                inventory_text = inventory_file.read().replace(
                    "dns_entries=false",
                    "dns_entries=true",
                )
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(inventory_text)
            self.namespace["write_instance_pool_name_dns_ownership"](
                inventory_path,
                {
                    "version": 1,
                    "cluster_name": CLUSTER_NAME,
                    "instance_pool_id": INSTANCE_POOL_ID,
                    "rrsets": [
                        {
                            "zone_id": DNS_ZONE_ID,
                            "zone_name": CLUSTER_NAME+".local",
                            "domain": "worker-one."+CLUSTER_NAME+".local",
                            "private_ips": ["10.0.0.10"],
                        },
                        {
                            "zone_id": DNS_ZONE_ID,
                            "zone_name": CLUSTER_NAME+".local",
                            "domain": "worker-two."+CLUSTER_NAME+".local",
                            "private_ips": ["10.0.0.11"],
                        },
                    ],
                },
            )
            inventory = self.namespace["parse_inventory"](inventory_path)
            self.namespace["cleanup_instance_pool_name_dns_records"](
                COMPARTMENT_ID,
                inventory,
                inventory_path=inventory_path,
            )

        deleted_domains = {rrset[1] for rrset in deleted_rrsets}
        self.assertTrue(canonical_domains.issubset(deleted_domains))
        self.assertIn(
            (DNS_ZONE_ID, added_slurm_domain, frozenset({"10.0.0.11"})),
            deleted_rrsets,
        )
        self.assertNotIn(terraform_slurm_domain, deleted_domains)

    def test_cn_cleanup_does_not_treat_terraform_slurm_alias_as_canonical(self):
        slurm_alias = "batch-HPC_instance-11"
        terraform_slurm_domain = slurm_alias+"."+CLUSTER_NAME+".local"
        full_instance = make_instance(
            INSTANCE_ID_ONE,
            slurm_alias,
            cluster_name=CLUSTER_NAME,
            compartment_id=COMPARTMENT_ID,
        )
        self.namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=lambda *args, **kwargs: response(instance_pool()),
            list_instance_pool_instances=lambda *args, **kwargs: response([
                member_summary(INSTANCE_ID_ONE, slurm_alias)
            ]),
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda *args, **kwargs: response(full_instance)
        )
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=response([SimpleNamespace(id=DNS_ZONE_ID)])
            )
        )
        verify_rrset = self.namespace[
            "verify_private_dns_a_rrset_ownership"
        ] = mock.Mock()
        delete_rrset = self.namespace[
            "delete_private_dns_a_rrset_if_owned"
        ] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(
                directory,
                slurm_rrsets=[{
                    "index_key": "0",
                    "domain": terraform_slurm_domain,
                    "private_ip": "10.0.0.10",
                }],
            )
            inventory_path = write_cluster_network_inventory(
                directory,
                hostname=slurm_alias,
            )
            with open(inventory_path, encoding="utf-8") as inventory_file:
                inventory_text = inventory_file.read().replace(
                    "dns_entries=false",
                    "dns_entries=true",
                )
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(inventory_text)
            ownership_path = self.namespace[
                "get_instance_pool_name_dns_ownership_path"
            ](inventory_path)
            self.assertFalse(os.path.exists(ownership_path))
            inventory = self.namespace["parse_inventory"](inventory_path)
            self.namespace["cleanup_instance_pool_name_dns_records"](
                COMPARTMENT_ID,
                inventory,
                inventory_path=inventory_path,
            )

        verify_rrset.assert_not_called()
        delete_rrset.assert_not_called()

    def test_cn_cleanup_rejects_pending_plan_for_another_parent_before_delete(self):
        delete_rrset = self.namespace[
            "delete_private_dns_a_rrset_if_owned"
        ] = mock.Mock()
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=mock.Mock(
                return_value=response([SimpleNamespace(id="ocid1.zone.test")])
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = write_cluster_network_inventory(directory)
            with open(inventory_path, encoding="utf-8") as inventory_file:
                inventory_text = inventory_file.read().replace(
                    "dns_entries=false",
                    "dns_entries=true",
                )
            with open(inventory_path, "w", encoding="utf-8") as inventory_file:
                inventory_file.write(inventory_text)
            self.namespace["write_instance_pool_hostname_sync_plan"](
                inventory_path,
                {
                    "version": 2,
                    "status": "pending",
                    "deployment_type": "CN",
                    "cluster_name": CLUSTER_NAME,
                    "cluster_network_id": "ocid1.clusternetwork.other",
                    "instance_pool_id": INSTANCE_POOL_ID,
                    "members": {
                        INSTANCE_ID_ONE: {
                            "hostname": "worker-one",
                            "private_ip": "10.0.0.10",
                            "inventory_hostname": "generated-one",
                            "previous_display_name": "generated-one",
                        }
                    },
                },
            )
            self.namespace["write_instance_pool_name_dns_ownership"](
                inventory_path,
                {
                    "version": 1,
                    "cluster_name": CLUSTER_NAME,
                    "instance_pool_id": INSTANCE_POOL_ID,
                    "rrsets": [{
                        "zone_id": "ocid1.zone.test",
                        "zone_name": CLUSTER_NAME+".local",
                        "domain": "worker-one."+CLUSTER_NAME+".local",
                        "private_ips": ["10.0.0.10"],
                    }],
                },
            )
            self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock(
                return_value={"10.0.0.10"}
            )
            inventory = self.namespace["parse_inventory"](inventory_path)
            with self.assertRaisesRegex(RuntimeError, "Cluster Network"):
                self.namespace["cleanup_instance_pool_name_dns_records"](
                    COMPARTMENT_ID,
                    inventory,
                    inventory_path=inventory_path,
                )

        delete_rrset.assert_not_called()


class ClusterNetworkPendingPlanTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def plan(self, cluster_network_id=CLUSTER_NETWORK_ID):
        return {
            "version": 2,
            "status": "pending",
            "deployment_type": "CN",
            "cluster_name": CLUSTER_NAME,
            "cluster_network_id": cluster_network_id,
            "instance_pool_id": INSTANCE_POOL_ID,
            "members": {
                INSTANCE_ID_ONE: {
                    "hostname": "worker-one",
                    "private_ip": "10.0.0.10",
                    "inventory_hostname": "generated-one",
                    "previous_display_name": "generated-one",
                }
            },
        }

    def test_v2_cluster_network_plan_requires_exact_parent_identity(self):
        validated = self.namespace["validate_instance_pool_hostname_sync_plan"](
            self.plan()
        )
        self.assertEqual(validated["cluster_network_id"], CLUSTER_NETWORK_ID)

        missing_identity = self.plan()
        del missing_identity["cluster_network_id"]
        with self.assertRaisesRegex(RuntimeError, "Cluster Network|cluster_network"):
            self.namespace["validate_instance_pool_hostname_sync_plan"](
                missing_identity
            )

    def test_v1_instance_pool_plan_remains_compatible(self):
        plan = self.plan()
        plan["version"] = 1
        plan.pop("deployment_type")
        plan.pop("cluster_network_id")
        self.assertIs(
            self.namespace["validate_instance_pool_hostname_sync_plan"](plan),
            plan,
        )

    def test_pending_plan_for_another_parent_is_rejected_before_any_update(self):
        update_names = self.namespace["update_instance_pool_display_names"] = mock.Mock()
        self.namespace["get_complete_cluster_network_instances"] = mock.Mock(
            return_value=(
                [{
                    "display_name": "generated-one",
                    "ip": "10.0.0.10",
                    "ocid": INSTANCE_ID_ONE,
                }],
                {INSTANCE_ID_ONE: {
                    "display_name": "generated-one",
                    "ip": "10.0.0.10",
                    "ocid": INSTANCE_ID_ONE,
                }},
            )
        )
        self.namespace["get_complete_instance_pool_instances"] = mock.Mock(
            side_effect=AssertionError("CN sync must use the parent-aware snapshot")
        )
        self.namespace["load_pending_instance_pool_node_removals"] = mock.Mock(
            return_value=[]
        )
        self.namespace["load_instance_pool_post_resize_recovery"] = mock.Mock(
            return_value=None
        )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_cluster_network_inventory(directory)
            self.namespace["write_instance_pool_hostname_sync_plan"](
                inventory_path,
                self.plan(cluster_network_id="ocid1.clusternetwork.other"),
            )
            with self.assertRaisesRegex(RuntimeError, "another|parent|Cluster Network"):
                self.namespace["synchronize_instance_pool_names"](
                    COMPARTMENT_ID,
                    INSTANCE_POOL_ID,
                    inventory_path,
                    CLUSTER_NAME,
                    max_wait_seconds=0,
                    cluster_network_id=CLUSTER_NETWORK_ID,
                )

        update_names.assert_not_called()

    def test_pending_cluster_network_plan_is_reused_without_recollecting_facts(self):
        member = {
            "display_name": "worker-one",
            "ip": "10.0.0.10",
            "ocid": INSTANCE_ID_ONE,
        }
        self.namespace["get_complete_cluster_network_instances"] = mock.Mock(
            return_value=([member], {INSTANCE_ID_ONE: member})
        )
        self.namespace["get_complete_instance_pool_instances"] = mock.Mock(
            side_effect=AssertionError("CN sync must use the parent-aware snapshot")
        )
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock(
            side_effect=AssertionError("pending plans must not recollect mutable facts")
        )
        self.namespace["load_pending_instance_pool_node_removals"] = mock.Mock(
            return_value=[]
        )
        self.namespace["load_instance_pool_post_resize_recovery"] = mock.Mock(
            return_value=None
        )
        self.namespace["load_pending_local_block_volume_deletions"] = mock.Mock(
            return_value=[]
        )
        self.namespace["migrate_instance_pool_oci_dns_ownership"] = mock.Mock()
        self.namespace["preflight_instance_pool_name_dns"] = mock.Mock()
        self.namespace["load_instance_pool_name_dns_ownership"] = mock.Mock(
            return_value=None
        )
        self.namespace["reconcile_instance_pool_name_dns"] = mock.Mock()
        self.namespace["refresh_instance_pool_hosts"] = mock.Mock()
        update_names = self.namespace["update_instance_pool_display_names"] = mock.Mock(
            return_value={INSTANCE_ID_ONE: "generated-one"}
        )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_cluster_network_inventory(directory)
            self.namespace["write_instance_pool_hostname_sync_plan"](
                inventory_path,
                self.plan(),
            )
            self.namespace["synchronize_instance_pool_names"](
                COMPARTMENT_ID,
                INSTANCE_POOL_ID,
                inventory_path,
                CLUSTER_NAME,
                max_wait_seconds=0,
                cluster_network_id=CLUSTER_NETWORK_ID,
            )

        collector.assert_not_called()
        self.assertEqual(
            update_names.call_args.kwargs["cluster_network_id"],
            CLUSTER_NETWORK_ID,
        )

    def test_partial_update_keeps_plan_and_retry_converges_without_new_facts(self):
        current = {
            INSTANCE_ID_ONE: {
                "display_name": "generated-one",
                "ip": "10.0.0.10",
                "ocid": INSTANCE_ID_ONE,
            },
            INSTANCE_ID_TWO: {
                "display_name": "generated-two",
                "ip": "10.0.0.11",
                "ocid": INSTANCE_ID_TWO,
            },
        }

        def complete_snapshot(*args, **kwargs):
            members = [dict(current[key]) for key in sorted(current)]
            return members, {member["ocid"]: member for member in members}

        self.namespace["get_complete_cluster_network_instances"] = mock.Mock(
            side_effect=complete_snapshot
        )
        self.namespace["get_complete_instance_pool_instances"] = mock.Mock(
            side_effect=AssertionError("CN sync must use the parent-aware snapshot")
        )
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock(
            side_effect=AssertionError("a committed retry must not recollect facts")
        )
        self.namespace["load_pending_instance_pool_node_removals"] = mock.Mock(
            return_value=[]
        )
        self.namespace["load_instance_pool_post_resize_recovery"] = mock.Mock(
            return_value=None
        )
        self.namespace["load_pending_local_block_volume_deletions"] = mock.Mock(
            return_value=[]
        )
        self.namespace["migrate_instance_pool_oci_dns_ownership"] = mock.Mock()
        self.namespace["preflight_instance_pool_name_dns"] = mock.Mock()
        self.namespace["load_instance_pool_name_dns_ownership"] = mock.Mock(
            return_value=None
        )
        self.namespace["reconcile_instance_pool_name_dns"] = mock.Mock()
        self.namespace["refresh_instance_pool_hosts"] = mock.Mock()
        update_attempts = []

        def update_names(*args, **kwargs):
            update_attempts.append(kwargs.get("cluster_network_id"))
            desired_names = args[3]
            previous_names = {
                instance_id: member["display_name"]
                for instance_id, member in current.items()
            }
            callback = kwargs.get("before_updates")
            if callback is not None:
                callback(previous_names)
            if len(update_attempts) == 1:
                # Model an interruption after one OCI resource converged but
                # before the remaining member, DNS, or Inventory was changed.
                current[INSTANCE_ID_ONE]["display_name"] = desired_names[
                    INSTANCE_ID_ONE
                ]
                raise RuntimeError("simulated partial OCI update")
            for instance_id, desired_name in desired_names.items():
                current[instance_id]["display_name"] = desired_name
            return previous_names

        self.namespace["update_instance_pool_display_names"] = update_names
        observations = {
            INSTANCE_ID_ONE: {
                "hostname": "worker-one",
                "private_ip": "10.0.0.10",
                "inventory_hostname": "generated-one",
            },
            INSTANCE_ID_TWO: {
                "hostname": "worker-two",
                "private_ip": "10.0.0.11",
                "inventory_hostname": "generated-two",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_cluster_network_inventory(
                directory,
                second_hostname="generated-two",
            )
            plan_path = self.namespace[
                "get_instance_pool_hostname_sync_plan_path"
            ](inventory_path)
            with self.assertRaisesRegex(RuntimeError, "partial OCI"):
                self.namespace["synchronize_instance_pool_names"](
                    COMPARTMENT_ID,
                    INSTANCE_POOL_ID,
                    inventory_path,
                    CLUSTER_NAME,
                    observed_hostnames_by_instance_id=observations,
                    max_wait_seconds=0,
                    cluster_network_id=CLUSTER_NETWORK_ID,
                )
            self.assertTrue(os.path.isfile(plan_path))
            with open(plan_path, encoding="utf-8") as plan_file:
                committed_plan = json.load(plan_file)
            self.assertEqual(committed_plan["version"], 2)
            self.assertEqual(committed_plan["deployment_type"], "CN")
            self.assertEqual(
                committed_plan["cluster_network_id"],
                CLUSTER_NETWORK_ID,
            )
            self.assertEqual(
                committed_plan["instance_pool_id"],
                INSTANCE_POOL_ID,
            )
            with open(inventory_path, encoding="utf-8") as inventory_file:
                self.assertIn("generated-one ansible_host=10.0.0.10", inventory_file.read())

            self.namespace["synchronize_instance_pool_names"](
                COMPARTMENT_ID,
                INSTANCE_POOL_ID,
                inventory_path,
                CLUSTER_NAME,
                max_wait_seconds=0,
                cluster_network_id=CLUSTER_NETWORK_ID,
            )
            self.assertFalse(os.path.exists(plan_path))
            with open(inventory_path, encoding="utf-8") as inventory_file:
                rewritten = inventory_file.read()

        self.assertIn("worker-one ansible_host=10.0.0.10", rewritten)
        self.assertIn("worker-two ansible_host=10.0.0.11", rewritten)
        self.assertEqual(
            update_attempts,
            [CLUSTER_NETWORK_ID, CLUSTER_NETWORK_ID],
        )
        collector.assert_not_called()

    def test_cluster_network_sync_refuses_pending_rdma_node_removal(self):
        complete_snapshot = self.namespace[
            "get_complete_cluster_network_instances"
        ] = mock.Mock()
        update_names = self.namespace[
            "update_instance_pool_display_names"
        ] = mock.Mock()
        self.namespace["load_pending_instance_pool_node_removals"] = mock.Mock(
            return_value=[{
                "cluster_name": CLUSTER_NAME,
                "compartment_id": COMPARTMENT_ID,
                "instance_pool_id": INSTANCE_POOL_ID,
            }]
        )
        self.namespace["load_instance_pool_post_resize_recovery"] = mock.Mock(
            return_value=None
        )

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_cluster_network_inventory(directory)
            with self.assertRaisesRegex(RuntimeError, "removal|remove"):
                self.namespace["synchronize_instance_pool_names"](
                    COMPARTMENT_ID,
                    INSTANCE_POOL_ID,
                    inventory_path,
                    CLUSTER_NAME,
                    max_wait_seconds=0,
                    cluster_network_id=CLUSTER_NETWORK_ID,
                )

        complete_snapshot.assert_not_called()
        update_names.assert_not_called()


class ClusterNetworkWiringTests(unittest.TestCase):
    def read(self, *path_parts):
        with open(
            os.path.join(REPOSITORY_ROOT, *path_parts),
            encoding="utf-8",
        ) as source_file:
            return source_file.read()

    def test_terraform_oci_name_dns_is_python_owned_for_all_compute_types(self):
        network = self.read("autoscaling", "tf_init", "network.tf")
        resource = network.split(
            'resource "oci_dns_rrset" "rrset-cluster-network-OCI"',
            1,
        )[1].split('\nresource "', 1)[0]
        self.assertRegex(
            resource,
            r"(?m)^\s*for_each\s*=\s*toset\(\[\]\)\s*$",
        )
        self.assertNotIn("var.dns_entries", resource)
        self.assertNotIn("var.cluster_network || var.compute_cluster", resource)

    def test_template_contains_apply_safe_v2_dns_ownership_marker(self):
        marker = self.read(
            "autoscaling",
            "tf_init",
            ".managed-pool-python-dns-v2",
        )
        self.assertEqual(marker, "python-owned-canonical-dns-v2\n")

    def test_v2_dns_marker_avoids_nested_terraform_during_initial_sync(self):
        namespace = load_resize_functions()
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_cluster_network_inventory(directory)
            with open(
                os.path.join(directory, "variables.tf"),
                "w",
                encoding="utf-8",
            ) as variables_file:
                variables_file.write(
                    'variable "compute_cluster" { default = false }\n'
                )
            with open(
                os.path.join(directory, "network.tf"),
                "w",
                encoding="utf-8",
            ) as network_file:
                network_file.write(
                    'resource "oci_dns_rrset" "rrset-cluster-network-OCI" {\n'
                    "  for_each = toset([])\n"
                    "}\n"
                )
            with open(
                os.path.join(
                    directory,
                    namespace["MANAGED_POOL_DNS_OWNERSHIP_MARKER_FILENAME"],
                ),
                "w",
                encoding="utf-8",
            ) as marker_file:
                marker_file.write("python-owned-canonical-dns-v2\n")
            with mock.patch.object(
                namespace["subprocess"],
                "run",
                side_effect=AssertionError(
                    "initial Terraform local-exec must not nest terraform state"
                ),
            ):
                changed = namespace["migrate_instance_pool_oci_dns_ownership"](
                    inventory_path
                )

        self.assertFalse(changed)

    def test_cn_destroy_does_not_treat_v1_marker_as_python_dns_ownership(self):
        resize = self.read("bin", "resize.py")
        cleanup_start = resize.index(
            "if args.mode == 'cleanup_compute_cluster':"
        )
        cleanup_end = resize.index("\ntry:\n    cn_summary", cleanup_start)
        cleanup = resize[cleanup_start:cleanup_end]
        gate = cleanup.split(
            "should_cleanup_managed_pool_dns = (",
            1,
        )[1].split("\n        if should_cleanup_managed_pool_dns:", 1)[0]
        self.assertIn("cluster_network", gate)
        self.assertIn("managed_dns_marker_path", gate)
        self.assertIn("managed_dns_ownership_path", gate)
        self.assertNotIn("INSTANCE_POOL_DNS_OWNERSHIP_MARKER_FILENAME", gate)

    def test_shell_compute_gate_covers_all_autoscaling_compute_types(self):
        for path_parts in (
            ("bin", "configure_as.sh"),
            ("bin", "resize.sh"),
            ("bin", "delete_cluster.sh"),
        ):
            with self.subTest(path=os.path.join(*path_parts)):
                source = self.read(*path_parts)
                helper = source.split(
                    "is_autoscaling_compute_deployment()",
                    1,
                )[1].split("\n}", 1)[0]
                self.assertRegex(
                    helper,
                    r"cluster_network.*\(true\|false\)",
                )
                self.assertIn("variables", helper)
                self.assertIn("inventory", helper)
                self.assertNotIn(
                    "is_autoscaling_managed_pool_deployment",
                    source,
                )
                self.assertNotIn('variable "compute_cluster"', helper)

    def test_cleanup_rejects_compute_cluster_child_only_state(self):
        resize = self.read("bin", "resize.py")
        cleanup_start = resize.index(
            'if args.mode == "cleanup_compute_cluster":'
        )
        cleanup_end = resize.index(
            "\nif args.mode == 'prepare_local_block_volume':",
            cleanup_start,
        )
        cleanup = resize[cleanup_start:cleanup_end]
        self.assertIn(
            "cleanup_has_tracked_compute_cluster = "
            "has_tracked_compute_cluster_resources(",
            cleanup,
        )
        self.assertIn(
            "Compute Cluster instances but ",
            cleanup,
        )

    def test_monitoring_and_initial_create_use_common_compute_path(self):
        resize_shell = self.read("bin", "resize.sh")
        create_shell = self.read("bin", "create_cluster.sh")
        reconcile = resize_shell.split(
            "reconcile_compute_monitoring()",
            1,
        )[1].split("\n}", 1)[0]
        self.assertIn("list --monitoring-output", reconcile)
        self.assertIn("node_OCID", reconcile)
        self.assertIn("START TRANSACTION", reconcile)
        self.assertNotIn('if [ "$compute_cluster" != "true" ]', create_shell)
        self.assertIn("--reconcile-monitoring", create_shell)

    def test_monitoring_resolves_cn_from_terraform_state_not_display_name(self):
        namespace = load_resize_functions()
        tracked_pool = instance_pool()
        tracked_network = cluster_network(tracked_pool)
        get_cluster_network = mock.Mock(return_value=response(tracked_network))
        namespace["computeManagementClient"] = SimpleNamespace(
            get_cluster_network=get_cluster_network,
        )
        namespace["get_summary"] = mock.Mock(
            side_effect=AssertionError("display-name lookup must not run")
        )

        with tempfile.TemporaryDirectory() as directory:
            write_cluster_network_state(directory)
            inventory_path = write_cluster_network_inventory(directory)
            inventory = namespace["parse_inventory"](inventory_path)
            resolved_network, resolved_pool, deployment_type = namespace[
                "get_summary_for_operation"
            ](
                COMPARTMENT_ID,
                CLUSTER_NAME,
                inventory_path,
                inventory,
                "list",
                True,
                monitoring_output=True,
            )

        self.assertIs(resolved_network, tracked_network)
        self.assertIs(resolved_pool, tracked_pool)
        self.assertEqual(deployment_type, "CN")
        get_cluster_network.assert_called_once_with(
            CLUSTER_NETWORK_ID,
            **namespace["get_oci_retry_kwargs"](),
        )

    def test_normal_cn_mutations_resolve_exact_parent_from_terraform_state(self):
        for mode in ("add", "remove", "remove_unreachable", "reconfigure"):
            with self.subTest(mode=mode):
                namespace = load_resize_functions()
                tracked_pool = instance_pool()
                tracked_network = cluster_network(tracked_pool)
                get_cluster_network = mock.Mock(
                    return_value=response(tracked_network)
                )
                namespace["computeManagementClient"] = SimpleNamespace(
                    get_cluster_network=get_cluster_network,
                )
                fallback = namespace["get_summary"] = mock.Mock(
                    side_effect=AssertionError(
                        "mutating modes must not select a same-name CN"
                    )
                )

                with tempfile.TemporaryDirectory() as directory:
                    write_cluster_network_state(directory)
                    inventory_path = write_cluster_network_inventory(directory)
                    inventory = namespace["parse_inventory"](inventory_path)
                    resolved_network, resolved_pool, deployment_type = namespace[
                        "get_summary_for_operation"
                    ](
                        COMPARTMENT_ID,
                        CLUSTER_NAME,
                        inventory_path,
                        inventory,
                        mode,
                        True,
                    )

                self.assertIs(resolved_network, tracked_network)
                self.assertIs(resolved_pool, tracked_pool)
                self.assertEqual(deployment_type, "CN")
                get_cluster_network.assert_called_once_with(
                    CLUSTER_NETWORK_ID,
                    **namespace["get_oci_retry_kwargs"](),
                )
                fallback.assert_not_called()

    def test_cn_mutation_without_terraform_identity_never_falls_back_to_name(self):
        namespace = load_resize_functions()
        fallback = namespace["get_summary"] = mock.Mock(
            side_effect=AssertionError("display-name fallback must not run")
        )
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_cluster_network_inventory(directory)
            inventory = namespace["parse_inventory"](inventory_path)
            with self.assertRaisesRegex(RuntimeError, "Terraform state"):
                namespace["get_summary_for_operation"](
                    COMPARTMENT_ID,
                    CLUSTER_NAME,
                    inventory_path,
                    inventory,
                    "add",
                    True,
                )
        fallback.assert_not_called()

    def test_cli_sync_passes_exact_cn_and_embedded_pool_identity(self):
        resize = self.read("bin", "resize.py")
        cli_sync = resize.split(
            "if args.mode == 'sync_instance_pool_names':",
            1,
        )[1].split("if CN != \"CC\":", 1)[0]
        self.assertIn("if not autoscaling:", cli_sync)
        self.assertIn("synchronize_autoscaling_compute_names(", cli_sync)
        self.assertIn(
            "expected_instance_pool_id=current_instance_pool_id",
            cli_sync,
        )
        self.assertIn(
            'expected_cluster_network_id=(cn_ocid if CN == "CN" else None)',
            cli_sync,
        )
        self.assertIn(
            'expected_compute_cluster_id=(cn_ocid if CN == "CC" else None)',
            cli_sync,
        )

    def test_ansible_success_paths_sync_all_autoscaling_compute_types(self):
        resize = self.read("bin", "resize.py")
        for function_name, next_function_name in (
            ("add_reconfigure", "reconfigure"),
            ("reconfigure", "getreachable"),
        ):
            with self.subTest(function=function_name):
                function_body = resize.split(
                    "def "+function_name+"(",
                    1,
                )[1].split("\ndef "+next_function_name+"(", 1)[0]
                self.assertIn("if autoscaling:", function_body)
                self.assertIn(
                    "synchronize_autoscaling_compute_names(",
                    function_body,
                )
                self.assertIn("expected_cluster_network_id=", function_body)
                self.assertIn("expected_compute_cluster_id=", function_body)

    def test_pending_cn_sync_is_resumed_before_resize_or_rdma_member_removal(self):
        resize = self.read("bin", "resize.py")
        resume_start = resize.index("# A failed synchronization")
        resume_end = resize.index(
            "if args.mode == 'sync_instance_pool_names':",
            resume_start,
        )
        resume = resize[resume_start:resume_end]
        for mode in ("add", "remove", "remove_unreachable", "reconfigure"):
            self.assertIn('"'+mode+'"', resume)
        self.assertIn("if autoscaling and args.mode in [", resume)
        resume_call = resize.index(
            "synchronize_autoscaling_compute_names(",
            resume_start,
            resume_end,
        )
        self.assertIn("expected_cluster_network_id=", resume)
        self.assertIn("expected_compute_cluster_id=", resume)
        detach = resize.index(
            "remove_instance_pool_member_and_managed_local_block_volume(",
            resume_end,
        )
        self.assertLess(resume_call, detach)

    def test_cn_removal_freezes_exact_member_ocids_before_ansible_or_detach(self):
        resize = self.read("bin", "resize.py")
        removal = resize.split(
            "hostnames_to_remove_len=len(hostnames_to_remove)",
            1,
        )[1].split("if args.mode == 'add':", 1)[0]
        self.assertRegex(
            removal,
            r'CN\s+in\s+\[(?=[^]]*"IP")(?=[^]]*"CN")[^]]*\]',
        )
        plan = removal.index("build_instance_pool_removal_journal_plan(")
        persist = removal.index("write_pending_instance_pool_node_removals(")
        ansible = removal.index("destroy_unreachable_reconfigure(")
        detach = removal.index(
            "remove_instance_pool_member_and_managed_local_block_volume("
        )
        self.assertLess(plan, persist)
        self.assertLess(persist, ansible)
        self.assertLess(persist, detach)
        frozen_targets = removal.split("removal_targets =", 1)[1].split(
            "for instanceName, frozen_node_removal",
            1,
        )[0]
        self.assertIn("planned_instance_pool_removals", frozen_targets)
        self.assertRegex(
            frozen_targets,
            r'CN\s+in\s+\[(?=[^]]*"IP")(?=[^]]*"CN")[^]]*\]',
        )

    def test_cn_add_and_no_reconfigure_paths_use_managed_pool_recovery_gate(self):
        resize = self.read("bin", "resize.py")
        add = resize.split("if args.mode == 'add':", 1)[1]
        marker = add.index("write_instance_pool_post_resize_recovery(")
        resize_pool = add.index("update_instance_pool_and_wait_for_state(")
        clear = add.rindex("clear_instance_pool_post_resize_recovery(")
        self.assertLess(marker, resize_pool)
        marker_gate = add[:marker].rsplit(
            'if CN in ["IP", "CN"] and autoscaling:',
            1,
        )
        clear_gate = add[:clear].rsplit(
            'elif CN in ["IP", "CN"] and autoscaling:',
            1,
        )
        self.assertEqual(len(marker_gate), 2)
        self.assertEqual(len(clear_gate), 2)

    def test_add_does_not_publish_generated_autoscaling_name_before_sync(self):
        resize = self.read("bin", "resize.py")
        add = resize.split("if args.mode == 'add':", 1)[1]
        dns = add.split("if dns_entries:", 1)[1].split(
            "# The pool size is durable",
            1,
        )[0]
        canonical_update = dns.rsplit("dns_client.update_rr_set", 1)[0]
        canonical_gate = canonical_update.rsplit("if ", 1)[1]
        self.assertRegex(canonical_gate, r"^not autoscaling:")
        self.assertNotIn('CN not in ["IP", "CN"]', dns)

    def test_sync_path_does_not_update_cluster_network_or_rdma_configuration(self):
        resize = self.read("bin", "resize.py")
        sync = resize.split(
            "def synchronize_instance_pool_names",
            1,
        )[1].split("\ndef prepare_local_block_volume_inventory", 1)[0]
        self.assertNotIn("update_cluster_network", sync)
        self.assertNotIn("UpdateClusterNetworkDetails", sync)
        self.assertNotIn("computeManagementClient.update_instance_pool(", sync)
        self.assertNotIn(
            "ComputeManagementClientCompositeOperations.update_instance_pool",
            sync,
        )
        self.assertNotIn("rdma_network", sync)


if __name__ == "__main__":
    unittest.main()
