# Instance Pool の VM Hyperthreading 制御：実現性調査

調査日: 2026-09-16

## 結論と検証範囲

**OCI の仕様と Terraform Provider の実装上、対応する VM Shape では Instance Configuration に HT（SMT）の On / Off を設定し、その Configuration を参照する Instance Pool を作成できる。** リポジトリが固定している OCI Provider **5.37.0 は対応済み**で、この機能のための Provider 更新は不要。

ただし、これは仕様・公開ソース・ローカルでのリクエスト生成確認に基づく実現性判断である。**実際の OCI サービスで Configuration → Pool → VM を作成して HT を確認する試験は未実施**。すべての VM Shape で利用可能と断定するものではない。

この資料は実装前の調査記録である。調査後、初期構築用と Autoscaling 用の `instance-pool-platform.tf` に Shape の対応判定を追加し、両方の `instance-pool-configuration.tf` で VM の HT 設定を反映した。実際の OCI での起動試験は引き続き未実施。

| 確認対象 | 結果 |
| --- | --- |
| VM 起動時の HT 制御 | Oracle 公式仕様で対応を確認 |
| Instance Configuration の VM 用 HT 属性 | AMD_VM / INTEL_VM の双方で対応を確認 |
| Instance Pool との接続 | Configuration を指定して起動する公式仕様を確認。HT を持つ Configuration の実機起動は未検証 |
| 現行 Provider 5.37.0 の送信処理 | 両 VM 型で true / false を送信する実装を確認 |
| Python SDK のリクエスト JSON 生成 | AMD / Intel × false / true の 4 ケース成功 |
| 対象リージョンの Shape 対応状況 | 読み取り API が 404 `NotAuthorizedOrNotFound` を返し、取得できず |
| 実環境の Terraform plan / apply、ゲスト OS の HT 状態 | 未実施。実装後のオフライン検証は後述 |

## OCI と Provider の根拠

### Instance Configuration は VM の HT 属性を持つ

Oracle の公式モデルに、次の型と `is_symmetric_multi_threading_enabled` 属性が定義されている。

- [InstanceConfigurationAmdVmLaunchInstancePlatformConfig](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/models/oci.core.models.InstanceConfigurationAmdVmLaunchInstancePlatformConfig.html): `type = AMD_VM`
- [InstanceConfigurationIntelVmLaunchInstancePlatformConfig](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/models/oci.core.models.InstanceConfigurationIntelVmLaunchInstancePlatformConfig.html): `type = INTEL_VM`

VM 起動時にこの値を指定するための設定は、Instance Configuration の `instance_details.launch_details.platform_config` に置く。以下は設定箇所を示す抜粋であり、単独で適用する Terraform ファイルではない。

```hcl
platform_config {
  type                                = "AMD_VM" # Intel は INTEL_VM
  is_symmetric_multi_threading_enabled = var.hyperthreading
}
```

[Instance Configuration 作成仕様](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/creatinginstanceconfig.htm)と[Instance Pool 作成仕様](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/creatinginstancepool.htm)は、Configuration をテンプレートとして Pool のインスタンスを起動する構成を定義している。VM 用 HT 属性とこの起動経路を合わせ、Pool 経由でも実現可能と判断した。サービス側での受け入れと起動後の値は実機試験で確認する。

### Provider 5.37.0 は対応済み

公開ソースで `mapToInstanceConfigurationLaunchInstancePlatformConfig` を確認した。

