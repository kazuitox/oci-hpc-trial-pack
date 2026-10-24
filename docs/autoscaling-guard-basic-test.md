# Autoscaling 連続失敗停止と管理者通知の基本テスト

デプロイ後の Controller で、コマンドの結果とメールを確認しながら進める手順です。
最初に検証用プロセスの成功・失敗を配布済みの停止判定へ渡し、回数・停止・通知・再開を確認します。
最後に実ジョブを1件投入し、定期実行からのクラスター作成と、通常完了のメールを送らないことを確認します。

1〜9を順番に実行してください。期待値に合わない場合は次へ進まず、10の診断コマンドで結果を記録します。
旧版の基本動作は利用者より成功報告あり。直接実行・管理ユーザー案内・通常完了通知の抑止は、更新後に確認してください。

## 1 検証用 Controller にデプロイする

Resource Manager では、次の設定でデプロイしてください。

| 設定 | 値 |
| --- | --- |
| Autoscaling | 有効 |
| 自動作成を停止する連続失敗回数 | `3` |
| 管理者へのメール通知を有効化 | 有効 |
| 管理者メールアドレス | テストメールを受信できるアドレス |

OCI Notifications から届いた Subscription 確認メールを承認します。
今回は primary Controller の単独確認です。HA 切り替え、OCI 容量不足、実際の Ansible 失敗、送信障害は追加試験で確認します。

Controller に `opc` または `ubuntu` でログインしてください。以降は同じターミナルで、`sudo` を付けずに実行します。
この試験は実際の停止状態を変更し、実際の管理者宛先へメールを送ります。ほかの利用者やジョブがない検証環境で行ってください。

```bash
whoami
squeue -h -o '%i %u %T %P %j'
ls -l /opt/oci-hpc/bin/autoscaling_guard.py
ls /opt/oci-hpc/bin/autoscaling_failure_notify.py
```

**確認：** ユーザーが `opc` または `ubuntu`、ジョブ一覧が空、両ファイルが存在し、`autoscaling_guard.py` に実行権限（`-rwxr-xr-x`）があること。
作成・削除中のクラスターもないことを確認します。

```bash
find /opt/oci-hpc/autoscaling/clusters -maxdepth 2 \
  \( -name currently_building -o -name currently_destroying \) -print
```

**確認：** 出力が空であること。作成・削除中なら完了を待ちます。

## 2 設定と定期実行の配布を確認する

次のコマンドは、Topic ID やメールアドレスを表示せず、有効状態だけを確認します。

```bash
python3 - <<'PY'
import json
from pathlib import Path

root = Path('/opt/oci-hpc')
guard = json.loads((root / 'conf/autoscaling_guard.json').read_text())
notice = json.loads((root / 'conf/autoscaling_failure_notification.json').read_text())
print('failure_threshold =', guard['failure_threshold'])
print('admin_user =', guard.get('admin_user'))
print('admin_notifications_enabled =', notice.get('enabled'))
print('topic_configured =', bool(notice.get('topic_id')))
print('spool =', notice.get('spool'))
assert guard['failure_threshold'] == 3
assert guard.get('admin_user') in ('opc', 'ubuntu')
assert notice.get('enabled') is True and notice.get('topic_id')
PY

crontab -l | grep -A 1 'slurm autoscaling disable resize'
cat /etc/cron.d/oci-hpc-autoscaling-failure-notify
grep -n 'autoscaling_guard.py' \
  /opt/oci-hpc/autoscaling/crontab/autoscale_slurm_disable-resize.sh
```

**確認：** `failure_threshold = 3`、通知の有効と Topic 設定が両方 `True` になること。`admin_user` が実際のログインユーザーと一致すること。
Autoscaling がコメントアウトされず毎分実行され、通知ワーカーも毎分実行されること。
通常の Autoscaling スクリプトから `autoscaling_guard.py` を呼び出していること。
`AssertionError` やファイル未配置なら、ここで止めてデプロイ内容を確認してください。

## 3 検証用処理と初期状態を準備する

検証用処理は、配布済み `autoscaling_guard.py` の `run_create` を使います。
通常のクラスター作成コマンドを、指定した終了コードで終わる小さなプロセスに差し替えます。
この段階では OCI インスタンスを作成せず、Ansible やキューの設定も変更しません。
ただし、回数・停止状態・通知設定・通知キューは実際のものを使います。

