# 回帰確認台帳

この台帳は、変更時に守る振る舞いと、その確認方法を結び付ける。実装・仕様・テストの変更と同じ変更単位で更新する。

「自動検証あり」は記載したローカルテストの範囲を意味し、実環境での成功や機能全体の網羅を保証しない。「自動検証未整備」は、現在の `tests/` に該当する専用テストを確認できていない項目。ここに示す手動確認は実施記録ではない。

## 自動検証がある振る舞い

| ID | 守る振る舞い | 対応するテストと範囲 |
| --- | --- | --- |
| REG-001 | Shape に対応する OCPU の上限・選択肢・表示条件を維持し、UI 入力と Terraform の参照先を一致させる。 | [test_instance_pool_flex_ocpus.py](../tests/test_instance_pool_flex_ocpus.py)：schema・変数宣言・locals の静的検査。 |
| REG-002 | ノード専用一時 Block Volume は所属タグ・属性で識別し、他のボリュームを削除しない。削除失敗時の記録、再試行、ロック、DNS・inventory・state 更新の整合性を維持する。 | [test_resize_local_block_volume.py](../tests/test_resize_local_block_volume.py)：OCI・Terraform 等をモック化した処理テスト。resize を通常運用として推奨する意味ではない。 |
| REG-003 | Slurm 通知は登録済みユーザーに配送し、無効時には送信しない。Slurm 側では通知をキューへ渡し、ワーカーで再試行・期限・レートを制御する。初期化時には既存ユーザーと後始末の記録を保持する。 | [test_slurm_oci_notifications.py](../tests/test_slurm_oci_notifications.py)：本文、キュー、ワーカー、レジストリ更新の処理テスト。 |
| REG-004 | LDAP ユーザー操作と通知リソース・レジストリを整合させる。通知無効時の従来動作、部分失敗の復旧、HA バックアップ同期を維持し、Terraform 管理ユーザーを CLI の削除対象にしない。 | [test_cluster_cli_notifications.py](../tests/test_cluster_cli_notifications.py)：LDAP・OCI・SSH 等をモック化した CLI / 同期処理テスト。 |
| REG-005 | 通知の後始末は対象デプロイの所有物だけに限定する。保護対象・Terraform 管理 Topic・別デプロイの Topic を削除しない。不正な応答や所有権不明の状態を成功として処理しない。 | [test_slurm_oci_notification_cleanup.py](../tests/test_slurm_oci_notification_cleanup.py)：所有権、履歴、削除、消滅確認、失敗時の処理テスト。 |
| REG-006 | 通知の実行時リソースの後始末は、必要な controller・権限が利用できる順序で行う。通常更新で後始末用リソースを置換せず、デプロイ ID と設定受け渡しを維持する。 | [test_slurm_notification_destroy_cleanup_terraform.py](../tests/test_slurm_notification_destroy_cleanup_terraform.py)：Terraform と配布テンプレートの静的検査。実際の destroy 順序は別途確認が必要。 |
| REG-007 | OOD のユーザーマッピングは LDAP ユーザー名と想定するメール形式を扱い、存在しないユーザーや危険な入力を拒否する。 | [test_openondemand_user_mapping.py](../tests/test_openondemand_user_mapping.py)：設定検査と `getent` を置き換えた mapper の実行。 |
| REG-008 | OpenComposer の Slurm ジョブ・History を OOD のヘッダー内から利用できる構成を維持する。アプリを事前登録し、JavaScript エラー時にも表示領域を失わない。 | [test_opencomposer_dashboard_link.py](../tests/test_opencomposer_dashboard_link.py)：Jinja2 のレンダリングと設定・HTML の検査。ブラウザー操作は含まない。 |
| REG-009 | 非 MPI・MPI・OpenMP のプロファイルに対応する入力項目と SBATCH 設定を生成し、MPI 実行環境ごとの選択肢を維持する。 | [test_opencomposer_execution_profiles.py](../tests/test_opencomposer_execution_profiles.py)：フォームテンプレートの静的検査。実際の MPI 通信は含まない。 |
| REG-010 | 通知フォームとジョブスクリプトを同じ有効条件で制御する。通知の選択は既定でオフとし、空の `--mail-type` や重複する指定を生成しない。 | [test_opencomposer_slurm_mail_notifications.py](../tests/test_opencomposer_slurm_mail_notifications.py)：フォームとスクリプト生成定義の静的検査。 |
| REG-011 | OOD の CPU Cores 表示はノードごとの物理コアを集計し、HT 有効・無効の混在や重複ノードで誤集計しない。 | [test_openondemand_system_status_cores.py](../tests/test_openondemand_system_status_cores.py)：配置設定の検査と Ruby による集計処理の実行。Ruby がない場合は実行テストをスキップ。 |
| REG-016 | Slurm利用者をDefinedタグ`hpc-cost.User`へ非同期反映する。アイドル時は`Management`、複数ユーザーの共有時はこのキーだけを除去し、他のDefinedタグとfreeformタグを保持する。遅延イベント、再試行、排他制御で新しい利用者を過去の状態へ戻さない。初期ノード・Autoscalingノードでも同じキーと既定値を使い、既存ノードの実行中ユーザー値を新規ノードへ複製しない。 | [test_slurm_oci_user_tags.py](../tests/test_slurm_oci_user_tags.py)：OCI・Slurmをモック化した更新処理。[test_slurm_user_tags_configuration.py](../tests/test_slurm_user_tags_configuration.py)：Terraform・設定経路の静的検査とJinja2レンダリング。[test_create_cluster_cost_tags.py](../tests/test_create_cluster_cost_tags.py)：作成タグの選択処理。[test_resize_local_block_volume.py](../tests/test_resize_local_block_volume.py)：ノード複製時のタグ保持・初期化。 |
| REG-017 | Instance Pool・Cluster Network・Compute Clusterの実OSホスト名とOCI表示名・DNS・inventoryを整合させる。所属・所有権とTerraform管理ノードを保護し、部分失敗の記録・再試行・排他制御を維持する。 | [Instance Pool](../tests/test_resize_instance_pool_hostname_sync.py)、[Cluster Network](../tests/test_resize_cluster_network_hostname_sync.py)、[Compute Cluster](../tests/test_resize_compute_cluster_hostname_sync.py)：OCI等をモック化した同期・移行・削除の処理テスト。REG-002 / REG-016と併せ、新規ノードの一時名とコストタグ初期化も確認する。 |
| REG-018 | AMD VMの対応するHT設定を初期・動的ノードへ渡す。非対応キュー設定を反映前に拒否し、VMではOS側のCPUオフライン化をしない。BMのCPU範囲表記と失敗検出を維持する。 | [Terraform設定](../tests/test_instance_pool_hyperthreading_terraform.py)、[HT role](../tests/test_hyperthreading_role.py)、[OS側制御](../tests/test_hyperthreading_guest.py)、[キュー検証](../tests/test_slurm_config_validation.py)。Terraform mock planは1.7以上が必要。実機のShape対応情報は別途確認する。 |

