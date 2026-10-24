#!/usr/bin/env python3
"""Latch automatic creation off after consecutive completed create failures.

Only the scheduler calls run; manual create/delete and notification delivery
remain independent. Compatible with the controller's Python 3.6.
"""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import pwd
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "conf/autoscaling_guard.json"


def management_user(config, fallback=None):
    if config.get("admin_user"):
        return config["admin_user"]
    # Older deployed configs were uploaded by the controller SSH user. Use the
    # owner rather than guessing from the OS or the current caller (e.g. root).
    try:
        name = pwd.getpwuid(DEFAULT_CONFIG.stat().st_uid).pw_name
        if name != "root":
            return name
    except (OSError, KeyError):
        pass
    return fallback or "デプロイ時の管理ユーザー（opc または ubuntu）"


def load_config(path):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"failure_threshold": 0}
    threshold = config.get("failure_threshold")
    if type(threshold) is not int or threshold < 0:
        raise ValueError("failure_threshold must be a nonnegative integer")
    return config


def initial_state():
    return {"version": 1, "generation": uuid.uuid4().hex,
            "consecutive_failures": 0, "stopped": False, "recovery_pending": False}


def read_state(directory):
    try:
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return initial_state()
    if (state.get("version") != 1 or not isinstance(state.get("generation"), str)
            or type(state.get("consecutive_failures")) is not int
            or state["consecutive_failures"] < 0
            or type(state.get("stopped")) is not bool
            or type(state.get("recovery_pending", False)) is not bool):
        raise ValueError("Invalid guard state; inspect state.json before resuming")
    return state