```bash
GUARD_TEST_DIR=$(mktemp -d /tmp/oci-hpc-guard-basic.XXXXXX)
export GUARD_TEST_DIR
printf 'テスト記録先: %s\n' "$GUARD_TEST_DIR"

cat > "$GUARD_TEST_DIR/probe.py" <<'PY'
import os
from pathlib import Path
import sys

sys.path.insert(0, '/opt/oci-hpc/bin')
import autoscaling_guard as guard

outcome = sys.argv[1]
if outcome not in ('success', 'fail'):
    raise SystemExit('指定は success または fail')
marker = Path(os.environ['GUARD_TEST_DIR']) / 'executed.txt'
code = 0 if outcome == 'success' else 1
args = ['1', 'guard-basic-test', 'test-type', 'test-queue', 'TEST-NO-JOB', '']
child = [sys.executable, '-c',
         'import sys; '
         'f=open(sys.argv[1], "a"); f.write(sys.argv[2]+"\\n"); f.close(); '
         'print("検証用プロセスを実行: exit="+sys.argv[2]); '
         'sys.exit(int(sys.argv[2]))', str(marker), str(code)]
result = guard.run_create(guard.load_config(guard.DEFAULT_CONFIG), args, command=child)
print('guard_returncode =', result)
raise SystemExit(result)
PY

/opt/oci-hpc/bin/autoscaling_guard.py resume
/opt/oci-hpc/bin/autoscaling_guard.py status
```

表示の項目名と値の間は空白・タブで整列します。以下の期待値は、表示内容を確認してください。

**確認：** 「自動作成を受付可能」「連続失敗回数: 0回 / 停止しきい値: 3回」と表示されること。
`resume` 自体では成功通知は送りません。

## 4 失敗1回の後に成功すると回数が0に戻る

まず失敗を1回渡します。終了コード `1` は意図した結果です。

```bash
python3 "$GUARD_TEST_DIR/probe.py" fail
PROBE_RC=$?
printf '終了コード: %s（期待値 1）\n' "$PROBE_RC"
/opt/oci-hpc/bin/autoscaling_guard.py status
```

**確認：** 「連続失敗回数: 1回」「自動作成を受付可能」と表示されること。
この検証用処理は実際の作成スクリプトを呼ばないため、通常の「作成失敗の詳細メール」は発生しません。
しきい値未満なので、停止メールも発生しません。

次に成功を渡します。

```bash
python3 "$GUARD_TEST_DIR/probe.py" success
PROBE_RC=$?
printf '終了コード: %s（期待値 0）\n' "$PROBE_RC"
/opt/oci-hpc/bin/autoscaling_guard.py status
```

**確認：** 「連続失敗回数: 0回」「自動作成を受付可能」と表示されること。
次回の通知ワーカー実行後、管理者メールの受信箱で次を確認します。

| 項目 | 期待値 |
| --- | --- |
| 件名 | `Autoscaling の自動作成が復旧しました` |
| Cluster | `guard-basic-test` |
| 連続失敗回数 | `1回 → 0回` |
| Queue | `test-queue` |
| Instance type | `test-type` |
| Nodes | `1` |
| Job ID | `TEST-NO-JOB` |

これは検証用プロセスの失敗後に成功したことを示す復旧通知です。実クラスターの作成成功とは区別してください。

## 5 連続失敗3回で停止メールが届く

以下のコマンドを、**1回実行するごとに**結果を確認しながら合計3回実行します。

```bash
python3 "$GUARD_TEST_DIR/probe.py" fail
PROBE_RC=$?
printf '終了コード: %s（期待値 1）\n' "$PROBE_RC"
/opt/oci-hpc/bin/autoscaling_guard.py status
```

| 実行回数 | 連続失敗回数 | 状態表示 | メール |
| --- | --- | --- | --- |
| 1回目 | 1回 | 自動作成を受付可能 | 停止通知なし |
| 2回目 | 2回 | 自動作成を受付可能 | 停止通知なし |
| 3回目 | 3回 | 停止中 | 停止通知あり |

3回目では `Autoscaling automatic creation stopped after 3 consecutive failures` も表示される想定です。
次回のワーカー実行後、管理者の受信箱で次を確認します。

- 件名が `Autoscaling 自動作成を停止しました`。
- 本文に `Consecutive failures: 3`、`Threshold: 3`、`Last cluster: guard-basic-test` がある。
- 再開コマンド `/opt/oci-hpc/bin/autoscaling_guard.py resume` と、実環境の `opc` または `ubuntu` ユーザーにて実行する案内が記載されている。

