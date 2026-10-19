# Autoscaling の残存インスタンス監視

Controller 上で OCI のインスタンス一覧と Slurm、ローカルのクラスター管理情報を5分ごとに照合し、想定外の残存を管理者へ通知する。対象は Autoscaling で作成した計算インスタンス。実際の請求額は取得せず、課金につながり得るリソース残存を早期に見つける。

クラスターを運用する管理者向けの手順である。初回は「1〜4」を順に確認し、有効化後は通知理由表と「5」を参照する。以下の導入・実環境試験は手順案であり、OCI 上での実施記録ではない。

実環境での確認は [テスト項目と実施手順](autoscaling-instance-monitor-test-plan.md) を参照する。メール疎通、正常動作、Slurm 未登録の残存、未使用ノードの残存、解消通知の5項目を順に確認できる。

## 1. インスタンスが残る経路を洗い出す

削除要求の成功だけでは残存の有無を判断できない。作成側にも、OCI での作成後に Ansible、名前同期、Slurm 登録で失敗する経路がある。

| 起こり得る状況 | OCI | Slurm / 管理情報 | 検出理由 |
| --- | --- | --- | --- |
| Terraform の途中失敗、SSH・Ansible・名前同期の失敗で Slurm に組み込まれない | 残存 | Slurm ノードなし | `SLURM_NODE_MISSING` |
| 作成失敗の後始末も失敗し、管理ディレクトリだけ消える | 残存 | ローカル記録なし | `LOCAL_RECORD_MISSING` |
| Slurm の後始末後に IAM、API、通信、Terraform の削除が失敗する | 残存 | Slurm ノードなし | `SLURM_NODE_MISSING` |
| 削除処理の停止・ハングで「削除中」の印が残る | 残存 | `currently_destroying` が長時間残る | `DELETE_STALLED` |
| 作成処理の停止・ハングで「作成中」の印が残る | 残存 | `currently_building` が長時間残る | `BUILD_STALLED` |
| Slurm の削除もできず、DOWN・DRAIN・応答なしのノードが残る | 残存 | 障害状態のノードあり | `SLURM_UNHEALTHY` |
| 削除失敗時に「削除中」の印が消え、その後の再試行も進まない | 残存 | Slurm に未使用ノードが残る | `IDLE_CLUSTER_RETAINED` または障害・未登録 |
| Autoscaling の定期実行が止まり、非永久クラスターが未使用のまま残る | 残存 | クラスター全体が IDLE | `IDLE_CLUSTER_RETAINED` |
| 手動停止や OCI の終了処理の停滞 | TERMINATED 以外 | 管理対象に残る | `OCI_NOT_RUNNING` |
| キュー設定を変更・削除し、既存クラスターを通常の削除経路で拾えなくなる | 残存 | キュー対応なし | `QUEUE_CONFIG_MISSING` |
| API 権限不足、OCI CLI / Slurm の停止、不正な取得結果 | 判定不能 | 照合できない | `MONITOR_UNAVAILABLE` |

同じインスタンスに複数の条件が当てはまる場合、作成・削除の停滞、記録欠落、OCI 状態、Slurm 未登録、Slurm 障害、キュー不明、アイドル残存の順で理由を一つ表示する。原因を断定する分類ではない。例えば Slurm 未登録には、作成失敗と削除途中失敗の両方が含まれる。

## 2. 所属を確かめてから三つの情報を照合する

