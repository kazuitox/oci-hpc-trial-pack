"""Execute the managed OpenComposer backend and UI without OCI or Slurm."""
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'playbooks/roles/openondemand'
UI = ROLE / 'files/opencomposer'


class OpenComposerCanceledStatusTests(unittest.TestCase):
    def test_status_registration_and_restart(self):
        tasks = yaml.safe_load((ROLE / 'tasks/opencomposer.yml').read_text())
        task = next(t for t in tasks if t.get('register') == 'ood_opencomposer_canceled_status')
        self.assertIn('"canceled" => "CANCELED"', task['ansible.builtin.lineinfile']['line'])
        self.assertIn('ood_opencomposer_canceled_status.changed', tasks[-1]['when'])
        self.assertLess(tasks.index(task), next(i for i, t in enumerate(tasks)
                                               if 'Verify the app' in t['name']))

    @unittest.skipUnless(shutil.which('ruby'), 'Ruby is required')
    def test_ruby_backend(self):
        available = subprocess.run(['ruby', '-e',
                                    'require "sqlite3"; require "minitest"; abort unless [].respond_to?(:filter_map)'],
                                   capture_output=True)
        if available.returncode:
            self.skipTest('Ruby 2.7+ with sqlite3 and minitest gems is required')
        tasks = yaml.safe_load((ROLE / 'tasks/opencomposer.yml').read_text())
        registration = next(t['ansible.builtin.lineinfile']['line'] for t in tasks
                            if t.get('register') == 'ood_opencomposer_canceled_status')
        result = subprocess.run(['ruby', str(ROOT / 'tests/opencomposer_canceled_status_test.rb'),
                                 str(UI), registration], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_javascript_filter_and_batch_actions(self):
        result = subprocess.run(['node', str(ROOT / 'tests/opencomposer_canceled_status_test.js'),
                                 str(UI / 'history.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
