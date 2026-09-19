# OCI HPC Trial Pack

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/kazuitox/oci-hpc-trial-pack/archive/refs/heads/master.zip)

本リポジトリは、Oracle Cloud Infrastructure (OCI) 上に HPC 環境を短時間で構築し、PoC や初期検証をすばやく開始することを目的としています。
この目的に合わせて、現時点では Oracle Linux 8 を対象 OS として動作確認しています。その他の OS やバージョンについては未検証のため、利用する場合は個別に検証してください。

Terraform / Oracle Resource Manager スタックとして、コントローラ、計算ノード、Slurm、LDAP、共有ストレージ、Autoscaling、監視、Open OnDemand などをまとめて構成します。

`schema.yaml` は日本語 UI 向けに整備されており、`SIMPLE` モードでは最小限の入力、`ADVANCED` モードでは詳細な構成項目を表示します。

## バージョニング

`oci-hpc-trial-pack` は `v1.0.0` から始まる独立したバージョン系列として管理します。最初のリリースは `oci-hpc v2.10.6.23` をベースとしており、既存の `v2.10.x` タグは旧系列の履歴として保持します。

- 後方互換性のある不具合修正: パッチバージョン（例: `v1.0.1`）
- 後方互換性のある機能追加: マイナーバージョン（例: `v1.1.0`）
- 破壊的変更: メジャーバージョン（例: `v2.0.0`）

変更履歴は [CHANGELOG.md](CHANGELOG.md) を参照してください。

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
Administratorグループの権限がない場合には【Autoscaling 用 IAM Policy / Dynamic Group を作成】のチェックを外し、テナント管理者にて以下のポリシーと動的グループを適切に設定をしてください。オプションを無効にすると、IAM Policy / Dynamic Groupだけでなく、その作成に必要なテナンシおよびホームリージョンの参照も実行しません。

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

Autoscaling は「ジョブごとにクラスターを作成する」方式です。Slurm の pending ジョブを cron で確認し、ジョブのノード数、キュー、インスタンスタイプに合わせて新しいクラスターを作成します。アイドル状態のクラスターは、デフォルトで 600 秒経過後に削除対象になります。

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
- `use_local_block_volume`: 各 Compute node 専用の一時 Block Volume をアタッチします。
- `local_block_volume_size`: ノードごとの Block Volume サイズ（50 GB 以上の整数）です。
- `local_block_volume_performance`: `0.  Lower performance`、`10. Balanced performance`、`20. High Performance` のいずれかを指定します。
- `local_block_volume_mount_point`: ノード内のマウントポイントを200文字以内の絶対パスで指定します。共有 NFS、NVMe、`/home` などの既存パスとは重複できません。

このノード専用 Block Volume は `use_scratch_nfs` で構成するクラスター内共有 NFS とは独立しています。XFS（Oracle Linux）または ext4（Ubuntu/Debian）で初期化し、ノード終了時に自動削除します。稼働中ノードの設定は後から付け替えず、`queues.conf` の変更後に新規作成されるクラスター／ノードから適用されます。初期 Permanent node にはスタック作成時の同名設定が適用されます。

設定を変更した後は、Slurm 設定を再生成します。

```bash
/opt/oci-hpc/bin/slurm_config.sh
```

Slurm の状態を初期状態に戻したい場合は、次を実行します。

```bash
/opt/oci-hpc/bin/slurm_config.sh --initial
```

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

Autoscaling の Instance Pool では、Ansible の構成完了後に各ノードから実際の OS
ホスト名を取得し、その値へ OCI インスタンス名と Primary VNIC の表示名を自動同期します。
名前を事前計算するのではなく、Ansible 適用後のホスト名を正として扱います。VNIC の
`hostname_label` は VCN 内部 DNS 用の別属性であり、変更しません。Secondary VNIC、
Cluster Network、Compute Cluster も対象外です。既存クラスターを移行する場合は、次の
再構成を一度実行してください。

ここでいう VNIC 名は Networking サービス上の VNIC リソースの表示名です。VNIC attachment
は公開 API に更新操作がないため、attachment 固有の表示名は同期対象に含みません。

IAM Policy を手動管理している場合は、実行主体に Primary VNIC の更新権限
（`VNIC_UPDATE`。通常は `use vnics`）も付与してください。

```bash
/opt/oci-hpc/bin/resize.sh --cluster_name <cluster_name> reconfigure
```

同期途中で失敗した場合、次回の `add`、`remove`、`remove_unreachable`、`reconfigure`
は未完了の再構成と名前同期だけを再開して終了します。同じ台数変更を続けて行う場合は、完了を
確認してからコマンドをもう一度実行してください。既存クラスターの旧 OCI 名 DNS レコードは、
この再構成時に Terraform から安全に引き継がれます。

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