## 6 停止中は処理を起動せず同じ停止通知を追加しない

4で2回、5で3回、合計5回の検証用プロセスが実行されたことを確認します。

```bash
wc -l "$GUARD_TEST_DIR/executed.txt"
```

**確認：** 行数が `5`。
停止中に成功を要求し、実際にはプロセスが起動されないことを確認します。

```bash
python3 "$GUARD_TEST_DIR/probe.py" success
/opt/oci-hpc/bin/autoscaling_guard.py status
wc -l "$GUARD_TEST_DIR/executed.txt"
```

**確認：** `Automatic cluster creation is stopped; skipping this creation request.` と表示され、行数が `5` のままであること。
「連続失敗回数: 3回」「停止中」が保持され、成功メールも新しい停止メールも発生しないこと。
起動を見送った場合の終了コードは `0` です。終了コードだけで成功と判定せず、実行記録と状態を見てください。

停止中の定期実行ログも確認します。

```bash
tail -n 60 "/opt/oci-hpc/logs/crontab_slurm_$(date +%Y%m%d).log"
```

実ジョブから作成要求が出ていなければ`Automatic cluster creation is stopped; skipped 0 creation request(s).`、停止中に作成要求がある場合はその件数を表示する想定です。停止中の実行では `cluster_to_build` と `Creating cluster` を表示しないこと、操作案内の `Run the following command` と `status` コマンドもログに出ないことを確認します。この検証用プロセスは Slurm ジョブを作らないため、通常は0件です。

## 7 手動再開後は再び処理を起動できる

```bash
/opt/oci-hpc/bin/autoscaling_guard.py resume
/opt/oci-hpc/bin/autoscaling_guard.py status
python3 "$GUARD_TEST_DIR/probe.py" success
wc -l "$GUARD_TEST_DIR/executed.txt"
/opt/oci-hpc/bin/autoscaling_guard.py status
```

**確認：** `resume` 後は「復旧確認: 次の作成成功を待っています」と表示され、まだ復旧通知が出ないこと。
検証用処理の成功後、行数が `6` へ増え、「連続失敗回数: 0回」「自動作成を受付可能」になること。
「Autoscaling の自動作成が復旧しました」が届き、本文に「手動再開前の連続失敗回数: 3回」があること。ここまでで、停止判定への入力・状態保存・通知・再開の基本確認は完了です。

## 8 実ジョブ1件で自動作成と通常完了メールの抑止を確認する

ここから実際の計算リソースを作成します。正常なキューの設定値を確認してください。

```bash
python3 - <<'PY'
import yaml
with open('/opt/oci-hpc/conf/queues.conf') as stream:
    config = yaml.safe_load(stream)
for queue in config['queues']:
    for item in queue['instance_types']:
        print('queue={}  type={}  keyword={}  permanent={}'.format(
            queue['name'], item['name'], item['instance_keyword'], item.get('permanent')))
PY
```

`permanent: false` の検証対象を選び、次の2つの値を置き換えます。
constraint には `instance_keyword` ではなく `instance_types[].name` を指定してください。

```bash
TEST_QUEUE='検証対象のqueue名'
TEST_TYPE='検証対象のtype名'
sinfo -N -p "$TEST_QUEUE" -o '%N %T %f'
squeue -h -p "$TEST_QUEUE" -o '%i %T %j'
```

**進める条件：** 該当 type のジョブを実行できる既存計算ノードがなく、ほかのジョブもないこと。
既存ノードで実行された場合は、新規作成の確認になりません。該当する環境を用意してから進めます。
既存ノードを削除する操作は、この基本手順には含めません。

```bash
GUARD_JOB_ID=$(sbatch --parsable \
  --partition="$TEST_QUEUE" --constraint="$TEST_TYPE" \
  --nodes=1 --ntasks=1 --time=00:10:00 \
  --job-name=guard-basic-real \
  --output=/dev/null \
  --wrap='sleep 300')
GUARD_JOB_ID=${GUARD_JOB_ID%%;*}
printf 'Job ID: %s\n' "$GUARD_JOB_ID"
squeue -j "$GUARD_JOB_ID" -o '%.18i %.10T %.12P %.24j %R'
```

