require 'minitest/autorun'
require 'sqlite3'
require 'json'
require 'time'

UI = ARGV.shift
# Use the status definition actually deployed into run.rb by Ansible.
eval(ARGV.shift)
JOB_ID = 'id'; JOB_STATUS_ID = 'status'; JOB_NAME = 'Job Name'
JOB_PARTITION = 'Partition'; JOB_SUBMISSION_TIME = 'Submission Time'
JOB_APP_NAME = 'appName'; JOB_DIR_NAME = 'appPath'; JOB_KEYS = 'job_keys'
HEADER_SCRIPT_LOCATION = '_script_location'; HEADER_SCRIPT_NAME = '_script_1'
HEADER_JOB_NAME = '_script_2'
OC_SCRIPT_CONTENT = '_script_content'

# Sinatra's helper DSL is replaced by a plain class; all helper bodies are real.
class HistoryHarness
  def self.helpers(&block)
    class_eval(&block)
  end
  class_eval(File.read(File.join(UI, 'history.rb')), File.join(UI, 'history.rb'))
end

class Scheduler; end
require File.join(UI, 'slurm.rb')
class SlurmHarness < Slurm
  attr_accessor :accounting, :fail_query
  def get_command_path(*)
    'sacct'
  end
  def capture_scheduler_command(_, command)
    return ['', 'accounting unavailable', Struct.new(:success?).new(false)] if fail_query
    data = command.include?('--helpformat') ? "JobID JobName Partition State\n" : accounting
    [data, '', Struct.new(:success?).new(true)]
  end
end

class CanceledStatusTest < Minitest::Test
  def setup
    @history = HistoryHarness.new
    @db = SQLite3::Database.new(':memory:')
    @db.results_as_hash = true
    @history.setup_history_db(@db)
    @scheduler = SlurmHarness.new
  end

  def teardown
    @db.close
  end

  def insert(id, status)
    record = @history.build_job_record(existing: nil, submit_data: {
      '_job_id' => id, '_status' => status, '_submission_time' => '2026-10-19T09:00:00',
      '_script_name' => 'job.sh', 'State' => 'RUNNING'
    }, scheduler_data: nil)
    @history.upsert_job(@db, record)
  end

  def test_slurm_canceled_states_and_existing_states
    states = ['CANCELLED', 'CANCELLED by 1234', 'CANCELLED+', 'COMPLETED',
              'FAILED', 'PENDING', 'RUNNING', 'COMPLETING', 'TIMEOUT', 'UNKNOWN']
    expected = %w[CANCELED CANCELED CANCELED COMPLETED FAILED QUEUED RUNNING RUNNING FAILED] + [nil]
    @scheduler.accounting = "JobID|JobName|Partition|State\n" + states.each_with_index.map do |state, i|
      "#{i}|job|debug|#{state}\n"
    end.join + "0.batch|batch|debug|CANCELLED\n0.extern|extern|debug|CANCELLED\n"
    jobs, error = @scheduler.query(states.each_index.map(&:to_s))
    assert_nil error
    assert_equal states.size, jobs.size
    states.each_index do |i|
      if expected[i].nil?
        assert_nil jobs[i.to_s][JOB_STATUS_ID]
      else
        assert_equal expected[i], jobs[i.to_s][JOB_STATUS_ID]
      end
      assert_equal states[i], jobs[i.to_s]['State']
    end
  end

  def test_accounting_errors_are_preserved
    @scheduler.fail_query = true
    jobs, error = @scheduler.query(['1'])
    assert_nil jobs
    assert_includes error, 'accounting unavailable'
  end

  def test_cancel_from_history_persists_canceled_and_keeps_script_and_details
    insert('1', 'RUNNING')
    @db.transaction { @history.mark_jobs_as_canceled(@db, ['1', 'missing']) }
    job = @history.find_job(@db, '1')
    assert_equal 'CANCELED', job['_status']
    assert_equal 'job.sh', job['_script_name']
    assert_equal 'RUNNING', JSON.parse(job['payload_json'])['State']
    refute_nil job['_updated_time']
    assert_empty @history.get_unfinished_job_ids(@db)
    assert_includes @history.output_status(job['_status']), '>Canceled</span>'
  end

  def test_terminal_jobs_are_excluded_from_refresh_queries
    [nil, 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED'].each_with_index do |state, i|
      insert(i.to_s, state)
    end
    assert_equal %w[0 1 2], @history.get_unfinished_job_ids(@db).sort
  end

  def test_accounting_refresh_saves_cli_cancellation_and_normal_completion
    insert('1', 'QUEUED')
    insert('2', 'RUNNING')
    db = @db
    @history.define_singleton_method(:open_history_db) { |*| db }
    @scheduler.accounting = "JobID|JobName|Partition|State\n1|job|debug|CANCELLED by 1234\n2|job|debug|COMPLETED\n"
    assert_nil @history.update_status({}, @scheduler, nil, nil, nil, nil, nil)
    assert_equal 'CANCELED', @history.find_job(@db, '1')['_status']
    assert_equal 'CANCELLED by 1234', JSON.parse(@history.find_job(@db, '1')['payload_json'])['State']
    assert_equal 'COMPLETED', @history.find_job(@db, '2')['_status']
    # A second refresh must not query terminal jobs again.
    @scheduler.fail_query = true
    assert_nil @history.update_status({}, @scheduler, nil, nil, nil, nil, nil)
  end

  def test_filters_and_both_sort_paths_include_canceled
    states = %w[QUEUED RUNNING COMPLETED FAILED CANCELED]
    states.each_with_index { |state, i| insert(i.to_s, state) }
    assert_includes @history.parse_history_statuses(nil), 'canceled'
    assert_equal ['canceled'], @history.parse_history_statuses('canceled invalid')
    assert_equal 'canceled', @history.serialize_history_statuses(['canceled'])
    assert_nil @history.serialize_history_statuses(@history.history_valid_statuses)
    sql_order = @history.history_sql_order(JOB_STATUS_ID, 'asc')
    assert_equal states, @db.execute("SELECT _status FROM jobs ORDER BY #{sql_order}").map { |r| r['_status'] }
    jobs = states.reverse.map { |state| {JOB_STATUS_ID => state, JOB_ID => '1'} }
    assert_equal states, jobs.sort_by { |job| @history.history_sort_key(job, JOB_STATUS_ID) }.map { |job| job[JOB_STATUS_ID] }
    clauses, params = @history.history_sql_where(['canceled'], '', '')
    assert_equal ['CANCELED'], @db.execute("SELECT _status FROM jobs WHERE #{clauses.join(' AND ')}", params).map { |r| r['_status'] }
  end
end