`use_ood` を有効にすると、Open OnDemand と [OpenComposer](https://github.com/RIKEN-RCCS/OpenComposer) をインストールします。ユーザーはブラウザからファイル操作、ジョブ投入、アプリケーション実行を行えます。スタックは Open OnDemand 用の初期パスワードも生成し、構成に反映します。

ブラウザシェルの無操作タイムアウトは30分です。接続の最大継続時間はOpen OnDemandのデフォルトである1時間のままです。

`use_ood`を有効にするとVNC用の`vnc`パーティション（Constraint: `dskv`、instance keyword: `desktop-v`）を作成します。Oracle Linux 8で`ood_dcv_enabled`も有効にすると、DCV専用の`dcv`パーティション（Constraint: `dskd`、instance keyword: `desktop-d`）と「Linux Desktop with Amazon DCV（検証用）」を追加します。VNCジョブは`vnc`、DCVジョブは`dcv`へ投入され、ノード構築時のAnsibleもキュー名に応じてTurboVNCまたはAmazon DCVだけを構成します。

`ood_desktop_use_gpu`の初期値は`false`で、VNCとDCVは`VM.Standard.E6.Flex`のCPUデスクトップノードを使用します。CPUノードではAnsibleが`Server with GUI`パッケージグループをインストールし、DCV仮想セッションはソフトウェア描画を使用します。`ood_desktop_use_gpu`を有効にすると、VNCとDCVの両方が`VM.GPU.A10.1`とGUI導入済みのGPUデスクトップ用Custom Imageを使用します。GPUノードではGUIの再インストールをスキップし、GDMのWayland無効化、NVIDIA Xorg `:0`、DCV-GL、VirtualGLを構成します。

Amazon DCV側では、利用時間、初期解像度、同時接続数、ホームディレクトリとのファイル転送をフォームで指定できます。GPU構成の場合のみDCV-GLの有効・無効も指定でき、ジョブはA10 GPU 1基とCPU 8基を要求します。CPU構成ではGPUを要求せずCPU 2基を要求します。割り当てノード上にユーザー専用のDCV仮想セッションを作成し、ジョブ終了時にセッションと48文字のワンタイム認証トークンを削除します。

新しい`dcv`ノードでは、Slurmへの登録前にAmazon DCV 2025.0-20103を公式配布元からダウンロードし、利用するRPMのSHA-256とRPM署名を検証してインストールします。GPU構成では`glxinfo`と`dcvgldiag`でNVIDIA OpenGLを確認し、DCVセッション作成時にも仮想セッションのrendererがNVIDIAであることを検証します。デスクトップノードから`d1uj6qtbmh3dt5.cloudfront.net`へのHTTPS通信を許可してください。

ブラウザー接続はOODの`/rnode/<host>/<port>/`を経由します。Webクライアントの経路はHTTPS/WSS（TCP）であり、QUIC/UDPは使用しません。本機能は検証用です。本構成ではAmazon DCV Serverに別途ライセンスを設定しないため、インストール時に自動的に適用される自動評価ライセンスを使用します。自動評価ライセンスはインストール後30日間有効で、有効期限後はAmazon DCVセッションを新規作成またはホストできません。継続利用または本番利用には、利用者が適切なライセンスを用意し、[Amazon DCVのライセンス条件](https://docs.aws.amazon.com/dcv/latest/adminguide/setting-up-license.html)およびEULAを確認・遵守する必要があります。

OpenComposer は `v2.0.2`（commit `7af3d94b36043d8019b1639cd8e463957eb7a37e`）に固定し、スタックの Slurm を直接利用するよう構成します。Open OnDemand の「04 OpenComposer」メニューには「Slurmジョブ」と「History」を追加します。どちらも Open OnDemand のカスタムページ内に OpenComposer を表示するため、ジョブ投入画面や履歴画面へ移動しても Open OnDemand のヘッダーが維持されます。OpenComposer には、`queues.conf` のパーティションとノードグループ（Slurm Constraint）を選択できる汎用 Slurm ジョブフォームも追加します。Constraint の候補には各パーティションの `instance_types[].name`、つまり生成される `slurm.conf` の `NodeName` における `Features` の最後の値を使用します。パーティションを変更すると、そのパーティションで利用できる Constraint だけが選択肢として有効になります。実行プロファイルは `なし`、`MPI`、`OpenMP` から選択します。`MPI` では `BM HPC (RDMA)`、`VM (TCP)`、`BM Standard (TCP)` の実行環境と、`Intel MPI (OneAPI)`、`OpenMPI v4.x`、`Platform MPI v9.x` のMPI実装を選択できます。`MPI` を選択した場合だけノードあたりのタスク数（`--ntasks-per-node`）、`OpenMP` を選択した場合だけ1タスクに割り当てるCPU数（`--cpus-per-task`）を指定できます。`VM (TCP)` の場合は `#SBATCH --ntasks-per-core=1` と `#SBATCH --exclusive`、`BM Standard (TCP)` の場合は `#SBATCH --exclusive` のみを追加し、選択した実行環境とMPI実装に対応するmpirunオプションを `export MPI_OPTIONS="..."` としてジョブスクリプトへ挿入します。`OpenMP` の場合は `OMP_NUM_THREADS`、`OMP_PROC_BIND`、`OMP_DISPLAY_AFFINITY` を設定します。実行時間の指定は有効・無効を選択でき、無効の場合は時間入力欄と `#SBATCH --time=` をジョブスクリプトから除外します。Slurm ジョブメール通知を有効化した場合は、メール通知の有無と `BEGIN` / `END` / `FAIL` をチェックボックスで選択でき、選択時だけ `#SBATCH --mail-type=` を生成します。フォームで生成したジョブスクリプトは投入前に編集できます。Ruby の依存 gem には Open OnDemand 4.0 が同梱する gem セットを利用します。

OpenComposerのPassengerアプリ設定はデプロイ時に事前生成するため、各ユーザーが初回アクセス時に`Initialize App`を実行する必要はありません。既に接続中のPUNは安全のため強制停止せず、次回起動時に生成済みの設定を読み込みます。

デプロイ後に `/opt/oci-hpc/conf/queues.conf` のパーティションや `instance_types[].name` を変更した場合は、プライマリコントローラで `/opt/oci-hpc/bin/slurm_config.sh` を実行してください。Slurm 設定と OpenComposer のジョブフォームが同時に再生成されます。OpenComposer や Apache の再起動は不要で、ブラウザでジョブ作成画面を再読み込みすると変更が反映されます。OpenComposer v2.0.2 の動的フォームで安全に扱うため、パーティション名と `instance_types[].name` には英数字、ハイフン、アンダースコアだけを使用してください。

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
