import importlib.util
import json
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLUSTER_NETWORK_TEST_PATH = os.path.join(
    REPOSITORY_ROOT,
    "tests",
    "test_resize_cluster_network_hostname_sync.py",
)


def load_cluster_network_test_support():
    spec = importlib.util.spec_from_file_location(
        "resize_cluster_network_hostname_test_support_for_compute_cluster",
        CLUSTER_NETWORK_TEST_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SUPPORT = load_cluster_network_test_support()
load_resize_functions = SUPPORT.load_resize_functions
make_instance = SUPPORT.make_instance
response = SUPPORT.response
configure_primary_vnics = SUPPORT.configure_primary_vnics


COMPARTMENT_ID = "ocid1.compartment.test"
CLUSTER_NAME = "batch-1-hpc"
COMPUTE_CLUSTER_ID = "ocid1.computecluster.tracked"
INSTANCE_ID_ONE = "ocid1.instance.one"
INSTANCE_ID_TWO = "ocid1.instance.two"
DNS_ZONE_ID = "ocid1.dns-zone.test"
DNS_ZONE_NAME = CLUSTER_NAME+".local"


def compute_cluster(**overrides):
    values = {
        "id": COMPUTE_CLUSTER_ID,
        "compartment_id": COMPARTMENT_ID,
        "display_name": CLUSTER_NAME,
        "lifecycle_state": "ACTIVE",
        "availability_domain": "AD-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def compute_instance(
    instance_id,
    display_name,
    private_ip,
    lifecycle_state="RUNNING",
    availability_domain="AD-1",
):
    instance = make_instance(
        instance_id,
        display_name,
        cluster_name=CLUSTER_NAME,
        compartment_id=COMPARTMENT_ID,
        lifecycle_state=lifecycle_state,
    )
    instance.availability_domain = availability_domain
    instance.private_ip_for_test = private_ip
    return instance


def member_dict(instance, private_ip):
    return {
        "display_name": instance.display_name,
        "ip": private_ip,
        "ocid": instance.id,
    }


def write_compute_cluster_state(
    directory,
    compute_cluster_ids=(COMPUTE_CLUSTER_ID,),
    tracked_instance_ids=(INSTANCE_ID_ONE,),
    slurm_rrsets=None,
):
    resources = []
    for compute_cluster_id in compute_cluster_ids:
        resources.append({
            "mode": "managed",
            "type": "oci_core_compute_cluster",
            "name": "compute_cluster",
            "instances": [{
                "attributes": {
                    "id": compute_cluster_id,
                    "availability_domain": "AD-1",
                }
            }],
        })
    if tracked_instance_ids:
        resources.append({
            "mode": "managed",
            "type": "oci_core_instance",
            "name": "compute_cluster_instances",
            "instances": [
                {
                    "index_key": index,
                    "attributes": {"id": instance_id},
                }
                for index, instance_id in enumerate(tracked_instance_ids)
            ],
        })
    if slurm_rrsets:
        resources.append({
            "mode": "managed",
            "type": "oci_dns_rrset",
            "name": "rrset-cluster-network-SLURM",
            "instances": [
                {
                    "index_key": index,
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
                for index, rrset in enumerate(slurm_rrsets)
            ],
        })
    # These look similar but are not root resources with the exact Terraform
    # addresses used by this autoscaling cluster.
    resources.extend([
        {
            "mode": "managed",
            "type": "oci_core_compute_cluster",
            "name": "unrelated",
            "instances": [{
                "attributes": {"id": "ocid1.computecluster.unrelated"}
            }],
        },
        {
            "module": "module.child",
            "mode": "managed",
            "type": "oci_core_compute_cluster",
            "name": "compute_cluster",
            "instances": [{
                "attributes": {"id": "ocid1.computecluster.child"}
            }],
        },
    ])
    with open(
        os.path.join(directory, "terraform.tfstate"),
        "w",
        encoding="utf-8",
    ) as state_file:
        json.dump({"resources": resources}, state_file)


def write_compute_cluster_inventory(
    directory,
    members,
    dns_entries=False,
    slurm=True,
):
    inventory_path = os.path.join(directory, "inventory")
    compute_lines = []
    for instance_id, member in members.items():
        compute_lines.append(
            member["inventory_hostname"]+
            " ansible_host="+member["private_ip"]+
            " ansible_user=opc role=compute oci_instance_id="+
            instance_id+"\n"
        )
    with open(inventory_path, "w", encoding="utf-8") as inventory_file:
        inventory_file.write(
            "[controller]\n"
            "controller ansible_host=10.0.0.2\n"
            "[slurm_backup]\n"
            "[login]\n"
            "[compute_to_add]\n"
            "[compute_configured]\n"+
            "".join(compute_lines)+
            "[compute_to_destroy]\n"
            "[nfs]\n"
            "[all:vars]\n"
            "cluster_name="+CLUSTER_NAME+"\n"
            "compute_cluster=true\n"
            "cluster_network=false\n"
            "queue=batch\n"
            "instance_type=HPC_instance\n"
            "private_subnet=10.0.0.0/24\n"
            "zone_name="+CLUSTER_NAME+".local\n"
            "dns_entries="+str(dns_entries).lower()+"\n"
            "slurm="+str(slurm).lower()+"\n"
        )
    return inventory_path


def observations(one="render-a", two="render-b"):
    return {
        INSTANCE_ID_ONE: {
            "hostname": one,
            "private_ip": "10.0.0.10",
            "inventory_hostname": "generated-one",
        },
        INSTANCE_ID_TWO: {
            "hostname": two,
            "private_ip": "10.0.0.11",
            "inventory_hostname": "generated-two",
        },
    }


def configure_complete_compute_cluster_api(namespace, instances):
    instances_by_id = {instance.id: instance for instance in instances}
    get_compute_cluster = mock.Mock(return_value=response(compute_cluster()))
    list_instances = mock.Mock(return_value=response(list(instances)))
    get_instance = mock.Mock(
        side_effect=lambda instance_id, **kwargs: response(
            instances_by_id[instance_id]
        )
    )
    namespace["computeClient"] = SimpleNamespace(
        get_compute_cluster=get_compute_cluster,
        list_instances=list_instances,
        get_instance=get_instance,
    )
    primary_vnics, secondary_vnics = configure_primary_vnics(
        namespace,
        instances_by_id,
    )
    for instance_id, instance in instances_by_id.items():
        primary_vnics[instance_id].private_ip = instance.private_ip_for_test
    return (
        instances_by_id,
        primary_vnics,
        secondary_vnics,
        get_compute_cluster,
        list_instances,
    )


def configure_sync_dependencies(namespace, current):
    def complete_snapshot(*args, **kwargs):
        members = [dict(current[key]) for key in sorted(current)]
        return members, {member["ocid"]: member for member in members}

    namespace["get_complete_compute_cluster_instances"] = mock.Mock(
        side_effect=complete_snapshot
    )
    namespace["get_complete_instance_pool_instances"] = mock.Mock(
        side_effect=AssertionError("Compute Cluster sync used an Instance Pool snapshot")
    )
    namespace["get_complete_cluster_network_instances"] = mock.Mock(
        side_effect=AssertionError("Compute Cluster sync used a Cluster Network snapshot")
    )
    namespace["load_pending_instance_pool_node_removals"] = mock.Mock(
        return_value=[]
    )
    namespace["load_instance_pool_post_resize_recovery"] = mock.Mock(
        return_value=None
    )
    namespace["load_pending_local_block_volume_deletions"] = mock.Mock(
        return_value=[]
    )
    namespace["migrate_instance_pool_oci_dns_ownership"] = mock.Mock()
    namespace["preflight_instance_pool_name_dns"] = mock.Mock()
    namespace["load_instance_pool_name_dns_ownership"] = mock.Mock(
        return_value=None
    )
    namespace["reconcile_instance_pool_name_dns"] = mock.Mock()
    namespace["refresh_instance_pool_hosts"] = mock.Mock()


class ComputeClusterTerraformIdentityTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def test_resolver_uses_only_the_exact_root_state_parent(self):
        tracked = compute_cluster()
        get_compute_cluster = mock.Mock(return_value=response(tracked))
        list_compute_clusters = mock.Mock(
            side_effect=AssertionError("display-name discovery must not be used")
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=get_compute_cluster,
            list_compute_clusters=list_compute_clusters,
        )

        with tempfile.TemporaryDirectory() as directory:
            # A parent without Terraform-tracked children is valid after a
            # scale-to-zero operation; parent identity must still be exact.
            write_compute_cluster_state(directory, tracked_instance_ids=())
            inventory_path = write_compute_cluster_inventory(directory, {})
            resolved = self.namespace[
                "get_tracked_compute_cluster_for_hostname_sync"
            ](
                inventory_path,
                COMPARTMENT_ID,
                CLUSTER_NAME,
            )

        self.assertIs(resolved, tracked)
        self.assertEqual(get_compute_cluster.call_args.args[0], COMPUTE_CLUSTER_ID)
        list_compute_clusters.assert_not_called()

    def test_resolver_rejects_duplicate_or_mismatched_parent_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(
                directory,
                compute_cluster_ids=(
                    COMPUTE_CLUSTER_ID,
                    "ocid1.computecluster.duplicate",
                ),
            )
            inventory_path = write_compute_cluster_inventory(directory, {})
            self.namespace["computeClient"] = SimpleNamespace(
                get_compute_cluster=mock.Mock()
            )
            with self.assertRaisesRegex(RuntimeError, "multiple|Multiple"):
                self.namespace[
                    "get_tracked_compute_cluster_for_hostname_sync"
                ](inventory_path, COMPARTMENT_ID, CLUSTER_NAME)

        invalid_parents = [
            compute_cluster(id="ocid1.computecluster.other"),
            compute_cluster(compartment_id="ocid1.compartment.other"),
            compute_cluster(display_name="another-cluster"),
            compute_cluster(lifecycle_state="DELETING"),
        ]
        for invalid_parent in invalid_parents:
            with self.subTest(parent=invalid_parent.__dict__):
                self.namespace["computeClient"] = SimpleNamespace(
                    get_compute_cluster=lambda *args, value=invalid_parent, **kwargs: response(value)
                )
                with tempfile.TemporaryDirectory() as directory:
                    write_compute_cluster_state(directory)
                    inventory_path = write_compute_cluster_inventory(directory, {})
                    with self.assertRaises(RuntimeError):
                        self.namespace[
                            "get_tracked_compute_cluster_for_hostname_sync"
                        ](inventory_path, COMPARTMENT_ID, CLUSTER_NAME)

    def test_resolver_rejects_child_instances_without_their_state_parent(self):
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=mock.Mock()
        )
        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(
                directory,
                compute_cluster_ids=(),
                tracked_instance_ids=(INSTANCE_ID_ONE,),
            )
            inventory_path = write_compute_cluster_inventory(directory, {})
            with self.assertRaisesRegex(RuntimeError, "parent|Compute Cluster"):
                self.namespace[
                    "get_tracked_compute_cluster_for_hostname_sync"
                ](inventory_path, COMPARTMENT_ID, CLUSTER_NAME)

        self.namespace["computeClient"].get_compute_cluster.assert_not_called()


class ComputeClusterMembershipTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def test_complete_snapshot_includes_live_state_external_resize_member(self):
        initial = compute_instance(
            INSTANCE_ID_ONE,
            "generated-one",
            "10.0.0.10",
        )
        resize_added = compute_instance(
            INSTANCE_ID_TWO,
            "cc-node-pending-0123456789abcdef0123456789abcdef",
            "10.0.0.11",
        )
        _, _, _, _, list_instances = configure_complete_compute_cluster_api(
            self.namespace,
            [initial, resize_added],
        )

        with tempfile.TemporaryDirectory() as directory:
            # Only the initial instance is in Terraform state.  A resize-added
            # instance is authenticated by the exact parent-scoped OCI list.
            write_compute_cluster_state(
                directory,
                tracked_instance_ids=(INSTANCE_ID_ONE,),
            )
            inventory_path = write_compute_cluster_inventory(directory, {})
            self.namespace[
                "get_tracked_compute_cluster_for_hostname_sync"
            ](inventory_path, COMPARTMENT_ID, CLUSTER_NAME)
            instances, instances_by_id = self.namespace[
                "get_complete_compute_cluster_instances"
            ](
                COMPARTMENT_ID,
                COMPUTE_CLUSTER_ID,
                CLUSTER_NAME,
                max_wait_seconds=0,
            )

        self.assertEqual(
            set(instances_by_id),
            {INSTANCE_ID_ONE, INSTANCE_ID_TWO},
        )
        self.assertEqual(len(instances), 2)
        self.assertEqual(
            instances_by_id[INSTANCE_ID_TWO]["ip"],
            "10.0.0.11",
        )
        self.assertEqual(
            list_instances.call_args.kwargs["compute_cluster_id"],
            COMPUTE_CLUSTER_ID,
        )
        self.assertEqual(
            list_instances.call_args.kwargs["compartment_id"],
            COMPARTMENT_ID,
        )

    def test_complete_snapshot_accepts_empty_active_cluster(self):
        configure_complete_compute_cluster_api(self.namespace, [])

        instances, instances_by_id = self.namespace[
            "get_complete_compute_cluster_instances"
        ](
            COMPARTMENT_ID,
            COMPUTE_CLUSTER_ID,
            CLUSTER_NAME,
            max_wait_seconds=0,
        )

        self.assertEqual(instances, [])
        self.assertEqual(instances_by_id, {})

    def test_complete_snapshot_rejects_duplicate_live_ocids(self):
        instance = compute_instance(
            INSTANCE_ID_ONE,
            "generated-one",
            "10.0.0.10",
        )
        configure_complete_compute_cluster_api(
            self.namespace,
            [instance, instance],
        )

        with self.assertRaisesRegex(RuntimeError, "duplicate|incomplete"):
            self.namespace["get_complete_compute_cluster_instances"](
                COMPARTMENT_ID,
                COMPUTE_CLUSTER_ID,
                CLUSTER_NAME,
                max_wait_seconds=0,
            )

    def test_complete_snapshot_rejects_member_outside_parent_boundary(self):
        invalid_members = [
            compute_instance(
                INSTANCE_ID_ONE,
                "generated-one",
                "10.0.0.10",
                lifecycle_state="STOPPED",
            ),
            compute_instance(
                INSTANCE_ID_ONE,
                "generated-one",
                "10.0.0.10",
                availability_domain="AD-2",
            ),
            compute_instance(
                INSTANCE_ID_ONE,
                "generated-one",
                "10.0.0.10",
            ),
        ]
        invalid_members[-1].compartment_id = "ocid1.compartment.other"

        for invalid_member in invalid_members:
            with self.subTest(member=invalid_member.__dict__):
                namespace = load_resize_functions()
                configure_complete_compute_cluster_api(
                    namespace,
                    [invalid_member],
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "parent|incomplete|Compute Cluster",
                ):
                    namespace["get_complete_compute_cluster_instances"](
                        COMPARTMENT_ID,
                        COMPUTE_CLUSTER_ID,
                        CLUSTER_NAME,
                        max_wait_seconds=0,
                    )

    def test_cleanup_snapshot_keeps_stopped_member_and_skips_terminating_member_without_vnic(self):
        stopped = compute_instance(
            INSTANCE_ID_ONE,
            "stopped-worker",
            "10.0.0.10",
            lifecycle_state="STOPPED",
        )
        terminating = compute_instance(
            INSTANCE_ID_TWO,
            "terminating-worker",
            "10.0.0.11",
            lifecycle_state="TERMINATING",
        )
        instances_by_id = {
            stopped.id: stopped,
            terminating.id: terminating,
        }
        list_instances = mock.Mock(
            return_value=response([stopped, terminating])
        )
        primary_vnic = SimpleNamespace(
            id="ocid1.vnic.stopped-primary",
            is_primary=True,
            lifecycle_state="AVAILABLE",
            private_ip="10.0.0.10",
        )

        def list_vnic_attachments(*args, **kwargs):
            if kwargs["instance_id"] == terminating.id:
                return response([])
            return response([
                SimpleNamespace(
                    lifecycle_state="ATTACHED",
                    vnic_id=primary_vnic.id,
                )
            ])

        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=lambda *args, **kwargs: response(
                compute_cluster(lifecycle_state="DELETING")
            ),
            list_instances=list_instances,
            get_instance=lambda instance_id, **kwargs: response(
                instances_by_id[instance_id]
            ),
            list_vnic_attachments=list_vnic_attachments,
        )
        self.namespace["virtualNetworkClient"] = SimpleNamespace(
            get_vnic=lambda *args, **kwargs: response(primary_vnic)
        )

        resolved = self.namespace[
            "get_compute_cluster_instances_for_cleanup"
        ](
            COMPARTMENT_ID,
            COMPUTE_CLUSTER_ID,
            CLUSTER_NAME,
        )

        self.assertEqual(resolved, [{
            "display_name": "stopped-worker",
            "ip": "10.0.0.10",
            "ocid": INSTANCE_ID_ONE,
        }])
        self.assertEqual(
            list_instances.call_args.kwargs["compute_cluster_id"],
            COMPUTE_CLUSTER_ID,
        )


class ComputeClusterDisplayNameApiTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def test_updates_instance_and_only_explicit_primary_vnic(self):
        instance = compute_instance(
            INSTANCE_ID_ONE,
            "generated-one",
            "10.0.0.10",
        )
        instances_by_id, primary_vnics, secondary_vnics, _, _ = (
            configure_complete_compute_cluster_api(
                self.namespace,
                [instance],
            )
        )
        primary = primary_vnics[INSTANCE_ID_ONE]
        secondary = secondary_vnics[INSTANCE_ID_ONE]
        original_primary_label = primary.hostname_label
        original_secondary = dict(secondary.__dict__)
        instance_updates = []
        vnic_updates = []

        def complete_snapshot(*args, **kwargs):
            current = member_dict(instance, primary.private_ip)
            return [current], {INSTANCE_ID_ONE: current}

        def update_instance(instance_id, details, **kwargs):
            instance_updates.append((instance_id, details, kwargs))
            instances_by_id[instance_id].display_name = details.display_name
            return response(instances_by_id[instance_id])

        def update_vnic(vnic_id, details, **kwargs):
            vnic_updates.append((vnic_id, details, kwargs))
            self.assertEqual(vnic_id, primary.id)
            primary.display_name = details.display_name
            return response(primary)

        self.namespace["get_complete_compute_cluster_instances"] = mock.Mock(
            side_effect=complete_snapshot
        )
        self.namespace["computeClient"].update_instance = update_instance
        self.namespace["virtualNetworkClient"].update_vnic = update_vnic
        before_updates = mock.Mock()

        previous = self.namespace["update_instance_pool_display_names"](
            COMPARTMENT_ID,
            None,
            CLUSTER_NAME,
            {INSTANCE_ID_ONE: "actual-ansible-host"},
            max_wait_seconds=0,
            before_updates=before_updates,
            expected_private_ips_by_instance_id={
                INSTANCE_ID_ONE: "10.0.0.10"
            },
            compute_cluster_id=COMPUTE_CLUSTER_ID,
        )

        self.assertEqual(previous, {INSTANCE_ID_ONE: "generated-one"})
        before_updates.assert_called_once_with(
            {INSTANCE_ID_ONE: "generated-one"}
        )
        self.assertEqual(instance.display_name, "actual-ansible-host")
        self.assertEqual(primary.display_name, "actual-ansible-host")
        self.assertEqual(primary.hostname_label, original_primary_label)
        self.assertEqual(secondary.__dict__, original_secondary)
        self.assertEqual(len(instance_updates), 1)
        self.assertEqual(len(vnic_updates), 1)
        self.assertFalse(hasattr(vnic_updates[0][1], "hostname_label"))

    def test_live_membership_mismatch_fails_before_any_mutation(self):
        instance = compute_instance(
            INSTANCE_ID_ONE,
            "generated-one",
            "10.0.0.10",
        )
        configure_complete_compute_cluster_api(self.namespace, [instance])
        current = member_dict(instance, "10.0.0.10")
        self.namespace["get_complete_compute_cluster_instances"] = mock.Mock(
            return_value=([current], {INSTANCE_ID_ONE: current})
        )
        instance_update = self.namespace["computeClient"].update_instance = mock.Mock()
        vnic_update = self.namespace["virtualNetworkClient"].update_vnic = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "membership|member"):
            self.namespace["update_instance_pool_display_names"](
                COMPARTMENT_ID,
                None,
                CLUSTER_NAME,
                {
                    INSTANCE_ID_ONE: "worker-one",
                    INSTANCE_ID_TWO: "worker-two",
                },
                max_wait_seconds=0,
                compute_cluster_id=COMPUTE_CLUSTER_ID,
            )

        instance_update.assert_not_called()
        vnic_update.assert_not_called()

    def test_all_primary_vnics_are_preflighted_before_first_mutation(self):
        first = compute_instance(
            INSTANCE_ID_ONE,
            "generated-one",
            "10.0.0.10",
        )
        second = compute_instance(
            INSTANCE_ID_TWO,
            "generated-two",
            "10.0.0.11",
        )
        _, primary_vnics, _, _, _ = configure_complete_compute_cluster_api(
            self.namespace,
            [first, second],
        )
        primary_vnics[INSTANCE_ID_TWO].is_primary = False
        current = {
            INSTANCE_ID_ONE: member_dict(first, "10.0.0.10"),
            INSTANCE_ID_TWO: member_dict(second, "10.0.0.11"),
        }
        self.namespace["get_complete_compute_cluster_instances"] = mock.Mock(
            return_value=(list(current.values()), current)
        )
        instance_update = self.namespace["computeClient"].update_instance = mock.Mock()
        vnic_update = self.namespace["virtualNetworkClient"].update_vnic = mock.Mock()
        before_updates = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "primary VNIC"):
            self.namespace["update_instance_pool_display_names"](
                COMPARTMENT_ID,
                None,
                CLUSTER_NAME,
                {
                    INSTANCE_ID_ONE: "worker-one",
                    INSTANCE_ID_TWO: "worker-two",
                },
                max_wait_seconds=0,
                before_updates=before_updates,
                compute_cluster_id=COMPUTE_CLUSTER_ID,
            )

        before_updates.assert_not_called()
        instance_update.assert_not_called()
        vnic_update.assert_not_called()


class ComputeClusterSynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def current_members(self):
        return {
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

    def test_compute_inventory_must_exactly_match_live_membership(self):
        current = self.current_members()
        configure_sync_dependencies(self.namespace, current)
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock()
        updater = self.namespace["update_instance_pool_display_names"] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(
                directory,
                {INSTANCE_ID_ONE: observations()[INSTANCE_ID_ONE]},
            )
            with self.assertRaisesRegex(RuntimeError, "membership|exactly"):
                self.namespace["synchronize_instance_pool_names"](
                    COMPARTMENT_ID,
                    None,
                    inventory_path,
                    CLUSTER_NAME,
                    observed_hostnames_by_instance_id={
                        INSTANCE_ID_ONE: observations()[INSTANCE_ID_ONE]
                    },
                    max_wait_seconds=0,
                    compute_cluster_id=COMPUTE_CLUSTER_ID,
                )

        collector.assert_not_called()
        updater.assert_not_called()

    def test_duplicate_actual_hostnames_fail_before_oci_mutation(self):
        current = self.current_members()
        configure_sync_dependencies(self.namespace, current)
        updater = self.namespace["update_instance_pool_display_names"] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
            )
            with self.assertRaisesRegex(RuntimeError, "unique|duplicate"):
                self.namespace["synchronize_instance_pool_names"](
                    COMPARTMENT_ID,
                    None,
                    inventory_path,
                    CLUSTER_NAME,
                    observed_hostnames_by_instance_id=observations(
                        "Same-Host",
                        "same-host",
                    ),
                    max_wait_seconds=0,
                    compute_cluster_id=COMPUTE_CLUSTER_ID,
                )

        updater.assert_not_called()

    def test_empty_compute_cluster_is_a_safe_noop(self):
        current = {}
        configure_sync_dependencies(self.namespace, current)
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(directory, {})
            synchronized, rewritten = self.namespace[
                "synchronize_instance_pool_names"
            ](
                COMPARTMENT_ID,
                None,
                inventory_path,
                CLUSTER_NAME,
                observed_hostnames_by_instance_id={},
                max_wait_seconds=0,
                compute_cluster_id=COMPUTE_CLUSTER_ID,
            )

        self.assertEqual(synchronized, [])
        self.assertEqual(
            rewritten.get("compute_configured", []),
            [],
        )
        collector.assert_not_called()

    def test_partial_update_persists_v3_plan_and_retry_reuses_observations(self):
        current = self.current_members()
        configure_sync_dependencies(self.namespace, current)
        collector = self.namespace["collect_instance_pool_os_hostnames"] = mock.Mock(
            side_effect=AssertionError("a v3 retry must not recollect host facts")
        )
        attempts = []

        def update_names(*args, **kwargs):
            self.assertIsNone(args[1])
            self.assertEqual(
                kwargs.get("compute_cluster_id"),
                COMPUTE_CLUSTER_ID,
            )
            desired = args[3]
            previous = {
                instance_id: member["display_name"]
                for instance_id, member in current.items()
            }
            callback = kwargs.get("before_updates")
            if callback is not None:
                callback(previous)
            attempts.append(dict(desired))
            if len(attempts) == 1:
                current[INSTANCE_ID_ONE]["display_name"] = desired[
                    INSTANCE_ID_ONE
                ]
                raise RuntimeError("simulated partial Compute Cluster update")
            for instance_id, desired_name in desired.items():
                current[instance_id]["display_name"] = desired_name
            return previous

        self.namespace["update_instance_pool_display_names"] = update_names

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
            )
            plan_path = self.namespace[
                "get_instance_pool_hostname_sync_plan_path"
            ](inventory_path)
            with self.assertRaisesRegex(RuntimeError, "partial Compute Cluster"):
                self.namespace["synchronize_instance_pool_names"](
                    COMPARTMENT_ID,
                    None,
                    inventory_path,
                    CLUSTER_NAME,
                    observed_hostnames_by_instance_id=observations(
                        "custom-render-one",
                        "custom-render-two",
                    ),
                    max_wait_seconds=0,
                    compute_cluster_id=COMPUTE_CLUSTER_ID,
                )
            self.assertTrue(os.path.isfile(plan_path))
            with open(plan_path, encoding="utf-8") as plan_file:
                plan = json.load(plan_file)
            self.assertEqual(plan["version"], 3)
            self.assertEqual(plan["deployment_type"], "CC")
            self.assertEqual(plan["compute_cluster_id"], COMPUTE_CLUSTER_ID)
            self.assertNotIn("instance_pool_id", plan)

            synchronized, _ = self.namespace[
                "synchronize_instance_pool_names"
            ](
                COMPARTMENT_ID,
                None,
                inventory_path,
                CLUSTER_NAME,
                max_wait_seconds=0,
                compute_cluster_id=COMPUTE_CLUSTER_ID,
            )
            self.assertFalse(os.path.exists(plan_path))
            with open(inventory_path, encoding="utf-8") as inventory_file:
                rewritten = inventory_file.read()

        self.assertEqual(len(attempts), 2)
        collector.assert_not_called()
        self.assertIn("custom-render-one ansible_host=10.0.0.10", rewritten)
        self.assertIn("custom-render-two ansible_host=10.0.0.11", rewritten)
        self.assertEqual(
            {member["display_name"] for member in synchronized},
            {"custom-render-one", "custom-render-two"},
        )

    def test_dispatcher_selects_compute_cluster_by_exact_state_identity(self):
        tracked = compute_cluster()
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=mock.Mock(return_value=response(tracked))
        )
        synchronize = self.namespace["synchronize_instance_pool_names"] = mock.Mock(
            return_value=([{"ocid": INSTANCE_ID_ONE}], {"compute_configured": []})
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(directory)
            inventory_path = write_compute_cluster_inventory(
                directory,
                {INSTANCE_ID_ONE: observations()[INSTANCE_ID_ONE]},
            )
            result = self.namespace["synchronize_autoscaling_compute_names"](
                COMPARTMENT_ID,
                inventory_path,
                CLUSTER_NAME,
                expected_compute_cluster_id=COMPUTE_CLUSTER_ID,
                observed_hostnames_by_instance_id={
                    INSTANCE_ID_ONE: observations()[INSTANCE_ID_ONE]
                },
                max_wait_seconds=0,
            )

        self.assertEqual(result[0], [{"ocid": INSTANCE_ID_ONE}])
        self.assertEqual(
            synchronize.call_args.kwargs["compute_cluster_id"],
            COMPUTE_CLUSTER_ID,
        )
        self.assertIsNone(synchronize.call_args.args[1])

    def test_dispatcher_rejects_mixed_compute_deployment_state(self):
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=mock.Mock()
        )
        synchronize = self.namespace["synchronize_instance_pool_names"] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(directory)
            state_path = os.path.join(directory, "terraform.tfstate")
            with open(state_path, encoding="utf-8") as state_file:
                state = json.load(state_file)
            state["resources"].append({
                "mode": "managed",
                "type": "oci_core_instance_pool",
                "name": "instance_pool",
                "instances": [{
                    "attributes": {"id": "ocid1.instancepool.mixed"}
                }],
            })
            with open(state_path, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file)
            inventory_path = write_compute_cluster_inventory(directory, {})
            with self.assertRaisesRegex(
                RuntimeError,
                "multiple compute deployment types",
            ):
                self.namespace["synchronize_autoscaling_compute_names"](
                    COMPARTMENT_ID,
                    inventory_path,
                    CLUSTER_NAME,
                    expected_compute_cluster_id=COMPUTE_CLUSTER_ID,
                    observed_hostnames_by_instance_id={},
                    max_wait_seconds=0,
                )

        synchronize.assert_not_called()