def save_state(directory, state):
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=str(directory))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(directory / "state.json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def locked(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def queue_notification(title, body):
    # Reuse the existing spool and retry worker. Saving state precedes delivery,
    # so missing OCI credentials or a failed publish cannot undo the stop.
    try:
        import autoscaling_failure_notify as notify
        config = json.loads(notify.DEFAULT_CONFIG.read_text(encoding="utf-8"))
        if not config.get("enabled") or not config.get("topic_id"):
            return
        message = {
            "id": "guard-" + uuid.uuid4().hex,
            "topic_id": config["topic_id"],
            "title": title,
            "body": notify.clean(body, notify.MAX_BODY),
        }
        notify.save_message(Path(config["spool"]), message)
    except Exception as error:
        print("Could not queue autoscaling event notification ({})".format(
            type(error).__name__), file=sys.stderr)


def notify_stop(state, admin_user=None):
    admin_user = admin_user or management_user({})
    queue_notification("Autoscaling 自動作成を停止しました",
                       ("Consecutive failures: {}\nThreshold: {}\n"
                     "Last cluster: {}\nQueue: {}\nInstance type: {}\n"
                     "停止前に開始した作成と既存クラスターの削除は継続します。\n"
                     "作成ログを確認して原因を解消後、{} ユーザーにて実行してください（sudo は付けません）。\n"
                     "/opt/oci-hpc/bin/autoscaling_guard.py resume\n").format(
                         state["consecutive_failures"], state["threshold"],
                         state["cluster"], state["queue"], state["instance_type"], admin_user))


def notify_success(arguments, recovered=False, stopped=False, previous_failures=None,
                   failures_before_resume=None):
    if not recovered or stopped:
        return
    title = "Autoscaling の自動作成が復旧しました"
    description = "失敗後のクラスター作成が成功し、連続失敗回数が0に戻りました。"
    if previous_failures is None:
        counts = "連続失敗回数: リセット前の回数は不明 → 0回"
    elif previous_failures == 0 and failures_before_resume is not None:
        counts = "連続失敗回数: 0回（手動再開後の追加失敗なし）"
    else:
        counts = "連続失敗回数: {}回 → 0回".format(previous_failures)
    if failures_before_resume is not None:
        counts += "\n手動再開前の連続失敗回数: {}回".format(failures_before_resume)
    queue_notification(title,
                       ("{}\n{}\n"
                        "Cluster: {}\nQueue: {}\nInstance type: {}\n"
                        "Nodes: {}\nJob ID: {}\nCompleted (UTC): {}\n"
                        "{}\n").format(
                            description, counts, arguments[1], arguments[3], arguments[2], arguments[0], arguments[4],
                            datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "この通知は今回の作成結果を示します。"))


def scheduler_creation_requests(requests):
    """Suppress creation planning while preserving the scheduler's delete pass.

    run_create still checks under the admission lock to cover a stop occurring
    after this snapshot. This check is for planning and accurate cron output.
    """
    config = {}
    try:
        config = load_config(DEFAULT_CONFIG)
        if not config["failure_threshold"]:
            return requests
        directory = Path(config.get("state_dir", str(ROOT / "autoscaling/guard")))
        with locked(directory):
            state = read_state(directory)
        if not state["stopped"]:
            return requests
        print("Automatic cluster creation is stopped; skipped {} creation request(s).".format(len(requests)))
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print("Could not verify the autoscaling guard state; skipping automatic creation ({}).".format(
            type(error).__name__))
    return []


def run_create(config, arguments, command=None):
    threshold = config["failure_threshold"]
    if not threshold:
        code = subprocess.call(command or [str(ROOT / "bin/create_cluster.sh")] + arguments)
        return code
    directory = Path(config.get("state_dir", str(ROOT / "autoscaling/guard")))
    # Spawn under the state lock: no create can be admitted after the latch is
    # committed. Already admitted processes finish normally, without a kill.
    with locked(directory):
        state_existed = (directory / 'state.json').exists()
        state = read_state(directory)
        if state["stopped"]:
            print("Automatic cluster creation is stopped; skipping this creation request.")
            return 0
        generation = state["generation"]
        attempt_id = uuid.uuid4().hex
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_state(directory, state)
        if not state_existed:
            print("Autoscaling guard initialized: utc={} reason=missing_state_file failures=0 generation={} state_dir={}".format(
                started_at, generation, directory), flush=True)
        try:
            process = subprocess.Popen(command or [str(ROOT / "bin/create_cluster.sh")] + arguments)
        except OSError as error:
            print("Could not launch automatic creation: {}".format(error), file=sys.stderr)
            process = None
        process_pid = getattr(process, 'pid', None)
        process_pid = process_pid if type(process_pid) is int else None
        print("Autoscaling guard attempt: utc={} attempt={} cluster={} pid={} failures={} generation={}".format(
            started_at, attempt_id, arguments[1], process_pid, state['consecutive_failures'], generation), flush=True)
    code = process.wait() if process is not None else 127
    stopped_now = False
    recovered = False
    with locked(directory):
        state_still_exists = (directory / 'state.json').exists()
        state = read_state(directory)
        previous_failures = state['consecutive_failures']
        failures_before_resume = state.get('failures_before_resume')
        if failures_before_resume is None and state.get('recovery_pending'):
            reset = state.get('last_reset', {})
            if reset.get('reason') == 'manual_resume':
                failures_before_resume = reset.get('previous_failures')
        # An operator resume starts a new generation; old in-flight results
        # must not immediately trip the newly resumed guard.
        if state["generation"] == generation and not state["stopped"]:
            recovered = code == 0 and (state["consecutive_failures"] > 0
                                      or state.get("recovery_pending", False))
            state.update(cluster=arguments[1], instance_type=arguments[2],
                         queue=arguments[3], returncode=code,
                         completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         last_attempt_id=attempt_id, last_attempt_pid=process_pid,
                         last_attempt_started_at=started_at)
            if code == 0:
                state["recovery_pending"] = False
                state.pop('failures_before_resume', None)
                if previous_failures:
                    state['last_reset'] = {'reason': 'creation_succeeded',
                                           'at': state['completed_at'],
                                           'previous_failures': previous_failures,
                                           'attempt_id': attempt_id,
                                           'cluster': arguments[1]}
            state["consecutive_failures"] = state["consecutive_failures"] + 1 if code else 0
            if state["consecutive_failures"] >= threshold:
                state.update(stopped=True, threshold=threshold)
                stopped_now = True
            save_state(directory, state)
            print("Autoscaling guard result: utc={} attempt={} cluster={} pid={} returncode={} failures={}->{} generation={}".format(
                state['completed_at'], attempt_id, arguments[1], process_pid, code,
                previous_failures, state['consecutive_failures'], generation), flush=True)
        else:
            print("Autoscaling guard result ignored: utc={} attempt={} cluster={} returncode={} reason={} failures={}".format(
                datetime.datetime.now(datetime.timezone.utc).isoformat(), attempt_id, arguments[1], code,
                'missing_state_file' if not state_still_exists else
                'generation_changed' if state['generation'] != generation else 'already_stopped',
                state['consecutive_failures']), flush=True)
        if stopped_now:
            # Serialize queuing with resume; notification publishing stays in
            # the existing retry worker and never blocks the state lock on OCI.
            notify_stop(state, admin_user=management_user(config))
            print("Autoscaling automatic creation stopped after {} consecutive failures".format(
                state["consecutive_failures"]))
    if code == 0 and recovered:
        notify_success(arguments, recovered=recovered, stopped=state["stopped"],
                       previous_failures=previous_failures, failures_before_resume=failures_before_resume)
    return code


def display_width(text):
    """Terminal columns for the Japanese status labels (standard tab stops)."""
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
               for char in text)


