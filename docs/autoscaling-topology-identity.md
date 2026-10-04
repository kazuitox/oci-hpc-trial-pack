# Autoscaling の Switch 名を完全一致で扱う

`instance_keyword` に `e5` と `e5-lite` が共存すると、区切りのない Switch 検索が両方の行を返します。削除処理には、検索・展開の失敗を空一覧として続行する箇所もあり、既存の非アクティブノード一覧を失うおそれがあります。修正版は名前と設定形式を維持し、必要な検索・Slurm コマンドが成功してから topology をまとめて更新します。

## master で確認した検索と更新

調査対象はローカル `master` の `52f0e79ffac92008ff9e1220234c791db7292e7a`。標準版・軽量版の作成、削除、ラック対応、到達不能ノードの回収、キュー設定と cron の読み手を確認しました。

| 処理・生成元 | 修正前の状態 | 修正内容 |
| --- | --- | --- |
| `slurm/tasks/destroy.yml` | クラスター・inactive の2検索で末尾区切りがない。検索・展開失敗を空リストとして続行する。 | 名前を完全一致で照合。必要な inactive がない場合やコマンド失敗時は更新前に停止する。 |
| `slurm/tasks/compute-rack-aware.yml` | inactive と個別ラックの検索が部分一致。ラック検索のエラーを無視する。 | inactive と個別ラックを完全一致で扱う。クラスター配下のラック一覧は `cluster_name + ':'` で取得する。 |
| `slurm/tasks/destroy-rack-aware.yml` | inactive と個別ラックが部分一致。inactive の取得失敗を空リストとして続行する。 | 完全一致で処理し、対象ラックと親 Switch を更新する。失敗を空リストに変換しない。 |
| `slurm/tasks/compute.yml`、`lite_compute.yml` | クラスター検索に空白境界、inactive 検索に末尾スペースがあり、`e5`／`e5-lite` 自体は区別できる。ただし正規表現の文字を解釈し、展開完了前の書き込みがある。 | 共通処理へ移し、文字列による識別と更新前の検証をそろえる。 |
| `destroy_unreachable/tasks/slurm*.yml` | 複数の `scontrol` 出力を Shell で分解し、複数の `lineinfile` で topology を更新する。Switch 更新の正規表現が名前をエスケープしない。 | ノード名の末尾 `-node-<番号>` から inactive 名を特定し、既存の一覧を取得してまとめて更新する。 |
| `slurm/tasks/server.yml` | 新規キューを追加する検索には空白境界があるが、正規表現の文字を解釈する。 | 行頭・空白境界を明示し、識別子を `regex_escape` で処理する。 |
| `autoscale_slurm*.sh` | キューと `instance_keyword` は `==` で照合。クラスター名はキューを特定してから番号と残りのキーワードを分け、ハイフンを含むキーワードも保持する。 | 今回の部分一致に関する変更は不要。 |
| `create_cluster.sh` とキュー検証 | `yq` の設定取得はキュー名・instance type 名の `==`。キーワードの重複検証も値の比較。 | 今回の部分一致に関する変更は不要。 |

末尾スペースを含む `grep -F` は、生成済みの通常形式で `e5`／`e5-lite` を区別する最小修正として有効です。`grep -F` だけでは部分一致が残ります。今回は検索・展開失敗時の保全と途中更新への対処も必要なため、共通の Ansible モジュール [slurm_topology.py](../playbooks/library/slurm_topology.py) を使います。

## 検証が終わるまで topology を書き換えない

共通処理は `SwitchName` をキーとする一覧を読み、名前を文字列として照合します。空白・タブの区切りを扱い、`.` などを正規表現として解釈しません。コメントや対象外の行、既存行の追加属性を保持します。

必要な inactive Switch の欠落、Switch 名やフィールドの重複、不正な行、Slurm コマンドの失敗、不正な hostlist はエラーになります。新規作成時のクラスター／ラック Switch の欠落と、既に削除済みのクラスターは許容します。`Nodes=` が空の inactive 行は正しい空一覧として扱います。

すべての展開・集合演算・圧縮が成功してから、一時ファイルを同じディレクトリに書き、Ansible の `atomic_move` で置き換えます。既存ファイルの所有者・権限を引き継ぎます。読み取りから置換まで `topology.conf.oci-hpc.lock` のロックを保持し、この共通処理同士の並行更新を直列化します。チェックモードでは更新予定だけを計算します。

このロックは、修正前のスクリプトや手動編集、server のキュー追加処理を直列化するものではありません。新旧の処理を並行実行せず、配布やキュー再生成は Autoscaling の作成・削除が動いていない時間に行ってください。

