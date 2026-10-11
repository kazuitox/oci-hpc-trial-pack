# localdisk

ローカルNVMeを計算ノードのスクラッチとして構成するロールです。
OCI Block Volume用の `local-block-volume` ロールとは独立しています。

## 構成

- NVMeなし：作成処理をスキップ。
- NVMe 1台、または `log_vol=false`：GPTの第1パーティションにXFS。
- NVMe複数台、かつ `log_vol=true`：パーティションを作らず、ディスク全体を
  `vg_nvmes` のPVとして使い、`lv_nvmes` にXFSを作成。
- `redundancy=false`：対象台数に合わせたストライピング。
- `redundancy=true`：2台はRAID1、4台以上の偶数台はRAID10。
  奇数台では最後の1台をVG内の未割当PVとして残す（自動的なホットスペアではない）。
  例：3台なら2台でRAID1、5台なら4台でRAID10。
- 新規RAID1/RAID10は `--nosync` で初期同期を省略する。
  新規にXFSを作る使い捨てスクラッチを前提とし、作成後の書込みはミラーされるが、
  作成前のディスク内容は同期しない。障害復旧等での将来の同期は抑止しない。
  `localdisk_raid_nosync=false` なら従来の初期同期を有効にできる。
- LV容量は参加するPVの最小空き容量に合わせて計算する。
  RAIDではPVごとに `localdisk_raid_reserve_mib`（既定16 MiB）を確保し、
  RAIDメタデータ領域を残す。容量が異なるディスクでは未使用領域が生じる。

通常の `nvme0n1` は `nvme_path` に、`nvme1n1` などの個別ディスクは
`nvme_path` に番号を付けたパスへマウントする。LVMの場合は `nvme_path` にまとめる。
Autoscalingのinventoryでは `nvme_path=/mnt/localdisk/`。

## 再実行と既存データ

OSや別用途のマウント、別VGのPV、識別できないファイルシステムを除外する。
既存のscratch PV/VG/LVは再利用し、LVのサイズ・RAID種類は変更しない。
ファイルシステム・マウント・権限設定は再確認する。
`--nosync` は新規LVにだけ適用し、既存LVの同期を停止したりLVを再作成したりしない。

Oracle Linux 8 / util-linux 2.32.1 に対応するため、`lsblk` には未対応の
`PTTYPE` 列を要求しない。各NVMeを `blkid -p -o export` で読み取り専用で調べ、
パーティションテーブルと署名情報を補完する。`rc=2` は標準出力・標準エラーが
空で、`lsblk` の既存署名・パーティション情報と矛盾しない場合に限り未検出として扱う。
その他のエラー、曖昧な署名（`rc=8`）、不正な出力、取得結果の欠落は構築エラーとし、
空ディスクに読み替えない。

LVM対象にパーティションが残っている場合、消去や変換は行わない。
当ロールが以前作った空の `p1` が残っていても、LVM構成はエラーとして扱う。
新規の空ディスクが通常の構築対象。

## 失敗時

`autoscaling=true` かつ `compute` グループのホストでは、ロールの失敗を
`rescue` で記録し、後続のノード構築を続行する。
その他のホストは失敗を維持する。必要なら `localdisk_ignore_errors` で上書きできる。
接続不能やPlaybook自体の構文エラーは、この仕組みの対象外。

`localdisk_status` は `ready` / `unavailable` / `failed` / `planned`（check mode）。
失敗時は `localdisk_failure` と `WARNING: Local scratch setup failed` に
失敗タスク・対象・エラーを残す。任意スクラッチのfstabには `nofail` を付ける。

ジョブや後続ロールのスクラッチ利用制御は行わない。未マウント時の書込み失敗や
Boot Volumeへの書込みは許容し、別ストレージへの切替・書込み禁止は行わない。
`use_local_block_volume`、`localdisk`、`log_vol`、`redundancy` の外部設定は変更しない。

## 検証

実ディスクの構成を変更しない選別・容量計算・互換性テスト：

```sh
python3.8 -m unittest discover -s /opt/oci-hpc/playbooks/roles/localdisk/tests -v
```

`test_probe_tools.py` は `/tmp` の一時的な通常ファイルだけにGPTを作り、
インストール済みの `blkid` / `parted` の出力も検証する（root権限やloopデバイスは不要）。

初回修正時にはAnsible 2.13.13で構文、失敗時の続行範囲、NVMeなしのcheck mode、
模擬ストレージを使った新規作成・既存LV再利用・filesystem/mount失敗のフローを検証。
実NVMeの初期化・マウントはこのテストでは実行しない。

2026-09-29のBM.Standard.E6.Ax.192実機検証では、利用者による手動コマンドで
空の960GB NVMe 2台からPV/VG、894.05GiBのRAID1、XFS、`/mnt/localdisk` の
マウントまで成功した。`--nosync` 自体と今回修正後のロール全体の実機実行は未検証。
回帰テストには実機の旧lsblk出力、blkid未検出、容量値、既存LV再利用、
失敗時のノード構築続行を含める。

## このリポジトリへの取り込み

Ansible モジュールを使う参照ロールを取り込み、従来の `files/localdisk.py` によるディスク操作を置き換えた。`community.general`（コントローラ構築では4.8.1）と `ansible.posix`、ロール内の `filter_plugins/localdisk.py` を必要とする。`--nosync` の警告を独自スクリプトの stderr 判定で失敗扱いする経路は使用しない。

`localdisk`・`log_vol`・`redundancy` の入力と inventory の `nvme_path` は従来どおり。既存 LV の RAID 種類やミラー配置を詳細に検査する旧スクリプトとは異なり、参照ロールは VG/LV 名とデバイスの所属を確認して再利用し、既存 LV の形式・容量は変更しない。奇数台では最後のPVもVG内に残し、冗長LVには割り当てない。個別マウントのfstabはラベル指定からUUID指定になる。

適用時はロール全体をコントローラへ配布する。初期・動的ノードの再構成は別途必要。現在の Autoscaling inventory は `autoscaling=true` を渡し、計算ノードは `compute` グループに属する。通常構築では失敗を維持する。既存 RAID1 が未フォーマットのノードは再実行時に XFS 作成へ進めるが、対象 LV とマウント先に必要なデータがないことを確認してから実行する。実ディスクでの新規作成と再実行の成功は取り込み後に別途確認する。
