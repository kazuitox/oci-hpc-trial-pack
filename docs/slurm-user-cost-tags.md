# Slurmのユーザー別コストタグ

この機能は、計算ノードのOCI Definedタグ`hpc-cost.User`をSlurmの利用者に合わせて更新します。ノードを1ジョブが占有する運用を想定しています。ジョブIDは内部の状態管理と履歴にだけ使用し、OCIタグには書き込みません。

| 状態 | `hpc-cost.User`タグ |
| --- | --- |
| ノード作成・起動 | `Management`（作成設定の既定値） |
| ジョブ実行中 | Slurmユーザー名 |
| ジョブ終了後・アイドル | `Management` |
| 通常のアイドル削除 | `Management`を維持 |

## 設定と前提

- スタックの`slurm_user_tags_enabled`は既定値`true`です。Slurmを有効にした構成でジョブに応じた更新を行います。Terraformはこのフラグにかかわらず、デプロイ先コンパートメント（`targetCompartment`）へ以下の名前空間とキーを作成します。初期ノードとAutoscalingで作成するノードの両方へ`hpc-cost.User`を付与します。
- `queues.conf`の`tags`、スタックの`tags`変数、省略時の作成処理は`Management`を既定値にします。明示的なキュー設定や作成コマンドのタグ指定は引き続き優先されます。この機能の初期値にそろえる場合は、それらも`Management`にしてください。
- コントローラーと計算ノードが同じ`slurm_nfs_path`を共有している必要があります。その配下の`oci-user-tags`をroot所有・0700で使用します。NFSのroot書き込みを禁止する独自構成では、この共有領域への書き込み権限を整えてください。
- 計算ノードではPython 3とIMDSv2を使用します。OCI CLIはコントローラーと予備コントローラーのroot管理環境`/opt/slurm-oci-user-tags/venv`へ配置します。

| 項目 | 値 |
| --- | --- |
| Namespace | `hpc-cost` |
| Namespace description | `Tags for tracking HPC compute costs by Slurm user.` |
| Tag key | `User` |
| Tag key description | `Slurm user responsible for compute usage, or Management when the node is idle.` |
| Tag value type | Static value（任意の文字列。値の選択肢を制限する`validator`は設定しません） |