- [5.37.0 の AMD_VM 変換処理](https://github.com/oracle/terraform-provider-oci/blob/v5.37.0/internal/service/core/core_instance_configuration_resource.go#L4102)
- [5.37.0 の INTEL_VM 変換処理](https://github.com/oracle/terraform-provider-oci/blob/v5.37.0/internal/service/core/core_instance_configuration_resource.go#L4244)

双方が `GetOkExists` で設定値を取得し、SDK モデルの `IsSymmetricMultiThreadingEnabled` に代入する。明示した `false` も保持されるため、「false が未指定扱いで落ちる」という問題ではない。

[5.29.0](https://github.com/oracle/terraform-provider-oci/blob/v5.29.0/internal/service/core/core_instance_configuration_resource.go)と[5.30.0](https://github.com/oracle/terraform-provider-oci/blob/v5.30.0/internal/service/core/core_instance_configuration_resource.go)の公開ソース比較では、5.30.0 で両 VM 型への SMT 送信処理が追加されている。現行の `versions.tf` と `autoscaling/tf_init/versions.tf` はともに 5.37.0。

Terraform のドキュメントには属性の Applicable 条件から VM 型が抜けている箇所があるため、今回は条件欄だけで判断せず、VM 専用モデルと実際の Provider の送信処理を根拠とした。

### Shape ごとの対応判定

型が AMD_VM / INTEL_VM であることに加え、対象 Shape が要求する HT 値を許容していることを確認する必要がある。

[ShapePlatformConfigOptions](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/models/oci.core.models.ShapePlatformConfigOptions.html)と[SMT の許容値](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/models/oci.core.models.ShapeSymmetricMultiThreadingEnabledPlatformOptions.html)は、ListShapes の戻り値で確認できる。Provider 5.37.0 の [oci_core_shapes](https://github.com/oracle/terraform-provider-oci/blob/v5.37.0/internal/service/core/core_shapes_data_source.go#L409)も次の情報を公開している。

```text
shapes[*].platform_config_options[0].type
shapes[*].platform_config_options[0].symmetric_multi_threading_options[0].allowed_values
```

実装では対象 Compartment / AD の情報を使い、Shape 名の推測や固定リストだけで判定しない。情報や SMT 項目がない場合は空として扱う必要がある。Arm Shape などへ AMD_VM / INTEL_VM の設定を送らない。HT Off を要求した非対応 x86 Shape についても、設定を黙って無視せず、対応不可または確認不能であることを明確にする。

既存の `data.tf` の `available_shapes` は画像互換登録用にリージョン全体を照会している。HT 判定のためにこの照会へ AD 制限を追加せず、対象 AD の確認が必要な場合は別のデータソースを用いる。

## 現行コードの原因と修正方針

キューから Terraform 変数への伝達はすでに存在する。

```text
queues.conf: instance_types[].hyperthreading
  → bin/create_cluster.sh: hyperthreading の読み取り、##HT## の置換
  → conf/variables.tpl: variable "hyperthreading"
  → Autoscaling の var.hyperthreading
```

修正前の初期構築用と Autoscaling 用の `instance-pool-configuration.tf` は、次の動作になっていた。

1. `platform_config` を生成する条件が `var.BIOS` のみ。
2. HT 設定に使用する値が `var.SMT` であり、`var.hyperthreading` を参照しない。
3. `locals.tf` の `platform_type` は BM 用の判定だけで、VM も既定の `GENERIC_BM` に分類される。

修正対象と方針:

| 対象 | 必要な変更 |
| --- | --- |
| `instance-pool-platform.tf` と Autoscaling 側の同名ファイル | 対象 VM Shape の platform type と SMT 許容値を取得・判定する |
| `instance-pool-configuration.tf` | 対応 VM では BIOS 設定と独立して、VM 用 type と `var.hyperthreading` を送る |
| `autoscaling/tf_init/instance-pool-configuration.tf` | 同じ制御を追加し、queues.conf の設定を反映する |
| BM の既存 platform_config | 既存の BIOS / SMT / NUMA 等の動作を維持する |
| 回帰確認 | 初期構築・Autoscaling 双方、HT true / false、BIOS / SMT との競合、BM / Arm / 非対応 Shape を確認する |

VM の `platform_config` には BM 専用の IOMMU、NUMA、percentage_of_cores_enabled などを含めない。VM で `BIOS=true`、`SMT=true`、`hyperthreading=false` が併存しても、VM の HT は `hyperthreading=false` に従う設計とする。

Instance Pool 側は、すでに該当 Instance Configuration の ID を参照しているため、接続方法の変更は不要。初期作成 VM はスタックの `hyperthreading`、Autoscaling VM はキューから渡された `hyperthreading` を用いる。

既存構成の HT 設定を変更する場合、Provider は Instance Configuration を再作成する。Pool が参照している Configuration は削除できないため、両方の Configuration に `create_before_destroy = true` を指定し、新しい Configuration への切り替え後に古いものを削除する。[Oracle の Instance Pool の制約](https://docs.oracle.com/en-us/iaas/Content/Compute/Concepts/instance-pools.htm)

Slurm はすでにキューの値から `ThreadsPerCore=1 / 2` を設定する。実機では OCPU 数、ゲスト OS のコア数、`slurmd -C` の整合を確認する。また、既存の OS 側 HT 無効化処理との併用も確認する。特に Ubuntu の処理は `/sys/devices/system/cpu/smt/control` に書き込むため、起動時点で HT が無効な環境での挙動を確認する。

## 実施したローカル検証

OCI Python SDK **2.163.1** を使い、`CreateInstanceConfigurationDetails` 内の `ComputeInstanceDetails → InstanceConfigurationLaunchInstanceDetails → platform_config` を実際の SDK シリアライザーに渡した。認証やネットワーク通信を伴わない JSON 生成のみの確認である。

出力された `platformConfig` は次のとおり。4 ケースとも boolean 値が維持されることを assert で確認した。

```json
{"type": "AMD_VM", "isSymmetricMultiThreadingEnabled": false}
{"type": "AMD_VM", "isSymmetricMultiThreadingEnabled": true}
{"type": "INTEL_VM", "isSymmetricMultiThreadingEnabled": false}
{"type": "INTEL_VM", "isSymmetricMultiThreadingEnabled": true}
```

この検証は Python SDK の JSON 生成確認であり、OCI サービスの受け入れ試験を代替しない。調査時点では Terraform の実行試験も未実施だったが、実装後に次のオフライン検証を追加した。

既存のローカル OCI 認証（DEFAULT、ap-tokyo-1）で、テナンシのルート Compartment に対する ListShapes を読み取り専用で試したが、404 `NotAuthorizedOrNotFound` が返った。この結果から対象 Shape の対応可否は判断できない。リソースの作成・変更は行っていない。

## 実装後のオフライン検証

- Terraform 1.5.7 / OCI Provider 5.37.0 で初期構築用と Autoscaling 用の構成を `validate`。Autoscaling 側は `conf/variables.tpl` に含まれる変数を一時ディレクトリで宣言して検証し、両方成功。
- Terraform 1.9.8 の mock provider を使い、初期構築 32 ケースと Autoscaling 32 ケースに成功。本番ファイルの Shape 判定、platform_config、作成条件、lifecycle を抽出して検証する。AMD / Intel の On・Off、BIOS / SMT との競合、空・null の対応情報、非対応値の拒否、BM 設定の維持、Arm、初期ノード数 0 を含む。各モジュールで HT=true の mock apply による状態作成と、その状態から HT=false に変更する plan も確認した（合計 62 mock plan / 2 mock apply）。
- Ubuntu の OS 側処理は、HT が既に要求どおりの状態、`forceoff` / `notsupported` / `notimplemented`、SMT 制御ファイル不在で不要な書込みをしないよう修正。専用の実行テストで 13 ケースを確認。

通常の回帰テスト:

```bash
python3 -m unittest discover -s tests -v
```

HT の mock plan テストは Terraform 1.7 以降が必要。実際の構成で必要なバージョンは、初期構築・Autoscaling ともに Terraform 1.5.0 以降、OCI Provider 5.37.0 のまま。

```bash
TERRAFORM_BINARY=/path/to/terraform \
  python3 -m unittest discover -s tests -p test_instance_pool_hyperthreading_terraform.py -v
```

既存の Provider ミラーを使用する場合は `TF_PLUGIN_DIR` にそのディレクトリを指定する。Terraform がない、または 1.7 未満の場合は mock plan テストをスキップする。mock は OCI への接続やリソース作成を行わず、実機試験の代替ではない。

## 実環境での合格条件

対象 Compartment、AD、Subnet、対応 Image とそれらを利用できる認証を定め、AMD / Intel の対象 Shape ごとに次を確認する。

1. ListShapes で platform type と要求する HT 値の許容を確認する。
2. HT=false を指定した Instance Configuration を作成し、取得結果の `instance_details.launch_details.platform_config` に VM 型と false が保存されることを確認する。
3. その Configuration を使い、1 台の Instance Pool を作成する。Pool と VM が RUNNING になることを確認する。
4. VM の取得結果に `platform_config.is_symmetric_multi_threading_enabled=false` が反映され、ゲスト OS の `lscpu` が `Thread(s) per core: 1` を示すことを確認する。
5. 同じ Shape / OCPU / Memory / Image で HT=true の Configuration と Pool を作成し、API の値が true、`Thread(s) per core: 2` になることを確認する。`queues.conf` / `var.hyperthreading` も true に揃え、OS 側の処理が HT を無効化しない条件で比較する。
6. `slurmd -C` と生成された Slurm ノード定義を照合し、ノード登録と小規模ジョブが成功することを確認する。
7. 検証用 Pool / Configuration と、残存する検証用ボリュームを確認して片付ける。

API の保存値だけで合格とせず、Pool が作成した VM の値と OS が見ているトポロジーまで確認する。[Oracle 公式の HT 確認方法](https://docs.oracle.com/iaas/Content/Compute/Tasks/disablesmt.htm)も `lscpu` のスレッド数を使っている。

既存 Pool の Configuration を差し替えても、既存 VM に新しい設定は自動適用されない。[Oracle の更新仕様](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/updatinginstancepool-updating-instance-configuration.htm)に従い、新規作成 VM を検証対象とする。既存環境へ修正版を展開する際は、コントローラ上の `/opt/oci-hpc/autoscaling/tf_init` にも変更を反映する必要がある。