## 自動検証が未整備の振る舞い

関連箇所を変更する際は、可能な範囲で自動検証を追加する。追加できない確認は、手順と未検証の理由を報告する。

| ID | 守る振る舞い | 参照先・確認方法 |
| --- | --- | --- |
| REG-012 | Autoscaling はジョブに応じてクラスターを作成し、アイドル時に削除する。`permanent` とキューの上限を尊重し、通常の cron では途中リサイズを有効化しない。 | [README](../README.md)、[cron 設定](../playbooks/roles/cron/tasks/el.yml)、[Autoscaling スクリプト](../autoscaling/crontab/autoscale_slurm_disable-resize.sh)。設定検査と、pending ジョブからの作成・実行・アイドル削除を確認する。 |
| REG-013 | 初期ノードと動的作成ノードへ必要な設定が渡る。SIMPLE / ADVANCED の表示を変えても必要な入力や既定値を失わない。 | [schema.yaml](../schema.yaml)、[inventory.tpl](../inventory.tpl)、[変数生成テンプレート](../conf/variables.tpl)、[動的ノード inventory](../autoscaling/tf_init/inventory.tpl)。REG-001 / REG-006 / REG-016 で確認する一部以外は、生成結果と構築結果を確認する。 |
| REG-014 | VNC / DCV と GPU の設定を区別し、旧 `ood_vnc_use_gpu=true` の互換動作を維持する。各ジョブを適切なパーティションへ投入する。 | [README](../README.md)、[locals.tf](../locals.tf)、[Open OnDemand role](../playbooks/roles/openondemand)。旧設定と新設定の CPU / GPU・DCV 有効 / 無効の組合せを確認する。 |
| REG-015 | Definedタグ`hpc-cost.User`はホームリージョンでデプロイ先コンパートメントへ作成する。IAM 自動作成・Slurm 通知・ユーザータグ更新の有効状態にかかわらず、そのための tenancy / home region 参照を行う。IAM 自動作成と通知が無効の場合、それらのポリシーや動的グループは作成しない。 | [data.tf](../data.tf)、[locals.tf](../locals.tf)、[iam.tf](../iam.tf)、[cost-tags.tf](../cost-tags.tf)。旧来の参照省略条件は、常時行う初期タグ付与をDefinedタグへ移したことにより変更した。各フラグの組合せで参照と生成されるリソースを確認する。 |

