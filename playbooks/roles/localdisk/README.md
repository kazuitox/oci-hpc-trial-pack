# localdisk ロール

このロールは、ホストの内蔵 NVMe を一時スクラッチ領域に構成します。`use_local_block_volume` は別機能の一時 Block Volume 用であり、このロールの有効条件には使いません。マウント先は `nvme_path`、所有者は `ansible_user`、グループは `privilege_group_name`、権限は `0775` です。

## 構成

- NVMe がない場合は何もしません。1 台、または `log_vol=false` の場合は従来どおり各ディスクに GPT の第 1 パーティションと XFS を作り、`locscratch<番号>` のラベルで個別にマウントします。既存の正しい XFS は再利用します。
- `log_vol=true` で複数台ある場合、**パーティションを作らずディスク全体**を `vg_nvmes` の PV にします。LV は `lv_nvmes`、ファイルシステムは XFS です。`redundancy=true` では 2 台は RAID1、4 台以上の偶数台は RAID10 です。`redundancy=false` ではストライピングします。奇数台の最後の 1 台は未使用にし、ホットスペアにはしません。未使用ディスクは実行結果の `unused` に記録します。
- 新規 VG の PE は 4 MiB です。新規 LV の容量は選択した各 PV の実際の PE 数と空き PE 数から求め、各 PV に 4 PE（PE が 4 MiB なら 16 MiB）を残します。RAID1 は 1 台分、RAID10 は台数の半分、ストライピングは全台数分を使います。既存 LV の容量・配置・同期状態は変更しません。

`localdisk_raid_nosync` は既定で `true` です。新規 RAID1 / RAID10 にだけ `lvcreate --nosync` を付け、空の使い捨て LV の初期全域同期を省略します。**作成前からあるデータの複製は行われません。** 作成後に書いたデータはミラーされます。`false` なら通常の初期同期を行います。既存 LV の resync を停止したり作り直したりしません。初期内容の保持や初回同期が必要な用途では `false` を指定してください。このオプション付きのロール全体は実機未検証です。

## 安全性と障害時の扱い

書き込み前に `lsblk`（PTTYPE 列は使用しない）、`blkid -p`、読み取り専用の `wipefs --no-act`、LVM の JSON レポート、`findmnt` を照合します。`blkid` の終了コード 2 は、出力と他の検査も空のときだけ空ディスクの根拠にします。使用中ディスク、別 VG、未知の署名・パーティション、想定外の LV、マウント先に残るファイルは自動変更せず失敗します。既存 VG だけがある場合は LV 作成から再開できます。VG に不足している空ディスクは、LV がまだない場合に限り追加します。既存の空パーティションや孤立 PV は由来を証明できないため、手動確認が必要です。強制初期化や署名消去は行いません。

Autoscaling の計算ノード（inventory の `role=compute`、かつ `initial` が真ではない）でこのロールの構成・フォーマット・マウント・権限設定が失敗した場合、処理名、対象 NVMe、エラーを警告として出して後続のノード構築を続けます。初期構築の計算ノードとそれ以外のホストでは失敗を返します。後続の Slurm 設定失敗はこのロールでは捕捉しません。スクラッチのマウントがないノードでジョブがスクラッチを使う場合の失敗や Boot Volume への書き込みは、ジョブ側で判別する必要があります。

検証は `python3 -m unittest discover -s playbooks/roles/localdisk/tests -p 'test_*.py'` と、ロールの `tasks/common.yml` を読み込む playbook の `ansible-playbook --syntax-check` で行います。Ansible の失敗継続モックを実行する場合は、PyYAML のある Python から `LOCALDISK_ANSIBLE_PLAYBOOK` に `ansible-playbook` の絶対パスを指定し、必要に応じて `LOCALDISK_ANSIBLE_COLLECTIONS` に `ansible.posix` の collection パスを指定します。テストはコマンド応答をモック化し、実ディスクへ書き込みません。実機で確認された手動 RAID1 作成手順は、このロール全体と `--nosync` の実機検証を意味しません。
