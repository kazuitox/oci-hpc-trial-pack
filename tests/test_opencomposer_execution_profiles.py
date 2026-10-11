import json
import os
import re
import unittest

import jinja2
import yaml


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(
    REPOSITORY_ROOT,
    "playbooks",
    "roles",
    "openondemand",
    "templates",
    "opencomposer_slurm_form.yml.j2",
)


class OpenComposerExecutionProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE_PATH, encoding="utf-8") as template_file:
            cls.template = template_file.read()

    def widget_body(self, key, next_key):
        match = re.search(
            rf"^  {key}:\n(?P<body>.*?)(?=^  {next_key}:)",
            self.template,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match, f"widget {key} was not found")
        return match.group("body")

    def test_execution_profile_defaults_to_non_mpi(self):
        body = self.widget_body("execution_profile", "mpi_profile")

        self.assertIn("label: 実行プロファイル", body)
        self.assertIn("value: {{ 'なし' | to_json }}", body)
        for profile in ("なし", "MPI", "OpenMP"):
            self.assertIn("{{ '" + profile + "' | to_json }}", body)

        for mpi_widget in (
            "mpi_profile",
            "mpi_bm_hpc",
            "mpi_vm",
            "mpi_bm_standard",
        ):
            self.assertIn("enable-" + mpi_widget, body)

    def test_parallelism_fields_follow_the_execution_profile(self):
        profile_body = self.widget_body("execution_profile", "mpi_profile")
        mpi_option = next(
            line
            for line in profile_body.splitlines()
            if "{{ 'MPI' | to_json }}" in line
        )
        openmp_option = next(
            line
            for line in profile_body.splitlines()
            if "{{ 'OpenMP' | to_json }}" in line
        )

        self.assertIn("enable-tasks_per_node", mpi_option)
        self.assertNotIn("enable-cpus_per_task", mpi_option)
        self.assertIn("enable-cpus_per_task", openmp_option)
        self.assertNotIn("enable-tasks_per_node", openmp_option)

        tasks_body = self.widget_body("tasks_per_node", "cpus_per_task")
        cpus_body = self.widget_body("cpus_per_task", "walltime_enabled")
        self.assertIn(
            "label: ノードあたりのタスク数（--ntasks-per-node）", tasks_body
        )
        self.assertIn(
            "label: 1タスクに割り当てるCPU数（--cpus-per-task）", cpus_body
        )
        self.assertIn("#SBATCH --ntasks-per-node=#{tasks_per_node}", self.template)
        self.assertIn("#SBATCH --cpus-per-task=#{cpus_per_task}", self.template)

    def test_mpi_profile_has_three_execution_environments(self):
        body = self.widget_body("mpi_profile", "mpi_bm_hpc")

        self.assertIn("label: MPI実行環境", body)
        self.assertIn("{{ 'BM HPC (RDMA)' | to_json }}", body)
        self.assertIn("{{ 'VM (TCP)' | to_json }}", body)
        self.assertIn("{{ 'BM Standard (TCP)' | to_json }}", body)
        self.assertIn("{{ ['--ntasks-per-core=1', '--exclusive'] | to_json }}", body)
        self.assertIn("{{ ['', '--exclusive'] | to_json }}", body)

    def test_every_mpi_environment_offers_all_mpi_implementations(self):
        widget_pairs = (
            ("mpi_bm_hpc", "mpi_vm"),
            ("mpi_vm", "mpi_bm_standard"),
            ("mpi_bm_standard", "nodes"),
        )

        for key, next_key in widget_pairs:
            with self.subTest(key=key):
                body = self.widget_body(key, next_key)
                for implementation in (
                    "Intel MPI (OneAPI)",
                    "OpenMPI v4.x",
                    "Platform MPI v9.x",
                ):
                    self.assertIn("{{ '" + implementation + "' | to_json }}", body)

    def test_platform_mpi_options_match_each_transport(self):
        rdma_body = self.widget_body("mpi_bm_hpc", "mpi_vm")
        vm_body = self.widget_body("mpi_vm", "mpi_bm_standard")
        standard_body = self.widget_body("mpi_bm_standard", "nodes")

        rdma_options = (
            "-intra=shm -e MPI_HASIC_UDAPL=ofa-v2-cma-roe-ens800f0np0 "
            "-UDAPL -e MPI_FLAGS=y  -d -v -prot"
        )
        tcp_options = "-intra=shm -TCP -e MPI_FLAGS=y  -d -v -prot"

        self.assertIn(rdma_options, rdma_body)
        self.assertNotIn(tcp_options, rdma_body)
        self.assertIn(tcp_options, vm_body)
        self.assertIn(tcp_options, standard_body)

    def test_intel_mpi_tcp_options_preserve_existing_flags_and_disable_external_collectives(self):
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters["to_json"] = json.dumps
        environment.filters["bool"] = bool
        expected_options = (
            "-genv I_MPI_FABRICS=shm:ofi -genv I_MPI_OFI_PROVIDER=tcp "
            "-genv I_MPI_DEBUG=5 -genv I_MPI_COLL_EXTERNAL 0"
        )
        for notifications_enabled in (False, True):
            with self.subTest(notifications_enabled=notifications_enabled):
                rendered = environment.from_string(self.template).render(
                    queues=[{
                        "name": "compute",
                        "instance_types": [{"name": "hpc", "default": True}],
                    }],
                    slurm_job_notifications_enabled=notifications_enabled,
                )
                form = yaml.safe_load(rendered)
                for widget in ("mpi_vm", "mpi_bm_standard"):
                    options = dict(form["form"][widget]["options"])
                    self.assertEqual(options["Intel MPI (OneAPI)"], expected_options)
                    self.assertIn('export MPI_OPTIONS="#{' + widget + '}"', form["script"])
                for widget in ("mpi_bm_hpc", "mpi_vm", "mpi_bm_standard"):
                    for label, options in form["form"][widget]["options"]:
                        if widget == "mpi_bm_hpc" or label != "Intel MPI (OneAPI)":
                            self.assertNotIn("I_MPI_COLL_EXTERNAL", options)

    def test_profile_specific_lines_are_conditionally_generated(self):
        for line in (
            "export OMP_NUM_THREADS=",
            "export OMP_PROC_BIND=close",
            "export OMP_DISPLAY_AFFINITY=TRUE",
        ):
            self.assertIn(line, self.template)

        for placeholder in (
            "#{mpi_profile_1}",
            "#{mpi_profile_2}",
            "#{mpi_bm_hpc}",
            "#{mpi_vm}",
            "#{mpi_bm_standard}",
            "#{execution_profile_1}",
            "#{execution_profile_2}",
            "#{execution_profile_3}",
        ):
            self.assertIn(placeholder, self.template)

        self.assertNotIn("#{mpi_shape", self.template)


if __name__ == "__main__":
    unittest.main()
