# Changelog

このファイルには、OCI HPC Trial Pack の主な変更を記録します。

## [Unreleased]

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

- Autoscaling の Compute Cluster でも、Ansible 適用後の実OSホスト名へOCIインスタンス名とPrimary VNIC表示名を同期し、増減・DNS・監視情報・削除処理で同期後の名前を扱えるようにしました。
- Enterprise Linux の OS 側 HT 無効化処理が、`0-1` などの範囲形式の CPU リストにも対応するようにしました。各コアで 1 スレッドを残し、CPU の状態変更に失敗した場合はエラーを返します。
- 対応する AMD / Intel VM の Instance Pool で、`hyperthreading` を Instance Configuration に反映し、初期ノードと Autoscaling ノードの HT を起動時に制御するようにしました。起動時に HT が無効な Ubuntu VM では、OS 側の不要な SMT 書込みを省略します。
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
