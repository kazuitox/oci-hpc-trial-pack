import os
from pathlib import Path
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class InitialCostTagSelectionTests(unittest.TestCase):
    """Execute the production selection block without provisioning resources."""

    def select_tag(self, queue_tag="EMPTY", instance_tag="EMPTY", cli_tag=None, debug=False):
        source = (REPOSITORY_ROOT / "bin" / "create_cluster.sh").read_text()
        selection = source[source.index('cli_tags=""'):source.index("# sed 安全化")]
        # yq's two queries are isolated behind a stub; the actual shell branch,
        # pipeline handling of absent values, and argument precedence execute.
        harness = """
debug="$TEST_DEBUG"
queues_conf=/unused/queues.conf
yq() {
  case "$2" in
    *'.instance_types'*) printf '%s\\n' "$TEST_INSTANCE_TAG" ;;
    *) printf '%s\\n' "$TEST_QUEUE_TAG" ;;
  esac
}
""" + selection + '\nprintf "%s" "$tags"\n'
        arguments = ["1", "cluster", "shape", "queue", "123"]
        if cli_tag is not None:
            arguments.append(cli_tag)
        if debug:
            arguments.append("-DEBUG")
        environment = dict(os.environ)
        environment.update(
            TEST_DEBUG="1" if debug else "0",
            TEST_QUEUE_TAG=queue_tag,
            TEST_INSTANCE_TAG=instance_tag,
            USER="controller-service-account",
        )
        result = subprocess.run(
            ["bash", "-c", harness, "tag-selection-test"] + arguments,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def test_absent_tag_uses_management_instead_of_service_account(self):
        self.assertEqual(self.select_tag(), "Management")

    def test_queue_override_is_preserved(self):
        self.assertEqual(self.select_tag(queue_tag="Research"), "Research")

    def test_instance_type_override_is_used_when_queue_tag_is_absent(self):
        self.assertEqual(self.select_tag(instance_tag="Engineering"), "Engineering")

    def test_queue_override_precedes_instance_type_override(self):
        self.assertEqual(
            self.select_tag(queue_tag="Research", instance_tag="Engineering"),
            "Research",
        )

    def test_explicit_cli_override_precedes_queue_override(self):
        self.assertEqual(
            self.select_tag(queue_tag="Research", cli_tag="Project A"),
            "Project A",
        )

    def test_debug_switch_is_not_used_as_a_tag(self):
        self.assertEqual(self.select_tag(debug=True), "Management")

    def test_explicit_cli_override_is_retained_in_debug_mode(self):
        self.assertEqual(self.select_tag(cli_tag="Research", debug=True), "Research")


if __name__ == "__main__":
    unittest.main()