Static valueはタグ値を固定する設定ではありません。`Management`やSlurmユーザー名を実行時に文字列として設定できます。[OCIのタグキー定義](https://docs.oracle.com/en-us/iaas/Content/Tagging/Tasks/Create_tag_key_definition.htm)、[Terraformのタグリソース](https://docs.oracle.com/en-us/iaas/tools/terraform-provider-oci/latest/docs/r/identity_tag.html)

### IAM

Terraformを実行するユーザーまたはResource Principalには、テナンシとホームリージョンを参照する権限、および対象コンパートメントの`tag-namespaces`を管理する権限が必要です。名前空間・キーはホームリージョンのAPIで作成し、所属コンパートメントは`targetCompartment`とします。これらはIAM自動作成オプションと`slurm_user_tags_enabled`が無効でも必要です。コントローラーのInstance Principalには、対象インスタンスの参照・更新権限に加え、`hpc-cost`名前空間の`use tag-namespaces`権限が必要です。通常のIAM自動作成ではスタックのポリシーを使用します。IAMを外部管理する場合は、以下の権限を追加してください。[OCI APIごとの必要権限](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/corepolicyreference_topic-Permissions_Required_for_Each_API_Operation.htm)、[Definedタグの必要権限](https://docs.oracle.com/en-us/iaas/Content/Tagging/Tasks/managingtagsandtagnamespaces.htm)

Instance Principal用のポリシー例（名前とコンパートメントは環境に合わせて置き換えます）:

```text
Allow dynamic-group <controller-dynamic-group> to manage instances in compartment <compute-compartment> where any {request.permission = 'INSTANCE_READ', request.permission = 'INSTANCE_UPDATE'}
Allow dynamic-group <controller-dynamic-group> to use tag-namespaces in compartment <compute-compartment> where target.tag-namespace.name = 'hpc-cost'
```

## 既存環境からの移行

名前空間の名前はテナンシ全体で一意です。同じテナンシに`hpc-cost`が既にある場合、重複作成はできません。既存定義のコンパートメント・用途・管理元を確認し、このスタックへ管理を引き継ぐ場合だけTerraform stateへ取り込みます。以下はTerraform CLIでの例です。`User`が未作成ならキーのimportは不要です。[タグ名前空間のimport](https://docs.oracle.com/en-us/iaas/tools/terraform-provider-oci/latest/docs/r/identity_tag_namespace.html)、[タグキーのimport](https://docs.oracle.com/en-us/iaas/tools/terraform-provider-oci/latest/docs/r/identity_tag.html)

```bash
terraform import oci_identity_tag_namespace.hpc_cost 'ocid1.tagnamespace.oc1..example'
terraform import oci_identity_tag.hpc_cost_user 'tagNamespaces/ocid1.tagnamespace.oc1..example/tags/User'
```

同じ名前空間・キーを複数スタックのstateへ重複登録しないでください。このスタックで管理する定義はスタックのdestroy対象です。特に既存定義を取り込む場合、キーの削除はテナンシ内でそのタグを使用するリソースにも影響します。[OCIのタグ削除](https://docs.oracle.com/en-us/iaas/Content/Tagging/Tasks/managingtagsandtagnamespaces.htm)

更新時は、名前空間・キーとIAMを準備してから、コントローラー・予備コントローラー・計算ノードへ修正版の設定とスクリプトを配布します。Gitの更新だけでは稼働環境や生成済みのAutoscaling構成は変わりません。初期スタックの更新とAnsibleの再適用、今後作成するクラスター用のTerraform原本・変数生成元の配布を確認してください。共有領域の既存の状態・履歴は削除しません。

既存ノードへの移行では、Terraformのplanに出るInstance Configurationの更新・置換を確認してください。新しい構成を作成してPoolの参照を切り替えた後に旧構成を削除するよう、`create_before_destroy`を設定しています。構成の変更だけではPool内の既存ノードのタグは更新されません。Compute Clusterの計算ノードも、ジョブ中のタグをTerraformが初期値へ戻さないように`hpc-cost.User`の変更を無視するため、既存ノードにキーがない場合はapplyだけでは追加されないことがあります。ワーカーが有効なら、現在のSlurm割当と照合した次の更新でユーザー名または`Management`を付与します。ワーカーが無効な既存ノードには、OCI Consoleなどで名前空間`hpc-cost`、キー`User`へ初期値（通常は`Management`、独自の作成時タグ指定がある場合はその値）を設定してください。新規ノードでは作成時に付与します。

以後の更新先は`hpc-cost.User`です。ワーカーは既存のfreeformタグ`user`を変更しません。Terraformで管理するリソースの更新では、旧`user`を削除する差分が出る場合があるため、planを確認してください。旧タグの値が残っていても現在の割当を表すとは限らないため、費用集計やタグ確認ではDefinedタグの名前空間`hpc-cost`とキー`User`を選択してください。新しいタグを適用する前の費用が新しいキーへ自動移行することはありません。

## 更新の流れ

1. ノード登録時にIMDSv2から自分のOCIDとリージョンを取得し、Slurmノード名と関連付けます。
2. Prolog／Epilogが共有領域へ最新の割当状態とUTCの履歴を記録します。フック内ではOCI APIやSlurm照会コマンドを実行しません。課金用処理の失敗はログに残し、既存のヘルスチェックやPyxisのフックとは独立して扱います。
3. コントローラーの`slurm-oci-user-tags.timer`がワーカー終了の5秒後に次の処理を起動し、最新の状態をOCIへ反映します。過去の終了イベントを順番に再送する方式ではなく、更新前後に状態の世代を確認します。共有ストレージ上のディレクトリを原子的に操作して、複数ノード・コントローラー間の更新を調整します。
4. ワーカーはSlurmの現在の割当状態とも照合し、Epilogが実行されなかった場合の状態を補正します。Slurmへの照会が失敗した場合は、その結果をアイドルと解釈しません。
5. タグ更新時は既存のDefinedタグを読み、`hpc-cost`名前空間の`User`だけを変更します。同じ名前空間の別キー、別の名前空間、freeformタグは保持します。ETagの条件付き更新で同時変更を検出し、失敗時は待ち時間を5秒から最大300秒へ延ばして再試行します。OCI上で終了処理中・終了済みと確認できたノードは更新対象から外します。

設定ファイルは`/etc/slurm/oci-user-tags.json`です。内部の状態と履歴は共有領域に置くため、計算ノードの削除後も残ります。履歴はローテーションする運用ログです。長期の利用実績にはSlurm accountingも使用してください。

## 検証手順

まず少数の計算ノードで確認します。以下はデプロイ先のコントローラーで実行する例です。

```bash
systemctl status slurm-oci-user-tags.timer
journalctl -u slurm-oci-user-tags.service --since '30 minutes ago'
```

1. 初期ノードとAutoscalingで作成した新規ノードのDefinedタグが`hpc-cost.User=Management`であることを確認します。
2. ユーザーAでノードを占有するジョブを投入し、`hpc-cost.User`がAへ変更されることを確認します。
3. 正常終了、キャンセル、タイムアウトのそれぞれで`Management`へ戻ることを確認します。
4. Aの後にユーザーBを実行し、過去のAの終了処理がBを上書きしないことを確認します。
5. テスト環境でOCI更新を一時的に失敗させ、計算ジョブが継続すること、権限・接続を復旧すると現在の利用者へ反映されることを確認します。
6. テスト環境で複数ユーザーのジョブを同じノードへ割り当て、`hpc-cost.User`だけが削除され、同じ名前空間の別キー・別の名前空間・freeformタグが残ることを確認します。利用者が1人になればユーザー名へ、ジョブがなくなれば`Management`へ戻ることを確認します。
7. Cost AnalysisとCost Reportsで名前空間`hpc-cost`、キー`User`を確認します。同じ1時間内の短いジョブと、数時間にまたがるジョブを分けて比較し、タグの反映時刻と費用の帰属を検証します。

## Computeインスタンスのタグが変わらない場合

`srun --pty /bin/bash -i`でシェルを開いたまま、ComputeインスタンスのDefinedタグ`hpc-cost.User`を確認します。終了後は`Management`への復帰を確認します。短時間で終了するジョブは、非同期更新の前に終了してユーザー名が表示されない場合があります。

コントローラーと対象の計算ノードの両方で`sudo cat /etc/slurm/oci-user-tags.json`を実行し、`state_dir`が同じ共有領域を指すことを確認してください。コントローラーの登録件数は次のように確認できます。

```bash
sudo python3 - <<'PY'
import json
from pathlib import Path
config = json.loads(Path('/etc/slurm/oci-user-tags.json').read_text())
records = list(Path(config['state_dir']).glob('*.json'))
print('state_dir:', config['state_dir'])
print('registered records:', len(records))
PY
```

ジョブの割当があるのに登録がない場合、ワーカーは対象ノードのOCIDを特定できず、タグを更新できません。修正版では共有パスと未登録ノードをログに出して失敗を報告します。旧版では登録が0件でも`Succeeded`になっていました。計算ノードの登録エラーは`sudo journalctl -t slurm-oci-user-tags -n 40 --no-pager`で確認できます。登録やフックの成功時にはログを出さないため、ログが空でも未実行とは限りません。

### 保存先が`/nfs/cluster`と`/share`に分かれる既存環境の修正

旧版では外部NFSを追加し、Slurmの状態保存先には使わない構成（`add_nfs=true`、`slurm_nfs=false`）で、自動作成ノードだけが`/share/oci-user-tags`を使う不具合がありました。修正版では通常構成・HA構成とも、コントローラーと同じ`slurm_nfs`条件で保存先を選びます。

以下は、コントローラーの正しい保存先が`/nfs/cluster/oci-user-tags`であることを確認済みの環境向けです。該当する各計算ノードで設定を直して再登録します。Slurmの再起動は不要です。実行中ジョブはワーカーが現在のSlurm割当から復元します。

```bash
sudo sed -i.before-cost-tags-fix 's#"/share/oci-user-tags"#"/nfs/cluster/oci-user-tags"#' /etc/slurm/oci-user-tags.json
sudo /usr/local/sbin/slurm-oci-user-tags register --config /etc/slurm/oci-user-tags.json
```

コントローラーにはノード作成・増設用の古い設定も残るため、次も修正します。Gitの更新だけでは生成済みファイルは変更されません。クラスタの作成・増設・削除処理が実行されていないときに行ってください。

```bash
for f in /opt/oci-hpc/conf/variables.tf /opt/oci-hpc/autoscaling/clusters/*/variables.tf; do
  [ -f "$f" ] || continue
  sudo sed -i.before-cost-tags-fix 's#^variable "slurm_nfs_path" { default = "/share" }#variable "slurm_nfs_path" { default = "/nfs/cluster" }#' "$f"
  grep -H slurm_nfs_path "$f"
done
for f in /opt/oci-hpc/autoscaling/clusters/*/inventory; do
  [ -f "$f" ] || continue
  sudo sed -i.before-cost-tags-fix 's#^slurm_nfs_path = /share$#slurm_nfs_path = /nfs/cluster#' "$f"
  grep -H slurm_nfs_path "$f"
done
```

各出力が`/nfs/cluster`を示すことを確認します。予備コントローラーがある場合は、そのノード作成用設定も同様にそろえてください。共有領域にある既存の状態・履歴ファイルは削除しません。

## 費用の見方と制限

- OCIの更新処理は非同期です。タイマー間隔、対象ノード数、API応答時間によって遅れが生じます。開始から終了までが短いジョブでは、ユーザー名がOCIタグに現れない場合があります。
- ノードを複数ユーザーが同時に共有すると、単一の`hpc-cost.User`タグでは按分できません。その間は`hpc-cost.User`だけを削除し、Cost Analysisでは値なしとして扱います。利用者が1人になればユーザー名へ、ジョブがなくなれば`Management`へ戻します。同じユーザーの複数ジョブは、そのユーザー名で集計できます。
- ユーザータグで集計する対象はComputeインスタンスです。ブートボリューム、専用Block Volume、共有ストレージ、コントローラーなどの費用を自動でユーザーへ按分する機能は含みません。
- 強制削除や障害により、`Management`への復帰より先にインスタンスが終了する場合があります。タグの修正を過去の費用へ遡及適用することはできません。
- `slurm_user_tags_enabled=false`では専用フックとワーカーを停止します。名前空間・キー、ノード作成時のタグ付与、既存のOCIタグと履歴は保持します。停止後はジョブに応じた更新を行わないため、既存ノードの値は必要に応じて運用側で設定してください。
- Cost AnalysisやCost and Usage Reportsへの表示のために、キーの`is_cost_tracking`を有効化する必要はありません。このスタックも明示的には有効化しません。[OCIのコスト追跡タグ](https://docs.oracle.com/en-us/iaas/Content/Tagging/Concepts/taggingoverview.htm)
- Cost Analysisの表示には最大48時間かかる場合があります。タグは適用前の費用に遡及せず、同じ時間内のタグ切り替えに対する秒・分単位の配賦精度は、この実装だけでは保証できません。[OCI Cost Analysis](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costanalysisoverview.htm)

厳密な費用配賦が必要な場合は、ノードのOCIDとSlurmの割当履歴をOCI Cost Reportsへ突き合わせ、別途定めた配賦ルールで計算してください。[OCI Cost Reports](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costusagereportsoverview.htm)
