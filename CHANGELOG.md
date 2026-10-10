# Changelog

このファイルには、OCI HPC Trial Pack の主な変更を記録します。

## [Unreleased]

### Security / Fixed

- Ubuntu 24.04 の `bc_desktop` で Xfce の起動スクリプト全体を専用 D-Bus セッション内で実行し、既存セッションとの競合を防ぎます。SAFE_PATH と他のデスクトップの `source` 方式を保持し、異常終了が末尾のログ出力で正常終了に変わらないよう終了コードを返します。Ubuntu の VNC 計算ノードには `dbus-daemon` を導入します。Oracle Linux 8 は変更しません。

- Oracle Linux 8 の新規構築で Node.js の有効ストリームがない場合に、確認コマンドの終了コード1で停止する不具合を修正しました。全ストリームを取得して22の有効状態を判定し、未有効・旧版有効・無効状態から22へ切り替えます。取得自体の失敗では停止します。

- Open OnDemand を 4.2.5、Dex を 2.45.1 に固定し、新規構築と既存環境の更新に対応しました。4.2 系リポジトリと Node.js 22 を使用し、ファイル転送の脆弱性 GHSA-frjp-qp79-hc3g の修正版を導入します。
- OIDC 暗号用秘密値をコントローラ上で生成・保存して再利用し、認証設定を root のみが読めるようにしました。ポータル生成の失敗を Apache の起動・再読み込みで無視しません。
- 標準 `ood_app.rb` の旧版コピーによる上書きを廃止し、日本語のホームディレクトリ表示を翻訳設定へ移しました。OpenComposer の再実行で管理対象の変更を保持し、Ubuntu の導入タスクと Slurm パスも対応しました。
- 更新後は NGINX アプリ設定と PUN を更新し、最後に完了記録を保存します。途中失敗後の再実行でも更新完了処理を省略しません。[OOD 専用 playbook と更新・復旧手順](docs/openondemand-4.2.5-upgrade.md)、秘密値の永続化・同時実行・認証設定の回帰テストを追加しました。実環境での更新と独自アプリの互換性確認は未実施です。

## [v1.3.3] - 2026-10-05

### Fixed

- Autoscaling の監視再同期などで使う `resize.sh` の実行ユーザー判定を実効 UID とユーザー名に基づく判定へ変更しました。`USER` が未設定・空・不一致・空白入りでも許可された `ubuntu` / `opc` は実行でき、root・許可外・ユーザー特定失敗時は非ゼロで停止します。`create_cluster.sh` の root 拒否も非ゼロ終了に修正しました。
- Resource Manager の初回構成で、SlurmDBD が DB 初期化を終える前に slurmctld を起動し、TRES 取得失敗で停止する競合を修正しました。cpu・mem TRES の取得後に起動し、サービスの active 状態に加えて scontrol ping の応答も待ちます。準備確認にはコマンド単位・全体の時間上限を設け、失敗時は理由を示して構成を停止します。
- 同じ slurm.conf が残る失敗後の再構成でもサービス起動と準備確認を行います。HA バックアップは backup としての応答を確認し、通常版・軽量版の reconfigure もローカルクラスターの応答確認後に実行します。子プロセスの SLURM_CLUSTERS を除外し、既存の設定パス・DB 接続先・キュー・topology・状態ファイルを保持します。実行順と失敗時停止の Ansible スタブテスト、適用・実機確認手順を追加しました。
- Autoscaling の topology 更新で `SLURM_CLUSTERS` に空文字を設定していた不具合を修正しました。Slurm が空のクラスター名を検索して作成・削除に失敗するため、子プロセスの環境からこの変数を除外します。`SLURM_CONF` などの設定は保持し、空値・別クラスター名の継承を検出する回帰テストを追加しました。
- Autoscaling の標準版・軽量版・ラック対応版の作成／削除と到達不能ノード回収で、Switch 名を完全一致で扱います。`e5` と `e5-lite` の混同を防ぎ、必要な Switch の欠落・重複・Slurm コマンド失敗時に空一覧で topology を上書きしないようにしました。
- topology 更新を共通の Ansible モジュールにまとめ、展開・圧縮を検証後、ロックを保持して原子的に置き換えます。既存のキーワード・名前・設定形式を維持します。既存環境では変更タスクと `playbooks/library/slurm_topology.py` を同時に配布してください。[調査・更新手順](docs/autoscaling-topology-identity.md)と共存・失敗時の回帰テストを追加しました。

