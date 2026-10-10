"""Ubuntu bc_desktop launcher: real ERB/Bash with a stub session bus."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'playbooks/roles/openondemand'
TEMPLATE = ROLE / 'files/var/www/ood/apps/sys/bc_desktop/template/script.sh.erb'


@unittest.skipUnless(shutil.which('ruby'), 'Ruby is required to render ERB')
class DesktopSessionTests(unittest.TestCase):
    def run_launcher(self, desktop, status=0, bus=True, signal=False):
        with tempfile.TemporaryDirectory(prefix='ood-desktop-') as tmp:
            root = Path(tmp)
            home = root / 'home'
            bad_bin = home / 'bin'
            bin_dir = root / 'bin'
            staged = root / 'staged desktop'
            for p in (bad_bin, bin_dir, staged / 'desktops'):
                p.mkdir(parents=True)
            for command in ('bash', 'tr', 'grep', 'cut'):
                (bin_dir / command).symlink_to(shutil.which(command))
            # macOS head lacks GNU head -c -1 used by the upstream SAFE_PATH.
            head = bin_dir / 'head'
            head.write_text(f'#!{sys.executable}\nimport sys\n'
                            'assert sys.argv[1:] == ["-c", "-1"]\n'
                            'sys.stdout.buffer.write(sys.stdin.buffer.read()[:-1])\n')
            head.chmod(0o755)
            for name, content in {
                'module': '#!/bin/bash\nexit 0\n',
                'getent': '#!/bin/bash\necho "user:x:1000:1000::/home/user:/bin/bash"\n',
                'xfconf-query': '#!/bin/bash\n[[ "$DBUS_SESSION_BUS_ADDRESS" == isolated ]] || exit 91\necho configured >> "$TRACE"\n',
            }.items():
                p = bin_dir / name
                p.write_text(content)
                p.chmod(0o755)
            # A user/Conda binary must never be selected by SAFE_PATH.
            (bad_bin / 'dbus-run-session').write_text('#!/bin/bash\nexit 92\n')
            (bad_bin / 'dbus-run-session').chmod(0o755)
            if bus:
                p = bin_dir / 'dbus-run-session'
                p.write_text('#!/bin/bash\n[[ ${SESSION_MANAGER+x} ]] && exit 93\n'
                             '[[ "$1" == -- ]] || exit 94\nshift\n'
                             'export DBUS_SESSION_BUS_ADDRESS=isolated\nexec "$@"\n')
                p.chmod(0o755)
            body = ('xfconf-query\n' if desktop == 'xfce' else
                    '[[ "$DBUS_SESSION_BUS_ADDRESS" == inherited ]] || return 95\n'
                    '[[ "$SESSION_MANAGER" == inherited ]] || return 96\n')
            body += 'echo desktop >> "$TRACE"\n'
            body += 'kill -TERM $$\n' if signal else (
                f'exit {status}\n' if desktop == 'xfce' else f'return {status}\n')
            (staged / 'desktops' / f'{desktop}.sh').write_text(body)
            ruby = '''require "erb"; require "pathname"; require "ostruct"
context = OpenStruct.new(desktop: ARGV[1])
session = OpenStruct.new(staged_root: Pathname.new(ARGV[2]))
print ERB.new(File.read(ARGV[0])).result(binding)
'''
            render = subprocess.run(['ruby', '-e', ruby, str(TEMPLATE), desktop, str(staged)],
                                    check=True, text=True, capture_output=True)
            launcher = root / 'script.sh'
            launcher.write_text(render.stdout)
            subprocess.run(['bash', '-n', str(launcher)], check=True, capture_output=True)
            env = dict(os.environ, HOME=str(home), PATH=f'{bad_bin}:{bin_dir}',
                       USER='user', TRACE=str(root / 'trace'),
                       SESSION_MANAGER='inherited', DBUS_SESSION_BUS_ADDRESS='inherited')
            result = subprocess.run([shutil.which('bash'), str(launcher)],
                                    env=env, text=True, capture_output=True)
            trace = (root / 'trace').read_text() if (root / 'trace').exists() else ''
            return result, trace

    def test_xfce_entire_script_is_isolated_and_status_propagates(self):
        for status in (0, 1, 42, 143):
            with self.subTest(status=status):
                result, trace = self.run_launcher('xfce', status)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(trace, 'configured\ndesktop\n')
                self.assertIn(f'ended with {status} status', result.stdout)

    def test_sigterm_status_propagates(self):
        result, trace = self.run_launcher('xfce', signal=True)
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertEqual(trace, 'configured\ndesktop\n')

    def test_other_desktops_keep_source_and_status(self):
        for desktop in ('gnome', 'mate'):
            for status in (0, 42, 143):
                with self.subTest(desktop=desktop, status=status):
                    result, trace = self.run_launcher(desktop, status, bus=False)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(trace, 'desktop\n')

    def test_missing_safe_path_bus_fails_before_desktop(self):
        result, trace = self.run_launcher('xfce', bus=False)
        self.assertEqual(result.returncode, 127)
        self.assertIn('dbus-run-session is required', result.stderr)
        self.assertEqual(trace, '')

    def test_distribution_and_provisioning_paths(self):
        tasks = yaml.safe_load((ROLE / 'tasks/ood_dashboard_customizations.yml').read_text())
        task = next(t for t in tasks if t['name'].endswith('Ubuntu desktop session launcher'))
        self.assertEqual(task['ansible.builtin.copy']['dest'],
                         '/var/www/ood/apps/sys/bc_desktop/template/script.sh.erb')
        self.assertEqual(task['when'], ["ansible_distribution == 'Ubuntu'",
                                       "ansible_distribution_version == '24.04'"])
        desktop = yaml.safe_load((ROOT / 'playbooks/roles/desktop/tasks/main.yml').read_text())
        task = next(t for t in desktop if 'ansible.builtin.apt' in t)
        self.assertEqual(task['ansible.builtin.apt']['name'], 'dbus-daemon')
        self.assertIn("queue | default('') == ood_vnc_queue", task['when'])
        self.assertIn("ansible_distribution == 'Ubuntu'", task['when'])
        self.assertIn("ansible_distribution_version == '24.04'", task['when'])
        for name in ('site.yml', 'new_nodes.yml', 'lite_new_nodes.yml'):
            plays = yaml.safe_load((ROOT / 'playbooks' / name).read_text())
            calls = [t for p in plays if p.get('hosts') == 'compute'
                     for t in p.get('tasks', []) if t.get('include_role', {}).get('name') == 'desktop']
            self.assertTrue(calls, name)
            self.assertIn('use_ood | default(false) | bool', calls[0]['when'])


if __name__ == '__main__':
    unittest.main()
