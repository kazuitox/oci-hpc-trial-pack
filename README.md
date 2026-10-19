# OCI HPC Trial Pack

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/kazuitox/oci-hpc-trial-pack/archive/refs/tags/v1.4.4.zip)

本リポジトリは、Oracle Cloud Infrastructure (OCI) 上に HPC 環境を短時間で構築し、PoC や初期検証をすばやく開始することを目的としています。
動作確認の中心は Oracle Linux 8 です。Ubuntu での動作確認報告も受けていますが、すべての OS・構成・更新経路の検証完了を示すものではありません。各リリースノートの確認範囲を参照し、利用する構成で個別に検証してください。

Terraform / Oracle Resource Manager スタックとして、コントローラ、計算ノード、Slurm、LDAP、共有ストレージ、Autoscaling、監視、Open OnDemand などをまとめて構成します。

`schema.yaml` は日本語 UI 向けに整備されており、`SIMPLE` モードでは最小限の入力、`ADVANCED` モードでは詳細な構成項目を表示します。

## Resource Manager へ手動アップロードする場合

Resource Manager 用のルート構成は Terraform **1.5.x** を対象にしています。`versions.tf` の `required_version = "~> 1.5.0, < 1.6"` は、1.5 系のパッチ版を許可し、1.6 以降を対象外にします。これは、Resource Manager がバージョン系列を判定できるよう `~> major.minor.0` で指定する [Oracle の Marketplace スタック・ガイドライン](https://docs.oracle.com/en-us/iaas/Content/Marketplace/app-publisher-guidelines-stacks.htm)に合わせた指定です。対応する実行版は [Resource Manager の対応表](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Reference/terraformversions.htm)で確認してください。

- フォルダをアップロードするときは、`versions.tf`、`schema.yaml`、`variables.tf` が直下にあるリポジトリのルートを選択します。`autoscaling/tf_init/` はコントローラ上で実行する別構成なので選択しません。
- アップロード後、スタック作成画面の Terraform バージョンが **1.5.x** になっていることを確認します。`Invalid Terraform version: .` が表示された場合は、修正済みの `versions.tf` を含むフォルダを再選択し、作業ディレクトリとバージョン欄を確認してください。画面側で値が空のままの場合は、選択状態・アップロード結果を別途確認する必要があります。
- 手動 ZIP を使う場合も、同じファイルを ZIP のルート（または明示した作業ディレクトリ）に置きます。`.git`、`.terraform`、Terraform state、秘密鍵、実環境ログを含めないでください。未コミットの修正を試す場合、`git archive HEAD` では変更内容や新規ファイルが入りません。
- 上のデプロイボタンは v1.4.4 を使います。手動アップロードには [v1.4.4 Release](https://github.com/kazuitox/oci-hpc-trial-pack/releases/tag/v1.4.4) に添付した `oci-hpc-trial-pack-v1.4.4.zip` を使用できます。リポジトリ全体をフォルダ選択すると `.git` や Python キャッシュも含まれる場合があるため、コミット済みの構成は `git archive --format=zip --output=/tmp/oci-hpc-trial-pack.zip HEAD` で ZIP にしてください。未公開のローカル修正の試験には、未コミットの変更も含めた構成を用意してください。

この上限制約はルート構成をローカル CLI で実行する場合にも適用されます。一方、Autoscaling 用の `autoscaling/tf_init/versions.tf` は Resource Manager でのバージョン選択には使わず、従来どおり Terraform **1.5.0 以上**を許可します。コントローラ側の Terraform を 1.5 系へ固定する変更ではありません。

## バージョニング

`oci-hpc-trial-pack` は `v1.0.0` から始まる独立したバージョン系列として管理します。最初のリリースは `oci-hpc v2.10.6.23` をベースとしており、既存の `v2.10.x` タグは旧系列の履歴として保持します。

- 後方互換性のある不具合修正: パッチバージョン（例: `v1.0.1`）
- 後方互換性のある機能追加: マイナーバージョン（例: `v1.1.0`）
- 破壊的変更: メジャーバージョン（例: `v2.0.0`）

変更履歴は [CHANGELOG.md](CHANGELOG.md) を参照してください。

## Autoscaling の識別子と topology 更新

`queues.conf` の `instance_keyword` は、`e5` と `e5-lite` のように一方が他方の先頭部分になる値も使えます。修正版は Switch 名を完全一致で照合し、必要な一覧の取得・展開に失敗した場合は `topology.conf` を更新しません。既存のキーワード・ノード名・クラスター名を維持します。 hostlist の展開・圧縮では `SLURM_CLUSTERS` を子プロセスの環境から除外し、コントローラーの `SLURM_CONF` は保持します。

既存コントローラーへの更新では、変更した Ansible タスクと `playbooks/library/slurm_topology.py` を一緒に配布してください。修正箇所、失敗時の動作、配布・切り戻し、フォーク元との違いは[調査・更新手順](docs/autoscaling-topology-identity.md)にまとめています。v1.3.3 の動作確認報告と検証範囲は[リリースノート](docs/releases/v1.3.3.md#検証結果と確認範囲)を参照してください。

## v1.4.4への更新

v1.4.4 は、Autoscaling の作成失敗通知、軽量版計算ノードの timezone 修正、OL8 Custom Image の既定値更新をまとめたリリースです。通知は既定で無効で、Slurm ジョブ通知とは独立した Topic と宛先を使用します。既存スタックでは構成 ZIP と保存済み入力値を更新し、Plan で IAM・通知リソースとイメージ・ノードの変更対象を確認してください。変更内容、配布ファイル、検証範囲は [v1.4.4 リリースノート](docs/releases/v1.4.4.md)を参照してください。

## v1.4.1への更新

v1.4.1 は、Open OnDemand のジョブ管理メニューと OpenComposer の履歴表示・更新を改善したパッチです。「03 ジョブ管理」へのメニュー集約、履歴の時間項目追加と Refresh、時刻表示の統一、Intel MPI の TCP オプション修正を含みます。既存環境には Open OnDemand role 全体を配布して再適用してください。変更内容と確認範囲は [v1.4.1 リリースノート](docs/releases/v1.4.1.md)を参照してください。

## v1.4.0への更新

v1.4.0 は、課金タグの有効・無効の選択機能を追加し、Open OnDemand を4.2.5へ更新したリリースです。Slurm 初回起動、Ubuntu Autoscaling の LDAP 再起動、Ubuntu VNC デスクトップ、内蔵 NVMe の LVM 構成も修正しています。既存環境では、課金タグ定義の削除保護、OOD の認証設定と更新手順、localdisk ロール全体の配布に注意してください。変更内容と確認範囲は [v1.4.0 リリースノート](docs/releases/v1.4.0.md)を参照してください。

## v1.3.3への更新

v1.3.3 は、Autoscaling の実行ユーザー判定、Slurm の起動待機、topology の Switch 名照合を修正したパッチです。既存コントローラには、新しい待機スクリプト・タスク・共通モジュールを含めて配布してください。変更内容、更新手順、検証範囲は[リリースノート](docs/releases/v1.3.3.md)にまとめています。

## v1.3.2への更新

v1.3.2は、新規Autoscalingノードの名前同期をSlurm起動前へ移し、OCI更新競合への限定的な再試行と構築途中からの再開を追加したパッチです。Resource ManagerのTerraformバージョン指定も修正しました。変更点と検証範囲は[リリースノート](docs/releases/v1.3.2.md)を参照してください。

## v1.3.1への更新

v1.3.1はAutoscalingの自動削除前にジョブとノード状態を確認し、DRAIN後の再確認を追加したパッチです。削除受付が不明な場合はDRAINを維持します。既存コントローラーへの配布、必要なSlurm操作権限、ログ確認と復旧・切り戻しは[リリースノート](docs/releases/v1.3.1.md)を参照してください。

## v1.3.0への更新

v1.3.0はホスト名同期、AMD VMのHT制御、Slurmユーザー別コストタグをまとめたリリースです。既存環境ではキュー設定、Terraform 1.5.0以上、タグ定義とIAM権限、名前・DNSの移行を確認してください。更新手順と制約は[リリースノート](docs/releases/v1.3.0.md)にまとめています。

## 主な構成

- コントローラノードを 1 台作成します。
- 計算ノードは Cluster Network、Compute Cluster、または Instance Pool で作成できます。
- Slurm をインストールし、ジョブ投入とキュー単位の Autoscaling を構成します。
- LDAP を有効にした場合、コントローラがクラスター内のユーザー管理を行います。
- `/home`、クラスター共有領域、scratch 領域を NFS または FSS / Block Volume / NVMe で構成できます。
- 追加 Login Node、Slurm バックアップコントローラ、Open OnDemand、Spack、OpenFOAM、ParaView、Enroot / Pyxis、PAM、Healthcheck、監視をオプションで有効化できます。
- RDMA NIC メトリックを Object Storage にアップロードするための PAR を作成できます。

## IAM とポリシー

スタックを実行するユーザーは、Administratorsグループに所属していることを想定しており、それによりデフォルトでオートスケーリングの利用に必要なポリシーと動的グループを自動で追加します。このオプションを有効にする場合は、テナンシのホームリージョンを参照し、IAM Policy / Dynamic Groupを作成するためのテナンシレベルの権限が必要です。
Administratorグループの権限がない場合には【Autoscaling 用 IAM Policy / Dynamic Group を作成】のチェックを外し、テナント管理者にて以下のポリシーと動的グループを適切に設定をしてください。オプションを無効にすると、Autoscaling用のIAM Policy / Dynamic Groupは作成しません。課金タグを使わない初回デプロイでは【利用者別の課金タグを有効化】（`cost_tags_enabled`）も外してください。課金タグが有効な場合は、スタック実行者にはテナンシとホームリージョンを参照する権限、およびデプロイ先コンパートメントの`tag-namespaces`を管理する権限が必要です。名前空間・キーの作成にはホームリージョンを使用します。

ポリシー1:
```text
allow service compute_management to use tag-namespace in tenancy
allow service compute_management to manage compute-management-family in tenancy
allow service compute_management to read app-catalog-listing in tenancy
```


動的グループ(名前: autoscaling_dg):
```text
Any {instance.compartment.id = '作成した CompartmentID を記入'}
```

動的グループを利用したポリシー2:

```text
allow dynamic-group autoscaling_dg to read app-catalog-listing in tenancy
allow dynamic-group autoscaling_dg to use tag-namespace in tenancy
allow dynamic-group autoscaling_dg to manage all-resources in tenancy
```


## OS とイメージ

デフォルトでは、コントローラと Login Node は Marketplace の `HPC_OL8` を使用します。計算ノードは、デフォルトで Object Storage URI から Oracle Linux 8.10 ベースのカスタムイメージを登録して使用する設定です。

OL8 のインポート用 Custom Image の既定値は `oci-hpc-v2.10-compute-image-v1.4` です。SIMPLE モードで事前構成済みイメージを使用する場合と、ADVANCED モードのインポート URL・表示名に適用します。SIMPLE モードでは、このイメージを controller にも使用します。Trial Pack 本体とは別のバージョン番号です。

既存の Resource Manager スタックでは構成 ZIP を更新してください。ADVANCED モードで保存済みの `compute_image_source_uri` と `compute_image_display_name` が v1.3 の場合は、v1.4 の値へ変更してください。URL は `variables.tf` の既定値を参照してください。既存スタックのイメージ変更は、登録済みイメージやノードの置換につながる可能性があるため、Plan で対象を確認してから Apply してください。構成ファイルの更新だけでは、稼働中のノードのイメージは更新されません。


```text
HPC_OL8
```


## 主要な入力項目

| 項目 | 説明 |
| --- | --- |
| `ui_mode` | `SIMPLE` または `ADVANCED`。詳細項目を出す場合は `ADVANCED`。 |
| `cluster_network` | RoCEv2 対応の Cluster Network を使用します。デフォルトは `true`。 |
| `compute_cluster` | Cluster Network の代わりに Compute Cluster を使用します。 |
| `node_count` | 初期クラスターの計算ノード数。 |
| `autoscaling` | Slurm ジョブに応じてクラスターを作成・削除します。デフォルトは `true`。 |
| `queue` | 初期キュー名。デフォルトは `compute`。 |
| `ldap` | LDAP を構成します。デフォルトは `true`。 |
| `home_nfs` / `home_fss` | `/home` の共有方法を選択します。 |
| `use_cluster_nfs` | クラスター共有領域をコントローラから NFS 共有します。 |
| `use_scratch_nfs` | scratch 領域を計算ノードから NFS 共有します。 |
| `private_deployment` | コントローラに Public IP を付与せず、Resource Manager Private Endpoint 経由で構成します。 |
| `login_node` | ユーザー用の追加 Login Node を作成します。 |
| `slurm_ha` | バックアップ Slurm Controller を作成します。 |
| `slurm_job_notifications_enabled` | OCI Notifications を使った Slurm ジョブメール通知を有効化します。デフォルトは `false`。 |
| `autoscaling_failure_notifications_enabled` | Autoscaling の計算ノード作成失敗を管理者へメール通知します。デフォルトは `false`。 |
| `autoscaling_failure_admin_email` | 作成失敗通知専用の EMAIL Subscription の宛先です。Slurm ジョブ通知とは別の Topic / Subscription を使用します。 |
| `slurm_notification_admin_email` | ローカル Controller ユーザー（通常は `opc`）のジョブ通知先メールアドレス。 |
| `use_ood` | Open OnDemand と OpenComposer をインストールします。 |
| `ood_dcv_enabled` | 検証用のAmazon DCVデスクトップと専用の`dcv`パーティションを有効化します。 |
| `ood_desktop_use_gpu` | VNCと、有効な場合はAmazon DCVのデスクトップノードにNVIDIA A10 GPUを使用します。 |
| `install_application` | 共有領域に追加アプリケーションをインストールします。Oracle Linux 8 と Ubuntu 24.04 で OpenFOAM v2312 / ParaView 5.11.2 を選択できます。Ubuntu 24.04 で OpenFOAM を選ぶと、controller 上の `/usr/mpi/gcc/openmpi-4.1.9a1` を使用してビルドします。 |
| `ood_source_cidr` | Open OnDemand の HTTPS/443 へのアクセスを許可する送信元 CIDR。 |
| `monitoring` | Grafana / Telegraf / InfluxDB によるシステム監視を有効化します。 |
| `autoscaling_monitoring` | Autoscaling の状態を Grafana ダッシュボードで確認できるようにします。 |
| `controller_object_storage_par` | RDMA NIC メトリックアップロード用の PAR を作成します。 |

旧バージョンで`ood_vnc_use_gpu=true`だった既存スタックは、Amazon DCV連携とA10 GPUデスクトップの両方を有効にした状態を維持します。個別設定に移行するには、UIに表示される旧設定をOffにしてください。

## Autoscaling

### 作成失敗の管理者通知

通知用の Dynamic Group と IAM Policy はテナンシのホームリージョンに作成し、Topic と EMAIL Subscription はデプロイ先リージョンに作成します。課金タグ・通常の IAM 自動作成・Slurm ジョブ通知をすべて無効にしても、作成失敗通知を有効にすればホームリージョンを取得します。

旧構成で Dynamic Group 作成時にホームリージョンを要求する `403-NotAllowed` が出た場合は、修正版の構成 ZIP を既存の Resource Manager スタックに反映し、Plan で作成・変更・置換対象を確認してから Apply を再実行してください。スタックのデプロイ先リージョンや OS を変更する必要はありません。

メール冒頭の `Failure phase` は、ログの Ansible 失敗・構築処理の失敗・Terraform エラー対象から「インスタンス作成」「セットアップ」を判定して表示します。根拠がない場合は「判定できませんでした」と表示します。AI による判定は行いません。

Ansible の `FAILED!` / `UNREACHABLE!` を検出したセットアップ失敗では、失敗タスク・ホスト・理由を通知し、OCI Work Request の取得・表示は行いません。インスタンス作成失敗や原因を判定できない失敗では、従来どおり OCI Work Request の診断を付けます。

Cluster Network の作成がタイムアウトした場合は、親の Work Request に加え、Cluster Network の詳細に含まれる所属 Instance Pool の失敗 Work Request も収集します。親が `PROVISIONING` のままでも、Pool 側に記録された `QuotaExceeded` などの原因を通知できます。Pool の所属関係は API の OCID で確認し、同じ表示名の別 Pool は診断対象にしません。Work Request 情報には対象リソース OCID と status も記載します。

OCI CLI は PATH を優先し、見つからない場合は実行ユーザーの `~/.local/bin/oci`・`~/bin/oci`、`/usr/local/bin/oci`・`/usr/bin/oci` の実行可能ファイルを探索します。これらにもない場合は、OCI 情報取得エラーを診断に残し、通知送信は失敗として再送対象を保持します。再送 cron もユーザーの CLI 配置先を PATH に含めます。既存環境には通知プログラムを配布し、cron 更新はスタックの Plan を確認してから Apply してください。

通知プログラムは Controller の Python 3.6 でも使える subprocess 引数で OCI CLI を呼び出します。旧版で `capture_output` の TypeError が出ていた環境は、`bin/autoscaling_failure_notify.py` を Controller の `/opt/oci-hpc/bin/` に更新してください。このエラーで停止した試行は未送信ファイルの保存前に止まるため、プログラム更新だけでは自動再送されません。保存した作成ログから `report` を再実行するか、更新後に失敗試験をやり直して確認します。

スタックの「計算ノードの作成失敗を管理者へ通知」を有効にし、管理者メールアドレスを入力すると、専用の OCI Notifications Topic / EMAIL Subscription と Controller 用の Dynamic Group / publish 権限を作成します。Slurm ジョブ通知の設定とは独立しており、管理者は OCI Notifications から届く Subscription 確認メールを承認する必要があります。IAM の自動作成を使わない環境でも、この通知用リソースをスタックに作成するため、スタック実行者にはホームリージョンでの Dynamic Group / Policy 作成と対象 Compartment での Topic / Subscription 作成権限が必要です。Controller から OCI API へ HTTPS で到達できることも確認してください。

作成が失敗すると、後始末の前に Controller が Terraform / Ansible ログと、該当リソースを特定できた場合の失敗 Work Request の error / log を収集します。メールにはクラスター、キュー、起点 Job ID、失敗箇所、作成ログのパスを記載します。Work Request を特定・取得できない場合は、その旨と Terraform / Ansible のエラーを記載します。`-DEBUG` で後始末を抑止した作成失敗も通知対象です。送信に失敗した通知は `/opt/oci-hpc/logs/autoscaling-failure-notifications/` に保持し、Controller の cron で毎分再試行します。送信失敗はクラスターの後始末を止めません。メール配信が成功したことは、作成失敗の繰り返しが止まったことを意味しません。

既存スタックで有効化する場合、Resource Manager の Plan で専用 IAM / Notifications リソースと Controller への通知プログラム配布を確認してから Apply します。この設定変更は既存計算ノードの再構成を要求しません。Controller 上の旧版スクリプトだけを個別に更新しても、Topic / 権限 / 設定ファイルは作成されません。通知を無効にすると Topic / Subscription と通知用 Policy / Dynamic Group を Terraform が削除し、Controller の再送 cron を停止します。未送信ファイルがある場合は無効化前に確認してください。

Autoscaling は「ジョブごとにクラスターを作成する」方式です。Slurm の pending ジョブを cron で確認し、ジョブのノード数、キュー、インスタンスタイプに合わせて新しいクラスターを作成します。アイドル状態のクラスターは、デフォルトで 600 秒経過後に削除対象になります。

Ubuntu の通常版・軽量版の動的ノード作成時、LDAP クライアントサービスの再起動は新規計算ノードで行い、既存コントローラでは行いません。初期構築時の設定は変更しません。

Autoscaling の計算ノードには、通常版・軽量版とも controller と同じスタック変数 `timezone`（既定値 `Asia/Tokyo`）を適用します。controller の OS 上で手動変更した timezone を自動取得する仕組みではありません。既存環境の軽量版では、controller 上の `/opt/oci-hpc/playbooks/lite_new_nodes.yml` と、実際に実行される `/opt/oci-hpc/playbooks/new_nodes.yml` に修正版を反映してください。以後に作成する計算ノードから適用され、稼働中ノードの timezone は自動では変わりません。

既存クラスターのノード数を途中で増減する運用は、非推奨で現状対象外です。現行の cron タスクも、既存クラスターのノード数を変更せず、クラスター単位の作成・削除を行うスクリプトを有効化します。

Autoscaling の設定ファイルはコントローラ上の次のパスに配置されます。

```text
/opt/oci-hpc/conf/queues.conf
```

サンプルはリポジトリ内の次のファイルです。

```text
conf/queues.conf.example
```

`queues.conf` では、キューごとに複数の `instance_types` を定義できます。重要な項目は次の通りです。

- `name`: Slurm の constraint として指定するインスタンスタイプ名。
- `instance_keyword`: 作成されるクラスター名と Slurm ノード名に使う短い識別子。
- `permanent`: `true` の場合、Autoscaling の削除対象にしません。
- `max_number_nodes`: キュー / インスタンスタイプ単位の最大ノード数。
- `max_cluster_size`: 1 クラスターあたりの最大ノード数。
- `max_cluster_count`: 同時に保持できる最大クラスター数。
- `cluster_network` / `compute_cluster`: 作成方式を指定します。
- `ad`: 複数 AD を空白区切りで指定すると、作成失敗時に別 AD を試行します。
- `hyperthreading`: 対応する **AMD VM** の Instance Pool では、`false` で HT Off、`true` で HT On を OCI 起動時に指定します。Intel VM の HT Off は対象外で、`false` を指定すると `slurm_config.sh` による設定反映時にエラーになります。直接作成する経路でも Terraform が作成前に拒否します。VM にはキューの値を使用し、BM 向けの `BIOS` / `SMT` 設定とは独立して制御します。
- `use_local_block_volume`: 各 Compute node 専用の一時 Block Volume をアタッチします。
- `local_block_volume_size`: ノードごとの Block Volume サイズ（50 GB 以上の整数）です。
- `local_block_volume_performance`: `0.  Lower performance`、`10. Balanced performance`、`20. High Performance` のいずれかを指定します。
- `local_block_volume_mount_point`: ノード内のマウントポイントを200文字以内の絶対パスで指定します。共有 NFS、NVMe、`/home` などの既存パスとは重複できません。

このノード専用 Block Volume は `use_scratch_nfs` で構成するクラスター内共有 NFS とは独立しています。XFS（Oracle Linux）または ext4（Ubuntu/Debian）で初期化し、ノード終了時に自動削除します。稼働中ノードの設定は後から付け替えず、`queues.conf` の変更後に新規作成されるクラスター／ノードから適用されます。初期 Permanent node にはスタック作成時の同名設定が適用されます。

VM の HT 設定は、対象 AD の Shape が返す対応情報を確認し、`AMD_VM` かつ要求値を許容する場合だけ適用します。Intel VM の `hyperthreading=true` は HT 用 platform_config を送信せず、Shape の既定設定を維持します。HT On で対応情報がない従来の VM も既定設定を維持し、Arm VM には SMT 設定を送信しません。

**OS 側の HT 制御はベアメタルに限定します。** AMD / Intel を含む VM では CPU をオフライン化せず、HT サービスも新規導入しません。既存 VM にこのリポジトリの HT サービスが残っている場合は、制御スクリプトに VM 判定を追加し、自動起動だけを無効化します。サービスの停止・CPU の再オンライン化は行いません。すでに OS 側で CPU がオフライン化された Intel VM は、この更新だけでは元に戻らないため、ジョブ終了後に `hyperthreading=true` の設定で再作成してください。BM の既存の BIOS / SMT 制御と OS 側の HT 制御は維持します。

2026-09-16 の実機試験では `VM.Standard.E6.Flex` の OCI 側 HT Off を確認しました。Intel VM は Configuration 経由の指定値が反映されない事象があり、OS 側の代替処理でも Slurm の CPU 番号変換に問題が生じたため、今回の HT Off 対応対象から外しました。Slurm のパッチや CPU 隔離設定の変更は導入しません。

OCI の起動時 HT 設定の変更は、新規作成する VM が対象です。Instance Configuration を変更しても、既存 VM の OCI 上の HT 設定には自動反映されません。初期 Permanent node はスタックの `hyperthreading` を使用します。既存環境では、コントローラの `/opt/oci-hpc/autoscaling/tf_init` と関連する playbook を更新してから、新しいクラスターを作成してください。仕様の根拠と実機での確認手順は[VM の HT 制御の調査資料](docs/vm-instance-pool-hyperthreading-feasibility.md)に記載しています。

設定を変更した後は、Slurm 設定を再生成します。

```bash
/opt/oci-hpc/bin/slurm_config.sh
```

実行時に `queues.conf` 全体を検証します。Intel VM など AMD / Arm と判定できない VM の `hyperthreading=false`、HT 値の未設定・不正値、`instance_keyword` の重複を検出すると、該当するキュー名・インスタンスタイプ名を表示して終了します。Slurm 設定は反映せず、`--initial` でもトポロジーファイルの削除や Slurm の再設定より前に止まります。AMD の Shape ごとの実際の SMT 対応状況は、引き続き Terraform が OCI の情報で確認します。

Slurm の状態を初期状態に戻したい場合は、次を実行します。

```bash
/opt/oci-hpc/bin/slurm_config.sh --initial
```

## ユーザー別のコストタグ

Resource Manager の「基本設定」にある【利用者別の課金タグを有効化】（`cost_tags_enabled`、既定値 `true`）で、初回デプロイ時に課金タグの使用を選べます。SIMPLE / ADVANCED の両モードで表示します。

`cost_tags_enabled=false` では課金タグの定義を作成せず、初期ノード・Autoscaling ノードへの `hpc-cost.User` の付与とジョブに応じた更新を行いません。ノード複製時も課金タグを追加・初期化せず、複製元のタグを保持します。クラスターの所属判定や削除制御、一時 Block Volume の管理タグは従来どおり使用します。

`cost_tags_enabled=true` かつ `slurm_user_tags_enabled=true`（どちらも既定値）にすると、SlurmのProlog／Epilogで計算ノードの利用者を記録し、コントローラーがOCIのDefinedタグ`hpc-cost.User`へ反映します。作成時とアイドル時の値は`Management`、ジョブ実行中はSlurmのユーザー名です。ジョブIDをOCIタグへ追加することはありません。

課金タグが有効な場合、Terraformがデプロイ先コンパートメント（`targetCompartment`）に名前空間`hpc-cost`とキー`User`を作成します。キーの値型はStatic value（任意の文字列）です。初期ノードとAutoscalingで作成するノードの両方に同じDefinedタグを使用します。`cost_tags_enabled=true` かつ `slurm_user_tags_enabled=false` では名前空間・キーとノード作成時のタグ付与は維持し、ジョブに応じた更新だけを停止します。

既存スタックは `cost_tags_enabled=true` のまま更新できます。タグ定義の state アドレスは `moved` ブロックで引き継ぎます。定義を誤って削除しないよう `prevent_destroy` で保護するため、既存の有効構成を `cost_tags_enabled=false` へ変更する plan や、タグ定義を含む destroy は停止します。更新だけを停止したい場合は `slurm_user_tags_enabled=false` を指定してください。スタック削除時の定義の扱いは[運用手順](docs/slurm-user-cost-tags.md#既存環境からの移行)を参照してください。

`queues.conf`の`tags`とスタックの`tags`変数の既定値も`Management`です。Cost Analysisでは名前空間`hpc-cost`、キー`User`を選んでユーザー別に集計します。対象は計算ノードのCompute費用で、ブートボリュームや共有ストレージの費用配賦は別途必要です。

タグはコントローラーで非同期に更新します。短いジョブはタグ反映前に終了することがあるため、ジョブの実行時間とOCIの費用が厳密に一致する仕組みではありません。ワーカーは既存のfreeformタグを変更しません。既存環境の更新、同名の名前空間がある場合の取り込み、IAM、動作確認と制限は[ユーザー別コストタグの運用手順](docs/slurm-user-cost-tags.md)を参照してください。

## ジョブ投入

Slurm ジョブは通常通り `sbatch` で投入できます。`queues.conf` の `instance_types[].name` を constraint に指定すると、そのインスタンスタイプに対応するクラスターが作成されます。

例:

```bash
#!/bin/sh
#SBATCH -n 72
#SBATCH --ntasks-per-node 36
#SBATCH --exclusive
#SBATCH --job-name sleep_job
#SBATCH --constraint hpc-default

cd /nfs/scratch
mkdir "$SLURM_JOB_ID"
cd "$SLURM_JOB_ID"

MACHINEFILE="hostfile"
scontrol show hostnames "$SLURM_JOB_NODELIST" > "$MACHINEFILE"
sed -i "s/$/:${SLURM_NTASKS_PER_NODE}/" "$MACHINEFILE"

cat "$MACHINEFILE"
sleep 1000
```

デフォルトでは、ジョブがインスタンスタイプを指定しない場合、キュー内で `default: true` のインスタンスタイプが使われます。デフォルト以外のキューへ投入する場合は、SBATCH ファイルに `#SBATCH --partition <queue_name>` を追加するか、コマンドラインで `sbatch -p <queue_name> job.sh` を指定します。

Ubuntu 22.04 かつ Hyperthreading を無効化した環境で `error: task_g_set_affinity: Invalid argument` が出る場合は、`--cpu-bind=none` または `--cpu-bind=sockets` を試してください。

Intel VM を HT Off の代替として OS 側で制御する構成は対象外です。判断の根拠となった Slurm の cgroup エラーは、[過去の検証記録](docs/vm-instance-pool-hyperthreading-feasibility.md#修正版スクリプトの-intel-vm-実機確認2026-09-17)を参照してください。

## ディレクトリとログ

コントローラ上では、クラスター管理用ファイルが次の場所に配置されます。

```text
/opt/oci-hpc
```

Autoscaling で作成されたクラスターごとの Terraform 作業ディレクトリ:

```text
/opt/oci-hpc/autoscaling/clusters/<cluster_name>
```

ログ:

```text
/opt/oci-hpc/logs
```

クラスターごとに `create_<cluster_name>_<date>.log` と `delete_<cluster_name>_<date>.log` が作成されます。cron のログは日付付きの `crontab_slurm_<yyyymmdd>.log` に出力されます。

## 手動クラスター操作

Autoscaling と同じ仕組みを使って、クラスターを手動で作成・削除できます。
コントローラ上の `create_cluster.sh` と `resize.sh` は `ubuntu` または `opc` の実行ユーザーで起動してください。
`resize.sh` は実際の実行ユーザーを確認します。`USER` 環境変数の設定は不要で、root や許可外ユーザーの場合はエラーで終了します。
監視情報の再同期だけを再試行する場合も、同じユーザーで `resize.sh --cluster_name <cluster_name> --reconcile-monitoring` を実行します。

作成:

```bash
/opt/oci-hpc/bin/create_cluster.sh <node_count> <cluster_name> <instance_type> <queue_name>
```

例:

```bash
/opt/oci-hpc/bin/create_cluster.sh 4 compute2-1-hpc HPC_instance compute2
```

クラスター名は次の形式にします。

```text
<queue_name>-<cluster_number>-<instance_keyword>
```

`instance_keyword` は `queues.conf` の値と一致させてください。

Autoscaling の Instance Pool、Cluster Network、Compute Cluster の新規作成では、最初に Ansible で
OS ホスト名を設定し、各ノードから取得した実際のホスト名へ OCI インスタンス名と Primary VNIC の表示名を
自動同期します。DNS・inventory の同期が成功してから、更新済み inventory を読み直して残りの構築と
Slurm の設定・起動へ進みます。通常版・軽量版の両方で、ジョブ開始に伴うコストタグ更新より先に
表示名更新を完了させる順序です。監視情報の整合は構築後に行います。VNIC の
`hostname_label` は VCN 内部 DNS 用の別属性であり、変更しません。Secondary VNIC と
VNIC attachment 固有の表示名も対象外です。

ここでいう VNIC 名は Networking サービス上の VNIC リソースの表示名です。VNIC attachment
は公開 API に更新操作がないため、attachment 固有の表示名は同期対象に含みません。

IAM Policy を手動管理している場合は、実行主体に Primary VNIC の更新権限
（`VNIC_UPDATE`。通常は `use vnics`）も付与してください。

新規作成の途中段階は、クラスターのディレクトリ内の `.initial-configure-stage` に記録します。
ディレクトリと作成途中のリソースを保持して構築処理を再実行した場合、名前同期の失敗後は同期を再開してから
残りの構築へ進み、後半の構築失敗後はホスト名設定・名前同期へ戻らず後半を再開します。監視更新だけの失敗は
監視更新のみを再試行します。再開記録はクラスター名・インスタンス OCID・IP と照合し、別 AD での作り直しなどで
対象が変わっていれば処理を停止します。記録を手動削除せず、対象リソースと作成ログを確認してください。
従来版の名前同期ジャーナルだけが残っている場合は、従来どおり名前同期・監視更新のみで復旧します。
通常の `create_cluster.sh` が作成失敗時に行う後始末は変更していません。

OCI インスタンスの表示名更新で `409 Conflict` と「instance ... is currently being modified, try again later」が
返された場合に限り、初回を含め最大 8 回、追加試行の開始期限を 60 秒として再試行します。
実行中の SDK 呼び出しを 60 秒で強制中断するものではありません。他のエラーは通常どおり失敗として扱い、
未完了の名前同期記録を保持します。カスタムイメージ内で `slurmd` が起動時から有効な場合は、
構築前にジョブを受け付けないイメージ設定であることを実環境で確認してください。

この変更を controller へ適用する際は、`bin/configure_as.sh`、`bin/resize.py`、
`bin/initial_configure_state.py` と `playbooks/new_nodes_hostname.yml`・`new_nodes.yml`・`lite_new_nodes.yml` を
同じリビジョンで揃えてください。コードの更新だけで作成済みノードを再構成する必要はありません。

以下は既存の管理 CLI による移行手順です。新規作成時の同期前倒しのために実行する必要はありません。

```bash
/opt/oci-hpc/bin/resize.sh --cluster_name <cluster_name> reconfigure
```

同期途中で失敗した場合、次回の `add`、`remove`、`remove_unreachable`、`reconfigure`
は未完了の再構成と名前同期だけを再開して終了します。同じ台数変更を続けて行う場合は、完了を
確認してからコマンドをもう一度実行してください。既存の Instance Pool、Cluster Network、
Compute Cluster の
旧 OCI 名 DNS レコードは、この再構成時に Terraform から安全に引き継がれます。
既存 Cluster Network と Compute Cluster の初回移行は Terraform の state lock 外で行う必要があるため、
`terraform apply` を直接再実行せず、上記の `resize.sh ... reconfigure` を使用してください。

Compute Cluster の `add` で `--no_reconfigure` を指定した場合、追加ノードは一時 OCI 名のままになり、
名前同期と canonical DNS 作成も保留されます。後から上記の `reconfigure` を実行してください。また、
通常の `remove` / `remove_unreachable` で安全に削除できるのは、`resize.sh add` が追加した Terraform
state 外のノードだけです。Terraform が管理する初期ノードを減らす場合は、クラスター単位で削除して
必要な台数で再作成してください。

削除:

```bash
/opt/oci-hpc/bin/delete_cluster.sh <cluster_name>
```

削除中に問題が起きた場合は、強制削除を指定できます。

```bash
/opt/oci-hpc/bin/delete_cluster.sh <cluster_name> FORCE
```

削除処理中のクラスターには、次のファイルが作成されます。

```text
/opt/oci-hpc/autoscaling/clusters/<cluster_name>/currently_destroying
```

## Autoscaling モニタリング

`autoscaling_monitoring` を有効にすると、Grafana でクラスターの作成・削除状況と Slurm ジョブ状況を確認できます。Grafana API の制約により、ダッシュボードのインポートは手動で行います。

1. ブラウザで `http://<controller_ip>:3000` にアクセスします。
2. 初期ユーザー名 / パスワードは `admin/admin` です。
3. `Configuration -> Data Sources` で `autoscaling` を選択します。
4. Password に `Monitor1234!` を入力し、`Save & test` を実行します。
5. 左メニューの `+` から `Import` を選択し、次の JSON をアップロードします。

```text
/opt/oci-hpc/playbooks/roles/autoscaling_mon/files/dashboard.json
```

Data Source には `autoscaling (MySQL)` を選択します。

## LDAP とユーザー管理

`ldap` を有効にした場合、コントローラはクラスター用 LDAP サーバーとして動作します。ホームディレクトリは共有構成のまま使うことを推奨します。

ユーザー管理はコントローラ上の `cluster` コマンドで行います。

```bash
cluster user add <name>
```

デフォルトでは `privilege` グループが作成されます。このグループは NFS へのアクセス権を持ち、設定により全ノードで sudo 権限を持ちます。デフォルト GID は `9876` です。

```bash
cluster user add <name> --gid 9876
cluster user add <name> --nossh --gid 9876
```

`--nossh` を指定すると、ノード間パスワードレス SSH 用のユーザー固有鍵を作成しません。

Slurm ジョブメール通知が有効な場合、`cluster user add` はメールアドレスも入力します。コマンド自体を `sudo` で実行する必要はありません。非対話実行では `--email`（`-e`）を指定できます。

```bash
cluster user add <name> --email user@example.com
cluster user delete <name>
```

追加時には LDAP の `mail` 属性、ユーザー専用の OCI Notifications Topic / EMAIL Subscription、通知レジストリをまとめて作成します。削除時には通知レジストリからユーザーを外した後、LDAP ユーザーと Subscription / Topic を削除します。OCI Notifications から届く確認メールは、受信者が承認する必要があります。OCI リソースの削除が一時的に失敗した場合は、復旧後に `cluster notification cleanup` を実行すると記録済みの削除処理を再試行できます。

ユーザー専用 Topic の名前は `slurm-<cluster>-<user>-<12桁の識別子>` です。末尾の識別子はデプロイ固有の通知スコープとユーザー名から決定的に生成され、同名のクラスタやユーザーが別スタックに存在する場合、および名前の正規化・切り詰め後に同じ文字列になる場合の衝突を防ぎます。同じ通知スコープとユーザー名の組み合わせでは常に同じ値になります。

## Slurm の初期起動と再構成の待機

Slurm 23.02.5 では、SlurmDBD のサービスが起動していても、DB の初期化が完了する前に slurmctld を起動すると、TRES を取得できず停止する場合があります。SlurmDBD の `active` 状態や 6819 番ポートだけでは準備完了を判定しません。

初回構成で slurm.conf を生成・変更した場合は、Munge の鍵を反映してから、次の順に処理します。

1. slurmctld を停止し、SlurmDBD を再起動する。
2. root 権限で `sacctmgr -nP show tres format=Type,Name,ID` を実行し、終了コード 0、3 列の応答、有効な正整数 ID、名前が空の `cpu`・`mem` TRES を確認する。
3. `systemctl reset-failed slurmctld` で前回の起動回数制限を解除し、slurmctld を再起動する。初回起動などで終了コード 1 と `Unit slurmctld.service not loaded.` の所定のメッセージだけが返った場合は、起動へ進む。権限エラーやタイムアウトなど、それ以外の失敗では停止する。
4. `scontrol ping` の終了コード 0 と `Slurmctld(primary) ... is UP` を確認する。
5. reconfigure ハンドラでも primary の応答を確認してから、`scontrol reconfigure` を実行する。

`reset-failed` の未読み込みメッセージを許容しても、続くサービス起動と ping 確認は省略しません。unit ファイルの欠落や起動失敗は、その段階で停止します。この処理は課金タグの有効・無効に依存しません。

前回の構成が失敗し、同じ slurm.conf が残っている場合も、SlurmDBD の起動・TRES 確認・slurmctld の起動・ping 確認を行います。この経路のサービス操作は `started` で、稼働中のサービスを再起動しません。HA のバックアップは、primary の SlurmDBD から TRES を取得してから起動し、`Slurmctld(backup) ... is UP` を確認します。バックアップの primary への昇格は要求しません。

確認コマンドは 1 回最大 15 秒、TRES と ping の各待機は最大 180 秒です。失敗した試行の後に最大 5 秒待って再確認し、最後の試行は残り時間で打ち切ります。reconfigure 自体の再試行は最大 45 秒です。通常の初回構成では、起動時の TRES・ping と reconfigure 前の ping を合わせ、準備確認は最大 540 秒、reconfigure の試行を含めて最大 585 秒です。これはサービス操作、SSH、他の構成タスクの時間を含みません。reset-failed は 15 秒でタイムアウトし、終了しなければさらに 2 秒後に強制終了します。

待機は role の `slurm_readiness_timeout`（180）、`slurm_readiness_command_timeout`（15）、`slurm_readiness_interval`（5）で指定します。各値の単位は秒です。固定 sleep のみの待機や、失敗を無視した続行は行いません。期限切れでは対象コマンド、試行回数、最後の終了コード・標準出力・標準エラーを Ansible に返し、後続処理を停止します。

確認と reconfigure の子プロセスでは `SLURM_CLUSTERS` を除外します。未設定・空文字・別クラスター名のいずれでもローカル設定を参照し、親環境は変更しません。role が指定する `SLURM_CONF` とその他の環境変数を保持します。Ubuntu は `/usr/local/bin`、Oracle Linux 8 は `/usr/bin` の Slurm コマンドを使います。動的ノードからの reconfigure は、委譲先コントローラの OS を基準に選択します。通常版・軽量版はこのハンドラを共有し、軽量版の計算ノードでは DBD や controller の再起動を追加しません。Slurm 無効時は role が呼び出されず、待機も実行しません。

既存環境へ適用する場合は、保守時間を確保し、この変更を含む `playbooks/roles/slurm/` 全体をコントローラの `/opt/oci-hpc/playbooks/roles/slurm/` に配布してから、通常の `/opt/oci-hpc/bin/configure.sh` を再実行します。新しい `files/slurm_readiness.py` と `tasks/` のファイルも必要です。Ansible が確認スクリプトを対象ホストへ一時転送するため、バックアップへ手動インストールする必要はありません。Resource Manager の構成ファイルを置き換えただけで、既存コントローラへの配布・再構成が実行されたと判断しないでください。DB 接続先、実行時の設定パス、キュー、topology.conf、Slurm の状態ファイルは変更・削除・初期化しません。復旧に `cluster configure --initial` を使う必要はありません。

実機確認は、Ubuntu 24.04 と Oracle Linux 8 のそれぞれで、新規 Resource Manager デプロイと既存環境の再構成を実施します。Slurm 有効の単一コントローラと HA、Slurm 無効の構成を対象に、次を確認してください。これらは利用者が検証環境で行う手順であり、ローカルテストの成功を実機検証済みの根拠にはしません。

- 新規デプロイ：初期構成ログで TRES 確認 → slurmctld 起動 → ping 成功 → reconfigure の順を確認する。Resource Manager の remote-exec が成功し、Ansible の `failed=0`・`unreachable=0`、configure の終了コード 0 を期待する。
- 再構成：role を配布し、通常の configure.sh を実行する。設定が変わった経路と、同じ設定で再実行した経路の双方で成功することを確認する。topology.conf と状態ファイルが残り、既存キューが維持されることも確認する。
- 応答と継続稼働：`sudo env -u SLURM_CLUSTERS timeout 15s sacctmgr -nP show tres format=Type,Name,ID` と `sudo env -u SLURM_CLUSTERS timeout 15s scontrol ping` を実行し、cpu・mem、primary の UP、6817 番ポートの待ち受け、数分間の継続稼働を確認する。Ubuntu で PATH に Slurm がない場合は `/usr/local/bin/`、Oracle Linux 8 では `/usr/bin/` をコマンド名に付ける。
- HA：バックアップで primary の DBD への TRES 問い合わせと backup の UP を確認する。バックアップが待機状態のまま構成に成功することを期待する。
- 環境変数：configure を実行する親環境の `SLURM_CLUSTERS` が未設定・空文字・別クラスター名の各ケースでも、確認対象がローカルクラスターであることを確認する。親の変数値が保持されることも確認する。
- 障害時：隔離した検証環境で DBD の準備遅延・接続失敗、controller の応答遅延・応答不能を再現する。遅延後の成功では再試行し、失敗が続く場合は期限内に構成が失敗することを期待する。DBD の失敗時は slurmctld の起動に、ping の失敗時は reconfigure に進まないことをログで確認する。
- 通常版・軽量版：新規構成成功後、各方式で動的ノードを構成し、共有ハンドラで ping → reconfigure が成功することを確認する。Slurm 無効時には確認コマンドもサービス操作も発生しないことを確認する。

この修正のローカル回帰テストは [test_slurm_startup_readiness.py](tests/test_slurm_startup_readiness.py)、保護する振る舞いと実機確認範囲は [回帰確認台帳の REG-021](docs/REGRESSION_LEDGER.md) を参照してください。

## Slurm ジョブメール通知

スタック作成時に「Slurm ジョブメール通知を有効化」を選び、管理者メールアドレスを入力すると、次のリソースと設定を自動構成します。

- primary Controller と、HA 構成では backup Controller だけを対象にした Dynamic Group
- 対象 Compartment の Notifications Topic を操作するための IAM Policy
- ローカル Controller ユーザー（通常は `opc`）用の Topic と EMAIL Subscription
- Slurm `MailProg`、Controller 上の OCI CLI 配信ワーカー、再試行用 systemd unit

初期 Subscription についても、管理者メールアドレスに届く OCI Notifications の確認メールを承認してください。ユーザーと Topic の対応は Controller の次のファイルで管理します。

```text
/opt/oci-hpc/conf/slurm_notification_users.json
```

ジョブスクリプトでは通常の Slurm オプションを指定します。宛先は実行 OS ユーザーから通知レジストリを参照して決めるため、`--mail-user` は不要です。

```bash
#SBATCH --mail-type=BEGIN,END,FAIL
```

Slurm の通知処理は OCI CLI を直接待たず、Controller 上のスプールへイベントを保存してから instance principal で OCI Notifications へ配信します。一時的な失敗は systemd timer が再試行し、通知処理の失敗によってジョブの開始・完了処理を失敗させません。OCI の EMAIL 配信制限を超えないよう最大10件/分に抑制し、配信不能ファイルは7日後に自動削除します。

通知メールの本文は、Cluster、Job ID、Job name、User、Event、State、Partition、Nodes、Queued time、Run time、Exit code、Termination signal、Working directory、Standard output、Standard errorを半角罫線の2列表で表示します。取得できない値は `-`、長い値は表の幅に合わせた継続行で表示します。

HA 構成では、LDAP ユーザーの追加・削除時に通知レジストリを backup Controller へ同期します。同期に失敗した場合は不整合を避けるため処理を安全に中断し、復旧後に primary Controller で次のコマンドを実行して再同期できます。

```bash
cluster notification sync
```

通知レジストリは両 Controller へ同期しますが、未配信イベントのスプールは各 Controller のローカル領域です。障害直前に active Controller に残った未配信イベントは、その Controller が復旧してワーカーが再開するまで配信されません。

LDAP ユーザー用の Topic / Subscription は `cluster user add` が作成するため Terraform state には含まれません。通知機能の無効化または Resource Manager でのスタック削除時には、Controller と通知用 IAM Policy を削除する前に destroy cleanup を実行し、現在のデプロイが `cluster-cli` で作成した Topic を自動削除します。cleanup が完了しない場合はリソースの取り残しを防ぐため Destroy を失敗させるので、原因を解消して再実行してください。この仕組みを含まないバージョンから更新する既存スタックでは、Resource Manager の Terraform バージョンを 1.5.x に更新し、Destroy 前に一度 Apply して cleanup を Terraform state に登録する必要があります。Apply 前に Controller や IAM リソースを削除した場合は、残った Topic を OCI Console または OCI CLI で手動削除してください。

既存スタックで通知機能を `true` から `false` に変更すると、ユーザー用通知リソースを削除する終了処理として扱います。同じスタックでの再有効化は対象外のため、一時停止目的では無効化せず、再び有効化する場合はスタックを新規作成してください。通知を有効にしたまま Slurm だけを無効化することはできません。Slurm も終了する場合は、通知機能も同時に `false` にしてください。

通知機能を有効にした既存スタックの `region` または `targetCompartment` を Apply で変更する移設操作は対象外です。移設する場合は、変更前の設定のままスタックを Destroy して通知リソースの cleanup 完了を確認してから、新しい配置先へスタックを作成してください。

Destroy 開始後は `cluster user add` など、通知リソースを新しく作る操作を行わないでください。

この機能を有効化するスタック実行者には、テナンシのホームリージョンで Dynamic Group と IAM Policy を作成できる権限、および対象 Compartment で Notifications Topic / Subscription を作成できる権限が必要です。新規 VCN の Service Gateway 経路、または既存ネットワークから OCI API への HTTPS 到達性も必要です。Instance Principal の権限は Controller インスタンス全体に付与されるため、Controller へのシェルアクセスは信頼できる利用者に限定し、通知リソースを配置する Compartment の分離も検討してください。

## 共有ホームディレクトリ

デフォルトでは、コントローラが `/home` を NFS で全ノードに共有します。FSS を使う場合は、既存 FSS の IP / パスを指定するか、スタックで FSS を作成できます。

既存 FSS を使う場合、マウントポイントに `/home` を直接指定しないでください。スタックは `$nfs_source_path/home` を作成し、必要なファイルをコピーしたうえで `/home` にマウントします。

## ローカル NVMe のスクラッチ

内蔵 NVMe は `localdisk=true` で構成します。2台を LVM RAID1 にまとめる場合は `log_vol=true`、`redundancy=true` を指定します。`log_vol` の既定値は `false` なので、RAID1 を利用する場合は明示的に変更してください。初期ノード・Autoscaling ノードのマウント先は `/mnt/localdisk`、VG は `vg_nvmes`、LV は `lv_nvmes`、ファイルシステムは XFS です。`use_local_block_volume` は別機能であり、内蔵 NVMe の利用には不要です。

localdisk ロールは Ansible の LVM・filesystem・mount モジュールで構成します。既存 LV のサイズ・RAID 形式は変更せず、XFS が未作成なら作成してマウントします。設定変更による既存の個別ディスク構成から LVM への自動変換は行いません。Autoscaling の計算ノードでは失敗を記録してノード構築を続けるため、構築完了後に `findmnt /mnt/localdisk` と `sudo lvs -a -o lv_name,vg_name,segtype,devices vg_nvmes` を確認してください。

既存環境に適用する際は、`tasks/`・`defaults/`・`filter_plugins/` を含む [localdisk ロール](playbooks/roles/localdisk/README.md) 全体をコントローラへ配布してください。新規ノードには新しいロールが使われます。既存ノードへの反映には対象を限定したロールの再実行が必要です。`community.general` と `ansible.posix` が必要ですが、通常のコントローラ構築処理で導入しています。

## 追加ストレージ

`use_cluster_nfs` を有効にすると、コントローラから `cluster_nfs_path` を NFS 共有します。デフォルトは `/nfs/cluster` です。

`use_scratch_nfs` を有効にすると、計算ノード側の NVMe または Block Volume を使って scratch 領域を NFS 共有します。デフォルトの scratch マウントポイントは `/nfs/scratch` です。

追加 NFS / FSS を `nfs_target_path` にマウントすることもできます。ただし、追加 NFS の設定から `/home` を直接構成しないでください。`/home` にはストレージ詳細オプションの専用設定を使います。

## プライベートサブネットへのデプロイ

`private_deployment` を `true` にすると、コントローラに Public IP を付与せず、Resource Manager Private Endpoint 経由で構成します。

- 新規 VCN を作成する場合、コントローラ用と計算ノード用のプライベートサブネットを作成します。
- 既存 VCN を使う場合は、コントローラ用 subnet と計算ノード用 private subnet を指定します。
- コントローラへ SSH 接続するには、Controller Service、VPN、FastConnect、踏み台ホスト、または到達可能な VCN Peering が必要です。

## Open OnDemand

`use_ood` を有効にすると、Open OnDemand **4.2.5** と Dex **2.45.1**、および [OpenComposer](https://github.com/RIKEN-RCCS/OpenComposer) をインストールします。ユーザーはブラウザからファイル操作、ジョブ投入、アプリケーション実行を行えます。スタックは Open OnDemand 用の初期パスワードも生成し、構成に反映します。

Filesの「Download（ZIPなし）」では、複数ファイルやフォルダを、手元の新しい保存用フォルダへ階層を保って取得します。ポップアップ内で、回転するアイコンによる容量計算の表示、合計サイズと名前の確認、保存先の選択、転送進捗の確認を順に行います。単一フォルダの初期名は元の名前です。進捗はファイル番号／総数と平均転送レートを表示し、計算中から転送中までキャンセルできます。ポップアップを閉じた後に結果メッセージは残しません。容量確認は選択した最上位フォルダごとに1回とし、上限の既定値は300秒です。この時間設定は従来のZIPダウンロードの容量確認にも適用します。HTTPS接続とChrome／Edgeなどの対応ブラウザが必要です。従来のDownloadも残しています。利用方法・対象外の項目・既存環境への適用・未実施の受入確認は[ZIPなし保存の手順](docs/ood-files-download.md)を参照してください。

本体と Dex はバージョンを固定し、既存環境の再構成でも指定版へ更新します。認証用の `oidc_crypto_passphrase` はコントローラ上で生成・保存して再利用します。既存 4.0 系からの更新では Node.js 22 への切り替えと Web セッションの再起動を伴います。[4.2.5 への更新・復旧手順](docs/openondemand-4.2.5-upgrade.md)を確認し、検証環境で独自アプリの動作を確認してから適用してください。

Ubuntu 24.04 の VNC デスクトップでは、Xfce の初期設定と起動をジョブ専用の D-Bus セッション内で実行します。他のデスクトップは従来の `source` 方式を使用し、いずれもデスクトップの終了コードを起動スクリプトから返します。必要な `dbus-daemon` は Ubuntu の VNC 計算ノードの構築時に導入します。Oracle Linux 8 の起動テンプレートとパッケージ構成は変更しません。

既存の Ubuntu 環境へ適用する場合は、コントローラへ更新した `playbooks/` を配布して Open OnDemand role を再実行し、VNC 計算ノードで `command -v dbus-run-session` が成功することを確認してください。不足する既存ノードでは `sudo apt-get install dbus-daemon` が必要です。変更は新しく投入するデスクトップジョブに反映されます。実行中・投入済みのジョブの出力ディレクトリにあるスクリプトは更新されません。[Ubuntu VNC の確認手順](docs/openondemand-4.2.5-upgrade.md#ubuntu-2404-の-vnc-デスクトップ)も参照してください。

ブラウザシェルの無操作タイムアウトは30分です。接続の最大継続時間はOpen OnDemandのデフォルトである1時間のままです。

`use_ood`を有効にするとVNC用の`vnc`パーティション（Constraint: `dskv`、instance keyword: `desktop-v`）を作成します。Oracle Linux 8で`ood_dcv_enabled`も有効にすると、DCV専用の`dcv`パーティション（Constraint: `dskd`、instance keyword: `desktop-d`）と「Linux Desktop with Amazon DCV（検証用）」を追加します。VNCジョブは`vnc`、DCVジョブは`dcv`へ投入され、ノード構築時のAnsibleもキュー名に応じてTurboVNCまたはAmazon DCVだけを構成します。

`ood_desktop_use_gpu`の初期値は`false`で、VNCとDCVは`VM.Standard.E6.Flex`のCPUデスクトップノードを使用します。CPUノードではAnsibleが`Server with GUI`パッケージグループをインストールし、DCV仮想セッションはソフトウェア描画を使用します。`ood_desktop_use_gpu`を有効にすると、VNCとDCVの両方が`VM.GPU.A10.1`とGUI導入済みのGPUデスクトップ用Custom Imageを使用します。GPUノードではGUIの再インストールをスキップし、GDMのWayland無効化、NVIDIA Xorg `:0`、DCV-GL、VirtualGLを構成します。

Amazon DCV側では、利用時間、初期解像度、同時接続数、ホームディレクトリとのファイル転送をフォームで指定できます。GPU構成の場合のみDCV-GLの有効・無効も指定でき、ジョブはA10 GPU 1基とCPU 8基を要求します。CPU構成ではGPUを要求せずCPU 2基を要求します。割り当てノード上にユーザー専用のDCV仮想セッションを作成し、ジョブ終了時にセッションと48文字のワンタイム認証トークンを削除します。

新しい`dcv`ノードでは、Slurmへの登録前にAmazon DCV 2025.0-20103を公式配布元からダウンロードし、利用するRPMのSHA-256とRPM署名を検証してインストールします。GPU構成では`glxinfo`と`dcvgldiag`でNVIDIA OpenGLを確認し、DCVセッション作成時にも仮想セッションのrendererがNVIDIAであることを検証します。デスクトップノードから`d1uj6qtbmh3dt5.cloudfront.net`へのHTTPS通信を許可してください。

ブラウザー接続はOODの`/rnode/<host>/<port>/`を経由します。Webクライアントの経路はHTTPS/WSS（TCP）であり、QUIC/UDPは使用しません。本機能は検証用です。本構成ではAmazon DCV Serverに別途ライセンスを設定しないため、インストール時に自動的に適用される自動評価ライセンスを使用します。自動評価ライセンスはインストール後30日間有効で、有効期限後はAmazon DCVセッションを新規作成またはホストできません。継続利用または本番利用には、利用者が適切なライセンスを用意し、[Amazon DCVのライセンス条件](https://docs.aws.amazon.com/dcv/latest/adminguide/setting-up-license.html)およびEULAを確認・遵守する必要があります。

OpenComposer は `v2.0.2`（commit `7af3d94b36043d8019b1639cd8e463957eb7a37e`）に固定し、スタックの Slurm を直接利用するよう構成します。Open OnDemand の「03 ジョブ管理」メニューには「Slurmジョブ」と「History」を追加します。どちらも Open OnDemand のカスタムページ内に OpenComposer を表示するため、ジョブ投入画面や履歴画面へ移動しても Open OnDemand のヘッダーが維持されます。OpenComposer には、`queues.conf` のパーティションとノードグループ（Slurm Constraint）を選択できる汎用 Slurm ジョブフォームも追加します。Constraint の候補には各パーティションの `instance_types[].name`、つまり生成される `slurm.conf` の `NodeName` における `Features` の最後の値を使用します。パーティションを変更すると、そのパーティションで利用できる Constraint だけが選択肢として有効になります。実行プロファイルは `なし`、`MPI`、`OpenMP` から選択します。`MPI` では `BM HPC (RDMA)`、`VM (TCP)`、`BM Standard (TCP)` の実行環境と、`Intel MPI (OneAPI)`、`OpenMPI v4.x`、`Platform MPI v9.x` のMPI実装を選択できます。`MPI` を選択した場合だけノードあたりのタスク数（`--ntasks-per-node`）、`OpenMP` を選択した場合だけ1タスクに割り当てるCPU数（`--cpus-per-task`）を指定できます。`VM (TCP)` の場合は `#SBATCH --ntasks-per-core=1` と `#SBATCH --exclusive`、`BM Standard (TCP)` の場合は `#SBATCH --exclusive` のみを追加し、選択した実行環境とMPI実装に対応するmpirunオプションを `export MPI_OPTIONS="..."` としてジョブスクリプトへ挿入します。`VM (TCP)` または `BM Standard (TCP)` で `Intel MPI (OneAPI)` を選択すると、既存の `MPI_OPTIONS` に `-genv I_MPI_COLL_EXTERNAL 0` を追加します。既存環境へ反映するには Open OnDemand role の再適用が必要です。`OpenMP` の場合は `OMP_NUM_THREADS`、`OMP_PROC_BIND`、`OMP_DISPLAY_AFFINITY` を設定します。実行時間の指定は有効・無効を選択でき、無効の場合は時間入力欄と `#SBATCH --time=` をジョブスクリプトから除外します。Slurm ジョブメール通知を有効化した場合は、メール通知の有無と `BEGIN` / `END` / `FAIL` をチェックボックスで選択でき、選択時だけ `#SBATCH --mail-type=` を生成します。フォームで生成したジョブスクリプトは投入前に編集できます。Ruby の依存 gem には Open OnDemand 4.2.5 が同梱する gem セットを利用します。

ジョブ投入・確認のメニューは OpenComposer に統一します。OOD 標準の「ジョブ投入」（Job Composer）と「Active Jobs」は、トップ画面のアイコン、ヘッダーメニュー、「All Apps」の一覧に表示しません。ファイル操作、ブラウザシェル、System Status、VNC／DCV、対話型セッションの表示は維持します。標準アプリ本体と既存データは保持するため、直接 URL へのアクセスを禁止する変更ではありません。

Project Manager と Module Browser も、ヘッダーメニューと「All Apps」の一覧に表示しません。対象アプリの `manifest.yml` のカテゴリを空にして表示対象から外します。非表示にした結果、項目がなくなった親メニューは表示されません。同じ親メニューに他の項目がある場合は、その項目を残します。アプリ本体と既存データは保持します。

既存環境へ反映するには、更新した `playbooks/roles/openondemand/` をコントローラへ配布し、[OOD 専用 playbook の手順](docs/openondemand-4.2.5-upgrade.md#更新した生成元で-ood-専用-playbook-を実行する)に従って再実行してください。設定と manifest の配置後にダッシュボードの再起動を要求します。適用後はブラウザを再読み込みし、標準のジョブ投入・Active Jobs・Project Manager・Module Browser が表示されず、項目がなくなった親メニューも表示されないことと、「03 ジョブ管理」の「Slurmジョブ」「History」を開けることを確認してください。

OpenComposerのPassengerアプリ設定はデプロイ時に事前生成するため、各ユーザーが初回アクセス時に`Initialize App`を実行する必要はありません。既に接続中のPUNは安全のため強制停止せず、次回起動時に生成済みの設定を読み込みます。

デプロイ後に `/opt/oci-hpc/conf/queues.conf` のパーティションや `instance_types[].name` を変更した場合は、プライマリコントローラで `/opt/oci-hpc/bin/slurm_config.sh` を実行してください。Slurm 設定と OpenComposer のジョブフォームが同時に再生成されます。OpenComposer や Apache の再起動は不要で、ブラウザでジョブ作成画面を再読み込みすると変更が反映されます。OpenComposer v2.0.2 の動的フォームで安全に扱うため、パーティション名と `instance_types[].name` には英数字、ハイフン、アンダースコアだけを使用してください。

History の表には、従来の項目に加えて `Start Time`（開始時刻）、`End Time`（終了時刻）、`Elapsed Time`（経過時間）、`CPU Time` を表示します。値は Slurm の `sacct` の `Start` / `End` / `Elapsed` / `CPUTime` を使用します。開始時刻・終了時刻は Submission Time と同じ `YYYY-MM-DD HH:MM:SS` 形式で表示します。`CPU Time` は経過時間と割り当て CPU 数の積で、実際に消費した CPU 時間の合計（`TotalCPU`）とは異なります。未開始・未終了・会計情報未反映の場合は、Slurm が返す値または空欄を表示します。`Refresh` ボタンは現在の検索条件・並び順・ページを維持し、表、ジョブ詳細、件数、ページ切替部分だけを更新します。検索欄への未適用の入力は保持し、更新対象には現在適用中の条件を使います。更新後も残っている行の選択は保持し、キャンセル対象は最新の状態で判定します。更新失敗時は元の表を保持してメッセージを表示します。

既存環境には更新した Open OnDemand role を再適用してください。固定版 OpenComposer の履歴ビューと追加 JavaScript を配布し、変更時に Passenger の再起動を要求します。既存の履歴 DB・ジョブスクリプトは保持します。完了済みジョブは OpenComposer が保存した会計情報を表示するため、保存済みの情報に時間項目がない過去の履歴は空欄のままです。

OpenComposer v2.0.2 の UI は、ブラウザから jsDelivr、cdnjs、Google Fonts の公開 CDN を参照します。閉域端末や厳格な Content Security Policy で利用する場合は、CDN へのアクセス許可またはアセットのローカル配信が別途必要です。

Open OnDemand の HTTPS/443 は `ood_source_cidr` で指定した送信元 CIDR からのみ許可されます。管理端末の固定グローバル IP など、必要な範囲に絞って指定してください。

## collect_logs.py

`/opt/oci-hpc/scripts/collect_logs.py` は、指定ノードの NVIDIA bug report、sosreport、console history log を収集します。コントローラ上で実行します。

到達可能なノードでは NVIDIA bug report と sosreport も取得します。SSH できないノードでは console history log のみを取得します。

必須引数:

```text
--hostname <hostname>
```

任意引数:

```text
--compartment-id <compartment_ocid>
```

例:

```bash
cd /opt/oci-hpc/scripts
python3 collect_logs.py --hostname compute-permanent-node-467
python3 collect_logs.py --hostname inst-jxwf6-keen-drake --compartment-id <compartment_ocid>
```

出力ファイルは `/home/<user>/<hostname>_<timestamp>` に保存されます。

複数ノードを処理する例:

```bash
for host in $(cat /home/opc/hostlist); do
  echo "$host"
  python3 collect_logs.py --hostname "$host"
done
```

## RDMA NIC メトリックの Object Storage アップロード

OCI-HPC はユーザー tenancy にデプロイされるため、OCI service team がクラスター内のメトリックを直接確認することはできません。`controller_object_storage_par` を有効にすると、RDMA NIC メトリックを Object Storage にアップロードするための PAR を作成できます。

Resource Manager の stack 作成時に `Create Object Storage PAR` を選択すると、PAR が作成され、`PAR_file_for_metrics` に保存されます。

メトリック収集とアップロードはコントローラ上で実行します。

```bash
/opt/oci-hpc/bin/upload_rdma_nic_metrics.sh
```

オプション:

```bash
/opt/oci-hpc/bin/upload_rdma_nic_metrics.sh -l 24 -i 5 -c <cluster_name>
```

- `-l`: 現在から何時間前までを収集するか。デフォルトは `24`。
- `-i`: メトリック集計間隔。デフォルトは `5` 分。
- `-c`: アップロードファイル名に付けるクラスター名。

デフォルト値は次の設定ファイルで変更できます。

```text
/opt/oci-hpc/bin/rdma_metrics_collection_config.conf
```
