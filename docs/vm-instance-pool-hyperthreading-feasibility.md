# Instance Pool の VM Hyperthreading 制御：実現性調査

調査日: 2026-09-16

## 結論と検証範囲

**OCI の仕様と Terraform Provider の実装上、対応する VM Shape では Instance Configuration に HT（SMT）の On / Off を設定し、その Configuration を参照する Instance Pool を作成できる。** リポジトリが固定している OCI Provider **5.37.0 は対応済み**で、この機能のための Provider 更新は不要。

ただし、これは仕様・公開ソースに基づく実現性判断であり、すべての VM Shape での実動作を保証するものではない。実装後のユーザー試験では **VM.Standard.E6.Flex は正常動作、VM.Standard3.Flex は HT=false の変数設定に対して HT On のまま**という結果になった。Intel は、同じ Shape / Image / AD / OCPU で Configuration を使わず作成した VM では SMT Off を確認した。Configuration 経由では Pool の有無にかかわらず On だった。通常作成では VFIO を指定しても SMT Off になり、VFIO 単独の制約では説明できない。Intel の Configuration 作成・保存・展開経路を主な調査対象とする。暗号化・Agent 設定の差などは残るため、原因となる処理は未確定。

初期構築用と Autoscaling 用の `instance-pool-platform.tf` に Shape の対応判定を追加し、両方の `instance-pool-configuration.tf` で VM の HT 設定を反映した。本資料には実装前の調査と、実装後のローカル検証・実環境の観測結果を記録する。

| 確認対象 | 結果 |
| --- | --- |
| VM 起動時の HT 制御 | Oracle 公式仕様で対応を確認 |
| Instance Configuration の VM 用 HT 属性 | AMD_VM / INTEL_VM の双方で対応を確認 |
| Instance Pool との接続 | Configuration を指定する公式仕様を確認。ユーザー試験では AMD が正常、Intel は HT On のまま |
| 現行 Provider 5.37.0 の送信処理 | 両 VM 型で true / false を送信する実装を確認。追加の実 API 試験で AMD / Intel の false 送信も確認 |
| Python SDK のリクエスト JSON 生成 | AMD / Intel × false / true の 4 ケース成功 |
| 対象リージョンの Shape 対応状況 | ap-osaka-1 の対象 AD で、E6.Flex / Standard3.Flex ともに SMT の許容値 true / false を確認 |
| 実環境の HT 状態 | AMD は SMT=false。Intel は Configuration 経由が SMT=true、通常の手動作成は SMT=false / ゲスト 4 CPU・1 thread per core |

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

## 実環境での追加調査（2026-09-16）

ユーザーが作成した ap-osaka-1 の VM と対応する Instance Pool / Configuration を読み取り API で確認した。

- `VM.Standard3.Flex`: 4 OCPU。生成済み `variables.tf` の `hyperthreading` は `false` だが、GetInstance の `platform_config.is_symmetric_multi_threading_enabled` は `true`。ゲストの `lscpu` も 8 CPU がすべてオンライン、4 コア、2 threads per core を示した。単なるコンソール上の vCPU 数の表示差ではない。
- 同じコントローラが作成した `VM.Standard.E6.Flex`: ユーザーは正常動作を報告。GetInstance の SMT は `false` だった。
- 同じ AD の ListShapes は、両 Shape に SMT 許容値 `[true, false]` を返した。
- Intel の GetInstanceConfiguration の `platformConfig` は `{"type":"INTEL_VM"}` のみで、SMT 項目を含まなかった。

この取得結果を切り分けるため、Python SDK 2.163.1 から一時 Instance Configuration を直接作成した。AMD / Intel × true / false の各送信 HTTP 本文に `isSymmetricMultiThreadingEnabled` と指定した boolean が含まれることを確認したが、いずれも作成応答と後続 GET の `platformConfig` は `type` のみだった。試験用 Configuration はすべて削除し、試験用 VM / Pool は作成していない。

**GetInstanceConfiguration の応答に SMT 項目がないことだけで、設定が保存されなかった・起動時に無視されたとは断定できない。** 実際に AMD VM の SMT は `false` だった。今回の直接 API 試験は Configuration の作成・取得までであり、Intel VM 起動時に指定が反映されるかを検証するものではない。Intel 作成時の Audit イベントにも HT のリクエスト本文は含まれず、送信値の確認には使用できなかった。