### Compatibility / 検証

- v1.3.2 に `fix/resize-user-check`、`fix/slurm-startup-readiness`、`fix/autoscaling-exact-keyword` を統合したパッチです。Terraform・IAM・provider・既存のキュー設定や識別子は変更していません。
- 統合後のローカル全件テストは543件、失敗0件、スキップ3件でした。利用者から動作確認完了の報告を受けています。確認したOS・構成・操作の詳細は未記録のため、HA・ラック対応など全構成の検証完了を示すものではありません。配布手順と検証範囲は[リリースノート](docs/releases/v1.3.3.md)を参照してください。

## [v1.3.2] - 2026-09-20

### Fixed

- Resource Manager 用のルート構成で Terraform バージョンを `~> 1.5.0, < 1.6` と明示し、Oracle のバージョン系列判定用の指定形式に合わせました。ルート構成は 1.5.x が対象です。コントローラ上で実行する Autoscaling 用構成の `>= 1.5.0` と provider バージョンは変更していません。手動フォルダ／ZIP アップロード時の確認手順と回帰テストを追加しました。
- Autoscaling の新規ノード作成で、OS ホスト名の設定直後に OCI インスタンス・Primary VNIC 表示名と DNS・inventory を同期し、成功してから残りの構築と Slurm 起動へ進むようにしました。通常版・軽量版の両方で、ジョブ開始によるコストタグ更新と表示名更新が重なる機会を減らします。
- 新規構築の再開段階をインスタンス OCID・IP とともに記録し、同期失敗後に残りの構築を飛ばして成功扱いにしないようにしました。後半の構築や監視更新の失敗時は該当段階から再開し、対象ノードが置換されていれば停止します。従来版の名前同期ジャーナルの復旧動作は維持します。
- インスタンス表示名の更新で「現在変更中」を示す `409 Conflict` が返った場合のみ、同一リクエストを最大 8 回・追加試行の開始期限 60 秒で再試行します。その他のエラーは再試行対象を広げず、部分失敗の記録を保持します。

## [v1.3.1] - 2026-09-19

### Fixed

- 通常のAutoscalingでクラスターを削除する直前に、Slurm topology・ノード状態・全状態のジョブを確認し、DRAIN後にも再確認するようにしました。RUNNING・SUSPENDED・COMPLETINGのジョブや、確認中の状態変化がある場合は削除を見送ります。
- アイドル時間を`LastBusyTime`と`SlurmdStartTime`から判定し、時刻不明のノードに架空の過去時刻を設定する処理を廃止しました。割当のない障害ノードは、同じ削除前確認を経て回収します。
- 削除プロセスの受付を`currently_destroying`で確認します。起動失敗・受付前終了時はこの実行が設定したDRAINだけを解除するよう試み、受付結果が不明な場合はDRAINを維持します。
- 安全確認のSlurmコマンドにタイムアウトを設定し、出力フィルターとなる環境変数を除外します。非root実行時のノード状態更新には`sudo -n`を使用します。

### Compatibility / 運用

- v1.3.0に`fix/autoscale-safe-node-removal`だけを統合したパッチです。実装変更は通常のAutoscalingスクリプトで、Terraform・IAM・キュー変数・計算ノード構成は変更していません。
- 既存コントローラーではAutoscalingの実行状況を確認して修正版スクリプトを配布してください。DRAINが残る場合の確認・復旧と切り戻し手順は[リリースノート](docs/releases/v1.3.1.md)を参照してください。
- Compute Clusterのフラグ解釈の変更は含みません。引き続き`cluster_network: true`と`compute_cluster: true`を使用します。

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

[v1.3.1]: https://github.com/kazuitox/oci-hpc-trial-pack/releases/tag/v1.3.1

[v1.3.3]: https://github.com/kazuitox/oci-hpc-trial-pack/releases/tag/v1.3.3
