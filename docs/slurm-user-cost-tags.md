# Slurmのユーザー別コストタグ

この機能は、計算ノードのOCI freeformタグ`user`をSlurmの利用者に合わせて更新します。ノードを1ジョブが占有する運用を想定しています。ジョブIDは内部の状態管理と履歴にだけ使用し、OCIタグには書き込みません。

| 状態 | `user`タグ |
| --- | --- |
| ノード作成・起動 | `Management`（作成設定の既定値） |
| ジョブ実行中 | Slurmユーザー名 |
| ジョブ終了後・アイドル | `Management` |
| 通常のアイドル削除 | `Management`を維持 |

## 設定と前提

- スタックの`slurm_user_tags_enabled`は既定値`true`です。Slurmを有効にした構成で使用します。
- `queues.conf`の`tags`、スタックの`tags`変数、省略時の作成処理は`Management`を既定値にします。明示的なキュー設定や作成コマンドのタグ指定は引き続き優先されます。この機能の初期値にそろえる場合は、それらも`Management`にしてください。
- コントローラーと計算ノードが同じ`slurm_nfs_path`を共有している必要があります。その配下の`oci-user-tags`をroot所有・0700で使用します。NFSのroot書き込みを禁止する独自構成では、この共有領域への書き込み権限を整えてください。
- 計算ノードではPython 3とIMDSv2を使用します。OCI CLIはコントローラーと予備コントローラーのroot管理環境`/opt/slurm-oci-user-tags/venv`へ配置します。
- コントローラーのInstance Principalに対象インスタンスの参照・更新権限が必要です。スタックが通常のIAMを自動作成する構成では既存ポリシーを使用します。IAMを外部管理する場合は、対象コンパートメントに対する`INSTANCE_READ`、`INSTANCE_UPDATE`を許可してください。タグ名前空間の新規作成は不要です。[OCI APIごとの必要権限](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/corepolicyreference_topic-Permissions_Required_for_Each_API_Operation.htm)

Instance Principal用のポリシー例（名前とコンパートメントは環境に合わせて置き換えます）:

```text
Allow dynamic-group <controller-dynamic-group> to manage instances in compartment <compute-compartment> where any {request.permission = 'INSTANCE_READ', request.permission = 'INSTANCE_UPDATE'}
```

## 更新の流れ

1. ノード登録時にIMDSv2から自分のOCIDとリージョンを取得し、Slurmノード名と関連付けます。
2. Prolog／Epilogが共有領域へ最新の割当状態とUTCの履歴を記録します。フック内ではOCI APIやSlurm照会コマンドを実行しません。課金用処理の失敗はログに残し、既存のヘルスチェックやPyxisのフックとは独立して扱います。
3. コントローラーの`slurm-oci-user-tags.timer`がワーカー終了の5秒後に次の処理を起動し、最新の状態をOCIへ反映します。過去の終了イベントを順番に再送する方式ではなく、更新前後に状態の世代を確認します。共有ストレージ上のディレクトリを原子的に操作して、複数ノード・コントローラー間の更新を調整します。
4. ワーカーはSlurmの現在の割当状態とも照合し、Epilogが実行されなかった場合の状態を補正します。Slurmへの照会が失敗した場合は、その結果をアイドルと解釈しません。
5. タグ更新時は既存のfreeformタグを読み、`user`だけを変更します。ETagの条件付き更新で同時変更を検出し、失敗時は待ち時間を5秒から最大300秒へ延ばして再試行します。OCI上で終了処理中・終了済みと確認できたノードは更新対象から外します。

設定ファイルは`/etc/slurm/oci-user-tags.json`です。内部の状態と履歴は共有領域に置くため、計算ノードの削除後も残ります。履歴はローテーションする運用ログです。長期の利用実績にはSlurm accountingも使用してください。

## 検証手順

まず少数の計算ノードで確認します。以下はデプロイ先のコントローラーで実行する例です。

```bash
systemctl status slurm-oci-user-tags.timer
journalctl -u slurm-oci-user-tags.service --since '30 minutes ago'
```

1. 新規ノードのOCIタグが`user=Management`であることを確認します。
2. ユーザーAでノードを占有するジョブを投入し、`user`がAへ変更されることを確認します。
3. 正常終了、キャンセル、タイムアウトのそれぞれで`Management`へ戻ることを確認します。
4. Aの後にユーザーBを実行し、過去のAの終了処理がBを上書きしないことを確認します。
5. テスト環境でOCI更新を一時的に失敗させ、計算ジョブが継続すること、権限・接続を復旧すると現在の利用者へ反映されることを確認します。
6. Cost AnalysisとCost Reportsを確認します。同じ1時間内の短いジョブと、数時間にまたがるジョブを分けて比較し、タグの反映時刻と費用の帰属を検証します。

## 費用の見方と制限

- OCIの更新処理は非同期です。タイマー間隔、対象ノード数、API応答時間によって遅れが生じます。開始から終了までが短いジョブでは、ユーザー名がOCIタグに現れない場合があります。
- ノードを複数ユーザーが同時に共有すると、単一の`user`タグでは按分できません。その間は`user`タグだけを削除し、Cost Analysisでは値なしとして扱います。利用者が1人になればユーザー名へ、ジョブがなくなれば`Management`へ戻します。同じユーザーの複数ジョブは、そのユーザー名で集計できます。
- ユーザータグで集計する対象はComputeインスタンスです。ブートボリューム、専用Block Volume、共有ストレージ、コントローラーなどの費用を自動でユーザーへ按分する機能は含みません。
- 強制削除や障害により、`Management`への復帰より先にインスタンスが終了する場合があります。タグの修正を過去の費用へ遡及適用することはできません。
- 機能を無効化すると専用フックとワーカーを停止します。既存のOCIタグと履歴は保持されるため、以後の集計に使う初期値は必要に応じて運用側で設定してください。
- Cost Analysisの表示には最大48時間かかる場合があります。タグは適用前の費用に遡及せず、同じ時間内のタグ切り替えに対する秒・分単位の配賦精度は、この実装だけでは保証できません。[OCI Cost Analysis](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costanalysisoverview.htm)

厳密な費用配賦が必要な場合は、ノードのOCIDとSlurmの割当履歴をOCI Cost Reportsへ突き合わせ、別途定めた配賦ルールで計算してください。[OCI Cost Reports](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costusagereportsoverview.htm)
