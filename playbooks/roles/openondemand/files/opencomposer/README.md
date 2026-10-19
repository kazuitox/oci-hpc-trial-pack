# OpenComposer の管理対象ファイル

`history.rb`、`slurm.rb`、`history.js` は、RIKEN-RCCS/OpenComposer v2.0.2 の commit `7af3d94b36043d8019b1639cd8e463957eb7a37e` を原本とし、キャンセル状態を分離する変更だけを加えています。原本のパスは順に `lib/history.rb`、`lib/schedulers/slurm.rb`、`public/history.js` です。ライセンスは同梱の `LICENSE`（MIT）を参照してください。

`tasks/opencomposer.yml` は固定リビジョンを確認してからこれらを配布し、`run.rb` の `JOB_STATUS` に `canceled` を登録します。変更時は Passenger の再起動を要求します。履歴 DB の形式は変更せず、新規レコードの `_status` に `CANCELED` を保存します。過去履歴の移行処理はありません。

OpenComposer の固定バージョンを更新する場合は、これらのファイルと `history.erb` を新しい原本と比較し、キャンセル状態の扱いを確認してください。関連テストは `tests/test_opencomposer_history.py` と `tests/test_opencomposer_canceled_status.py` です。後者の Ruby 実行テストには Ruby 2.7 以上と `sqlite3`・`minitest` gem が必要です。
