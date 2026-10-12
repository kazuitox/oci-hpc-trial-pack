"""Protect timezone propagation and its application on lightweight nodes."""
from pathlib import Path
import re
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[1]


class AutoscalingTimezoneTests(unittest.TestCase):
    def test_initial_and_dynamic_nodes_apply_timezone_before_slurm(self):
        for filename, hosts in (("site.yml", "all"),
                                ("new_nodes.yml", "compute"),
                                ("lite_new_nodes.yml", "compute")):
            with self.subTest(playbook=filename):
                plays = yaml.safe_load((ROOT / "playbooks" / filename).read_text())
                timezone_plays = [
                    (i, play) for i, play in enumerate(plays)
                    if "timezone" in play.get("roles", [])
                ]
                self.assertEqual(len(timezone_plays), 1)
                index, play = timezone_plays[0]
                self.assertEqual(play["hosts"], hosts)
                self.assertTrue(play["become"])
                self.assertNotIn("when", play)
                slurm_plays = [
                    i for i, item in enumerate(plays)
                    if any(task.get("include_role", {}).get("name") == "slurm"
                           for task in item.get("tasks", []))
                ]
                self.assertTrue(slurm_plays)
                self.assertTrue(all(index < i for i in slurm_plays))

    def test_controller_and_backup_forward_configured_timezone(self):
        for filename in ("controller.tf", "slurm_ha.tf"):
            with self.subTest(source=filename):
                source = (ROOT / filename).read_text()
                # Both initial inventory and the Autoscaling variable template
                # must use the stack value, including the HA controller.
                for template in ("inventory.tpl", "conf/variables.tpl"):
                    bindings = re.search(
                        r'templatefile\("[^"\n]*/' + re.escape(template)
                        + r'",\s*\{(.*?)\}\)', source, re.S)
                    self.assertIsNotNone(bindings)
                    self.assertRegex(bindings.group(1), r"timezone\s*=\s*var\.timezone\b")
        dynamic = (ROOT / "autoscaling/tf_init/controller_update.tf").read_text()
        self.assertRegex(dynamic, r"timezone\s*=\s*var\.timezone\b")
        variables = (ROOT / "conf/variables.tpl").read_text()
        self.assertIn('variable "timezone" { default = "${timezone}" }', variables)

    def test_inventory_and_role_preserve_non_default_timezones(self):
        env = Environment(undefined=StrictUndefined)
        tasks = yaml.safe_load(
            (ROOT / "playbooks/roles/timezone/tasks/common.yml").read_text())
        setting = next(task["timezone"]["name"] for task in tasks if "timezone" in task)
        for timezone in ("Asia/Tokyo", "America/Los_Angeles", "Etc/UTC"):
            for filename in ("inventory.tpl", "autoscaling/tf_init/inventory.tpl"):
                with self.subTest(timezone=timezone, inventory=filename):
                    source = (ROOT / filename).read_text()
                    variables = source.split("[all:vars]", 1)[1]
                    line = next(line for line in variables.splitlines()
                                if line.startswith("timezone="))
                    rendered = env.from_string(
                        line.replace("${timezone}", "{{ timezone }}")
                    ).render(timezone=timezone)
                    self.assertEqual(rendered, "timezone=" + timezone)
                    value = rendered.split("=", 1)[1]
                    self.assertEqual(env.from_string(setting).render(timezone=value), timezone)

    def test_timezone_role_reaches_setting_and_lite_switch_keeps_playbook(self):
        tasks = yaml.safe_load((ROOT / "playbooks/roles/timezone/tasks/main.yml").read_text())
        self.assertIn({"include_tasks": "common.yml"}, tasks)
        switch = (ROOT / "bin/switch_playbook.sh").read_text()
        self.assertIn("${BASE_DIR}/new_nodes.yml:${BASE_DIR}/lite_new_nodes.yml", switch)


if __name__ == "__main__":
    unittest.main()