## 既存環境ではタスクとモジュールを一緒に配布する

`instance_keyword`、`queues.conf`、クラスターのディレクトリ名、Switch 名、ノード名は維持します。Terraform・IAM の変更やノードの再作成は、この修正の適用要件ではありません。

既存コントローラーでは Autoscaling の cron と進行中の作成・削除・回収処理を確認し、更新中に新旧のタスクが混在しないようにします。`playbooks/roles/slurm/tasks/` と `playbooks/roles/destroy_unreachable/tasks/` の変更ファイルに加え、**`playbooks/library/slurm_topology.py` も配布してください**。Ansible は playbook の隣の `library/` からモジュールを読みます。HA のバックアップ側でも同じ配布物を使います。

切り戻す場合は、同じ変更単位のタスクを旧版へ戻します。新モジュールだけを削除すると修正版のタスクは動きません。識別子の形式を変えていないため、切り戻し用の topology 形式変換は不要です。既に壊れた topology の自動修復は行いません。エラーになった場合はバックアップや Slurm・inventory と照合して復旧してください。

この変更は topology 更新の失敗をエラーとして返します。`delete_cluster.sh` が Ansible cleanup 失敗後に Terraform destroy を続ける既存の制御は変更していません。削除全体を中断する運用変更とは区別してください。

## v2.10.6.20 では作成側にも部分一致が残っている

ローカルタグの実装を確認したところ、`compute.yml` と `lite_compute.yml` のクラスター検索にも `grep "SwitchName={{cluster_name}}"` が残っています。既存の空リストへの rescue と併せて対処する必要があります。

今回の変更は現在の `master` 向けです。v2.10.6.20 へは、共通モジュールと対象タスクを一緒に移植し、その版の inventory・Ansible・Slurm で検証してください。タグのコード変更や顧客環境への配布は、この作業では実施していません。

## フォーク元では設定と topology の設計が変わっている

2026-10-02 に取得したフォーク元の最新 `master` は、[v3.2.1 のコミット `419ef59599f44862302cfa2de5752a57e68dbfdb`](https://github.com/oracle-quickstart/oci-hpc/commit/419ef59599f44862302cfa2de5752a57e68dbfdb)（2026-09-08）でした。取得したツリーを検索した範囲では、`instance_keyword` はありません。

[Configurations モデル](https://github.com/oracle-quickstart/oci-hpc/blob/419ef59599f44862302cfa2de5752a57e68dbfdb/mgmt/lib/database.py)に `hostname_convention` があり、[topology 生成タスク](https://github.com/oracle-quickstart/oci-hpc/blob/419ef59599f44862302cfa2de5752a57e68dbfdb/playbooks/roles/slurm/tasks/generate-topology.yml)は設定名から `od-<name>` の Switch と、`hostname_convention-[1-max_number_nodes]` のノード一覧を生成します。[管理コード](https://github.com/oracle-quickstart/oci-hpc/blob/419ef59599f44862302cfa2de5752a57e68dbfdb/mgmt/lib/functions.py)には、パーティション単位の `partition:inactive` を読み書きする経路もあります。設定を構造化して読み、管理対象と対象外の行を分ける設計は今回の参考になります。

互換性の判断として、この実装を単に取り込んで `instance_keyword` を削除することは避けます。本リポジトリではキーワードがノード名、topology、クラスター名、Slurm／GRES テンプレートに使われています。廃止するなら、これらと既存クラスターの移行を別途設計する必要があります。

## ローカル検証と実環境確認を分ける

[回帰テスト](../tests/test_autoscaling_topology_identity.py)は `e5`／`e5-lite` の両方向の作成・削除、ラック、到達不能ノード、空一覧、欠落・重複・コマンド失敗を確認します。Ansible を利用できる場合は実タスクから共通モジュールを呼び、一時ファイルと Slurm スタブで内容・権限の保持を検証します。2つのプロセスから `e5`／`e5-lite` を同時更新し、両方の変更が残ることも確認します。

```bash
python3 -m unittest tests.test_autoscaling_topology_identity
```

実 OCI、Slurm、OL8、Ubuntu、HA での構築・削除は未検証です。許可された検証環境では `e5` と `e5-lite` を登録し、標準版・軽量版でそれぞれのジョブからクラスター作成・実行・アイドル削除を確認してください。ラック対応と回収処理では対象の Switch だけが更新され、既存一覧が保持されることを確認します。故意の欠落・重複・コマンド失敗の試験はコピーした設定で行い、失敗前後の内容を比較します。