1. 初期コンパートメント・リージョンと、現在の `queues.conf` に指定された各コンパートメント・リージョンで、OCI のインスタンス一覧を全ページ取得する。`TERMINATED` は対象外とする。
2. 新規 Autoscaling ノードの freeform タグ `oci_hpc_autoscaling_deployment` と設定の `deployment_id` が一致するものを選ぶ。この ID は既存の `local.slurm_notification_deployment_id` を共用し、Slurm ジョブ通知や課金タグの有効・無効に依存しない。Instance Pool、Cluster Network、Compute Cluster の3経路で同じタグを付ける。
3. タグがない既存ノードは、`autoscaling/clusters/<cluster>/inventory` の compute セクションにある `oci_instance_id` と照合する。所属タグが別デプロイを示す場合は inventory よりタグを優先して除外する。初期クラスター名も除外する。
4. OCI の VNIC（仮想ネットワーク接続）の private IP / hostname と inventory の名前・IP を、Slurm の `NodeName` / `NodeAddr` / `NodeHostName` に照合する。OCI 表示名だけで判定しない。複数ノードに一致する場合は監視不能にする。
5. 作成・削除の印、Slurm 状態、未終了ジョブ、`queues.conf` の `permanent` を確認して、理由表の条件に分類する。Terraform state や作成ログの内容は通知に添付しない。
6. 異常の初回観測時刻を保存し、15分以上かつ2回以上観測された異常を OCI Notifications の管理者向け Topic（通知先）に送る。5分間隔なら、初回観測の約15分後が最初の通知目安となる。

`IDLE_CLUSTER_RETAINED` は、OCI に見える対象クラスターの全メンバーが Slurm の正確な `IDLE`、`CPUAlloc=0` で、割当先のある未終了ジョブもない場合に限る。`SUSPENDED` や `COMPLETING` も未終了ジョブとして扱う。`permanent=true` の正常なアイドル待機は通知しないが、未登録・障害・作成削除の停滞は通知する。

監視処理は読み取りと通知だけを行う。インスタンス削除、Slurm の DRAIN、Terraform の state 更新は行わない。削除の判断には実行中ジョブや利用者の意図の確認が必要なため、自動削除とは分けて運用する。

## 3. 有効化して dry-run で対象を確認する

Resource Manager の「Autoscaling 管理者メール通知」で、「管理者へのメール通知を有効化」を選び、共通の管理者メールアドレスを指定する。Terraform の入力は次のとおり。

```hcl
autoscaling_failure_notifications_enabled = true
autoscaling_failure_admin_email           = "hpc-admin@example.com"
slurm                                    = true
```

この1項目で作成失敗通知と残存監視が同時に有効になる。既存の Autoscaling 作成失敗通知 Topic と管理者宛先を共用し、Topic / Subscription のリソース名は維持する。管理者の EMAIL Subscription（メール購読）の確認を承認する。管理者通知の既定値は `false` で、無効時は監視用の読み取り IAM Policy を作成せず、両機能の定期実行を停止する。既存スタックで管理者通知が有効なら、この版の適用時に残存監視と必要な読み取り権限が追加される。

構成更新後に Plan を確認し、実環境への適用は通常の変更承認に従って行う。監視の配布は独立した `null_resource` で行い、計算ノードの再構成用トリガーには追加していない。既存スタックで新たに作成失敗通知も有効にする場合は、そのリソース追加も Plan の確認対象になる。

有効化すると、主 Controller で次が配置される。

| 配置先 | 内容 |
| --- | --- |
| `/opt/oci-hpc/bin/autoscaling_instance_monitor.py` | 監視本体。Python 3.6 以降、PyYAML、OCI CLI、Slurm コマンドを使用 |
| `/opt/oci-hpc/conf/autoscaling_instance_monitor.json` | 監視・通知の設定。所有者だけが読み書きできる権限 |
| `/etc/cron.d/oci-hpc-autoscaling-instance-monitor` | Controller ユーザーで5分ごとに起動 |
| `/opt/oci-hpc/logs/autoscaling-instance-monitor/state.json` | 異常の初回観測、最新観測、最終送信、監視状態 |
| `/opt/oci-hpc/logs/autoscaling_instance_monitor.log` | コマンド失敗等の診断 |

生成元 `autoscaling/tf_init/` の3つのインスタンス定義も配布する。今後作成するクラスターに識別タグが付く。既に作成済みのインスタンスや `autoscaling/clusters/<cluster>/` 内の生成済み定義は自動更新しない。

Controller 上で `opc` / `ubuntu` として次を実行する。OCI・Slurm を照合して即時の検出候補を表示し、通知・状態更新は行わない。表示結果は猶予期間を適用する前の候補である。

```bash
python3 /opt/oci-hpc/bin/autoscaling_instance_monitor.py --dry-run
```