**確認：** 数字の Job ID が返ること。投入が失敗したら以降へ進まず、キュー名・constraint・権限を確認します。
ジョブの標準出力は `/dev/null` へ捨てるため、計算ノードから Controller の一時ディレクトリへ書き込む必要はありません。
定期実行と構築の完了を待ち、次のコマンドを繰り返して確認します。構築時間は環境により異なります。

```bash
squeue -j "$GUARD_JOB_ID" -o '%.18i %.10T %.12P %.24j %R'
/opt/oci-hpc/bin/autoscaling_guard.py status
tail -n 60 "/opt/oci-hpc/logs/crontab_slurm_$(date +%Y%m%d).log"
```

**合格条件：** 新規クラスターの作成後にジョブが `RUNNING` へ進み、作成ログが正常終了すること。
今回は直前に失敗がないため、新たな完了メールも復旧メールも発生しないこと。作成結果は `status` と作成ログで確認します。
`status` の「作成結果: 成功（終了コード 0）」「連続失敗回数: 0回」「自動作成を受付可能」も確認します。
ジョブが先に終了した場合は、次で `COMPLETED` と終了コードを確認できます。

```bash
sacct -j "$GUARD_JOB_ID" --format=JobID,JobName,State,ExitCode,NodeList
```

## 9 終了状態を確認して記録を残す

ジョブは約5分で終了します。テストを途中で終える場合だけ、確認済みの検証用 Job ID を指定してキャンセルします。

```bash
scancel "$GUARD_JOB_ID"
```

状態とジョブ結果を記録します。

```bash
/opt/oci-hpc/bin/autoscaling_guard.py status --debug > "$GUARD_TEST_DIR/final-status.json"
sacct -j "$GUARD_JOB_ID" --format=JobID,JobName,State,ExitCode,NodeList \
  > "$GUARD_TEST_DIR/final-job.txt"
printf '記録先: %s\n' "$GUARD_TEST_DIR"
```

**確認：** 自動作成が停止していないこと。作成したクラスターはジョブ終了後、通常のアイドル削除へ進むことを確認します。
手動の FORCE 削除や Terraform state の編集は行いません。停止設定は `3` のまま残ります。
中断により停止中のまま残った場合は、診断情報を保存し、原因を確認してから `resume` してください。

## 10 期待値に合わない場合の確認コマンド

回数が予想外にリセットされた場合は、再開や検証用処理を追加実行する前に、次の結果を記録します。

```bash
/opt/oci-hpc/bin/autoscaling_guard.py status --debug
grep -nE 'Autoscaling guard (initialized|attempt|result)' \
  "/opt/oci-hpc/logs/crontab_slurm_$(date +%Y%m%d).log" | tail -n 40
```

`last_reset` でリセット元、`last_attempt_id` で試行を確認します。ログの `failures=2->0` は、そのプロセスの結果を反映して2回から0回に更新した記録です。`reason=missing_state_file` は状態ファイル不在からの初期化です。実行中のプロセスを待っている間は、成功結果によるリセットを行わない想定です。診断版を配布する前の更新履歴は、これらの項目には記録されていません。

メールが届かない場合は、キューと送信ログを確認します。

```bash
find /opt/oci-hpc/logs/autoscaling-failure-notifications \
  -maxdepth 1 -type f -name '*.json' -printf '%f\n'
tail -n 80 /opt/oci-hpc/logs/autoscaling_failure_notify.log
```

キューが残っていれば未送信です。原因を確認後、待たずに再送したい場合は次を実行できます。
これはキュー内の既存通知もまとめて再送対象にします。

```bash
python3 /opt/oci-hpc/bin/autoscaling_failure_notify.py retry
```

キューが空でもメール受信の証明にはなりません。Subscription の承認、宛先、迷惑メール、OCI Notifications の配送状況を確認してください。
基本確認中は、ほかの通知を止めるために通知設定を無効化したり、未送信の JSON を削除したりしないでください。

状態が合わない場合は、次を記録してください。

```bash
/opt/oci-hpc/bin/autoscaling_guard.py status
cat "$GUARD_TEST_DIR/executed.txt"
tail -n 80 "/opt/oci-hpc/logs/crontab_slurm_$(date +%Y%m%d).log"
```

設定ファイルの全文には Topic ID などが含まれます。結果を共有するときは認証情報・識別子を取り除いてください。
この基本確認では、しきい値0、通知無効、並行作成、HA、実際の構築失敗とその後始末は未確認として残します。
