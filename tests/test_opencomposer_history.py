"""Managed OpenComposer History configuration, ERB and refresh regressions."""
from pathlib import Path
import json
import shutil
import subprocess
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'playbooks/roles/openondemand'
UI = ROLE / 'files/opencomposer'


def history_config():
    env = jinja2.Environment()
    env.filters['to_json'] = json.dumps
    env.filters['bool'] = bool
    text = env.from_string((ROLE / 'templates/opencomposer_conf.yml.erb.j2').read_text()).render(
        ood_opencomposer_apps_dir='/apps', ood_servername='controller.example',
        ood_opencomposer_scheduler_bin='/usr/bin', ood_opencomposer_copy_environment=False)
    return yaml.safe_load(text)


class OpenComposerHistoryTests(unittest.TestCase):
    def test_fields_preserve_defaults_and_use_slurm_accounting_keys(self):
        fields = history_config()['history']
        self.assertEqual(fields[:3], ['OC_HISTORY_JOB_NAME', 'OC_HISTORY_PARTITION',
                                    'OC_HISTORY_SUBMISSION_TIME'])
        self.assertEqual(fields[3:], [
            {'Start': {'label': 'Start Time'}}, {'End': {'label': 'End Time'}},
            {'Elapsed': {'label': 'Elapsed Time'}}, {'CPUTime': {'label': 'CPU Time'}}])

    def test_managed_files_are_deployed_before_verification_and_restart(self):
        tasks = yaml.safe_load((ROLE / 'tasks/opencomposer.yml').read_text())
        index = next(i for i, t in enumerate(tasks) if t.get('register') == 'ood_opencomposer_history_ui')
        task = tasks[index]
        self.assertEqual(task['loop'], [
            {'src': 'history.erb', 'dest': 'views/history.erb'},
            {'src': 'history-refresh.js', 'dest': 'public/history-refresh.js'},
            {'src': 'LICENSE', 'dest': 'LICENSE'}])
        self.assertEqual(task['ansible.builtin.copy']['mode'], '0644')
        self.assertIn('Verify the app', tasks[index + 1]['name'])
        self.assertIn('ood_opencomposer_history_ui.changed', tasks[-1]['when'])
        self.assertIn('Copyright (c) 2024 RIKEN R-CCS', (UI / 'LICENSE').read_text())

    @unittest.skipUnless(shutil.which('ruby'), 'Ruby is required for ERB execution')
    def test_erb_populated_and_empty_results(self):
        harness = r'''
require 'erb'
require 'date'
require 'time'
JOB_SUBMISSION_TIME='Submission Time'
JOB_ID='id'; JOB_APP_NAME='app'; JOB_DIR_NAME='dir'; JOB_STATUS_ID='status'
JOB_KEYS='keys'; HEADER_SCRIPT_LOCATION='location'; HEADER_SCRIPT_NAME='script'
HISTORY_KEY_MAP={}
@conf={'description_color'=>'#123456'}; @detail_open='false'; @filter=''
@filter_mode='and'; @filter_column_items=[]; @date_range_items=[]; @date_range='all'
@statuses=['running']; @script_name='/pun/sys/OpenComposer'; @apps_dir='/apps'
@my_ood_url=''; @login_node=nil; @rows=10; @current_page=1; @page_size=1
@start_index=0; @end_index=0; @history_search_elapsed_label='0.001'
@history_hash={JOB_SUBMISSION_TIME=>'Submission Time', 'Start'=>'Start Time', 'End'=>'End Time', 'Elapsed'=>'Elapsed Time', 'CPUTime'=>'CPU Time'}
def output_action_modal(*); ''; end
def output_action_badge(*); ''; end
def output_job_id_modal(*); '<div class="modal"></div>'; end
def output_job_script_modal(*); ''; end
def output_history_sort_controls(*); ''; end
def history_highlight_filter(*); ''; end
def job_details_modal_matches_filter?(*); false; end
def job_script_modal_matches_filter?(*); false; end
def create_manifest(*); nil; end
def output_related_apps_icon(*); []; end
def output_status(v); v; end
def output_pagination(*); 'pagination'; end
# Match the pinned OpenComposer v2.0.2 formatter used for Submission Time.
def format_history_table_value(key, value)
  return value unless key == JOB_SUBMISSION_TIME
  Time.parse(value.to_s).strftime("%Y-%m-%d %H:%M:%S")
rescue ArgumentError
  value
end
def output_text(v,*); ERB::Util.html_escape(v.to_s); end
@jobs=ARGV[1]=='empty' ? [] : [{JOB_ID=>'42', JOB_APP_NAME=>'Slurm', JOB_DIR_NAME=>'slurm',
HEADER_SCRIPT_LOCATION=>'/work', HEADER_SCRIPT_NAME=>'job.sh', JOB_STATUS_ID=>'Running',
JOB_SUBMISSION_TIME=>'2026-10-11T09:00:00',
'Start'=>'2026-10-11T10:00:00', 'End'=>'2026-10-11T15:08:38', 'Elapsed'=>'01:00:00', 'CPUTime'=>'08:00:00'}]
unless @jobs.empty?
  case ARGV[1]
  when 'unknown'
    @jobs[0]['Start']='Unknown'; @jobs[0]['End']='Unknown'
  when 'blank'
    @jobs[0]['Start']=nil; @jobs[0]['End']=''
  when 'formatted'
    @jobs[0]['Start']='2026-10-11 10:00:00'; @jobs[0]['End']='2026-10-11 15:08:38'
  end
end
@jobs_size=@jobs.size
puts ERB.new(File.read(ARGV[0])).result(binding)
'''
        for state in ['populated', 'empty', 'unknown', 'blank', 'formatted']:
            result = subprocess.run(['ruby', '-e', harness, str(UI / 'history.erb'), state],
                                    capture_output=True, text=True, check=True)
            html = result.stdout
            self.assertIn('id="_historyRefresh"', html)
            self.assertIn('id="_historyResults"', html)
            self.assertLess(html.index('</div>\n<script src="./history.js">'),
                            html.index('history-refresh.js'))
            if state != 'empty':
                for text in ['Start Time', 'End Time', 'Elapsed Time', 'CPU Time',
                             '2026-10-11 09:00:00', '01:00:00', '08:00:00']:
                    self.assertIn(text, html)
                if state in ('populated', 'formatted'):
                    self.assertIn('2026-10-11 10:00:00', html)
                    self.assertIn('2026-10-11 15:08:38', html)
                    self.assertNotIn('2026-10-11T', html)
                elif state == 'unknown':
                    self.assertEqual(html.count('Unknown'), 2)
                else:
                    self.assertNotIn('2026-10-11 10:00:00', html)
                    self.assertNotIn('2026-10-11 15:08:38', html)
                self.assertNotIn('d-none d-lg-table-cell', html)
            else:
                self.assertIn('(No Jobs)', html)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for refresh execution')
    def test_refresh_behavior(self):
        subprocess.run(['node', str(ROOT / 'tests/opencomposer_history_refresh_test.js'),
                        str(UI / 'history-refresh.js')], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
