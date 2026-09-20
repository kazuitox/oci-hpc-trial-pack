import os
import re
import unittest


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_repository_file(*path_parts):
    with open(os.path.join(REPOSITORY_ROOT, *path_parts), encoding="utf-8") as source:
        return source.read()


class SlurmNotificationDestroyCleanupTerraformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notifications = read_repository_file("notifications.tf")
        cls.locals = read_repository_file("locals.tf")
        cls.versions = read_repository_file("versions.tf")
        cls.ansible_tasks = read_repository_file(
            "playbooks", "roles", "slurm", "tasks", "notifications.yml"
        )
        cls.inventory = read_repository_file("inventory.tpl")
        cls.autoscaling_inventory = read_repository_file(
            "autoscaling", "tf_init", "inventory.tpl"
        )
        cls.autoscaling_controller = read_repository_file(
            "autoscaling", "tf_init", "controller_update.tf"
        )
        cls.generated_variables = read_repository_file("conf", "variables.tpl")
        cls.controller = read_repository_file("controller.tf")
        cls.slurm_ha = read_repository_file("slurm_ha.tf")
        cls.bootstrap = read_repository_file(
            "playbooks",
            "roles",
            "slurm",
            "templates",
            "slurm_notification_bootstrap.json.j2",
        )
        cls.cluster_cli = read_repository_file(
            "playbooks", "roles", "cluster-cli", "files", "cluster"
        )

    def cleanup_resource(self):
        match = re.search(
            r'resource\s+"terraform_data"\s+"slurm_notification_runtime_cleanup"\s*'
            r'\{(?P<body>.*)\n\}',
            self.notifications,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        return match.group("body")

    def test_cleanup_anchor_requires_terraform_1_5(self):
        self.assertRegex(
            self.versions,
            r'required_version\s*=\s*"~>\s*1\.5\.0(?:\s*,[^"\n]+)?"',
        )

    def test_cleanup_anchor_exists_when_notifications_are_enabled(self):
        resource = self.cleanup_resource()
        self.assertIn(
            "count = var.slurm_job_notifications_enabled ? 1 : 0",
            resource,
        )

    def test_notifications_require_slurm_without_destroying_the_anchor(self):
        resource = self.cleanup_resource()
        self.assertIn("slurm_enabled = var.slurm", resource)
        self.assertRegex(
            resource,
            r"(?s)precondition\s*\{.*?condition\s*=\s*var\.slurm",
        )

    def test_cleanup_anchor_preserves_connection_and_config_in_self_output(self):
        resource = self.cleanup_resource()
        for assignment in (
            "host          = local.host",
            "user          = local.controller_username",
            "private_key   = tls_private_key.ssh.private_key_pem",
            "config_base64 = local.slurm_notification_destroy_cleanup_config_base64",
        ):
            self.assertIn(assignment, resource)

        self.assertIn("host        = self.output.host", resource)
        self.assertIn("user        = self.output.user", resource)
        self.assertIn("private_key = self.output.private_key", resource)
        self.assertIn("'${self.output.config_base64}'", resource)

    def test_cleanup_anchor_is_not_replaced_during_normal_updates(self):
        resource = self.cleanup_resource()
        self.assertNotIn("triggers_replace", resource)
        self.assertIn("teardown sentinel", resource)

    def test_cleanup_runs_before_controller_subscription_and_policy_destroy(self):
        resource = self.cleanup_resource()
        for dependency in (
            "null_resource.cluster",
            "oci_ons_subscription.slurm_local_user_email",
            "oci_identity_policy.slurm_notification_controllers",
        ):
            self.assertIn(dependency, resource)
        self.assertRegex(resource, r"when\s*=\s*destroy")
        self.assertRegex(resource, r"on_failure\s*=\s*fail")
        self.assertIn(
            "/usr/local/libexec/oci-hpc/slurm-notification-destroy-cleanup "
            "--config-base64",
            resource,
        )

    def test_destroy_provisioner_references_only_self_output(self):
        resource = self.cleanup_resource()
        marker = 'provisioner "remote-exec" {'
        self.assertIn(marker, resource)
        body = resource[resource.index(marker) :]
        self.assertNotRegex(
            body,
            r"\b(?:local|var|tls_private_key|oci_core_instance|"
            r"oci_ons_subscription|oci_identity_policy)\.",
        )

    def test_cleanup_payload_contains_scope_and_protects_terraform_topic(self):
        payload = re.search(
            r"slurm_notification_destroy_cleanup_config_base64\s*=\s*"
            r"base64encode\(jsonencode\(\{(?P<body>.*?)\n\s*\}\)\)",
            self.locals,
            re.DOTALL,
        )
        self.assertIsNotNone(payload)
        body = payload.group("body")
        for key in (
            "region",
            "compartment_id",
            "cluster_name",
            "cluster_scope_id",
            "deployment_id",
            "registry_path",
            "protected_topic_ids",
        ):
            self.assertRegex(body, r"(?m)^\s*" + key + r"\s*=")
        self.assertIn("[local.slurm_notification_topic_id]", body)

    def test_deployment_id_is_stable_and_tags_all_notification_resources(self):
        self.assertRegex(
            self.locals,
            r"slurm_notification_deployment_id\s*=\s*substr\(sha256\("
            r"tls_private_key\.ssh\.public_key_openssh\),\s*0,\s*32\)",
        )
        self.assertEqual(
            self.notifications.count('"notification_deployment_id"'),
            4,
        )
        self.assertEqual(
            self.notifications.count("local.slurm_notification_deployment_id"),
            4,
        )

    def test_deployment_id_is_plumbed_to_ansible_and_autoscaling(self):
        inventory_assignment = (
            "slurm_notification_deployment_id="
            "${jsonencode(slurm_notification_deployment_id)}"
        )
        self.assertIn(inventory_assignment, self.inventory)
        self.assertIn(inventory_assignment, self.autoscaling_inventory)
        self.assertIn(
            'variable "slurm_notification_deployment_id"',
            self.generated_variables,
        )
        self.assertEqual(
            self.controller.count(
                "slurm_notification_deployment_id = "
                "local.slurm_notification_deployment_id"
            ),
            2,
        )
        self.assertEqual(
            self.slurm_ha.count(
                "slurm_notification_deployment_id = "
                "local.slurm_notification_deployment_id"
            ),
            2,
        )
        self.assertIn(
            "slurm_notification_deployment_id = "
            "var.slurm_notification_deployment_id",
            self.autoscaling_controller,
        )
        self.assertIn(
            '"deployment_id": {{ slurm_notification_deployment_id | to_json }}',
            self.bootstrap,
        )

    def test_cluster_cli_tags_topics_with_the_deployment_id(self):
        self.assertIn("'deployment_id'", self.cluster_cli)
        self.assertIn(
            "'notification_deployment_id': config['deployment_id']",
            self.cluster_cli,
        )

    def test_destroy_helper_is_always_installed_and_affects_payload_hash(self):
        task = re.search(
            r"- name: Slurm notifications \| Install destroy cleanup helper\n"
            r"(?P<body>.*?)(?=\n- name:|\Z)",
            self.ansible_tasks,
            re.DOTALL,
        )
        self.assertIsNotNone(task)
        self.assertIn("src: slurm_oci_notification_cleanup.py", task.group("body"))
        self.assertIn(
            "dest: /usr/local/libexec/oci-hpc/slurm-notification-destroy-cleanup",
            task.group("body"),
        )
        self.assertNotRegex(task.group("body"), r"(?m)^\s*when:")
        self.assertIn(
            'filesha256("${path.module}/playbooks/roles/slurm/files/'
            'slurm_oci_notification_cleanup.py")',
            self.locals,
        )


if __name__ == "__main__":
    unittest.main()