class ComputeClusterLaunchDisplayNameTests(unittest.TestCase):
    class Model:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AgentConfig:
        pass

    def setUp(self):
        self.namespace = load_resize_functions()
        models = self.namespace["oci"].core.models
        models.LaunchInstanceAgentConfigDetails = type(
            "LaunchInstanceAgentConfigDetails",
            (),
            {},
        )
        models.CreateVnicDetails = self.Model
        models.LaunchInstanceShapeConfigDetails = self.Model
        models.LaunchInstanceDetails = self.Model
        self.namespace["cluster_name"] = CLUSTER_NAME

    def test_pending_name_is_unique_and_does_not_parse_an_ansible_hostname(self):
        generated = [
            self.namespace["generate_compute_cluster_launch_display_name"](
                CLUSTER_NAME
            )
            for _ in range(2)
        ]
        for name in generated:
            self.assertRegex(
                name,
                r"^cc-node-pending-[0-9a-f]{32}$",
            )
            self.assertLessEqual(len(name), 63)
        self.assertNotEqual(generated[0], generated[1])

    def test_pending_name_stays_dns_safe_for_long_cluster_names(self):
        name = self.namespace[
            "generate_compute_cluster_launch_display_name"
        ]("cluster-"+("x"*200))

        self.assertEqual(len(name), len("cc-node-pending-")+32)
        self.assertEqual(
            self.namespace["validate_os_hostname"](name),
            name,
        )

        existing = compute_instance(
            INSTANCE_ID_ONE,
            # Deliberately arbitrary and without a numeric suffix.  This is
            # the expected state after syncing to the final Ansible hostname.
            "actual-ansible-host",
            "10.0.0.10",
        )
        existing.agent_config = self.AgentConfig()
        existing.shape_config = SimpleNamespace(
            baseline_ocpu_utilization="BASELINE_1_1",
            memory_in_gbs=128,
            local_disks=0,
            ocpus=16,
        )
        existing.availability_domain = "AD-1"
        existing.shape = "BM.Optimized3.36"
        existing.source_details = SimpleNamespace(source_id="ocid1.image.test")
        existing.metadata = {"ssh_authorized_keys": "test-key"}
        existing.defined_tags = {"hpc-cost": {"User": "alice"}}
        primary_vnic = SimpleNamespace(
            id="ocid1.vnic.primary",
            is_primary=True,
            lifecycle_state="AVAILABLE",
            private_ip="10.0.0.10",
            subnet_id="ocid1.subnet.test",
        )
        self.namespace["computeClient"] = SimpleNamespace(
            list_vnic_attachments=lambda *args, **kwargs: response([
                SimpleNamespace(
                    lifecycle_state="ATTACHED",
                    vnic_id=primary_vnic.id,
                )
            ])
        )
        self.namespace["virtualNetworkClient"] = SimpleNamespace(
            get_vnic=lambda *args, **kwargs: response(primary_vnic)
        )
        local_volume = {
            "enabled": False,
            "size_in_gbs": 1000,
            "vpus_per_gb": 10,
            "mount_point": "/scratch",
        }

        pending_name = self.namespace[
            "generate_compute_cluster_launch_display_name"
        ](CLUSTER_NAME)

        launch = self.namespace["getLaunchInstanceDetails"](
            existing,
            COMPARTMENT_ID,
            COMPUTE_CLUSTER_ID,
            pending_name,
            local_volume,
        )

        self.assertEqual(launch.display_name, pending_name)
        self.assertNotIn("actual-ansible-host", launch.display_name)
        self.assertEqual(launch.defined_tags["hpc-cost"]["User"], "Management")
        self.assertEqual(existing.defined_tags["hpc-cost"]["User"], "alice")

    def test_rollback_uses_the_launch_response_ocid_not_a_temporary_name(self):
        launched = compute_instance(
            INSTANCE_ID_ONE,
            "final-hostname-may-already-have-changed",
            "10.0.0.10",
        )
        get_instance = mock.Mock(return_value=response(launched))
        name_lookup = self.namespace["find_cluster_instance_by_name"] = mock.Mock(
            side_effect=AssertionError("rollback must not rediscover by display name")
        )
        terminate = self.namespace[
            "terminate_instance_and_delete_launch_volumes"
        ] = mock.Mock()
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=get_instance
        )
        self.namespace["comp_ocid"] = COMPARTMENT_ID
        self.namespace["cluster_name"] = CLUSTER_NAME

        self.namespace["rollback_compute_cluster_instances"]([{
            "ocid": INSTANCE_ID_ONE,
            "display_name": "temporary-pending-name",
        }])

        get_instance.assert_called_once_with(INSTANCE_ID_ONE)
        name_lookup.assert_not_called()
        terminate.assert_called_once_with(INSTANCE_ID_ONE)


class ComputeClusterRemovalPlanSafetyTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def configure_plan_dependencies(self):
        initial = compute_instance(
            INSTANCE_ID_ONE,
            "generated-one",
            "10.0.0.10",
        )
        added = compute_instance(
            INSTANCE_ID_TWO,
            "generated-two",
            "10.0.0.11",
        )
        instances_by_id = {
            initial.id: initial,
            added.id: added,
        }
        get_instance = mock.Mock(
            side_effect=lambda instance_id, **kwargs: response(
                instances_by_id[instance_id]
            )
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=mock.Mock(
                return_value=response(compute_cluster())
            ),
            get_instance=get_instance,
        )
        primary_ip = self.namespace[
            "get_instance_primary_private_ip"
        ] = mock.Mock(
            side_effect=lambda compartment_id, instance_id, **kwargs: (
                "10.0.0.10"
                if instance_id == INSTANCE_ID_ONE
                else "10.0.0.11"
            )
        )
        volume_check = self.namespace[
            "instance_has_exclusive_managed_local_block_volume"
        ] = mock.Mock(return_value=False)
        current_instances = [
            member_dict(initial, "10.0.0.10"),
            member_dict(added, "10.0.0.11"),
        ]
        return current_instances, get_instance, primary_ip, volume_check

    def test_routine_plan_allows_only_state_external_compute_cluster_member(self):
        current_instances, get_instance, primary_ip, volume_check = (
            self.configure_plan_dependencies()
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(
                directory,
                tracked_instance_ids=(INSTANCE_ID_ONE,),
            )
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            plan = self.namespace["build_compute_cluster_removal_plan"](
                inventory,
                COMPARTMENT_ID,
                COMPUTE_CLUSTER_ID,
                CLUSTER_NAME,
                current_instances,
                {INSTANCE_ID_TWO},
                inventory_path=inventory_path,
            )

        self.assertEqual(
            plan,
            [{
                "instance_id": INSTANCE_ID_TWO,
                "instance_display_name": "generated-two",
                "instance_names": ["generated-two"],
                "private_ip": "10.0.0.11",
                "delete_launch_created_data_volumes": False,
            }],
        )
        get_instance.assert_called_once()
        self.assertEqual(get_instance.call_args.args[0], INSTANCE_ID_TWO)
        primary_ip.assert_called_once()
        volume_check.assert_called_once_with(
            COMPARTMENT_ID,
            INSTANCE_ID_TWO,
        )

    def test_routine_plan_rejects_terraform_tracked_initial_member_preflight(self):
        current_instances, get_instance, primary_ip, volume_check = (
            self.configure_plan_dependencies()
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(
                directory,
                tracked_instance_ids=(INSTANCE_ID_ONE,),
            )
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            with self.assertRaisesRegex(
                RuntimeError,
                "Terraform|state|tracked",
            ):
                self.namespace["build_compute_cluster_removal_plan"](
                    inventory,
                    COMPARTMENT_ID,
                    COMPUTE_CLUSTER_ID,
                    CLUSTER_NAME,
                    current_instances,
                    {INSTANCE_ID_ONE},
                    inventory_path=inventory_path,
                )

        get_instance.assert_not_called()
        primary_ip.assert_not_called()
        volume_check.assert_not_called()

    def test_routine_plan_requires_existing_exact_state_parent_identity(self):
        invalid_state_configurations = [
            {
                "compute_cluster_ids": (),
                "tracked_instance_ids": (INSTANCE_ID_ONE,),
                "write_state": True,
            },
            {
                "compute_cluster_ids": ("ocid1.computecluster.other",),
                "tracked_instance_ids": (INSTANCE_ID_ONE,),
                "write_state": True,
            },
            {
                "write_state": False,
            },
        ]

        for state_configuration in invalid_state_configurations:
            with self.subTest(state_configuration=state_configuration):
                namespace = load_resize_functions()
                initial = compute_instance(
                    INSTANCE_ID_ONE,
                    "generated-one",
                    "10.0.0.10",
                )
                added = compute_instance(
                    INSTANCE_ID_TWO,
                    "generated-two",
                    "10.0.0.11",
                )
                get_instance = mock.Mock()
                namespace["computeClient"] = SimpleNamespace(
                    get_compute_cluster=mock.Mock(
                        return_value=response(compute_cluster())
                    ),
                    get_instance=get_instance,
                )
                primary_ip = namespace[
                    "get_instance_primary_private_ip"
                ] = mock.Mock()
                volume_check = namespace[
                    "instance_has_exclusive_managed_local_block_volume"
                ] = mock.Mock()
                current_instances = [
                    member_dict(initial, "10.0.0.10"),
                    member_dict(added, "10.0.0.11"),
                ]

                with tempfile.TemporaryDirectory() as directory:
                    if state_configuration["write_state"]:
                        write_compute_cluster_state(
                            directory,
                            compute_cluster_ids=state_configuration[
                                "compute_cluster_ids"
                            ],
                            tracked_instance_ids=state_configuration[
                                "tracked_instance_ids"
                            ],
                        )
                    inventory_path = write_compute_cluster_inventory(
                        directory,
                        observations(),
                    )
                    inventory = namespace["parse_inventory"](inventory_path)

                    with self.assertRaisesRegex(
                        RuntimeError,
                        "Terraform|state|Compute Cluster",
                    ):
                        namespace["build_compute_cluster_removal_plan"](
                            inventory,
                            COMPARTMENT_ID,
                            COMPUTE_CLUSTER_ID,
                            CLUSTER_NAME,
                            current_instances,
                            {INSTANCE_ID_TWO},
                            inventory_path=inventory_path,
                        )

                get_instance.assert_not_called()
                primary_ip.assert_not_called()
                volume_check.assert_not_called()


class ComputeClusterDnsSafetyTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def ownership_document(self, rrsets, terraform_state_released=True):
        return self.namespace["build_name_dns_ownership_document"](
            CLUSTER_NAME,
            rrsets,
            compute_cluster_id=COMPUTE_CLUSTER_ID,
            terraform_state_released=terraform_state_released,
        )

    def final_name_rrset(self, hostname, private_ip):
        return {
            "zone_id": DNS_ZONE_ID,
            "zone_name": DNS_ZONE_NAME,
            "domain": hostname+"."+DNS_ZONE_NAME,
            "private_ips": [private_ip],
        }

    def test_v2_ownership_ledger_requires_exact_compute_cluster_identity(self):
        document = self.ownership_document([
            self.final_name_rrset("worker-one", "10.0.0.10")
        ])

        self.assertEqual(document["version"], 2)
        self.assertEqual(document["deployment_type"], "CC")
        self.assertEqual(document["compute_cluster_id"], COMPUTE_CLUSTER_ID)
        self.namespace["validate_instance_pool_dns_ownership_identity"](
            document,
            CLUSTER_NAME,
            compute_cluster_id=COMPUTE_CLUSTER_ID,
        )
        for arguments in [
            {
                "expected_cluster_name": "another-cluster",
                "compute_cluster_id": COMPUTE_CLUSTER_ID,
            },
            {
                "expected_cluster_name": CLUSTER_NAME,
                "compute_cluster_id": "ocid1.computecluster.other",
            },
            {
                "expected_cluster_name": CLUSTER_NAME,
                "instance_pool_id": "ocid1.instancepool.wrong-type",
            },
        ]:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(RuntimeError, "another|Compute Cluster"):
                    self.namespace[
                        "validate_instance_pool_dns_ownership_identity"
                    ](document, **arguments)

        malformed = json.loads(json.dumps(document))
        malformed["instance_pool_id"] = "ocid1.instancepool.mixed"
        with self.assertRaisesRegex(RuntimeError, "Compute Cluster|malformed"):
            self.namespace["validate_instance_pool_name_dns_ownership"](
                malformed
            )

        pending_transfer = self.ownership_document(
            [self.final_name_rrset("worker-one", "10.0.0.10")],
            terraform_state_released=False,
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.namespace["validate_instance_pool_dns_ownership_identity"](
                pending_transfer,
                CLUSTER_NAME,
                compute_cluster_id=COMPUTE_CLUSTER_ID,
            )

    def test_cluster_cleanup_preflights_ledger_and_dynamic_slurm_then_preserves_terraform_slurm(self):
        managed_slurm_domain = "batch-HPC_instance-11."+DNS_ZONE_NAME
        dynamic_slurm_domain = "batch-HPC_instance-12."+DNS_ZONE_NAME
        ownership = self.ownership_document([
            self.final_name_rrset("worker-one", "10.0.0.10"),
            self.final_name_rrset("worker-two", "10.0.0.11"),
        ])
        events = []

        def verify(zone_id, domain, expected_private_ips):
            events.append(("verify", zone_id, domain, set(expected_private_ips)))
            return set(expected_private_ips)

        def delete(zone_id, domain, expected_private_ips):
            events.append(("delete", zone_id, domain, set(expected_private_ips)))
            return True

        self.namespace["verify_private_dns_a_rrset_ownership"] = verify
        self.namespace["delete_private_dns_a_rrset_if_owned"] = delete
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: response([
                SimpleNamespace(id=DNS_ZONE_ID)
            ])
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(
                directory,
                tracked_instance_ids=(INSTANCE_ID_ONE, INSTANCE_ID_TWO),
                slurm_rrsets=[{
                    "domain": managed_slurm_domain,
                    "private_ip": "10.0.0.10",
                }],
            )
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
                dns_entries=True,
                slurm=True,
            )
            self.namespace["write_instance_pool_name_dns_ownership"](
                inventory_path,
                ownership,
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            self.namespace["cleanup_compute_cluster_name_dns_records"](
                COMPARTMENT_ID,
                inventory,
                inventory_path,
                COMPUTE_CLUSTER_ID,
                live_instances=[
                    {"ocid": INSTANCE_ID_ONE, "ip": "10.0.0.10"},
                    {"ocid": INSTANCE_ID_TWO, "ip": "10.0.0.11"},
                ],
            )

            ownership_path = self.namespace[
                "get_instance_pool_name_dns_ownership_path"
            ](inventory_path)
            self.assertFalse(os.path.exists(ownership_path))

        first_delete = next(
            index for index, event in enumerate(events) if event[0] == "delete"
        )
        self.assertTrue(all(event[0] == "verify" for event in events[:first_delete]))
        deleted = {
            event[2]: event[3] for event in events if event[0] == "delete"
        }
        self.assertEqual(
            deleted,
            {
                "worker-one."+DNS_ZONE_NAME: {"10.0.0.10"},
                "worker-two."+DNS_ZONE_NAME: {"10.0.0.11"},
                dynamic_slurm_domain: {"10.0.0.11"},
            },
        )
        self.assertNotIn(managed_slurm_domain, deleted)

    def test_cluster_cleanup_dns_conflict_aborts_before_first_deletion(self):
        worker_one_domain = "worker-one."+DNS_ZONE_NAME
        worker_two_domain = "worker-two."+DNS_ZONE_NAME
        ownership = self.ownership_document([
            self.final_name_rrset("worker-one", "10.0.0.10"),
            self.final_name_rrset("worker-two", "10.0.0.11"),
        ])
        inspected_domains = []
        delete_rr_set = mock.Mock()

        def get_rr_set(**kwargs):
            domain = kwargs["domain"]
            inspected_domains.append(domain)
            private_ip = (
                "10.0.0.10"
                if domain == worker_one_domain
                else "10.0.0.99"
            )
            return response(SimpleNamespace(items=[
                SimpleNamespace(rtype="A", rdata=private_ip)
            ]))

        self.namespace["dns_client"] = SimpleNamespace(
            get_rr_set=get_rr_set,
            delete_rr_set=delete_rr_set,
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(directory)
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
                dns_entries=True,
                slurm=False,
            )
            self.namespace["write_instance_pool_name_dns_ownership"](
                inventory_path,
                ownership,
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                self.namespace["cleanup_compute_cluster_name_dns_records"](
                    COMPARTMENT_ID,
                    inventory,
                    inventory_path,
                    COMPUTE_CLUSTER_ID,
                )

            self.assertIsNotNone(
                self.namespace["load_instance_pool_name_dns_ownership"](
                    inventory_path
                )
            )

        self.assertEqual(
            inspected_domains,
            [worker_one_domain, worker_two_domain],
        )
        delete_rr_set.assert_not_called()

    def test_full_cleanup_without_ledger_treats_missing_dns_zone_as_noop(self):
        list_zones = mock.Mock(return_value=response([]))
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=list_zones,
        )
        verify = self.namespace[
            "verify_private_dns_a_rrset_ownership"
        ] = mock.Mock()
        delete = self.namespace[
            "delete_private_dns_a_rrset_if_owned"
        ] = mock.Mock()
        exact_zone_lookup = self.namespace[
            "get_single_private_dns_zone_id"
        ] = mock.Mock(
            side_effect=AssertionError(
                "an already deleted private DNS zone must be an idempotent no-op"
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(directory)
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
                dns_entries=True,
                slurm=True,
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            self.namespace["cleanup_compute_cluster_name_dns_records"](
                COMPARTMENT_ID,
                inventory,
                inventory_path,
                COMPUTE_CLUSTER_ID,
            )

        self.assertEqual(list_zones.call_count, 2)
        exact_zone_lookup.assert_not_called()
        verify.assert_not_called()
        delete.assert_not_called()

    def test_single_node_deletion_removes_only_its_ledger_entry_and_dynamic_slurm(self):
        ownership = self.ownership_document([
            self.final_name_rrset("worker-one", "10.0.0.10"),
            self.final_name_rrset("worker-two", "10.0.0.11"),
        ])
        events = []

        def verify(zone_id, domain, expected_private_ips):
            events.append(("verify", domain, set(expected_private_ips)))
            return set(expected_private_ips)

        def delete(zone_id, domain, expected_private_ips):
            events.append(("delete", domain, set(expected_private_ips)))
            return True

        self.namespace["verify_private_dns_a_rrset_ownership"] = verify
        self.namespace["delete_private_dns_a_rrset_if_owned"] = delete
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: response([
                SimpleNamespace(id=DNS_ZONE_ID)
            ])
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=lambda *args, **kwargs: response(
                compute_cluster()
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(directory)
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
                dns_entries=True,
                slurm=True,
            )
            self.namespace["write_instance_pool_name_dns_ownership"](
                inventory_path,
                ownership,
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            preflight_changed = self.namespace[
                "delete_compute_cluster_node_name_dns_records"
            ](
                COMPARTMENT_ID,
                inventory,
                inventory_path,
                COMPUTE_CLUSTER_ID,
                {"generated-one", "worker-one"},
                "10.0.0.10",
                preflight_only=True,
            )
            preflight_ledger = self.namespace[
                "load_instance_pool_name_dns_ownership"
            ](inventory_path)
            self.assertTrue(preflight_changed)
            self.assertEqual(len(preflight_ledger["rrsets"]), 2)
            self.assertTrue(events)
            self.assertTrue(all(event[0] == "verify" for event in events))
            events.clear()

            changed = self.namespace[
                "delete_compute_cluster_node_name_dns_records"
            ](
                COMPARTMENT_ID,
                inventory,
                inventory_path,
                COMPUTE_CLUSTER_ID,
                {"generated-one", "worker-one"},
                "10.0.0.10",
            )
            remaining = self.namespace[
                "load_instance_pool_name_dns_ownership"
            ](inventory_path)

        self.assertTrue(changed)
        self.assertEqual(
            [rrset["domain"] for rrset in remaining["rrsets"]],
            ["worker-two."+DNS_ZONE_NAME],
        )
        first_delete = next(
            index for index, event in enumerate(events) if event[0] == "delete"
        )
        self.assertTrue(all(event[0] == "verify" for event in events[:first_delete]))
        self.assertEqual(
            {event[1] for event in events if event[0] == "delete"},
            {
                "worker-one."+DNS_ZONE_NAME,
                "batch-HPC_instance-11."+DNS_ZONE_NAME,
            },
        )
        self.assertNotIn(
            "worker-two."+DNS_ZONE_NAME,
            {event[1] for event in events},
        )

    def test_routine_node_deletion_rejects_terraform_managed_slurm_rrset(self):
        slurm_domain = "batch-HPC_instance-11."+DNS_ZONE_NAME
        ownership = self.ownership_document([
            self.final_name_rrset("worker-one", "10.0.0.10")
        ])
        events = []

        def verify(zone_id, domain, expected_private_ips):
            events.append(("verify", zone_id, domain, set(expected_private_ips)))
            return set(expected_private_ips)

        def delete(zone_id, domain, expected_private_ips):
            events.append(("delete", zone_id, domain, set(expected_private_ips)))
            return True

        self.namespace["verify_private_dns_a_rrset_ownership"] = verify
        self.namespace["delete_private_dns_a_rrset_if_owned"] = delete
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=lambda *args, **kwargs: response(
                compute_cluster()
            )
        )
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: response([
                SimpleNamespace(id=DNS_ZONE_ID)
            ])
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(
                directory,
                slurm_rrsets=[{
                    "domain": slurm_domain,
                    "private_ip": "10.0.0.10",
                }],
            )
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
                dns_entries=True,
                slurm=True,
            )
            self.namespace["write_instance_pool_name_dns_ownership"](
                inventory_path,
                ownership,
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            with self.assertRaisesRegex(RuntimeError, "Terraform-managed Slurm"):
                self.namespace[
                    "delete_compute_cluster_node_name_dns_records"
                ](
                    COMPARTMENT_ID,
                    inventory,
                    inventory_path,
                    COMPUTE_CLUSTER_ID,
                    {"generated-one", "worker-one"},
                    "10.0.0.10",
                )

        self.assertFalse(any(event[0] == "delete" for event in events))

    def test_terraform_slurm_rdata_mismatch_blocks_routine_node_deletion(self):
        slurm_domain = "batch-HPC_instance-11."+DNS_ZONE_NAME
        delete = self.namespace[
            "delete_private_dns_a_rrset_if_owned"
        ] = mock.Mock()
        self.namespace["verify_private_dns_a_rrset_ownership"] = mock.Mock()
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=lambda *args, **kwargs: response(
                compute_cluster()
            )
        )
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: response([
                SimpleNamespace(id=DNS_ZONE_ID)
            ])
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(
                directory,
                slurm_rrsets=[{
                    "domain": slurm_domain,
                    "private_ip": "10.0.0.99",
                }],
            )
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
                dns_entries=True,
                slurm=True,
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            with self.assertRaisesRegex(RuntimeError, "Terraform-managed Slurm"):
                self.namespace[
                    "delete_compute_cluster_node_name_dns_records"
                ](
                    COMPARTMENT_ID,
                    inventory,
                    inventory_path,
                    COMPUTE_CLUSTER_ID,
                    {"generated-one", "worker-one"},
                    "10.0.0.10",
                )

        delete.assert_not_called()

    def test_legacy_node_deletion_preflights_exact_canonical_names_and_ip(self):
        events = []

        def verify(zone_id, domain, expected_private_ips):
            events.append(("verify", zone_id, domain, set(expected_private_ips)))
            return set(expected_private_ips)

        def delete(zone_id, domain, expected_private_ips):
            events.append(("delete", zone_id, domain, set(expected_private_ips)))
            return True

        self.namespace["verify_private_dns_a_rrset_ownership"] = verify
        self.namespace["delete_private_dns_a_rrset_if_owned"] = delete
        self.namespace["computeClient"] = SimpleNamespace(
            get_compute_cluster=lambda *args, **kwargs: response(
                compute_cluster()
            )
        )
        self.namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: response([
                SimpleNamespace(id=DNS_ZONE_ID)
            ])
        )

        with tempfile.TemporaryDirectory() as directory:
            write_compute_cluster_state(directory)
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
                dns_entries=True,
                slurm=True,
            )
            inventory = self.namespace["parse_inventory"](inventory_path)

            changed = self.namespace[
                "delete_compute_cluster_node_name_dns_records"
            ](
                COMPARTMENT_ID,
                inventory,
                inventory_path,
                COMPUTE_CLUSTER_ID,
                {"legacy-generated", "worker-final"},
                "10.0.0.10",
                preflight_only=True,
            )
            self.assertTrue(changed)
            self.assertTrue(events)
            self.assertTrue(all(event[0] == "verify" for event in events))
            self.assertEqual(
                {
                    (event[1], event[2], frozenset(event[3]))
                    for event in events
                },
                {
                    (
                        DNS_ZONE_ID,
                        "legacy-generated."+DNS_ZONE_NAME,
                        frozenset({"10.0.0.10"}),
                    ),
                    (
                        DNS_ZONE_ID,
                        "worker-final."+DNS_ZONE_NAME,
                        frozenset({"10.0.0.10"}),
                    ),
                    (
                        DNS_ZONE_ID,
                        "batch-HPC_instance-11."+DNS_ZONE_NAME,
                        frozenset({"10.0.0.10"}),
                    ),
                },
            )
            events.clear()

            self.namespace[
                "delete_compute_cluster_node_name_dns_records"
            ](
                COMPARTMENT_ID,
                inventory,
                inventory_path,
                COMPUTE_CLUSTER_ID,
                {"legacy-generated", "worker-final"},
                "10.0.0.10",
            )

        first_delete = next(
            index for index, event in enumerate(events) if event[0] == "delete"
        )
        self.assertTrue(all(event[0] == "verify" for event in events[:first_delete]))
        self.assertEqual(
            {event[2] for event in events if event[0] == "delete"},
            {
                "legacy-generated."+DNS_ZONE_NAME,
                "worker-final."+DNS_ZONE_NAME,
                "batch-HPC_instance-11."+DNS_ZONE_NAME,
            },
        )

    def test_locked_terraform_state_blocks_legacy_compute_nodes_migration_without_write(self):
        old_configuration = (
            'resource "oci_core_instance" "compute_cluster_instances" {\n'
            "  display_name = \"generated\"\n"
            "  lifecycle {\n"
            "    ignore_changes = [\n"
            "      freeform_tags,\n"
            "    ]\n"
            "  }\n"
            "}\n"
        )
        write_configuration = self.namespace[
            "write_text_atomic_preserving_metadata"
        ] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(directory, {})
            compute_nodes_path = os.path.join(directory, "compute-nodes.tf")
            with open(compute_nodes_path, "w", encoding="utf-8") as config_file:
                config_file.write(old_configuration)
            with open(
                os.path.join(directory, ".terraform.tfstate.lock.info"),
                "w",
                encoding="utf-8",
            ) as lock_file:
                lock_file.write("{}\n")

            with self.assertRaisesRegex(RuntimeError, "Terraform state is locked"):
                self.namespace[
                    "migrate_compute_cluster_instance_display_name_management"
                ](inventory_path)
            with open(compute_nodes_path, encoding="utf-8") as config_file:
                persisted = config_file.read()

        self.assertEqual(persisted, old_configuration)
        write_configuration.assert_not_called()


class ComputeClusterNodeRemovalJournalTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def removal_record(
        self,
        instance_id,
        display_name,
        private_ip,
        delete_launch_created_data_volumes,
    ):
        return {
            "instance_id": instance_id,
            "instance_display_name": display_name,
            "instance_names": [display_name, display_name+"-ansible"],
            "private_ip": private_ip,
            "delete_launch_created_data_volumes": (
                delete_launch_created_data_volumes
            ),
        }

    def journal(self, no_reconfigure=True):
        return {
            "version": 1,
            "status": "pending",
            "cluster_name": CLUSTER_NAME,
            "compartment_id": COMPARTMENT_ID,
            "compute_cluster_id": COMPUTE_CLUSTER_ID,
            "no_reconfigure": no_reconfigure,
            "source_instance_ids": [INSTANCE_ID_ONE, INSTANCE_ID_TWO],
            "removals": [
                self.removal_record(
                    INSTANCE_ID_ONE,
                    "worker-one",
                    "10.0.0.10",
                    True,
                ),
                self.removal_record(
                    INSTANCE_ID_TWO,
                    "worker-two",
                    "10.0.0.11",
                    False,
                ),
            ],
        }

    def test_atomic_private_journal_round_trips_every_frozen_removal_field(self):
        document = self.journal(no_reconfigure=True)
        real_replace = os.replace

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(directory, {})
            with mock.patch.object(
                os,
                "replace",
                side_effect=real_replace,
            ) as replace:
                self.namespace[
                    "write_compute_cluster_node_removal_journal"
                ](inventory_path, document)

            journal_path = self.namespace[
                "get_compute_cluster_node_removal_journal_path"
            ](inventory_path)
            self.assertEqual(
                os.path.basename(journal_path),
                ".pending-compute-cluster-node-removals.json",
            )
            self.assertEqual(os.stat(journal_path).st_mode & 0o777, 0o600)
            self.assertEqual(
                self.namespace[
                    "load_compute_cluster_node_removal_journal"
                ](inventory_path),
                document,
            )
            with open(journal_path, encoding="utf-8") as journal_file:
                persisted = json.load(journal_file)

            replace.assert_called_once()
            temporary_path, replaced_path = replace.call_args.args
            self.assertEqual(replaced_path, journal_path)
            self.assertEqual(
                os.path.dirname(temporary_path),
                os.path.dirname(journal_path),
            )
            self.assertFalse(os.path.exists(temporary_path))

        self.assertEqual(persisted, document)
        self.assertEqual(
            persisted["removals"],
            [
                {
                    "instance_id": INSTANCE_ID_ONE,
                    "instance_display_name": "worker-one",
                    "instance_names": ["worker-one", "worker-one-ansible"],
                    "private_ip": "10.0.0.10",
                    "delete_launch_created_data_volumes": True,
                },
                {
                    "instance_id": INSTANCE_ID_TWO,
                    "instance_display_name": "worker-two",
                    "instance_names": ["worker-two", "worker-two-ansible"],
                    "private_ip": "10.0.0.11",
                    "delete_launch_created_data_volumes": False,
                },
            ],
        )
        self.assertTrue(persisted["no_reconfigure"])

    def test_journal_reader_rejects_group_or_world_access(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(directory, {})
            self.namespace[
                "write_compute_cluster_node_removal_journal"
            ](inventory_path, self.journal())
            journal_path = self.namespace[
                "get_compute_cluster_node_removal_journal_path"
            ](inventory_path)
            os.chmod(journal_path, 0o640)

            with self.assertRaisesRegex(RuntimeError, "unsafe|permission"):
                self.namespace[
                    "load_compute_cluster_node_removal_journal"
                ](inventory_path)

    def test_validation_rejects_ambiguous_identity_and_non_boolean_flags(self):
        invalid_documents = []

        wrong_version = self.journal()
        wrong_version["version"] = 2
        invalid_documents.append(wrong_version)

        wrong_status = self.journal()
        wrong_status["status"] = "complete"
        invalid_documents.append(wrong_status)

        missing_cluster_id = self.journal()
        missing_cluster_id["compute_cluster_id"] = ""
        invalid_documents.append(missing_cluster_id)

        numeric_reconfigure_flag = self.journal()
        numeric_reconfigure_flag["no_reconfigure"] = 1
        invalid_documents.append(numeric_reconfigure_flag)

        numeric_volume_flag = self.journal()
        numeric_volume_flag["removals"][0][
            "delete_launch_created_data_volumes"
        ] = 1
        invalid_documents.append(numeric_volume_flag)

        duplicate_instance = self.journal()
        duplicate_instance["removals"][1]["instance_id"] = INSTANCE_ID_ONE
        invalid_documents.append(duplicate_instance)

        duplicate_private_ip = self.journal()
        duplicate_private_ip["removals"][1]["private_ip"] = "10.0.0.10"
        invalid_documents.append(duplicate_private_ip)

        duplicate_display_name = self.journal()
        duplicate_display_name["removals"][1][
            "instance_display_name"
        ] = "worker-one"
        duplicate_display_name["removals"][1]["instance_names"] = [
            "worker-one",
            "worker-two-ansible",
        ]
        invalid_documents.append(duplicate_display_name)

        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(RuntimeError):
                    self.namespace[
                        "validate_compute_cluster_node_removal_journal"
                    ](document)

    def test_resume_uses_only_committed_records_and_clears_after_completion(self):
        document = self.journal(no_reconfigure=True)
        events = []
        inspect = self.namespace[
            "inspect_compute_cluster_node_removal_journal"
        ] = mock.Mock(side_effect=lambda *args: events.append("inspect"))
        preflight = self.namespace[
            "preflight_compute_cluster_node_removal_dns"
        ] = mock.Mock(side_effect=lambda *args: events.append("preflight"))

        def complete(journal, inventory, inventory_path):
            events.append("complete")
            self.assertEqual(journal, document)
            self.assertEqual(
                [record["instance_id"] for record in journal["removals"]],
                [INSTANCE_ID_ONE, INSTANCE_ID_TWO],
            )
            return 2

        self.namespace["complete_compute_cluster_node_removals"] = complete
        destroy_inventory = self.namespace[
            "destroy_unreachable_reconfigure"
        ] = mock.Mock(
            side_effect=AssertionError(
                "the persisted --no_reconfigure choice must be reused"
            )
        )
        real_clear = self.namespace[
            "clear_compute_cluster_node_removal_journal"
        ]

        def clear(inventory_path):
            events.append("clear")
            real_clear(inventory_path)

        self.namespace["clear_compute_cluster_node_removal_journal"] = clear

        with tempfile.TemporaryDirectory() as directory:
            inventory_members = observations()
            inventory_members["ocid1.instance.not-selected"] = {
                "hostname": "new-cli-selection",
                "private_ip": "10.0.0.12",
                "inventory_hostname": "new-cli-selection",
            }
            inventory_path = write_compute_cluster_inventory(
                directory,
                inventory_members,
            )
            self.namespace[
                "write_compute_cluster_node_removal_journal"
            ](inventory_path, document)
            journal_path = self.namespace[
                "get_compute_cluster_node_removal_journal_path"
            ](inventory_path)

            completed = self.namespace[
                "resume_pending_compute_cluster_node_removals"
            ](
                inventory_path,
                CLUSTER_NAME,
                COMPARTMENT_ID,
                COMPUTE_CLUSTER_ID,
                force=True,
            )

            self.assertFalse(os.path.exists(journal_path))

        self.assertEqual(completed, 2)
        self.assertEqual(events, ["inspect", "preflight", "complete", "clear"])
        inspect.assert_called_once_with(
            document,
            CLUSTER_NAME,
            COMPARTMENT_ID,
            COMPUTE_CLUSTER_ID,
        )
        preflight.assert_called_once()
        destroy_inventory.assert_not_called()

    def test_resume_reuses_persisted_reconfigure_choice_and_exact_names(self):
        document = self.journal(no_reconfigure=False)
        self.namespace[
            "inspect_compute_cluster_node_removal_journal"
        ] = mock.Mock()
        self.namespace[
            "preflight_compute_cluster_node_removal_dns"
        ] = mock.Mock()
        complete = self.namespace[
            "complete_compute_cluster_node_removals"
        ] = mock.Mock(return_value=2)
        destroy_inventory = self.namespace[
            "destroy_unreachable_reconfigure"
        ] = mock.Mock(return_value=0)

        with tempfile.TemporaryDirectory() as directory:
            inventory_members = observations()
            inventory_members["ocid1.instance.not-selected"] = {
                "hostname": "new-cli-selection",
                "private_ip": "10.0.0.12",
                "inventory_hostname": "new-cli-selection",
            }
            inventory_path = write_compute_cluster_inventory(
                directory,
                inventory_members,
            )
            self.namespace[
                "write_compute_cluster_node_removal_journal"
            ](inventory_path, document)

            completed = self.namespace[
                "resume_pending_compute_cluster_node_removals"
            ](
                inventory_path,
                CLUSTER_NAME,
                COMPARTMENT_ID,
                COMPUTE_CLUSTER_ID,
            )

        self.assertEqual(completed, 2)
        destroy_inventory.assert_called_once()
        self.assertEqual(
            destroy_inventory.call_args.args[1],
            ["generated-one", "generated-two"],
        )
        self.assertNotIn(
            "new-cli-selection",
            destroy_inventory.call_args.args[1],
        )
        self.assertEqual(complete.call_args.args[0], document)

    def test_resume_failure_keeps_the_whole_journal_for_an_exact_retry(self):
        document = self.journal(no_reconfigure=True)
        self.namespace[
            "inspect_compute_cluster_node_removal_journal"
        ] = mock.Mock()
        self.namespace[
            "preflight_compute_cluster_node_removal_dns"
        ] = mock.Mock()
        self.namespace[
            "complete_compute_cluster_node_removals"
        ] = mock.Mock(
            side_effect=RuntimeError("second termination failed")
        )
        clear = self.namespace[
            "clear_compute_cluster_node_removal_journal"
        ] = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
            )
            self.namespace[
                "write_compute_cluster_node_removal_journal"
            ](inventory_path, document)

            with self.assertRaisesRegex(
                RuntimeError,
                "second termination failed",
            ):
                self.namespace[
                    "resume_pending_compute_cluster_node_removals"
                ](
                    inventory_path,
                    CLUSTER_NAME,
                    COMPARTMENT_ID,
                    COMPUTE_CLUSTER_ID,
                )

            self.assertEqual(
                self.namespace[
                    "load_compute_cluster_node_removal_journal"
                ](inventory_path),
                document,
            )

        clear.assert_not_called()


class ComputeClusterRemovalAnsibleTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

    def test_mixed_multi_node_results_fail_without_committing_inventory(self):
        backup_inventory = self.namespace["backup_inventory"] = mock.Mock()
        write_inventory = self.namespace["write_inventory"] = mock.Mock()
        update_cluster = self.namespace["update_cluster"] = mock.Mock(
            side_effect=[9, 0]
        )
        self.namespace["get_instances"] = mock.Mock(
            side_effect=AssertionError(
                "both removal targets are already resolved from Inventory"
            )
        )
        self.namespace["comp_ocid"] = COMPARTMENT_ID
        self.namespace["cn_ocid"] = COMPUTE_CLUSTER_ID
        self.namespace["CN"] = "CC"

        with tempfile.TemporaryDirectory() as directory:
            inventory_path = write_compute_cluster_inventory(
                directory,
                observations(),
            )
            with open(inventory_path, encoding="utf-8") as inventory_file:
                inventory_before = inventory_file.read()
            real_isfile = os.path.isfile
            with mock.patch.object(
                os.path,
                "isfile",
                side_effect=lambda path: (
                    True
                    if path == "/etc/ansible/hosts"
                    else real_isfile(path)
                ),
            ), mock.patch.object(
                self.namespace["time"],
                "sleep",
            ), mock.patch.object(
                os,
                "remove",
            ) as remove, mock.patch.object(
                os,
                "system",
            ) as system:
                result = self.namespace["destroy_unreachable_reconfigure"](
                    inventory_path,
                    ["generated-one", "generated-two"],
                    "/test/resize_remove_unreachable.yml",
                )
            with open(inventory_path, encoding="utf-8") as inventory_file:
                inventory_after = inventory_file.read()

        self.assertEqual(result, 9)
        backup_inventory.assert_called_once_with(inventory_path)
        self.assertEqual(update_cluster.call_count, 2)
        self.assertEqual(
            [call.kwargs["add_vars"] for call in update_cluster.call_args_list],
            [
                {"unreachable_node_list": "10.0.0.10"},
                {"unreachable_node_list": "10.0.0.11"},
            ],
        )
        self.assertEqual(write_inventory.call_count, 1)
        self.assertEqual(
            write_inventory.call_args.args[0]["compute_configured"],
            [],
        )
        self.assertEqual(inventory_after, inventory_before)
        remove.assert_not_called()
        system.assert_not_called()


class ComputeClusterWiringTests(unittest.TestCase):
    def read(self, *path_parts):
        with open(
            os.path.join(REPOSITORY_ROOT, *path_parts),
            encoding="utf-8",
        ) as source_file:
            return source_file.read()

    def test_terraform_does_not_restore_synced_instance_or_vnic_names(self):
        compute_nodes = self.read(
            "autoscaling",
            "tf_init",
            "compute-nodes.tf",
        )
        lifecycle = compute_nodes.split("lifecycle {", 1)[1]

        self.assertRegex(lifecycle, r"(?m)^\s*display_name,\s*$")
        self.assertRegex(
            lifecycle,
            r"(?m)^\s*create_vnic_details\[0\]\.display_name,\s*$",
        )

    def test_compute_cluster_canonical_name_dns_is_not_owned_by_terraform(self):
        network = self.read("autoscaling", "tf_init", "network.tf")
        resource = network.split(
            'resource "oci_dns_rrset" "rrset-cluster-network-OCI"',
            1,
        )[1].split(
            'resource "oci_dns_rrset" "rrset-cluster-network-SLURM"',
            1,
        )[0]

        self.assertRegex(
            resource,
            r"(?m)^\s*for_each\s*=\s*toset\(\[\]\)\s*$",
        )
        self.assertNotIn("var.compute_cluster ?", resource)

    def test_initial_configuration_and_cli_include_compute_clusters(self):
        configure = self.read("bin", "configure_as.sh")
        resize = self.read("bin", "resize.py")

        self.assertIn("sync_instance_pool_names", configure)
        self.assertGreaterEqual(
            configure.count("is_autoscaling_compute_deployment"),
            3,
        )
        self.assertLess(
            configure.index(".instance-pool-hostname-sync.json"),
            configure.index("ansible-playbook"),
        )
        sync_after_ansible = configure.find(
            "synchronize_",
            configure.index("ansible-playbook"),
        )
        self.assertGreater(sync_after_ansible, configure.index("ansible-playbook"))
        cli_sync_gate = resize.split(
            "if args.mode == 'sync_instance_pool_names':",
            1,
        )[1].split("try:", 1)[0]
        self.assertTrue(
            "CN not in" not in cli_sync_gate,
            "sync_instance_pool_names CLI gate still excludes Compute Cluster",
        )
        cli_sync_block = resize.split(
            "if args.mode == 'sync_instance_pool_names':",
            1,
        )[1].split('\nif CN != "CC":', 1)[0]
        self.assertTrue(
            "synchronize_autoscaling_compute_names" in cli_sync_block,
            "the CLI still dispatches through the managed-pool-only resolver",
        )

    def test_resize_add_uses_pending_uuid_and_rollback_records_launch_ocid(self):
        resize = self.read("bin", "resize.py")
        add_block = resize.split("if args.mode == 'add':", 1)[1]

        self.assertIn("generate_compute_cluster_launch_display_name", add_block)
        self.assertNotIn("display_name'].split('-')[-1]", add_block)
        self.assertIn(
            "launched_compute_instances.append(launched_instance)",
            add_block,
        )
        direct_launch = add_block.index("computeClient.launch_instance(")
        record_ocid = add_block.index(
            'launched_instance["ocid"] = launched_instance_id'
        )
        wait_running = add_block.index(
            "wait_for_compute_cluster_instance_running("
        )
        self.assertLess(direct_launch, record_ocid)
        self.assertLess(record_ocid, wait_running)
        self.assertIn(
            "rollback_compute_cluster_instances(launched_compute_instances)",
            add_block,
        )

    def test_compute_cluster_add_never_uses_pool_only_terraform_state_rewriter(self):
        resize = self.read("bin", "resize.py")
        add_block = resize.split("if args.mode == 'add':", 1)[1]
        state_updates = [
            match.start()
            for match in re.finditer(r"updateTFState\(", add_block)
        ]

        self.assertEqual(len(state_updates), 1)
        state_update = state_updates[0]
        pool_only_guard = add_block.rfind('if CN != "CC":', 0, state_update)
        self.assertGreaterEqual(pool_only_guard, 0)
        guarded_body = add_block[
            pool_only_guard:add_block.find("\n        if ", state_update)
        ]
        self.assertIn("updateTFState(inventory,cluster_name,newsize)", guarded_body)

    def test_compute_cluster_remove_freezes_identity_and_preflights_all_dns_before_mutation(self):
        resize = self.read("bin", "resize.py")
        remove_block = resize.split(
            "planned_compute_cluster_removals = []",
            1,
        )[1].split('print("STDOUT: Resized to "+str(newsize)', 1)[0]

        build_plan = remove_block.index("build_compute_cluster_removal_plan(")
        build_journal = remove_block.index(
            "build_compute_cluster_node_removal_journal("
        )
        preflight = remove_block.index(
            "preflight_compute_cluster_node_removal_dns("
        )
        journal_write = remove_block.index(
            "write_compute_cluster_node_removal_journal("
        )
        inventory_playbook = remove_block.index(
            "destroy_unreachable_reconfigure(",
            journal_write,
        )
        completion = remove_block.index(
            "complete_compute_cluster_node_removals(",
            inventory_playbook,
        )
        journal_clear = remove_block.index(
            "clear_compute_cluster_node_removal_journal(",
            completion,
        )

        self.assertLess(build_plan, build_journal)
        self.assertIn(
            "inventory_path=inventory",
            remove_block[build_plan:build_journal],
        )
        self.assertLess(build_journal, preflight)
        self.assertLess(preflight, journal_write)
        self.assertLess(journal_write, inventory_playbook)
        self.assertLess(preflight, inventory_playbook)
        self.assertLess(inventory_playbook, completion)
        self.assertLess(completion, journal_clear)

        complete_function = resize.split(
            "def complete_compute_cluster_node_removals(",
            1,
        )[1].split(
            "def get_pending_compute_cluster_inventory_names(",
            1,
        )[0]
        exact_dns_delete = complete_function.index(
            "delete_compute_cluster_node_name_dns_records("
        )
        termination = complete_function.index(
            "terminate_instance_and_delete_launch_volumes("
        )
        final_membership_check = complete_function.rindex(
            "inspect_compute_cluster_node_removal_journal("
        )
        self.assertLess(exact_dns_delete, termination)
        self.assertLess(termination, final_membership_check)
        for frozen_field in (
            'record["instance_id"]',
            'record["instance_names"]',
            'record["private_ip"]',
            'record[\n                    "delete_launch_created_data_volumes"',
        ):
            self.assertIn(frozen_field, complete_function)

    def test_pending_compute_cluster_removal_resumes_before_new_selection(self):
        resize = self.read("bin", "resize.py")
        main = resize.split("batchsize=12", 1)[1]
        identity_resolution = main.index("get_summary_for_operation(")
        identity_assignment = main.index("cn_ocid =cn_summary.id", identity_resolution)
        resume = main.index(
            "resume_pending_compute_cluster_node_removals(",
            identity_assignment,
        )
        normal_resize = main.index(
            "else:\n    wait_for_running_status",
            resume,
        )
        resume_block = main[resume:normal_resize]

        self.assertLess(identity_resolution, identity_assignment)
        self.assertLess(identity_assignment, resume)
        self.assertIn("if completed_compute_cluster_removals:", resume_block)
        self.assertIn("exit(0)", resume_block)
        self.assertNotIn("args.number", resume_block)
        self.assertNotIn("getreachable(", resume_block)
        self.assertNotIn("build_compute_cluster_removal_plan(", resume_block)

    def test_compute_cluster_cleanup_cli_uses_cleanup_snapshot_not_running_snapshot(self):
        resize = self.read("bin", "resize.py")
        cleanup_block = resize.split(
            "if args.mode == 'cleanup_compute_cluster':",
            1,
        )[1].split(
            "try:\n    cn_summary,ip_summary,CN = get_summary_for_operation(",
            1,
        )[0]

        self.assertIn(
            "get_compute_cluster_instances_for_cleanup(",
            cleanup_block,
        )
        self.assertNotIn(
            "get_complete_compute_cluster_instances(",
            cleanup_block,
        )

    def test_multi_node_ansible_failure_cannot_be_overwritten_by_later_success(self):
        resize = self.read("bin", "resize.py")
        removal_playbook = resize.split(
            "def destroy_unreachable_reconfigure(",
            1,
        )[1].split("def destroy_reconfigure(", 1)[0]

        self.assertIn("update_flag = 0", removal_playbook)
        self.assertIn("node_update_flag = update_cluster(", removal_playbook)
        self.assertIn(
            "if node_update_flag != 0 and update_flag == 0:",
            removal_playbook,
        )
        self.assertNotIn(
            "\n            update_flag = update_cluster(",
            removal_playbook,
        )

    def test_compute_cluster_cleanup_allows_already_deleted_dns_zone(self):
        resize = self.read("bin", "resize.py")
        cleanup = resize.split(
            "def cleanup_compute_cluster_name_dns_records(",
            1,
        )[1].split(
            "def delete_compute_cluster_node_name_dns_records(",
            1,
        )[0]
        legacy_fallback = cleanup.split(
            "legacy_canonical_rrsets = []",
            1,
        )[1]

        self.assertIn("if len(zones) > 1:", legacy_fallback)
        self.assertIn("if zones:", legacy_fallback)
        self.assertNotIn("get_single_private_dns_zone_id(", legacy_fallback)

    def test_count_remove_selects_only_state_external_compute_cluster_nodes(self):
        resize = self.read("bin", "resize.py")
        main = resize.split("batchsize=12", 1)[1]
        selection = main.split(
            "if args.mode == 'remove' and not resuming_pending_node_removals:",
            1,
        )[1].split("hostnames_to_remove_len=len(hostnames_to_remove)", 1)[0]

        state_boundary = selection.index(
            "get_compute_cluster_state_external_member_ids("
        )
        candidate_filter = selection.index(
            'instance["ocid"] in state_external_instance_ids'
        )
        count_rejection = selection.index(
            "The requested Compute Cluster reduction exceeds"
        )
        self.assertLess(state_boundary, candidate_filter)
        self.assertLess(candidate_filter, count_rejection)

    def test_compute_cluster_mutations_check_terraform_lock_and_inventory_commit(self):
        resize = self.read("bin", "resize.py")
        external_boundary = resize.split(
            "def get_compute_cluster_state_external_member_ids(",
            1,
        )[1].split(
            "def build_compute_cluster_node_removal_journal(",
            1,
        )[0]
        completion = resize.split(
            "def complete_compute_cluster_node_removals(",
            1,
        )[1].split(
            "def get_pending_compute_cluster_inventory_names(",
            1,
        )[0]
        inventory_commit = resize.split(
            "def destroy_unreachable_reconfigure(",
            1,
        )[1].split("def destroy_reconfigure(", 1)[0]

        self.assertIn(
            "refuse_terraform_state_mutation_while_locked(",
            external_boundary,
        )
        self.assertGreaterEqual(
            completion.count(
                "refuse_terraform_state_mutation_while_locked("
            ),
            2,
        )
        self.assertIn('["sudo", "mv", tmp_inventory, inventory]', inventory_commit)
        self.assertIn("check=True", inventory_commit)


if __name__ == "__main__":
    unittest.main()