def format_status(state):
    labels = ['Autoscaling の状態', '管理ユーザー', '判定範囲', '連続失敗回数',
              '復旧確認', '作成結果', '  クラスター名', '  キュー',
              '  インスタンスタイプ', '  作成結果', '  完了日時（UTC）']
    label_width = max(display_width(label) for label in labels)

    def field(label, value):
        return label + ' ' * (label_width - display_width(label)) + ':\t' + str(value)

    threshold = state["configured_threshold"]
    if not threshold:
        status = "連続失敗による自動停止は無効"
    elif state["effective_stopped"]:
        status = "停止中（新しいクラスターの自動作成を見合わせています）"
    else:
        status = "自動作成を受付可能"
    lines = [field('Autoscaling の状態', status),
             field('管理ユーザー', state.get('admin_user', management_user({}))),
             field('判定範囲', 'この Controller の全キュー・全インスタンスタイプ共通'),
             field('連続失敗回数', '{}回 / 停止しきい値: {}'.format(
                 state["consecutive_failures"], "{}回".format(threshold) if threshold else "無効（0）"))]
    if not threshold:
        lines.append("停止判定が無効の間は失敗回数を更新しません。表示は保存済みの値です。")
    if state["stopped"] and not threshold:
        lines.append("保存済みの停止記録があります。停止判定を再び有効にする前に確認してください。")
    if state.get("recovery_pending"):
        lines.append(field('復旧確認', '次の作成成功を待っています'))
    if "cluster" in state:
        lines += ["", "最後に記録した作成試行（現在の稼働状況ではありません）:",
                  field('  クラスター名', state['cluster']),
                  field('  キュー', state.get('queue', '不明')),
                  field('  インスタンスタイプ', state.get('instance_type', '不明')),
                  field('  作成結果', '{}（終了コード {}）'.format(
                      '成功' if state.get('returncode') == 0 else '失敗', state.get('returncode', '不明'))),
                  field('  完了日時（UTC）', state.get('completed_at', '不明'))]
    else:
        lines.append(field('作成結果', '記録なし（初回実行前、または手動再開で記録を初期化済み）'))
    if state["effective_stopped"]:
        lines += ["", "原因を解消してから、{} ユーザーにて実行してください（sudo は付けません）。".format(
                      state.get("admin_user", management_user({}))),
                  "/opt/oci-hpc/bin/autoscaling_guard.py resume",
                  "実行中ジョブ・開始済みの作成・既存クラスターの安全な削除は継続します。"]
    lines.append("※ この表示は停止判定の状態です。OCI・Slurm の正常性を検査した結果ではありません。")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--debug", action="store_true", help="status / resume の詳細を JSON で表示")
    parser.add_argument("action", choices=("run", "status", "resume"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action in ("status", "resume"):
        if args.arguments == ["--debug"]:
            args.debug = True
        elif args.arguments:
            parser.error("status / resume accepts only --debug")
    try:
        config = load_config(args.config)
        if args.action == "run":
            if len(args.arguments) < 5:
                parser.error("run requires nodes, cluster, instance-type, queue and job-id")
            return run_create(config, args.arguments)
        directory = Path(config.get("state_dir", str(ROOT / "autoscaling/guard")))
        with locked(directory):
            if args.action == "resume":
                previous = read_state(directory)
                state = initial_state()
                state['last_reset'] = {'reason': 'manual_resume',
                                       'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                       'previous_failures': previous['consecutive_failures']}
                state["recovery_pending"] = bool(previous["stopped"] or previous["consecutive_failures"]
                                                 or previous.get("recovery_pending", False))
                if state['recovery_pending']:
                    count = previous['consecutive_failures'] or previous.get('failures_before_resume')
                    reset = previous.get('last_reset', {})
                    if count is None and reset.get('reason') == 'manual_resume':
                        count = reset.get('previous_failures')
                    if count is not None:
                        state['failures_before_resume'] = count
                save_state(directory, state)
            else:
                state = read_state(directory)
            state["configured_threshold"] = config["failure_threshold"]
            state["effective_stopped"] = bool(config["failure_threshold"] and state["stopped"])
            state["admin_user"] = management_user(config)
            if args.debug:
                print(json.dumps(state, ensure_ascii=False, indent=2))
            else:
                if args.action == "resume":
                    print("自動作成の停止を解除し、連続失敗回数を0にしました。")
                    print("次回の定期実行から作成を受け付けます。作成成功後に復旧を判定します。")
                print(format_status(state))
        return 0
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print("Autoscaling guard error; could not authorize creation or save its result: {}".format(error),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