### Intel VM の再作成による確認

同じ条件で再作成した `VM.Standard3.Flex` でも、次を確認した。

- クラスターの `variables.tf` は `hyperthreading = false`。
- そのクラスターの `instance-pool-configuration.tf` に VM 用の動的 `platform_config` が存在し、`is_symmetric_multi_threading_enabled = tobool(var.hyperthreading)` を指定している。初回・再作成の両方の作成ログでも、plan に `is_symmetric_multi_threading_enabled = false` が含まれていた。
- 作成された VM の GetInstance は `type=INTEL_VM`、SMT=`true`。4 OCPU / 8 vCPU で、ゲストも 8 CPU すべてオンライン、2 threads per core のまま。
- 当該 VM が所属する Pool と、その Pool が参照する Instance Configuration の対応も API で確認した。Configuration の取得結果は引き続き SMT 項目なし。

この結果から、単に古いテンプレートや HT=true の変数が使用されたという説明では整合しない。ただし、対象クラスター作成時の送信 HTTP 本文は取得していないため、Provider と OCI サービスのどちらの段階で差異が生じたかは断定しない。公開モデルの対応と ListShapes の許容値だけで、Intel の実機動作を確認済みとは扱わない。

### Provider 5.37.0 の実送信を確認

対象 VM と同じリージョン / AD / Image、4 OCPU / 16 GB の最小構成で、Terraform 1.5.7 と OCI Provider 5.37.0 から一時 Instance Configuration を作成した。AMD / Intel の両方で apply が成功した。VM / Pool は作成せず、試験後の destroy ですべての一時 Configuration を削除した。