## 変更範囲に応じた実環境確認

依頼で許可された検証環境で、関係する項目だけを実施する。ローカルの文書変更やテスト実行のために、これらの環境を新規作成する必要はない。

| 対象 ID | 手順と期待結果 |
| --- | --- |
| REG-001 / REG-013 | Resource Manager の SIMPLE / ADVANCED を切り替え、対象 Shape の入力を確認する。初期ノードと Autoscaling ノードで、指定した CPU・メモリ・関連設定が反映される。 |
| REG-002 | 対象ノードの一時 Block Volume のアタッチ・マウントと削除を確認する。共有領域や対象外 Volume が残り、部分失敗時の記録から後始末を再開できる。 |
| REG-003 / REG-004 / REG-010 | 通知有効・無効、登録済み・未登録のユーザーでジョブを投入する。選択したイベントだけが意図した宛先へ届き、送信失敗がジョブ進行を妨げない。HA 変更時はバックアップ側のレジストリ同期も確認する。 |
| REG-005 / REG-006 | 通常更新で後始末が発火しないことを確認する。スタック削除の検証では、動的に作成された対象 Topic が削除され、対象外・保護対象 Topic が後始末処理から除外される。 |
| REG-007 / REG-008 / REG-009 / REG-011 | OOD にログインし、メニュー・History・ヘッダー・CPU 表示を確認する。変更した実行プロファイルで生成スクリプトを確認し、小規模ジョブの完了を確認する。 |
| REG-012 | ジョブの constraint / partition に合うクラスターが作成されること、アイドル削除、permanent の保持、設定上限の適用を確認する。 |
| REG-014 | 変更した CPU / GPU・VNC / DCV の組合せでデスクトップジョブを投入し、所定のキュー、ノード構成、ブラウザー接続を確認する。旧設定を使う更新でも互換動作が保たれる。 |
| REG-015 | 対象となる IAM / 通知設定の組合せで Terraform の参照と権限要求を確認する。タグ定義に必要な参照と、それ以外の機能に由来する参照を区別する。 |
| REG-016 | [ユーザー別コストタグの検証手順](slurm-user-cost-tags.md#検証手順)に従い、デプロイ先コンパートメントに`hpc-cost`とStatic valueの`User`が作成されること、初期ノード・Autoscalingノードで`Management`→ユーザー名→`Management`へ反映されることを確認する。複数ユーザー共有時は対象キーのみ除去し、他タグを保持する。フラグ無効時も定義と作成時タグは残り、非同期更新だけが停止する。既存のfreeformタグからの更新とIAM外部管理時の必要権限も確認する。 |
| REG-017 | 3種類の作成方式でOSホスト名とOCIインスタンス・Primary VNIC表示名を照合する。既存構成はREADMEのreconfigure手順でDNSを移行し、部分失敗後の再開、対象外ノード・共有リソースの保持を確認する。 |
| REG-018 | AMD VMのHT On/Offを初期・動的ノードで確認する。Intel VMのHT Off拒否、HT Onの従来構成、BM制御を確認する。既存VMの構成更新だけでHTが変わったと判断しない。 |

## 台帳の更新方法

- ID は既存のものを使い続ける。新しい保護対象には新しい ID を付ける。
- 不具合修正時は、守る振る舞いと再発を検出するテストを対応づける。自動化した項目は検証済みの範囲を明記して移動・更新する。
- 仕様変更で保護条件を変える場合は、理由・互換性への影響を変更説明に残し、実装・テスト・台帳を整合させる。
- 毎回のテスト結果や環境固有のログは台帳に積み上げず、作業報告や PR に実施日・対象環境・結果・スキップ・未実施を記録する。
