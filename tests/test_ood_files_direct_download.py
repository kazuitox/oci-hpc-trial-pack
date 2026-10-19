"""Offline execution of the Files download extension and its deployment wiring."""
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'playbooks/roles/openondemand'


class FilesDirectDownloadTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_download_behaviors(self):
        subprocess.run(['node', '--test', str(ROOT / 'tests/ood_files_direct_download_test.js')], check=True)

    def test_deployment_and_restart(self):
        tasks = yaml.safe_load((ROLE / 'tasks/ood_dashboard_customizations.yml').read_text())
        copies = {t['ansible.builtin.copy']['dest']: t for t in tasks if 'ansible.builtin.copy' in t}
        restart = next(t for t in tasks if 'Request a Passenger restart' in t['name'])
        for dest, register in [
            ('/etc/ood/config/apps/dashboard/views/files/_download_button.html.erb', 'ood_files_download_view'),
            ('/var/www/ood/public/files-direct-download.js', 'ood_files_download_script'),
        ]:
            task = copies[dest]
            self.assertTrue((ROLE / 'files' / task['ansible.builtin.copy']['src']).is_file())
            self.assertEqual(task['register'], register)
            self.assertIn(register + '.changed', restart['when'])
            self.assertLess(tasks.index(task), tasks.index(restart))

    def test_directory_timeout_is_rendered_and_configuration_restarts_dashboard(self):
        import jinja2
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.assertEqual(defaults['ood_download_dir_timeout_seconds'], 300)
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters['bool'] = bool
        template = environment.from_string((ROLE / 'templates/ondemand.yml.j2').read_text())
        for enabled in (False, True):
            for seconds in (300, 600):
                config = yaml.safe_load(template.render(
                    ood_dcv_enabled=enabled, ood_download_dir_timeout_seconds=seconds))
                self.assertEqual(config['download_dir_timeout_seconds'], seconds)
                self.assertIsInstance(config['download_dir_timeout_seconds'], int)
            fallback = yaml.safe_load(template.render(ood_dcv_enabled=enabled))
            self.assertEqual(fallback['download_dir_timeout_seconds'], 300)
        tasks = yaml.safe_load((ROLE / 'tasks/ood_dashboard_customizations.yml').read_text())
        configuration = next(t for t in tasks if t.get('ansible.builtin.template', {}).get('src') == 'ondemand.yml.j2')
        self.assertEqual(configuration['ansible.builtin.template']['dest'], '/etc/ood/config/ondemand.d/ondemand.yml')
        restart = next(t for t in tasks if 'Request a Passenger restart' in t['name'])
        self.assertIn(configuration['register'] + '.changed', restart['when'])

    @unittest.skipUnless(shutil.which('ruby'), 'Ruby is required')
    def test_view_obeys_download_setting_and_uses_nonce(self):
        view = ROLE / 'files/etc/ood/config/apps/dashboard/views/files/_download_button.html.erb'
        ruby = r'''
require 'erb'
require 'ostruct'
require 'pathname'
module Configuration
  def self.download_enabled?; ENV['ENABLED'] == '1'; end
end
@user_configuration = OpenStruct.new(public_url: Pathname.new('/public'))
def content_security_policy_nonce; 'test-nonce'; end
puts ERB.new(File.read(ARGV[0]), trim_mode: '-').result(binding)
'''
        import os
        for enabled in ('0', '1'):
            result = subprocess.run(['ruby', '-e', ruby, str(view)], env={**os.environ, 'ENABLED': enabled}, check=True, text=True, capture_output=True).stdout
            if enabled == '0':
                self.assertNotIn('<button', result)
                self.assertNotIn('<script', result)
            else:
                self.assertIn('id="download-btn"', result)
                self.assertIn('id="ood-direct-download-btn"', result)
                self.assertIn('src="/public/files-direct-download.js?v=9"', result)
                self.assertIn('nonce="test-nonce"', result)
                self.assertIn('type="button" disabled', result)
                self.assertIn('ZIPなし保存を読み込み中', result)
                self.assertIn('<dialog id="ood-direct-download-dialog"', result)
                self.assertIn('id="ood-direct-download-name-form" method="dialog"', result)
                self.assertNotIn('まとめて保存するフォルダ名', result)
                self.assertIn('id="ood-direct-download-summary"', result)
                self.assertIn('id="ood-direct-download-name-submit" type="submit" class="btn btn-primary" hidden disabled', result)
                self.assertIn('class="order-last text-start"', result)
                self.assertIn('class="spinner-border text-primary"', result)
                self.assertIn('<fieldset id="ood-direct-download-name-fields" hidden>', result)
                self.assertIn('id="ood-direct-download-progress"', result)
                self.assertIn('id="ood-direct-download-close"', result)
                self.assertIn('flex: 0 0 100%;', result)
                self.assertIn('overflow-wrap: anywhere;', result)
                # OOD 4.2.5's default Apache alias maps /public to /var/www/ood/public.
                # Resolve the rendered script URL through that alias to its deployed copy.
                import re
                from urllib.parse import urlsplit
                url = urlsplit(re.search(r'<script src="([^"]+)"', result).group(1))
                deployed = '/var/www/ood/public/' + url.path.removeprefix('/public/')
                tasks = yaml.safe_load((ROLE / 'tasks/ood_dashboard_customizations.yml').read_text())
                copy = next(t['ansible.builtin.copy'] for t in tasks
                            if t.get('ansible.builtin.copy', {}).get('dest') == deployed)
                self.assertEqual(copy['src'], 'files-direct-download.js')
                self.assertIn('function install()', (ROLE / 'files' / copy['src']).read_text())