終了コードは、照合成功が `0`、情報取得・設定・状態ファイルの異常が `2`。`0` は残存候補ゼロを意味しない。通知送信の成否は保持された `last_sent` とログを確認する。読み取りに失敗した後の再実行では、取得可能になったことを確認してから通常監視へ進む。

### 既存の野良インスタンスも対象に加える

タグも inventory も失った既存インスタンスは、表示名だけではこの環境の所有物と断定できない。OCI と過去の作成記録を照合したうえで、設定 JSON に OCID とクラスター名の対応を追記する。例の値はダミーである。

```json
"legacy_instance_clusters": {
  "ocid1.instance.oc1.example.verified-orphan": "batch-1-hpc"
}
```

これは JSON オブジェクト内に追加する項目。別デプロイの所属タグがあるインスタンスには適用しない。明示的に除外する場合は `excluded_instance_ids` の配列を指定する。初期クラスターの除外は `excluded_cluster_names` で維持する。

キューのリージョン・コンパートメントを削除した後も走査を続けるには、設定 JSON の `additional_scopes` に以前の組を残す。過去の別リージョンを自動で探索する機能はない。

```json
"additional_scopes": [
  {"region": "example-region", "compartment_id": "ocid1.compartment.oc1..example"}
]
```

手編集した設定は Terraform の監視配布リソースの再作成時に上書きされる。運用上の追加設定は別途管理し、構成更新後に再適用して dry-run で確認する。

### キューが別コンパートメントを使う場合は読み取り権限も確認する

Terraform が追加する監視用 Policy は初期 `targetCompartment` に限定する。別コンパートメントの Compute / VNIC を読む場合は、同じ Controller Dynamic Group にその対象の `read instances` と `read virtual-network-family` が必要になる。VCN が別コンパートメントなら、その VNIC 側の権限も確認する。API キー認証の場合は CLI を実行するユーザー側の権限を確認する。

インスタンス一覧、VNIC 一覧の全ページ取得、Topic への送信は公式 CLI の仕様に従う。参照：[インスタンス一覧](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/list-instances.htm)、[VNIC 一覧](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/compute/instance/list-vnics.html)、[Core Services の IAM 権限](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/corepolicyreference.htm)、[通知送信](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/ons/message/publish.html)。

## 4. 猶予と通知間隔を環境に合わせる

設定 JSON では次の値を変更できる。いずれも正の整数で秒を指定する。

| 項目 | 既定値 | 意味 |
| --- | --- | --- |
| `grace_seconds` | 900 | 同じインスタンス・同じ理由の観測を通知するまでの猶予 |
| `build_timeout_seconds` | 10800 | 作成の印が残っていても通常の作成途中として扱う上限。構築処理の最大150分を考慮して3時間 |
| `delete_timeout_seconds` | 3600 | 削除の印が残っていても通常の削除途中として扱う上限 |
| `repeat_seconds` | 21600 | 送信済み異常が続く場合の再通知間隔。既定で6時間 |
| `max_sample_gap_seconds` | 1800 | 観測間隔がこれを超えると継続判定をやり直す |

作成・削除の上限を過ぎてからも `grace_seconds` の観測猶予がある。印がない作成失敗や削除失敗は通常の15分猶予で判定する。印の時刻はファイルの更新時刻を使い、プロセスが実行中かどうかまでは証明しない。

送信失敗時には送信済み時刻を進めず、異常が続いている間は次回に再試行する。送信に成功した異常が照合上から消えると解消通知を送り、解消通知の失敗も次回に再試行する。初回通知前に消えた短時間の異常は通知しない。未送信の異常が解消した場合は過去の警報を後送しない。

OCI または Slurm の取得が失敗した回は、既存インスタンスの異常・解消判定を更新せず、別の監視不能状態を記録する。15分以上続けば `MONITOR_UNAVAILABLE` を同じ Topic へ通知する。通知経路自体が停止している間は配送できないため、ログと状態ファイルで確認する。各外部コマンドに時間制限を設け、同時起動はファイルロックで抑止する。

## 5. 通知を受けたら、原因と実行中ジョブを確認する

通知には理由、インスタンス OCID、クラスター名、リージョン、Shape、OCI / Slurm の状態、初回観測時刻を含める。監視不能通知には失敗した処理の例外種別を含め、生の CLI エラーや秘密情報を本文へ転記しない。

