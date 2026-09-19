# Changelog

このファイルには、OCI HPC Trial Pack の主な変更を記録します。

## [Unreleased]

## [v1.3.0] - 2026-09-19

### Added

- Instance Pool・Cluster Network・Compute Clusterで、Ansible適用後の実OSホスト名へOCIインスタンス名とPrimary VNIC表示名を同期します。DNS・inventory・監視情報を整合させ、部分失敗時は未完了の再構成を再開します。
- Slurmの利用者をOCI Definedタグ`hpc-cost.User`へ非同期反映します。ノード作成時・アイドル時は`Management`、ジョブ実行中はユーザー名とし、複数ユーザー共有時は対象キーを除去します。再試行・排他制御・Slurm割当との照合に対応します。

### Fixed

- 対応するAMD VMのInstance Poolで、`hyperthreading`をOCI起動時の構成へ反映します。初期ノードとAutoscalingノードを同じ方式で制御します。
- ベアメタルのOS側HT制御が`0-1`などのCPU範囲表記に対応し、CPU状態の変更失敗をエラーとして扱います。
- コントローラーと動的作成ノードのユーザータグ状態保存先を統一し、新規ノードに複製元の実行中ユーザーを引き継がないようにしました。
- ホスト名同期とコストタグの統合テストを、新しいノード作成関数・Primary VNICの取得方式に対応させました。

### Compatibility / 更新時の注意

- Autoscaling側もTerraform 1.5.0以上が必要です。Instance Configurationは新しい構成を作成してから旧構成を削除します。既存VMのHT設定は構成更新だけでは変わらず、新規作成時に反映されます。
- Intel VMのHT Offは対象外です。`hyperthreading=false`などの非対応・不正なキュー設定はSlurm設定反映前に拒否します。VMではOS側のCPUオフライン化を行いません。旧サービスでCPUをオフライン化した既存VMは、ジョブ終了後に適切な設定で再作成してください。
- 既存クラスターの名前・DNS移行は`resize.sh --cluster_name <cluster_name> reconfigure`で行います。通常のAutoscalingは引き続きクラスター単位の作成・削除です。
- freeformタグ`user`からDefinedタグ`hpc-cost.User`へ移行します。機能フラグやIAM自動作成が無効でもタグ定義の作成・テナンシ／ホームリージョン参照の権限が必要です。既存の同名タグ定義とTerraform stateの管理元を確認してください。
- 詳細は[リリースノート](docs/releases/v1.3.0.md)と[コストタグの移行手順](docs/slurm-user-cost-tags.md)を参照してください。
- `fix/autoscale-safe-node-removal`は含みません。独立した後続リリースで扱います。

## v1.2.0までの既公開変更（累積記録）

以下は旧`Unreleased`に残っていた、v1.0.0以降からv1.2.0までの既公開変更です。v1.3.0の新規変更には含まれません。各版の公開内容は[GitHub Releases](https://github.com/kazuitox/oci-hpc-trial-pack/releases)を参照してください。

### Added

- Open OnDemandに「04 OpenComposer」メニューを追加し、「Slurmジョブ」と「History」をOpen OnDemandのヘッダー内で利用できるようにしました。
- OpenComposerの実行プロファイルを非MPI、MPI、OpenMPで切り替えられるようにし、Platform MPI v9.xとプロファイル別のCPU・タスク数指定を追加しました。
- Slurmの`BEGIN` / `END` / `FAIL`イベントをOCI Notifications経由でメール送信する機能を追加しました。
- OpenComposerのジョブフォームにメール通知の有効化と通知タイミングの選択項目を追加しました。
- LDAPユーザーの追加・削除と連動してNotifications Topic / Subscriptionおよび通知レジストリを管理できるようにしました。

### Changed

- 計算ノードのFlex ShapeごとにOCPU数の入力上限を切り替え、E5/E6 Shapeで最大126 OCPUを指定できるようにしました。
- Slurmジョブ通知メールの本文を、既存項目を維持した固定幅のテキスト表に変更しました。
- Open OnDemandダッシュボードのOpenComposerリンクから、Slurmジョブ投入フォームを直接開くようにしました。
- Open OnDemandのAmazon DCV連携とNVIDIA A10 GPUデスクトップを独立したオプションに分離しました。
- Amazon DCVをCPU shapeで利用できるようにし、CPUノードでのGUI導入とGPUノードでのDCV-GL構成をAnsibleで分岐しました。
- Amazon DCVの検証用途、自動評価ライセンスの有効期間、継続利用時のライセンス責任をUIとドキュメントに明記しました。

### Fixed

- OpenComposerをデプロイ時にPassengerアプリとして事前登録し、Open OnDemand統合画面がJavaScriptエラー時にも白画面にならないようにしました。

## [v1.0.0] - 2026-08-23

### Added

- `oci-hpc-trial-pack` として最初の正式リリースを作成しました。
- Semantic Versioningに基づく新しいバージョン系列を開始しました。

### Changed

- リポジトリ名を `oci-hpc-v2.10` から `oci-hpc-trial-pack` に変更しました。
- OCI Resource ManagerのデプロイURLを新しいリポジトリ名に更新しました。

### Compatibility

- このリリースは `oci-hpc v2.10.6.23` をベースとしています。
- 既存の `v2.10.x` タグは旧系列の履歴として保持します。
- Object Storage上のカスタムイメージ名と実行環境の `/opt/oci-hpc` パスは変更していません。

[v1.0.0]: https://github.com/kazuitox/oci-hpc-trial-pack/releases/tag/v1.0.0

[v1.3.0]: https://github.com/kazuitox/oci-hpc-trial-pack/releases/tag/v1.3.0
