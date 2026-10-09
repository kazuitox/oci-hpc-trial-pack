# Open OnDemand 4.2.5 へ更新する

この手順は、OCI HPC Trial Pack の既存コントローラを管理する担当者向けです。Oracle Linux 8 と Ubuntu 24.04 を対象に、Open OnDemand（OOD）を 4.2.5、Dex を 2.45.1 に更新します。検証環境で以下の手順と利用機能を確認してから、実環境へ適用してください。ここに記載する実機操作は、リポジトリのローカル検証では実施していません。

## 更新で認証設定と標準アプリを移行する

公式アドバイザリ [GHSA-frjp-qp79-hc3g](https://github.com/OSC/ondemand/security/advisories/GHSA-frjp-qp79-hc3g) は、4.2.5 未満のファイル転送処理を影響対象としています。本修正は、新規構築と既存環境の再構成で本体を 4.2.5 に固定します。より新しい版を導入済みの場合は、ダウングレードせず、その版との互換性を別途確認してください。

Dex 認証で必要な `oidc_crypto_passphrase` は、コントローラ上でランダムに生成します。`/etc/ood/config/.oidc_crypto_passphrase` に root 所有・0600 で保存し、再実行でも再利用します。設定先の `/etc/ood/config/ood_portal.yml` も 0600 にします。保存した値が空・破損していれば停止し、黙って作り直すことはありません。このファイルをバックアップ対象に含め、Git や作業ログへ秘密値を載せないでください。

標準アプリの `dashboard/app/apps/ood_app.rb` は、旧版のコピーによる上書きをやめます。「ホームディレクトリ」の表示は翻訳設定で維持します。既存の OpenComposer v2.0.2、ヘッダー内の Slurm ジョブ・History、物理コア表示、VNC／DCV の設定は継続利用する構成です。これらの実機互換性は更新後に確認します。Ubuntu では従来欠けていた OpenComposer の導入タスクも実行し、Slurm の `/usr/local/bin` を参照します。

Oracle Linux 8 は Node.js のモジュールを 22 に切り替えます。Ubuntu は公式のリポジトリ登録パッケージで Node.js 22 の取得先を設定します。コントローラで Node.js を利用する別アプリがあれば、その互換性も確認してください。`--insecure` は既存の静的 Dex ログインを有効にするために残しています。Apache の起動・再読み込みでポータル生成に失敗した場合は、以後その失敗を無視しません。

## 更新前の環境を復旧できる状態にする

作業時間中は OOD の Web セッションが切断されます。実行中の Slurm ジョブは PUN（ユーザーごとの Web プロセス）の終了とは別ですが、VNC／DCV への再接続も検証してください。ジョブとユーザーデータの保持を実機で確認するまでは、重要なジョブのない時間帯に更新します。

1. OS、OOD・Dex・Node.js のバージョン、利用している認証とアプリを記録します。Oracle Linux 8 は `rpm -q ondemand ondemand-dex`、Ubuntu は `dpkg-query -W ondemand ondemand-dex` で確認できます。
2. コントローラの更新前イメージなど、パッケージと依存関係を含めて戻せる復旧手段を用意します。パッケージ単体のダウングレードでは、Node.js・Ruby・Dex・設定を元に戻し切れない場合があります。
3. `/etc/ood`、`/var/www/ood/apps/sys`、独自の公開アセット、Apache の設定と systemd drop-in を、権限を保持して保護された場所へバックアップします。共有ホーム内の OOD／OpenComposer データも対象に含めます。バックアップには認証情報が含まれるため、公開場所や Git に置かないでください。
4. 実機だけで編集した標準アプリと設定の差分を控えます。`ood_app.rb` は通常のパッケージ更新で標準版へ置き換わるため、必要な独自変更は 4.2.5 の実装に移植してください。

すでに 4.2.5 を導入した後に旧 Ansible を実行していた場合、標準ファイルが旧コピーへ戻っている可能性があります。その場合は独自差分を退避し、4.2.5 の `ondemand` パッケージを再インストールしてから、この修正を適用します。

## 更新した生成元で OOD 専用 playbook を実行する

修正したリポジトリを、対象コントローラの `/opt/oci-hpc` に配布します。`openondemand` role 全体と `playbooks/openondemand.yml` を同時に更新し、廃止した `files/var/www/ood/apps/sys/dashboard/app/apps/ood_app.rb` が配布先の生成元にも残らないようにします。`conf/queues.conf` と `/etc/ansible/hosts` は既存環境のものを保持してください。

コントローラ上で、既存 inventory と SSH 鍵を使って実行します。別の鍵パスを利用している場合は、次のコマンドを環境に合わせて変更してください。

```bash
cd /opt/oci-hpc/playbooks
ansible-playbook -i /etc/ansible/hosts \
  --private-key ~/.ssh/cluster.key openondemand.yml
```

inventory の `use_ood=true` が対象です。この playbook は既存のコントローラへ OOD role を適用し、Terraform のリソース更新や計算ノードの再構成を行いません。認証情報を含む変数をコマンドラインへ直接渡すことは避けてください。秘密値の生成・設定を伴うため、`--check` だけでは更新の成否を確認できません。

本体・Dex の更新後、認証設定・アプリを配置し、ポータル生成、Apache の構文検査、Dex／Apache の再起動を行います。最後にインストール版を確認し、NGINX アプリ設定を再生成して PUN を終了します。完了記録 `/etc/ood/config/.oci-hpc-ood-version` は、この処理が成功した後に保存します。途中で失敗した場合、修正して再実行すれば未完了の処理も再試行します。パッケージ更新が完了済みでも、完了記録がない限り PUN の終了を省略しません。

OpenComposer の checkout は v2.0.2 の指定 commit を検査し、再実行時に role が配置した manifest などの変更を保持します。異なる commit の場合は停止します。実機で独自に変更したファイルは、role が管理するファイルへの再配置と衝突しないか確認してください。

## RM の構築途中で失敗した場合は配布先も更新する

Oracle Linux 8 の新規構築で `Inspect enabled Node.js module stream` が `No matching Modules to list` で失敗した旧修正を利用している場合、修正済み ZIP を用意してください。有効ストリームがない初期状態では、この旧確認コマンドが終了コード1を返します。修正後は全ストリームから22の `[e]`（有効）を判定し、未有効なら22を有効化します。リポジトリの取得自体に失敗した場合は、そのまま停止します。

新しい検証スタックを作る場合は、修正済み ZIP をアップロードして新規デプロイします。失敗した既存スタックを再利用する場合、RM の ZIP を更新しただけでは既存コントローラの `/opt/oci-hpc/playbooks` に変更が配布されるとは限りません。今回の停止は `null_resource.cluster` の構成処理で起きていますが、playbooks の配布は別の `null_resource.controller` が担当します。

既存スタックを再利用するには、まず修正した OOD role をコントローラへ配布して、配布先の確認コマンドに `--enabled` が残っていないことを確認します。その後、同じ RM スタックの構成 ZIP を更新し、Plan で再実行対象とリソースの置換・削除を確認してから Apply します。コントローラや共有データの置換が計画された場合は、その影響と復旧方法を確認してから進めてください。既存スタックに残るリソースを、再試行のために一律で削除する必要はありません。

## ログインからジョブ完了まで確認する

更新後の期待結果を以下に示します。ローカル回帰テストの成功だけでは、この確認を完了した扱いにしません。

| 確認する操作 | 期待結果 |
| --- | --- |
| パッケージ確認 | `ondemand` が 4.2.5、`ondemand-dex` が 2.45.1。Node.js は 22 系。 |
| ポータル生成・サービス確認 | playbook が失敗せず完了する。`httpd` または `apache2` と `ondemand-dex` が active。生成失敗がログに残っていない。 |
| LDAP／静的ログイン | 利用中の認証方式でログインし、想定した OS ユーザーへ対応付けられる。 |
| ファイルとブラウザシェル | 一覧、アップロード、コピー、ダウンロードが動作する。日本語メニューとシェルを利用できる。 |
| OpenComposer | Slurm ジョブと History を OOD ヘッダー内で開ける。partition／constraint を選んで小規模ジョブを投入し、完了と履歴を確認できる。 |
| System Status | SMT の有効・無効が混在するノードで CPU Cores の値を確認する。 |
| VNC／DCV | 有効なアプリで所定のキューへ投入し、接続・終了・更新前セッションへの再接続を確認する。GPU を使う構成も確認する。 |
| role の再実行 | 同じ秘密値を再利用し、管理対象の独自設定を維持する。パッケージ変更と未完了の更新がなければ PUN を強制終了しない。 |
| `use_ood=false` | OOD role を実行せず、秘密値生成や依存追加を要求しない。 |

障害が出た場合は、機密情報を除いた失敗タスクとサービスログを保存して原因を切り分けます。認証用ファイルの内容を出力せず、保存した秘密値を復元してください。復旧が必要なら、更新前のコントローラと設定を復元し、共有データとの整合性を確認します。復旧時に新しい秘密値を失うと Web セッションの再認証が必要になります。

## 公式の修正内容と更新手順

- [4.2.5 リリース](https://github.com/OSC/ondemand/releases/tag/v4.2.5)
- [4.2 更新手順](https://osc.github.io/ood-documentation/latest/release-notes/v4.2-release-notes.html#upgrade-instructions)
- [4.1 更新時の Node.js 22 への切り替え](https://osc.github.io/ood-documentation/latest/release-notes/v4.1-release-notes.html#upgrade-instructions)
- [OIDC 暗号用秘密値の要件](https://github.com/OSC/ondemand/security/advisories/GHSA-3wm4-r2jj-43pp)
- [回帰確認台帳](REGRESSION_LEDGER.md)
