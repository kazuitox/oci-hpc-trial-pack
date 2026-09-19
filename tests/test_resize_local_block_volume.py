import ipaddress
import os
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


def load_resize_functions():
    fake_oci = types.ModuleType("oci")
    fake_oci.pagination = SimpleNamespace(
        list_call_get_all_results=lambda function, *args, **kwargs: function(*args, **kwargs)
    )
    fake_oci.exceptions = SimpleNamespace(ServiceError=FakeServiceError)
    fake_requests = types.ModuleType("requests")
    with open(RESIZE_PATH, "r") as source_file:
        function_source = source_file.read().split("batchsize=12", 1)[0]
    namespace = {"__name__": "resize_functions_test"}
    with mock.patch.dict(sys.modules, {"oci": fake_oci, "requests": fake_requests}):
        exec(compile(function_source, RESIZE_PATH, "exec"), namespace)
    return namespace


class ComputeClusterLaunchCostTagTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()

        class FakeModel:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        self.namespace["oci"].core = SimpleNamespace(
            models=SimpleNamespace(
                LaunchInstanceAgentConfigDetails=FakeModel,
                CreateVnicDetails=FakeModel,
                LaunchInstanceShapeConfigDetails=FakeModel,
                LaunchInstanceDetails=FakeModel,
                LaunchCreateVolumeFromAttributes=FakeModel,
                LaunchAttachIScsiVolumeDetails=FakeModel,
            )
        )
        self.namespace["computeClient"] = SimpleNamespace(
            list_vnic_attachments=lambda **kwargs: SimpleNamespace(
                data=[SimpleNamespace(display_name=None, subnet_id="ocid1.subnet.test")]
            )
        )
        self.instance = SimpleNamespace(
            id="ocid1.instance.source",
            agent_config=FakeModel(),
            display_name="test-cluster-node-2",
            availability_domain="test-ad",
            shape="BM.HPC2.36",
            shape_config=SimpleNamespace(
                baseline_ocpu_utilization=None,
                memory_in_gbs=384,
                ocpus=36,
                local_disks=1,
            ),
            source_details=FakeModel(source_id="ocid1.image.test"),
            metadata={"ssh_authorized_keys": "test-key"},
        )

    def launch(self, source_tags, local_scratch=False, source_defined_tags=None):
        self.instance.freeform_tags = source_tags
        self.instance.defined_tags = source_defined_tags
        return self.namespace["getLaunchInstanceDetails"](
            self.instance,
            "ocid1.compartment.test",
            "ocid1.computecluster.test",
            2,
            0,
            {
                "enabled": local_scratch,
                "size_in_gbs": 100,
                "vpus_per_gb": 10,
                "mount_point": "/scratch",
            },
        )

    def test_new_node_does_not_inherit_running_users_cost_tag(self):
        for local_scratch in (False, True):
            with self.subTest(local_scratch=local_scratch):
                source_tags = {
                    "user": "alice",
                    "cluster_name": "test-cluster",
                    "parent_cluster": "test-cluster",
                    "customer": "research",
                }
                source_defined_tags = {
                    "hpc-cost": {"User": "alice", "Project": "research", "user": "keep"},
                    "other": {"User": "keep", "CostCenter": 123},
                }
                details = self.launch(source_tags, local_scratch=local_scratch,
                                      source_defined_tags=source_defined_tags)
                self.assertEqual(details.defined_tags, {
                    "hpc-cost": {"User": "Management", "Project": "research", "user": "keep"},
                    "other": {"User": "keep", "CostCenter": 123},
                })
                self.assertNotIn("user", details.freeform_tags)
                self.assertEqual(details.freeform_tags["customer"], "research")
                self.assertEqual(details.freeform_tags["cluster_name"], "test-cluster")
                self.assertEqual(details.freeform_tags["parent_cluster"], "test-cluster")
                self.assertEqual(source_tags["user"], "alice")
                self.assertEqual(source_defined_tags["hpc-cost"]["User"], "alice")
                self.assertIsNot(details.freeform_tags, source_tags)
                self.assertIsNot(details.defined_tags, source_defined_tags)
                for namespace in source_defined_tags:
                    self.assertIsNot(details.defined_tags[namespace], source_defined_tags[namespace])
                self.assertEqual(details.freeform_tags["oci_hpc_local_block_volume"],
                                 "true" if local_scratch else "false")
                self.assertEqual(hasattr(details, "launch_volume_attachments"), local_scratch)

    def test_legacy_source_without_tags_still_starts_as_management(self):
        for source_tags in (None, {}, {"parent_cluster": "test-cluster"}):
            for source_defined_tags in (None, {}, {"hpc-cost": {}}, {"other": {"User": "keep"}}):
                for local_scratch in (False, True):
                    with self.subTest(source_tags=source_tags, source_defined_tags=source_defined_tags,
                                      local_scratch=local_scratch):
                        details = self.launch(source_tags, local_scratch=local_scratch,
                                              source_defined_tags=source_defined_tags)
                        self.assertEqual(details.defined_tags["hpc-cost"], {"User": "Management"})
                        self.assertNotIn("user", details.freeform_tags)
                        if source_defined_tags and "other" in source_defined_tags:
                            self.assertEqual(details.defined_tags["other"], {"User": "keep"})


class LocalBlockVolumeAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_resize_functions()
        self.instance = SimpleNamespace(
            id="ocid1.instance.test",
            display_name="test-cluster-node-0",
            compartment_id="ocid1.compartment.test",
            freeform_tags={
                "cluster_name": "test-cluster",
                "parent_cluster": "test-cluster",
                "oci_hpc_local_block_volume": "true",
                "oci_hpc_local_block_volume_size": "100",
                "oci_hpc_local_block_volume_vpus": "10",
                "oci_hpc_local_block_volume_mount": "/scratch",
            },
        )
        self.expected_config = {
            "enabled": True,
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
            "mount_point": "/scratch",
        }

    def make_attachment(self, scope="cluster", launch_marker=False, include_launch_marker=True):
        display_name = (
            "test-cluster-local-scratch-attachment"
            if scope == "cluster"
            else "test-cluster-node-0-local-scratch-attachment"
        )
        values = {
            "device": "/dev/oracleoci/oraclevdc",
            "lifecycle_state": "ATTACHED",
            "attachment_type": "iscsi",
            "instance_id": self.instance.id,
            "display_name": display_name,
            "volume_id": "ocid1.volume.test",
            "ipv4": "169.254.2.2",
            "port": 3260,
            "iqn": "iqn.test",
        }
        if include_launch_marker:
            values["is_volume_created_during_launch"] = launch_marker
        return SimpleNamespace(**values)

    def configure_clients(self, attachment, volume_tags=None):
        if volume_tags is None:
            volume_tags = {
                "cluster_name": "test-cluster",
                "parent_cluster": "test-cluster",
                "oci_hpc_local_scratch": "true",
            }
        volume_name = (
            "test-cluster-local-scratch"
            if attachment.display_name == "test-cluster-local-scratch-attachment"
            else "test-cluster-node-0-local-scratch"
        )
        self.namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id: SimpleNamespace(data=self.instance),
            list_volume_attachments=lambda **kwargs: SimpleNamespace(data=[attachment]),
        )
        self.namespace["blockstorageClient"] = SimpleNamespace(
            get_volume=lambda volume_id: SimpleNamespace(
                data=SimpleNamespace(
                    display_name=volume_name,
                    freeform_tags=volume_tags,
                    size_in_gbs=100,
                    vpus_per_gb=10,
                )
            )
        )

    def resolve(self, attachment):
        self.configure_clients(attachment)
        return self.namespace["get_local_block_volume_attachment"](
            self.instance.compartment_id,
            self.instance.id,
            max_wait_seconds=0,
            expected_config=self.expected_config,
            expected_cluster_name="test-cluster",
            instance=self.instance,
        )

    def test_cluster_scoped_attachment_accepts_false_none_and_missing_launch_marker(self):
        attachments = [
            self.make_attachment(launch_marker=False),
            self.make_attachment(launch_marker=None),
            self.make_attachment(include_launch_marker=False),
        ]
        for attachment in attachments:
            with self.subTest(marker=getattr(attachment, "is_volume_created_during_launch", "missing")):
                self.assertIs(self.resolve(attachment), attachment)

    def test_cluster_scoped_attachment_still_requires_ownership_tags(self):
        attachment = self.make_attachment(launch_marker=False)
        self.configure_clients(attachment, volume_tags={})
        with self.assertRaisesRegex(RuntimeError, "ownership tags"):
            self.namespace["get_local_block_volume_attachment"](
                self.instance.compartment_id,
                self.instance.id,
                max_wait_seconds=0,
                expected_config=self.expected_config,
                expected_cluster_name="test-cluster",
                instance=self.instance,
            )

    def test_cluster_scoped_attachment_still_requires_expected_volume_attributes(self):
        attachment = self.make_attachment(launch_marker=False)
        invalid_volumes = [
            SimpleNamespace(
                display_name="unexpected-volume",
                freeform_tags={
                    "parent_cluster": "test-cluster",
                    "oci_hpc_local_scratch": "true",
                },
                size_in_gbs=100,
                vpus_per_gb=10,
            ),
            SimpleNamespace(
                display_name="test-cluster-local-scratch",
                freeform_tags={
                    "parent_cluster": "test-cluster",
                    "oci_hpc_local_scratch": "true",
                },
                size_in_gbs=101,
                vpus_per_gb=10,
            ),
            SimpleNamespace(
                display_name="test-cluster-local-scratch",
                freeform_tags={
                    "parent_cluster": "test-cluster",
                    "oci_hpc_local_scratch": "true",
                },
                size_in_gbs=100,
                vpus_per_gb=20,
            ),
        ]
        self.namespace["computeClient"] = SimpleNamespace(
            list_volume_attachments=lambda **kwargs: SimpleNamespace(data=[attachment])
        )
        for invalid_volume in invalid_volumes:
            self.namespace["blockstorageClient"] = SimpleNamespace(
                get_volume=lambda volume_id, volume=invalid_volume: SimpleNamespace(data=volume)
            )
            with self.subTest(volume=invalid_volume):
                with self.assertRaises(RuntimeError):
                    self.namespace["get_local_block_volume_attachment"](
                        self.instance.compartment_id,
                        self.instance.id,
                        max_wait_seconds=0,
                        expected_config=self.expected_config,
                        expected_cluster_name="test-cluster",
                        instance=self.instance,
                    )

    def test_instance_scoped_attachment_requires_true_launch_marker(self):
        attachments = [
            self.make_attachment(scope="instance", launch_marker=False),
            self.make_attachment(scope="instance", launch_marker=None),
            self.make_attachment(scope="instance", include_launch_marker=False),
        ]
        for attachment in attachments:
            self.configure_clients(attachment, volume_tags={})
            with self.subTest(marker=getattr(attachment, "is_volume_created_during_launch", "missing")):
                with self.assertRaisesRegex(RuntimeError, "not created with instance"):
                    self.namespace["get_local_block_volume_attachment"](
                        self.instance.compartment_id,
                        self.instance.id,
                        max_wait_seconds=0,
                        expected_config=self.expected_config,
                        expected_cluster_name="test-cluster",
                        instance=self.instance,
                    )
        valid_attachment = self.make_attachment(scope="instance", launch_marker=True)
        self.configure_clients(valid_attachment, volume_tags={})
        self.assertIs(
            self.namespace["get_local_block_volume_attachment"](
                self.instance.compartment_id,
                self.instance.id,
                max_wait_seconds=0,
                expected_config=self.expected_config,
                expected_cluster_name="test-cluster",
                instance=self.instance,
            ),
            valid_attachment,
        )

    def test_prepare_inventory_uses_cluster_scoped_false_marker_attachment(self):
        attachment = self.make_attachment(launch_marker=False)
        self.configure_clients(attachment)
        inventory = """[compute_configured]
test-cluster-node-0 ansible_host=10.0.0.10 ansible_user=opc role=compute oci_instance_id=ocid1.instance.test use_local_block_volume=false
[compute_to_add]
[nfs]
[all:vars]
cluster_name=test-cluster
nvme_path=/mnt/localdisk
scratch_nfs_path=/nfs/scratch
cluster_nfs_path=/nfs/cluster
nfs_target_path=/share
"""
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            with open(inventory_path, "w") as inventory_file:
                inventory_file.write(inventory)
            self.namespace["prepare_local_block_volume_inventory"](
                inventory_path,
                max_wait_seconds=0,
            )
            with open(inventory_path, "r") as inventory_file:
                prepared_inventory = inventory_file.read()
        self.assertIn("use_local_block_volume=true", prepared_inventory)
        self.assertIn("local_block_volume_iscsi_ip=169.254.2.2", prepared_inventory)
        self.assertIn("local_block_volume_iqn=iqn.test", prepared_inventory)

    def test_cluster_scoped_false_marker_is_selected_for_explicit_deletion(self):
        attachment = self.make_attachment(launch_marker=False)
        self.configure_clients(attachment)
        self.namespace["cluster_name"] = "test-cluster"
        self.namespace["inventory_dict"] = {
            "all:vars": [
                "nvme_path=/mnt/localdisk\n",
                "scratch_nfs_path=/nfs/scratch\n",
                "cluster_nfs_path=/nfs/cluster\n",
                "nfs_target_path=/share\n",
            ]
        }
        selected_attachment = self.namespace[
            "get_exclusive_managed_local_block_volume_attachment"
        ](self.instance.compartment_id, self.instance.id)
        self.assertIs(selected_attachment, attachment)
        self.assertFalse(
            self.namespace["instance_has_exclusive_managed_local_block_volume"](
                self.instance.compartment_id,
                self.instance.id,
            )
        )