1. OCI Console で対象インスタンスと所属を確認する。クラスターの管理ディレクトリ・inventory・作成削除ログが残っていれば照合する。
2. Slurm のノード状態と未終了ジョブを確認し、実行中の計算や意図的な DRAIN でないかを判断する。
3. IAM、API・ネットワーク障害、Terraform の削除失敗、Autoscaling の定期実行停止など、原因を修復する。
4. 既存のクラスター単位の削除手順で再試行する。管理情報を失っている場合は、所有とジョブがないことを確認してから個別に対応する。
5. 次回の正常な照合結果と OCI 側の状態を確認する。解消通知は「前の検出条件がなくなった」という意味であり、課金停止の証明には使わない。

## 検知範囲と実環境で確かめる事項

この監視は残存インスタンスの検出であり、Boot / Block Volume、予約容量、ネットワーク等を含む全課金の監査ではない。STOPPED や TERMINATING を含め、実際の課金条件を個別には判断しない。

Controller 自体の停止、cron の停止、通知経路全体の停止はこのプログラムだけでは通知できない。外部監視で `state.json` の `last_success` が更新されることと Controller の死活を確認する運用を併用する。状態ファイルの破損・設定誤りは終了コード `2` とログで知らせるが、その状態ではメールの送信まで保証しない。

HA 構成でも配布先は主 Controller のみ。バックアップで同時起動すると通知が重複するため、自動フェイルオーバーは実装していない。主系障害時は外部監視で検知し、運用手順で切り替える。

所有タグの除去・改変と管理情報喪失が同時に起きると、明示登録なしには検知できない。ジョブが動いている正常な ALLOCATED / MIXED ノードや永久ノードの正常待機も、残存異常とはしない。誤った長時間ジョブによる費用は、別途ジョブ時間制限や費用監視で扱う。

OCI 一覧に加えて対象インスタンスごとに VNIC を取得するため、ノード数に応じて照合時間が増える。所要時間が5分を超える場合は実際の API 負荷・cron 間隔・観測上限を調整する。

実環境では、Instance Pool / Cluster Network / Compute Cluster それぞれで、未登録残存、削除途中失敗、DOWN / DRAIN、永久ノード、正常な長時間作成、API 読み取り失敗、通知失敗と再送、HA 主系障害を隔離環境で確認する。通知 Subscription の承認と実際のメール受信も確認する。ローカルのモック・CLI スタブ検証だけでは OCI 上の成功を保証しない。不明な運用判断は、対象の通知・ログを添えてクラスター管理者へ確認する。

用語：inventory は Controller のノード管理一覧、OCID は OCI リソースの識別子、IAM は API 操作の許可設定、dry-run は通知・状態保存を行わずに照合する実行方法を指す。

## 実行時にエラーが出る場合

失敗時は `Monitor failed [段階]: CommandError: 操作 failed (exit 終了コード): 診断` の形式で表示する。例えば `OCI_INSTANCE_LIST` は OCI インスタンス一覧、`OCI_VNIC_LIST` は VNIC 情報、`SLURM_SNAPSHOT` は Slurm 情報の取得段階を示す。コマンドが見つからない場合や90秒の制限時間を超えた場合も表示する。機密情報を含む行と秘密鍵本体、コマンドの全引数は表示しない。

旧版の `Monitor failed: RuntimeError` だけでは失敗原因を特定できない。Controller の `bin/autoscaling_instance_monitor.py` を更新し、同じ dry-run を再実行して表示される段階・操作・診断を確認する。OCI の権限エラーと Slurm の接続エラーを区別してから対応する。

Python 3.6 の OCI CLI が `Python was configured to use ASCII` で停止する場合は、監視と通知の子プロセスに UTF-8 ロケールが必要。修正版は `LC_ALL=C.UTF-8` / `LANG=C.UTF-8` を明示する。旧版の監視は内部で `LC_ALL=C` を上書きするため、シェル側の export だけでは直らない。Controller の `autoscaling_instance_monitor.py` と `autoscaling_failure_notify.py` を更新する。