[Oracle 公式の詳細ログ設定](https://docs.oracle.com/en-us/iaas/Content/dev/terraform/troubleshooting.htm)に従って `TF_LOG=DEBUG` と `OCI_GO_SDK_DEBUG=v` を有効にし、CreateInstanceConfiguration の送信 HTTP 本文を確認した。SMT に関係する部分は次のとおり。

```json
{"type":"AMD_VM","isSymmetricMultiThreadingEnabled":false}
{"type":"INTEL_VM","isSymmetricMultiThreadingEnabled":false}
```

どちらも作成応答の `platformConfig` は `type` のみだったが、Terraform state では SMT=false だった。したがって、state の false も実 VM の HT Off を保証するものではない。

この試験により、Provider 5.37.0 が Intel の false を送信できることを実際の API 通信で確認できた。対象クラスター自体の送信本文を記録した試験ではなく、一時 Configuration からの VM 起動も行っていないため、Intel の起動経路で反映されない原因までは確定していない。続いて、下記のとおり同一 Configuration からの単体起動も比較した。OS 側の修正でオンライン CPU 数が減っても、この OCI 側の課題が解決したとは扱わない。

### 同一 Configuration からの単体起動と Pool 起動の比較

ユーザーが Intel クラスターを再作成し、その新しい Instance Configuration を指定して CLI の `launch-compute-instance` を実行した。起動リクエストでは不足している `createVnicDetails.subnetId` だけを補い、`platformConfig` / `agentConfig` は指定しなかった。

作成した単体 VM と、同じ Configuration を参照する Pool に所属する VM を GetInstance で比較した。

| 項目 | Pool 経由 | Configuration から単体起動 |
| --- | --- | --- |
| 作成時刻（UTC、2026-09-16） | 13:25:23 | 13:26:29 |
| 状態 | RUNNING | RUNNING |
| Shape / OCPU / Memory | VM.Standard3.Flex / 4 / 16 GB | 同左 |
| Image / AD | 同一 Image、ap-osaka-1 の同一 AD | 同左 |
| Fault Domain | FAULT-DOMAIN-3 | FAULT-DOMAIN-2 |
| platform type | INTEL_VM | INTEL_VM |
| GetInstance の SMT | true | true |
| GetInstance の vCPU 数 | 8 | 8 |

**Pool を経由しない起動でも SMT=true を観測したため、Pool に固有の問題だけでは説明できない。** Configuration の保存・展開、または VM の起動段階を引き続き切り分ける必要がある。続いて、ユーザーが Configuration を使わない通常の VM 作成で SMT Disable を指定した（次節）。この単体 VM のゲスト OS 側の確認はまだ行っていない。

起動手順上、次の点にも注意する。

- コンソールからの起動では、Configuration に保存された `isManagementDisabled=true` に対して起動画面が false を送信し、上書き拒否エラーとなった。CLI で `agentConfig` を省略し、保存値を継承すると起動できた。
- クラスター削除に伴って Configuration も削除されるため、過去の OCID を再使用すると `IncorrectState: instance configuration ... is Deleted` になる。比較対象のクラスターを保持し、現在の Pool が参照する Configuration ID を確認してから起動する。

### Configuration を使用しない手動作成との比較

ユーザーが通常の VM 作成で SMT Disable を指定した Intel VM を確認した。GetInstance の SMT は `false`、ゲストの `lscpu` は CPU 数 4、オンライン CPU 0–3、4 コア、1 thread per core だった。

| 項目 | Configuration から単体起動 | Configuration なしの手動作成 |
| --- | --- | --- |
| Shape / OCPU / Memory | VM.Standard3.Flex / 4 / 16 GB | 同左 |
| Image / AD / Fault Domain | 同一 Image、ap-osaka-1 の同一 AD、FAULT-DOMAIN-2 | 同左 |
| GetInstance の SMT | true | false |
| GetInstance の vCPU 数 | 8 | 8 |
| ゲストの CPU 数 / threads per core | 未採取 | 4 / 1 |
| network_type | VFIO | PARAVIRTUALIZED |
| is_pv_encryption_in_transit_enabled | false | true |
| is_management_disabled | true | false |

**この Intel Shape / Image で SMT Off は実現できる。** 一方、Configuration を使った経路と通常作成では結果が異なる。基本の計算資源条件だけでなく Fault Domain も一致したが、ネットワーク方式、転送時暗号化、Agent 設定は異なる。したがって現時点で Configuration のサービス不具合と断定せず、これらの差を含めて確認する。

リポジトリの初期構築用と Autoscaling 用 `instance-pool-configuration.tf` は、この Intel Shape に `network_type = "VFIO"` を指定している。[Oracle のネットワーク方式の仕様](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/instances.htm)では Standard3.Flex は Paravirtualized / SR-IOV の両方をサポートする。調査した公式 SMT 資料には、SR-IOV との組合せに関する制約を確認できなかった。これは制約が存在しないことの証明ではなく、ネットワーク方式が今回の原因だという証拠もまだない。

次に再現試験をする場合は、Agent 設定や暗号化を含む共通の launch details を揃え、通常起動と Configuration 経由起動を比較する。その後、次節の VFIO を指定した通常作成を確認したため、現時点では追加起動よりも Oracle への照会を優先する。上記の起動オプション差も省略せず提示する。

なお、SMT Off の手動作成 VM でも `shape_config.vcpus` は 8 だったため、この値だけで SMT の状態を判定してはならない。`platform_config.is_symmetric_multi_threading_enabled` とゲストのトポロジーを併せて確認する。

### VFIO を指定した通常作成での追加確認

ユーザーが **Instance Configuration を使わない通常の VM 作成**で VFIO と SMT Disable を指定したことを確認した。GetInstance は `network_type=VFIO`、SMT=`false` を返し、ゲストの `lscpu` も 4 CPU / オンライン 0–3 / 1 thread per core だった。Shape、Image、4 OCPU、16 GB、AD、FAULT-DOMAIN-2 は、前の通常作成と Configuration からの単体起動に一致する。

| 作成経路 | network_type | GetInstance の SMT | ゲストで確認した結果 |
| --- | --- | --- | --- |
| 通常の VM 作成 | PARAVIRTUALIZED | false | 4 CPU / 1 thread per core |
| 通常の VM 作成 | VFIO | false | 4 CPU / 1 thread per core |
| Configuration から単体起動 | VFIO | true | 未採取 |
| Configuration から Pool 起動 | VFIO | true | 同条件の先行試験で 8 CPU / 2 threads per core |

**VFIO と SMT Off は、この Intel Shape / Image で両立する。** 「VFIO では SMT Off にできない」という一般的な非互換性では、今回の事象を説明できない。Configuration + PARAVIRTUALIZED の試験を行ったわけではない点にも注意する。

通常作成の VFIO VM も、PV 接続ボリュームの転送時暗号化は true、management disabled は false であり、Configuration の入力とは一部の差が残る。これらを含む入力差があるため、保存時に false が失われたという断定はしない。

現時点でネットワーク方式を恒久変更する根拠はなく、追加の推測に基づく Terraform 修正は行わない。次は Oracle に、対象 Configuration の作成要求の受信値、保存値、LaunchInstanceConfiguration / Pool が VM 起動に渡した platform 設定を追跡してもらう。公開モデルと実サービスの対応状況、GET で SMT 項目が返らない理由、既知の制約・不具合も照会対象とする。Provider の別試験で false 送信を確認した事実と、ユーザー環境の作成ログで plan=false を確認した事実は区別して伝える。

### OS 側で別途確認した不具合

Enterprise Linux 用の既存 `control_hyperthreading.sh` は `thread_siblings_list` をカンマで分割し、2 番目の CPU だけをオフライン化していた。Linux の CPU リストが `0-1` のような範囲形式の場合、対象 CPU を抽出できず、処理が成功扱いのまま全 CPU がオンラインに残ることをテストで再現した。

範囲・カンマ・混在形式を展開して各コアの先頭 CPU を残し、他の兄弟 CPU をオフラインにするよう修正した。書込み失敗はサービスへエラーとして返す。これは OS 側の利用スレッド数を制御する修正であり、OCI の VM platform 設定を変更するものではない。OS 側で兄弟 CPU をオフラインにした場合、`lscpu` の総 CPU 数は 8 のままでもよい。4 コアの VM ではオンライン CPU 数が 4、各コアのオンラインスレッド数が 1 であることを確認する。ユーザーが再作成した Intel VM の `cpu0/topology/thread_siblings_list` は実際に `0-1` であり、この不具合に該当することを確認した。修正版スクリプトの実機適用後の結果は未確認。

## 実環境での合格条件

対象 Compartment、AD、Subnet、対応 Image とそれらを利用できる認証を定め、AMD / Intel の対象 Shape ごとに次を確認する。

1. ListShapes で platform type と要求する HT 値の許容を確認する。
2. HT=false を指定した Instance Configuration を作成する。plan と送信内容の `instance_details.launch_details.platform_config` に正しい VM 型と false が含まれることを確認する。上記の実測では GET が SMT 項目を返さないため、取得結果だけで保存値を判定しない。
3. その Configuration を使い、1 台の Instance Pool を作成する。Pool と VM が RUNNING になることを確認する。
4. VM の取得結果に `platform_config.is_symmetric_multi_threading_enabled=false` が反映され、ゲスト OS の `lscpu` が `Thread(s) per core: 1` を示すことを確認する。
5. 同じ Shape / OCPU / Memory / Image で HT=true の Configuration と Pool を作成し、API の値が true、`Thread(s) per core: 2` になることを確認する。`queues.conf` / `var.hyperthreading` も true に揃え、OS 側の処理が HT を無効化しない条件で比較する。
6. `slurmd -C` と生成された Slurm ノード定義を照合し、ノード登録と小規模ジョブが成功することを確認する。
7. 検証用 Pool / Configuration と、残存する検証用ボリュームを確認して片付ける。

API の保存値だけで合格とせず、Pool が作成した VM の値と OS が見ているトポロジーまで確認する。[Oracle 公式の HT 確認方法](https://docs.oracle.com/iaas/Content/Compute/Tasks/disablesmt.htm)も `lscpu` のスレッド数を使っている。

既存 Pool の Configuration を差し替えても、既存 VM に新しい設定は自動適用されない。[Oracle の更新仕様](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/updatinginstancepool-updating-instance-configuration.htm)に従い、新規作成 VM を検証対象とする。既存環境へ修正版を展開する際は、コントローラ上の `/opt/oci-hpc/autoscaling/tf_init` にも変更を反映する必要がある。