class LocalBlockVolumeDeletionTests(unittest.TestCase):
    def make_cluster_instance(self, lifecycle_state="RUNNING"):
        return SimpleNamespace(
            display_name="test-cluster-node-0",
            lifecycle_state=lifecycle_state,
            freeform_tags={"parent_cluster": "test-cluster"},
        )

    def make_cluster_volume(self):
        return SimpleNamespace(
            display_name="test-cluster-local-scratch",
            compartment_id="ocid1.compartment.test",
            freeform_tags={
                "cluster_name": "test-cluster",
                "parent_cluster": "test-cluster",
                "oci_hpc_local_scratch": "true",
            },
            size_in_gbs=100,
            vpus_per_gb=10,
            lifecycle_state="AVAILABLE",
        )

    def make_instance_pool(self, size=1):
        return SimpleNamespace(
            id="ocid1.instancepool.test",
            compartment_id="ocid1.compartment.test",
            size=size,
        )

    def test_resize_lock_rejects_concurrent_operation_for_same_inventory(self):
        namespace = load_resize_functions()
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            with open(inventory_path, "w") as inventory_file:
                inventory_file.write("[all:vars]\n")
            first_lock = namespace["acquire_resize_lock"](inventory_path)
            try:
                with self.assertRaisesRegex(RuntimeError, "Another resize operation"):
                    namespace["acquire_resize_lock"](inventory_path)
            finally:
                namespace["fcntl"].flock(first_lock.fileno(), namespace["fcntl"].LOCK_UN)
                first_lock.close()
            replacement_lock = namespace["acquire_resize_lock"](inventory_path)
            replacement_lock.close()

    def test_explicit_delete_retries_detach_conflict_until_volume_is_gone(self):
        namespace = load_resize_functions()
        get_volume_results = [
            SimpleNamespace(data=SimpleNamespace(lifecycle_state="AVAILABLE")),
            SimpleNamespace(data=SimpleNamespace(lifecycle_state="AVAILABLE")),
            FakeServiceError(404),
        ]
        delete_attempts = []

        def get_volume(volume_id):
            result = get_volume_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        def delete_volume(volume_id):
            delete_attempts.append(volume_id)
            if len(delete_attempts) == 1:
                raise FakeServiceError(409)

        namespace["blockstorageClient"] = SimpleNamespace(
            get_volume=get_volume,
            delete_volume=delete_volume,
        )
        namespace["time"].sleep = lambda seconds: None
        namespace["delete_managed_local_block_volume"]("ocid1.volume.test", max_wait_seconds=1)
        self.assertEqual(delete_attempts, ["ocid1.volume.test", "ocid1.volume.test"])

    def test_pool_remove_preserves_other_launch_volumes_and_deletes_only_scratch(self):
        namespace = load_resize_functions()
        namespace["cluster_name"] = "test-cluster"
        attachment = SimpleNamespace(
            volume_id="ocid1.volume.scratch",
            is_volume_created_during_launch=False,
        )
        volume = self.make_cluster_volume()
        operations = []
        namespace["blockstorageClient"] = SimpleNamespace(
            get_volume=lambda volume_id: SimpleNamespace(data=volume)
        )
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id: SimpleNamespace(data=self.make_cluster_instance())
        )
        namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=lambda instance_pool_id: SimpleNamespace(
                data=self.make_instance_pool(size=1)
            )
        )
        namespace["get_exclusive_managed_local_block_volume_attachment"] = (
            lambda compartment_id, instance_id: attachment
        )
        remember_deletion = namespace["remember_pending_local_block_volume_deletion"]

        def remember(inventory_path, record):
            operations.append("journal")
            remember_deletion(inventory_path, record)

        namespace["remember_pending_local_block_volume_deletion"] = remember
        namespace["detach_instance_from_pool_if_needed"] = (
            lambda compartment_id, pool_id, instance_id: operations.append("detach")
        )

        def terminate(instance_id, delete_launch_created_data_volumes=None):
            self.assertFalse(delete_launch_created_data_volumes)
            operations.append("terminate-preserve-launch-volumes")

        namespace["terminate_instance_and_delete_launch_volumes"] = terminate
        namespace["delete_managed_local_block_volume"] = (
            lambda volume_id: operations.append("delete-"+volume_id)
        )
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remove_instance_pool_member_and_managed_local_block_volume"](
                "ocid1.compartment.test",
                "ocid1.instancepool.test",
                "ocid1.instance.test",
                "10.0.0.10",
                inventory_path,
            )
            pending_deletions = namespace["load_pending_local_block_volume_deletions"](
                inventory_path
            )
            self.assertEqual(len(pending_deletions), 1)
            self.assertEqual(pending_deletions[0]["instance_private_ip"], "10.0.0.10")
            self.assertEqual(
                pending_deletions[0]["instance_pool_size_before_removal"],
                1,
            )
        self.assertEqual(
            operations,
            [
                "journal",
                "detach",
                "terminate-preserve-launch-volumes",
                "delete-ocid1.volume.scratch",
            ],
        )

    def test_failed_volume_delete_is_recovered_from_pending_journal(self):
        namespace = load_resize_functions()
        namespace["cluster_name"] = "test-cluster"
        attachment = SimpleNamespace(
            volume_id="ocid1.volume.scratch",
            is_volume_created_during_launch=False,
        )
        volume = self.make_cluster_volume()
        namespace["blockstorageClient"] = SimpleNamespace(
            get_volume=lambda volume_id: SimpleNamespace(data=volume)
        )
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id: SimpleNamespace(data=self.make_cluster_instance())
        )
        namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=lambda instance_pool_id: SimpleNamespace(
                data=self.make_instance_pool(size=1)
            )
        )
        namespace["get_exclusive_managed_local_block_volume_attachment"] = (
            lambda compartment_id, instance_id: attachment
        )
        namespace["detach_instance_from_pool_if_needed"] = lambda *args: None
        namespace["terminate_instance_and_delete_launch_volumes"] = lambda *args, **kwargs: None
        namespace["delete_managed_local_block_volume"] = (
            lambda volume_id: (_ for _ in ()).throw(RuntimeError("mock delete failure"))
        )
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            with self.assertRaisesRegex(RuntimeError, "mock delete failure"):
                namespace["remove_instance_pool_member_and_managed_local_block_volume"](
                    "ocid1.compartment.test",
                    "ocid1.instancepool.test",
                    "ocid1.instance.test",
                    "10.0.0.10",
                    inventory_path,
                )
            pending_path = namespace["get_pending_local_block_volume_deletions_path"](
                inventory_path
            )
            self.assertTrue(os.path.isfile(pending_path))

            namespace["computeClient"] = SimpleNamespace(
                get_instance=lambda instance_id: SimpleNamespace(
                    data=SimpleNamespace(
                        display_name="test-cluster-node-0",
                        lifecycle_state="TERMINATED",
                        freeform_tags={"parent_cluster": "test-cluster"},
                    )
                )
            )
            recovered_volume_ids = []
            namespace["delete_managed_local_block_volume"] = recovered_volume_ids.append
            ready_deletions = namespace["retry_pending_local_block_volume_deletions"](
                inventory_path,
                "test-cluster",
                "ocid1.compartment.test",
            )
            self.assertEqual(recovered_volume_ids, ["ocid1.volume.scratch"])
            self.assertEqual(
                [record["volume_id"] for record in ready_deletions],
                ["ocid1.volume.scratch"],
            )
            self.assertTrue(os.path.exists(pending_path))

    def test_detach_failure_leaves_pending_deletion_journal(self):
        namespace = load_resize_functions()
        namespace["cluster_name"] = "test-cluster"
        attachment = SimpleNamespace(
            volume_id="ocid1.volume.scratch",
            is_volume_created_during_launch=False,
        )
        namespace["blockstorageClient"] = SimpleNamespace(
            get_volume=lambda volume_id: SimpleNamespace(data=self.make_cluster_volume())
        )
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id: SimpleNamespace(data=self.make_cluster_instance())
        )
        namespace["computeManagementClient"] = SimpleNamespace(
            get_instance_pool=lambda instance_pool_id: SimpleNamespace(
                data=self.make_instance_pool(size=1)
            )
        )
        namespace["get_exclusive_managed_local_block_volume_attachment"] = (
            lambda compartment_id, instance_id: attachment
        )
        namespace["detach_instance_from_pool_if_needed"] = (
            lambda *args: (_ for _ in ()).throw(RuntimeError("mock detach failure"))
        )
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            with self.assertRaisesRegex(RuntimeError, "mock detach failure"):
                namespace["remove_instance_pool_member_and_managed_local_block_volume"](
                    "ocid1.compartment.test",
                    "ocid1.instancepool.test",
                    "ocid1.instance.test",
                    "10.0.0.10",
                    inventory_path,
                )
            pending_deletions = namespace["load_pending_local_block_volume_deletions"](
                inventory_path
            )
        self.assertEqual(
            [record["volume_id"] for record in pending_deletions],
            ["ocid1.volume.scratch"],
        )

    def test_retry_leaves_active_pool_member_for_normal_remove_flow(self):
        namespace = load_resize_functions()
        namespace["cluster_name"] = "test-cluster"
        volume = self.make_cluster_volume()
        namespace["blockstorageClient"] = SimpleNamespace(
            get_volume=lambda volume_id: SimpleNamespace(data=volume)
        )
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id: SimpleNamespace(
                data=self.make_cluster_instance()
            )
        )
        namespace["instance_is_pool_member"] = lambda *args: True
        namespace["terminate_instance_and_delete_launch_volumes"] = (
            lambda *args, **kwargs: self.fail("active pool member must not be terminated by retry")
        )
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remember_pending_local_block_volume_deletion"](inventory_path, record)
            with self.assertRaisesRegex(RuntimeError, "must be resumed explicitly"):
                namespace["retry_pending_local_block_volume_deletions"](
                    inventory_path,
                    "test-cluster",
                    "ocid1.compartment.test",
                )
            self.assertEqual(
                namespace["load_pending_local_block_volume_deletions"](inventory_path),
                [record],
            )

    def test_explicit_retry_resumes_active_pool_member_without_double_delete(self):
        namespace = load_resize_functions()
        namespace["cluster_name"] = "test-cluster"
        volume = self.make_cluster_volume()
        namespace["blockstorageClient"] = SimpleNamespace(
            get_volume=lambda volume_id: SimpleNamespace(data=volume)
        )
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id: SimpleNamespace(data=self.make_cluster_instance())
        )
        namespace["instance_is_pool_member"] = lambda *args: True
        operations = []
        namespace["detach_instance_from_pool_if_needed"] = (
            lambda *args: operations.append("detach")
        )
        namespace["terminate_instance_and_delete_launch_volumes"] = (
            lambda instance_id, delete_launch_created_data_volumes=None: operations.append(
                ("terminate", delete_launch_created_data_volumes)
            )
        )
        namespace["delete_managed_local_block_volume"] = (
            lambda volume_id: operations.append(("delete", volume_id))
        )
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remember_pending_local_block_volume_deletion"](inventory_path, record)
            ready_deletions = namespace["process_pending_local_block_volume_deletions_for_operation"](
                inventory_path,
                "test-cluster",
                "ocid1.compartment.test",
                "remove",
                ["test-cluster-node-0"],
            )
            self.assertEqual(ready_deletions, [record])
            pending_path = namespace["get_pending_local_block_volume_deletions_path"](
                inventory_path
            )
            self.assertTrue(os.path.exists(pending_path))
        self.assertEqual(
            operations,
            [
                "detach",
                ("terminate", False),
                ("delete", "ocid1.volume.scratch"),
            ],
        )

    def test_pending_resume_policy_allows_only_explicit_remove_or_cluster_cleanup(self):
        namespace = load_resize_functions()
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        resume_values = []
        namespace["retry_pending_local_block_volume_deletions"] = (
            lambda *args,
            resume_pool_members=False,
            resume_pool_member_instance_ids=None,
            expected_instance_pool_id=None: resume_values.append(
                (
                    resume_pool_members,
                    set(resume_pool_member_instance_ids or []),
                    expected_instance_pool_id,
                )
            ) or [record]
        )
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remember_pending_local_block_volume_deletion"](inventory_path, record)
            namespace["process_pending_local_block_volume_deletions_for_operation"](
                inventory_path,
                "test-cluster",
                "ocid1.compartment.test",
                "remove",
                ["test-cluster-node-0"],
            )
            namespace["process_pending_local_block_volume_deletions_for_operation"](
                inventory_path,
                "test-cluster",
                "ocid1.compartment.test",
                "remove",
                [],
            )
            cleanup_ready_deletions = namespace[
                "process_pending_local_block_volume_deletions_for_operation"
            ](
                inventory_path,
                "test-cluster",
                "ocid1.compartment.test",
                "cleanup_compute_cluster",
                [],
            )
            self.assertEqual(cleanup_ready_deletions, [record])
            self.assertEqual(
                namespace["load_pending_local_block_volume_deletions"](
                    inventory_path
                ),
                [record],
            )
        self.assertEqual(
            resume_values,
            [
                (False, {"ocid1.instance.test"}, None),
                (False, set(), None),
                (True, set(), None),
            ],
        )

    def test_retry_finishes_detached_active_instance_before_deleting_volume(self):
        namespace = load_resize_functions()
        namespace["cluster_name"] = "test-cluster"
        volume = self.make_cluster_volume()
        namespace["blockstorageClient"] = SimpleNamespace(
            get_volume=lambda volume_id: SimpleNamespace(data=volume)
        )
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        namespace["computeClient"] = SimpleNamespace(
            get_instance=lambda instance_id: SimpleNamespace(
                data=self.make_cluster_instance()
            )
        )
        namespace["instance_is_pool_member"] = lambda *args: False
        operations = []
        namespace["terminate_instance_and_delete_launch_volumes"] = (
            lambda instance_id, delete_launch_created_data_volumes=None: operations.append(
                ("terminate", delete_launch_created_data_volumes)
            )
        )
        namespace["delete_managed_local_block_volume"] = (
            lambda volume_id: operations.append(("delete", volume_id))
        )
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remember_pending_local_block_volume_deletion"](inventory_path, record)
            ready_deletions = namespace["retry_pending_local_block_volume_deletions"](
                inventory_path,
                "test-cluster",
                "ocid1.compartment.test",
            )
            self.assertEqual(ready_deletions, [record])
            pending_path = namespace["get_pending_local_block_volume_deletions_path"](
                inventory_path
            )
            self.assertTrue(os.path.exists(pending_path))
        self.assertEqual(
            operations,
            [
                ("terminate", False),
                ("delete", "ocid1.volume.scratch"),
            ],
        )

    def test_update_tf_state_pushes_new_pool_size_and_updates_variables(self):
        namespace = load_resize_functions()
        pushed_state = []
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            state_path = os.path.join(temp_directory, "terraform.tfstate")
            variables_path = os.path.join(temp_directory, "variables.tf")
            with open(inventory_path, "w") as inventory_file:
                inventory_file.write("[all]\n")
            with open(state_path, "w") as state_file:
                state_file.write(
                    '{\n  "serial": 4,\n  "resources": [\n'
                    '    {"instances": [{"attributes": {\n'
                    '      "size": 2,\n'
                    '      "display_name": "test-cluster"\n'
                    '    }}]}\n  ]\n}\n'
                )
            with open(variables_path, "w") as variables_file:
                variables_file.write('variable "node_count" { default = "2" }\n')

            def run_terraform(command, cwd, check):
                self.assertEqual(command[:3], ["terraform", "state", "push"])
                self.assertEqual(cwd, temp_directory)
                self.assertTrue(check)
                with open(command[3], "r") as temporary_state:
                    pushed_state.append(temporary_state.read())
                return SimpleNamespace(returncode=0)

            with mock.patch.object(namespace["subprocess"], "run", side_effect=run_terraform):
                self.assertTrue(
                    namespace["updateTFState"](
                        inventory_path,
                        "test-cluster",
                        1,
                    )
                )

            with open(variables_path, "r") as variables_file:
                self.assertEqual(
                    variables_file.read(),
                    'variable "node_count" { default = "1" }\n',
                )
        self.assertEqual(len(pushed_state), 1)
        self.assertIn('"serial": 5', pushed_state[0])
        self.assertIn('"size": 1', pushed_state[0])

    def test_update_tf_state_keeps_variables_when_state_push_fails(self):
        namespace = load_resize_functions()
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            state_path = os.path.join(temp_directory, "terraform.tfstate")
            variables_path = os.path.join(temp_directory, "variables.tf")
            with open(inventory_path, "w") as inventory_file:
                inventory_file.write("[all]\n")
            with open(state_path, "w") as state_file:
                state_file.write('{\n  "serial": 1,\n  "size": 2\n}\n')
            original_variables = 'variable "node_count" { default="2" }\n'
            with open(variables_path, "w") as variables_file:
                variables_file.write(original_variables)

            with mock.patch.object(
                namespace["subprocess"],
                "run",
                side_effect=OSError("mock terraform failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Failed to update Terraform state"):
                    namespace["updateTFState"](
                        inventory_path,
                        "test-cluster",
                        1,
                    )

            with open(variables_path, "r") as variables_file:
                self.assertEqual(variables_file.read(), original_variables)

    def test_update_tf_state_rejects_missing_autoscaling_state(self):
        namespace = load_resize_functions()
        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            with open(inventory_path, "w") as inventory_file:
                inventory_file.write("[all]\n")
            with self.assertRaisesRegex(RuntimeError, "Terraform state was not found"):
                namespace["updateTFState"](
                    inventory_path,
                    "test-cluster",
                    1,
                )

    def test_cleanup_pending_dns_deletes_both_records_and_keeps_journal(self):
        namespace = load_resize_functions()
        namespace["dns_entries"] = True
        namespace["zone_name"] = "test-cluster.local"
        namespace["queue"] = "compute"
        namespace["instance_type"] = "hpc"
        namespace["private_subnet_cidr"] = ipaddress.ip_network("10.0.0.0/24")
        namespace["slurm_enabled"] = True
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        deleted_domains = []
        namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: SimpleNamespace(
                data=[SimpleNamespace(id="ocid1.dnszone.test")]
            ),
            get_rr_set=lambda **kwargs: SimpleNamespace(
                data=SimpleNamespace(
                    items=[SimpleNamespace(rtype="A", rdata="10.0.0.10")]
                )
            ),
            delete_rr_set=lambda **kwargs: deleted_domains.append(kwargs["domain"]),
        )

        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remember_pending_local_block_volume_deletion"](
                inventory_path,
                record,
            )

            completed = namespace["cleanup_pending_local_block_volume_dns_records"](
                inventory_path,
                [record],
                "test-cluster",
                "ocid1.compartment.test",
            )

            self.assertEqual(completed, 1)
            self.assertEqual(
                deleted_domains,
                [
                    "test-cluster-node-0.test-cluster.local",
                    "compute-hpc-11.test-cluster.local",
                ],
            )
            self.assertEqual(
                namespace["load_pending_local_block_volume_deletions"](
                    inventory_path
                ),
                [record],
            )

    def test_pending_dns_ignores_slurm_alias_when_slurm_is_disabled(self):
        namespace = load_resize_functions()
        namespace["zone_name"] = "test-cluster.local"
        namespace["queue"] = "compute"
        namespace["instance_type"] = "hpc"
        namespace["private_subnet_cidr"] = ipaddress.ip_network("10.0.0.0/24")
        namespace["slurm_enabled"] = False

        self.assertEqual(
            namespace["get_pending_node_dns_domains"](
                {
                    "instance_display_name": "worker-alpha",
                    "instance_private_ip": "10.0.0.10",
                }
            ),
            ["worker-alpha.test-cluster.local"],
        )

    def test_pending_dns_preflights_all_records_before_any_delete(self):
        namespace = load_resize_functions()
        namespace["zone_name"] = "test-cluster.local"
        namespace["slurm_enabled"] = False
        records = [
            {
                "instance_display_name": "worker-alpha",
                "instance_private_ip": "10.0.0.10",
            },
            {
                "instance_display_name": "worker-beta",
                "instance_private_ip": "10.0.0.11",
            },
        ]
        delete_rr_set = mock.Mock()

        def get_rr_set(**kwargs):
            private_ip = (
                "10.0.0.10"
                if kwargs["domain"].startswith("worker-alpha.")
                else "10.9.9.9"
            )
            return SimpleNamespace(
                data=SimpleNamespace(
                    items=[SimpleNamespace(rtype="A", rdata=private_ip)]
                )
            )

        namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: SimpleNamespace(
                data=[SimpleNamespace(id="ocid1.dnszone.test")]
            ),
            get_rr_set=get_rr_set,
            delete_rr_set=delete_rr_set,
        )

        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            namespace["delete_pending_local_block_volume_dns_records_safely"](
                records,
                "ocid1.compartment.test",
            )

        delete_rr_set.assert_not_called()

    def test_pending_dns_rejects_ambiguous_zone_before_delete(self):
        namespace = load_resize_functions()
        namespace["zone_name"] = "test-cluster.local"
        namespace["slurm_enabled"] = False
        get_rr_set = mock.Mock()
        delete_rr_set = mock.Mock()
        namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(id="ocid1.dnszone.one"),
                    SimpleNamespace(id="ocid1.dnszone.two"),
                ]
            ),
            get_rr_set=get_rr_set,
            delete_rr_set=delete_rr_set,
        )

        with self.assertRaisesRegex(RuntimeError, "Multiple private DNS zones"):
            namespace["delete_pending_local_block_volume_dns_records_safely"](
                [
                    {
                        "instance_display_name": "worker-alpha",
                        "instance_private_ip": "10.0.0.10",
                    }
                ],
                "ocid1.compartment.test",
            )

        get_rr_set.assert_not_called()
        delete_rr_set.assert_not_called()

    def test_cleanup_pending_dns_succeeds_without_zone_and_keeps_journal(self):
        namespace = load_resize_functions()
        namespace["dns_entries"] = True
        namespace["zone_name"] = "test-cluster.local"
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: SimpleNamespace(data=[]),
            delete_rr_set=lambda **kwargs: self.fail(
                "DNS deletion must not run after the zone is removed"
            ),
        )

        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remember_pending_local_block_volume_deletion"](
                inventory_path,
                record,
            )

            completed = namespace["cleanup_pending_local_block_volume_dns_records"](
                inventory_path,
                [record],
                "test-cluster",
                "ocid1.compartment.test",
            )

            self.assertEqual(completed, 1)
            self.assertEqual(
                namespace["load_pending_local_block_volume_deletions"](
                    inventory_path
                ),
                [record],
            )

    def test_finalize_deletes_both_dns_records_and_forgets_journal_after_state_update(self):
        namespace = load_resize_functions()
        namespace["dns_entries"] = True
        namespace["zone_name"] = "test-cluster.local"
        namespace["queue"] = "compute"
        namespace["instance_type"] = "hpc"
        namespace["private_subnet_cidr"] = ipaddress.ip_network("10.0.0.0/24")
        namespace["slurm_enabled"] = True
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        operations = []

        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            pending_path = namespace["get_pending_local_block_volume_deletions_path"](
                inventory_path
            )
            namespace["remember_pending_local_block_volume_deletion"](inventory_path, record)

            def list_zones(**kwargs):
                self.assertTrue(os.path.exists(pending_path))
                return SimpleNamespace(data=[SimpleNamespace(id="ocid1.dnszone.test")])

            def delete_rr_set(**kwargs):
                self.assertTrue(os.path.exists(pending_path))
                operations.append(("dns", kwargs["domain"]))

            def update_state(path, cluster_name, size):
                self.assertTrue(os.path.exists(pending_path))
                operations.append(("state", path, cluster_name, size))
                return True

            namespace["dns_client"] = SimpleNamespace(
                list_zones=list_zones,
                get_rr_set=lambda **kwargs: SimpleNamespace(
                    data=SimpleNamespace(
                        items=[SimpleNamespace(rtype="A", rdata="10.0.0.10")]
                    )
                ),
                delete_rr_set=delete_rr_set,
            )
            namespace["updateTFState"] = update_state
            completed = namespace["finalize_pending_local_block_volume_deletions"](
                inventory_path,
                [record],
                "test-cluster",
                "ocid1.compartment.test",
                current_pool_size=0,
                current_instance_pool_id="ocid1.instancepool.test",
                active_instance_display_names=set(),
                active_instance_private_ips=set(),
            )

            self.assertEqual(completed, 1)
            self.assertFalse(os.path.exists(pending_path))

        self.assertEqual(
            operations,
            [
                ("dns", "test-cluster-node-0.test-cluster.local"),
                ("dns", "compute-hpc-11.test-cluster.local"),
                ("state", inventory_path, "test-cluster", 0),
            ],
        )

    def test_finalize_keeps_journal_when_metadata_or_state_update_fails(self):
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }

        for failure_stage in ["metadata", "state"]:
            with self.subTest(failure_stage=failure_stage):
                namespace = load_resize_functions()
                namespace["dns_entries"] = failure_stage == "metadata"
                namespace["zone_name"] = "test-cluster.local"
                namespace["queue"] = "compute"
                namespace["instance_type"] = "hpc"
                namespace["private_subnet_cidr"] = ipaddress.ip_network("10.0.0.0/24")

                if failure_stage == "metadata":
                    namespace["dns_client"] = SimpleNamespace(
                        list_zones=lambda **kwargs: (_ for _ in ()).throw(
                            RuntimeError("mock metadata failure")
                        )
                    )
                    namespace["updateTFState"] = lambda *args: self.fail(
                        "state update must not run after metadata failure"
                    )
                    expected_error = "mock metadata failure"
                else:
                    namespace["updateTFState"] = lambda *args: (_ for _ in ()).throw(
                        RuntimeError("mock state failure")
                    )
                    expected_error = "mock state failure"

                with tempfile.TemporaryDirectory() as temp_directory:
                    inventory_path = os.path.join(temp_directory, "inventory")
                    namespace["remember_pending_local_block_volume_deletion"](
                        inventory_path,
                        record,
                    )
                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        namespace["finalize_pending_local_block_volume_deletions"](
                            inventory_path,
                            [record],
                            "test-cluster",
                            "ocid1.compartment.test",
                            current_pool_size=0,
                            current_instance_pool_id="ocid1.instancepool.test",
                            active_instance_display_names=set(),
                            active_instance_private_ips=set(),
                        )
                    self.assertEqual(
                        namespace["load_pending_local_block_volume_deletions"](
                            inventory_path
                        ),
                        [record],
                    )

    def test_finalize_rejects_record_from_different_instance_pool_before_updates(self):
        namespace = load_resize_functions()
        namespace["dns_entries"] = True
        namespace["zone_name"] = "test-cluster.local"
        namespace["queue"] = "compute"
        namespace["instance_type"] = "hpc"
        namespace["private_subnet_cidr"] = ipaddress.ip_network("10.0.0.0/24")
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: self.fail(
                "DNS update must not run for a different instance pool"
            ),
            delete_rr_set=lambda **kwargs: self.fail(
                "DNS update must not run for a different instance pool"
            ),
        )
        namespace["updateTFState"] = lambda *args: self.fail(
            "state update must not run for a different instance pool"
        )

        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remember_pending_local_block_volume_deletion"](
                inventory_path,
                record,
            )

            with self.assertRaisesRegex(RuntimeError, "different instance pool"):
                namespace["finalize_pending_local_block_volume_deletions"](
                    inventory_path,
                    [record],
                    "test-cluster",
                    "ocid1.compartment.test",
                    current_pool_size=0,
                    current_instance_pool_id="ocid1.instancepool.other",
                    active_instance_display_names=set(),
                    active_instance_private_ips=set(),
                )

            self.assertEqual(
                namespace["load_pending_local_block_volume_deletions"](
                    inventory_path
                ),
                [record],
            )

    def test_finalize_rejects_pool_size_that_was_not_decremented_before_updates(self):
        namespace = load_resize_functions()
        namespace["dns_entries"] = True
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }
        namespace["dns_client"] = SimpleNamespace(
            list_zones=lambda **kwargs: self.fail(
                "DNS lookup must not run before the instance pool shrinks"
            ),
            delete_rr_set=lambda **kwargs: self.fail(
                "DNS deletion must not run before the instance pool shrinks"
            ),
        )
        namespace["updateTFState"] = lambda *args: self.fail(
            "state update must not run before the instance pool shrinks"
        )

        with tempfile.TemporaryDirectory() as temp_directory:
            inventory_path = os.path.join(temp_directory, "inventory")
            namespace["remember_pending_local_block_volume_deletion"](
                inventory_path,
                record,
            )

            with self.assertRaisesRegex(RuntimeError, "was not decremented"):
                namespace["finalize_pending_local_block_volume_deletions"](
                    inventory_path,
                    [record],
                    "test-cluster",
                    "ocid1.compartment.test",
                    current_pool_size=1,
                    current_instance_pool_id="ocid1.instancepool.test",
                    active_instance_display_names=set(),
                    active_instance_private_ips=set(),
                )

            self.assertEqual(
                namespace["load_pending_local_block_volume_deletions"](
                    inventory_path
                ),
                [record],
            )

    def test_finalize_rejects_reused_hostname_or_ip_before_dns_delete_and_state_update(self):
        record = {
            "cluster_name": "test-cluster",
            "compartment_id": "ocid1.compartment.test",
            "instance_id": "ocid1.instance.test",
            "instance_display_name": "test-cluster-node-0",
            "instance_private_ip": "10.0.0.10",
            "instance_pool_id": "ocid1.instancepool.test",
            "instance_pool_size_before_removal": 1,
            "volume_id": "ocid1.volume.scratch",
            "volume_display_name": "test-cluster-local-scratch",
            "size_in_gbs": 100,
            "vpus_per_gb": 10,
        }

        for reused_identity in ["hostname", "private_ip"]:
            with self.subTest(reused_identity=reused_identity):
                namespace = load_resize_functions()
                namespace["dns_entries"] = True
                namespace["zone_name"] = "test-cluster.local"
                namespace["queue"] = "compute"
                namespace["instance_type"] = "hpc"
                namespace["private_subnet_cidr"] = ipaddress.ip_network("10.0.0.0/24")
                operations = []

                def list_zones(**kwargs):
                    operations.append("dns-list")
                    return SimpleNamespace(
                        data=[SimpleNamespace(id="ocid1.dnszone.test")]
                    )

                namespace["dns_client"] = SimpleNamespace(
                    list_zones=list_zones,
                    delete_rr_set=lambda **kwargs: self.fail(
                        "DNS deletion must not run for a reused node identity"
                    ),
                )
                namespace["updateTFState"] = lambda *args: self.fail(
                    "state update must not run for a reused node identity"
                )
                active_display_names = (
                    {record["instance_display_name"]}
                    if reused_identity == "hostname"
                    else set()
                )
                active_private_ips = (
                    {record["instance_private_ip"]}
                    if reused_identity == "private_ip"
                    else set()
                )

                with tempfile.TemporaryDirectory() as temp_directory:
                    inventory_path = os.path.join(temp_directory, "inventory")
                    namespace["remember_pending_local_block_volume_deletion"](
                        inventory_path,
                        record,
                    )

                    with self.assertRaisesRegex(
                        RuntimeError,
                        "hostname or private IP is in use",
                    ):
                        namespace["finalize_pending_local_block_volume_deletions"](
                            inventory_path,
                            [record],
                            "test-cluster",
                            "ocid1.compartment.test",
                            current_pool_size=0,
                            current_instance_pool_id="ocid1.instancepool.test",
                            active_instance_display_names=active_display_names,
                            active_instance_private_ips=active_private_ips,
                        )

                    self.assertEqual(operations, [])
                    self.assertEqual(
                        namespace["load_pending_local_block_volume_deletions"](
                            inventory_path
                        ),
                        [record],
                    )


if __name__ == "__main__":
    unittest.main()
