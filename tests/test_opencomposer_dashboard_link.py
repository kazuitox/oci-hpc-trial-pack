import json
import os
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
    "opencomposer_dashboard_manifest.yml.j2",
)
HISTORY_TEMPLATE_PATH = os.path.join(
    REPOSITORY_ROOT,
    "playbooks",
    "roles",
    "openondemand",
    "templates",
    "opencomposer_history_manifest.yml.j2",
)
ONDEMAND_TEMPLATE_PATH = os.path.join(
    REPOSITORY_ROOT,
    "playbooks",
    "roles",
    "openondemand",
    "templates",
    "ondemand.yml.j2",
)
WIDGET_TEMPLATE_PATH = os.path.join(
    REPOSITORY_ROOT,
    "playbooks",
    "roles",
    "openondemand",
    "templates",
    "opencomposer_dashboard_widget.html.erb.j2",
)
FRAME_WIDGET_PATH = os.path.join(
    REPOSITORY_ROOT,
    "playbooks",
    "roles",
    "openondemand",
    "files",
    "etc",
    "ood",
    "config",
    "apps",
    "dashboard",
    "views",
    "widgets",
    "_opencomposer_frame.html.erb",
)
OPENCOMPOSER_TASKS_PATH = os.path.join(
    REPOSITORY_ROOT,
    "playbooks",
    "roles",
    "openondemand",
    "tasks",
    "opencomposer.yml",
)


class OpenComposerDashboardLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE_PATH, encoding="utf-8") as template_file:
            cls.template = template_file.read()
        with open(HISTORY_TEMPLATE_PATH, encoding="utf-8") as template_file:
            cls.history_template = template_file.read()
        with open(ONDEMAND_TEMPLATE_PATH, encoding="utf-8") as template_file:
            cls.ondemand_template = template_file.read()
        with open(WIDGET_TEMPLATE_PATH, encoding="utf-8") as template_file:
            cls.widget_template = template_file.read()
        with open(FRAME_WIDGET_PATH, encoding="utf-8") as frame_widget_file:
            cls.frame_widget = frame_widget_file.read()
        with open(OPENCOMPOSER_TASKS_PATH, encoding="utf-8") as tasks_file:
            cls.opencomposer_tasks = tasks_file.read()

        environment = jinja2.Environment()
        environment.filters["basename"] = os.path.basename
        environment.filters["bool"] = bool
        environment.filters["to_json"] = json.dumps
        cls.manifest_template = environment.from_string(cls.template)
        cls.history_manifest_template = environment.from_string(
            cls.history_template
        )
        cls.ondemand_config_template = environment.from_string(
            cls.ondemand_template
        )
        cls.dashboard_widget_template = environment.from_string(
            cls.widget_template
        )

    def render_manifest(self, install_dir):
        rendered = self.manifest_template.render(
            ood_opencomposer_install_dir=install_dir
        )
        return yaml.safe_load(rendered)

    def test_slurm_link_is_grouped_under_opencomposer(self):
        manifest = self.render_manifest(
            "/var/www/ood/apps/sys/OpenComposer"
        )
        self.assertEqual(manifest["name"], "Slurmジョブ")
        self.assertEqual(manifest["category"], "03 ジョブ管理")
        self.assertEqual(manifest["icon"], "fas://terminal")
        self.assertEqual(
            manifest["url"],
            "/pun/sys/dashboard/custom/opencomposer_slurm",
        )
        self.assertFalse(manifest["new_window"])

    def test_history_link_is_grouped_under_opencomposer(self):
        manifest = yaml.safe_load(
            self.history_manifest_template.render()
        )
        self.assertEqual(manifest["name"], "History")
        self.assertEqual(manifest["category"], "03 ジョブ管理")
        self.assertEqual(manifest["icon"], "fas://history")
        self.assertEqual(
            manifest["url"],
            "/pun/sys/dashboard/custom/opencomposer_history",
        )
        self.assertFalse(manifest["new_window"])

    def test_dashboard_defines_both_opencomposer_custom_pages(self):
        config = yaml.safe_load(
            self.ondemand_config_template.render(ood_dcv_enabled=False)
        )

        self.assertIn("sys/OpenComposer", config["pinned_apps"])
        self.assertIn("sys/OpenComposerHistory", config["pinned_apps"])
        self.assertEqual(
            config["custom_pages"]["opencomposer_slurm"]["rows"][0][
                "columns"
            ][0]["widgets"],
            ["opencomposer_slurm"],
        )
        self.assertEqual(
            config["custom_pages"]["opencomposer_history"]["rows"][0][
                "columns"
            ][0]["widgets"],
            ["opencomposer_history"],
        )

    def test_widget_embeds_the_configured_opencomposer_endpoint(self):
        rendered = self.dashboard_widget_template.render(
            ood_opencomposer_install_dir=(
                "/var/www/ood/apps/sys/CustomComposer"
            ),
            opencomposer_widget_title="History",
            opencomposer_widget_endpoint="history",
        )

        self.assertIn('title: "History"', rendered)
        self.assertIn(
            'path: "/pun/sys/CustomComposer/history"', rendered
        )

    def test_standard_job_apps_are_hidden_and_other_launchers_remain(self):
        for dcv_enabled in (False, True):
            with self.subTest(dcv_enabled=dcv_enabled):
                config = yaml.safe_load(
                    self.ondemand_config_template.render(
                        ood_dcv_enabled=dcv_enabled
                    )
                )
                self.assertNotIn("sys/activejobs", config["pinned_apps"])
                self.assertNotIn("sys/myjobs", config["pinned_apps"])
                for app in (
                    "files", "bc_desktop", "shell", "system-status",
                    "OpenComposer", "OpenComposerHistory",
                ):
                    self.assertIn("sys/" + app, config["pinned_apps"])
                self.assertEqual(
                    "sys/dcv" in config["pinned_apps"], dcv_enabled
                )

        # OOD 4.2.5's OodApp.should_appear_in_nav? excludes an empty
        # category; navigation and All Apps both use that app list.
        for app in ("activejobs", "myjobs"):
            with self.subTest(app=app):
                path = os.path.join(
                    REPOSITORY_ROOT, "playbooks", "roles", "openondemand",
                    "files", "var", "www", "ood", "apps", "sys", app,
                    "manifest.yml",
                )
                with open(path, encoding="utf-8") as manifest_file:
                    manifest = yaml.safe_load(manifest_file)
                self.assertEqual(manifest["category"], "")

    def test_manifest_updates_restart_dashboard_after_installation(self):
        tasks_path = os.path.join(
            REPOSITORY_ROOT, "playbooks", "roles", "openondemand",
            "tasks", "ood_dashboard_customizations.yml",
        )
        with open(tasks_path, encoding="utf-8") as tasks_file:
            tasks = yaml.safe_load(tasks_file)
        override_index = next(
            i for i, task in enumerate(tasks)
            if task.get("register") == "ood_system_app_overrides"
        )
        overrides = tasks[override_index]
        self.assertIn("activejobs/manifest.yml", overrides["loop"])
        self.assertIn("myjobs/manifest.yml", overrides["loop"])
        self.assertIn("projects/manifest.yml", overrides["loop"])
        self.assertIn("module-browser/manifest.yml", overrides["loop"])
        restart_index = next(
            i for i, task in enumerate(tasks)
            if task.get("ansible.builtin.file", {}).get("path")
            == "/var/www/ood/apps/sys/dashboard/tmp/restart.txt"
        )
        self.assertGreater(restart_index, override_index)
        self.assertIn(
            "ood_system_app_overrides.changed", tasks[restart_index]["when"]
        )
        for manifest in (
            "ood_opencomposer_dashboard_manifest",
            "ood_opencomposer_history_manifest",
        ):
            self.assertIn(manifest + ".changed", tasks[restart_index]["when"])

    def test_project_manager_and_module_browser_are_hidden(self):
        for dcv_enabled in (False, True):
            config = yaml.safe_load(
                self.ondemand_config_template.render(ood_dcv_enabled=dcv_enabled)
            )
            # Keep OOD's automatic grouping: it removes categories with no
            # visible apps while retaining categories with other apps.
            self.assertNotIn("nav_bar", config)
            self.assertNotIn("nav_categories", config)
            for app, name, url in (
                ("projects", "Project Manager", "projects"),
                ("module-browser", "Module Browser", "module-browser"),
            ):
                with self.subTest(dcv_enabled=dcv_enabled, app=app):
                    self.assertNotIn("sys/" + app, config["pinned_apps"])
                    path = os.path.join(
                        REPOSITORY_ROOT, "playbooks", "roles", "openondemand",
                        "files", "var", "www", "ood", "apps", "sys", app,
                        "manifest.yml",
                    )
                    with open(path, encoding="utf-8") as manifest_file:
                        manifest = yaml.safe_load(manifest_file)
                    self.assertEqual(manifest["category"], "")
                    self.assertEqual(manifest["name"], name)
                    self.assertEqual(manifest["url"], url)
                    self.assertFalse(manifest["new_window"])
                    if app == "projects":
                        self.assertEqual(manifest["role"], "projects")

    def test_frame_keeps_the_ood_header_and_hides_opencomposer_nav(self):
        self.assertIn('class="opencomposer-frame"', self.frame_widget)
        self.assertIn(
            'querySelector("#_form_container > nav")',
            self.frame_widget,
        )
        self.assertIn(
            'document.body.classList.add("opencomposer-custom-page")',
            self.frame_widget,
        )
        self.assertIn(
            'nonce="<%= content_security_policy_nonce %>"',
            self.frame_widget,
        )

    def test_frame_id_is_not_html_escaped_inside_javascript(self):
        self.assertIn(
            "document.getElementById(<%= raw(frame_id.to_json) %>)",
            self.frame_widget,
        )

    def test_frame_remains_visible_when_javascript_fails(self):
        self.assertNotIn("visibility: hidden", self.frame_widget)

    def test_opencomposer_system_app_is_staged_before_first_user_access(self):
        config_path = (
            "/var/lib/ondemand-nginx/config/apps/sys/"
            "{{ ood_opencomposer_install_dir | basename }}.conf"
        )
        self.assertGreaterEqual(
            self.opencomposer_tasks.count(config_path),
            2,
        )
        self.assertIn(
            "/opt/ood/nginx_stage/sbin/update_nginx_stage",
            self.opencomposer_tasks,
        )
        self.assertIn(
            "ood_opencomposer_nginx_config_before.stat.size",
            self.opencomposer_tasks,
        )
        self.assertIn(
            "ood_opencomposer_nginx_config_after.stat.size",
            self.opencomposer_tasks,
        )


if __name__ == "__main__":
    unittest.main()
