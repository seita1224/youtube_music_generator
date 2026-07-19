# ADR-0022: ストレージ抽象化 = `fsspec`

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, infra, ops

## 背景

要件「保存先は切り替え可能(ローカルFS / S3 等を抽象化)」の具体実装。
保管対象:

- 楽曲ファイル(WAV/FLAC)= 30〜180MB / 投稿、**永続保存**
- 中間 mix(連結後音声)= 30〜100MB / 投稿、**永続保存**
- サムネ画像(PNG/JPG)= 1〜5MB、**永続保存**
- 動画ファイル(MP4)= 100〜300MB / 投稿、**ライフサイクル管理**(投稿後 N 日で削除 or 移送、ADR-0003)
- 副次データ(波形メタ・生成ログ等)

年間規模(ADR-0004 の 1日1〜2本):

- 楽曲・サムネ・中間 mix: 年間 60〜200GB
- 動画ファイル: 年間 70〜200GB(ライフサイクル運用で大幅削減可能)

将来的に Backblaze B2 / Cloudflare R2 / S3 等への移行も視野に入れる必要がある。

## 決定

- **`fsspec`** を採用してファイルシステム操作を統一インターフェースに抽象化
- 設定: 環境変数 `STORAGE_URL` で切替
  - 例: `STORAGE_URL=file:///var/yt-music/`
  - 例: `STORAGE_URL=s3://yt-music/data/`(R2 / B2 / MinIO も S3 互換 protocol で同じ)
- `fsspec` の `AbstractFileSystem` を内包する **`StorageAdapter`** を `app/storage/` に実装
  - メソッド: `put`, `get`, `open`, `delete`, `exists`, `list`, `presigned_url`(クラウド時)
  - 楽曲・サムネ・動画それぞれにパス命名規則を定義(例: `audio/{year}/{month}/{video_id}/{track_id}.wav`)
- ストレージライフサイクル(動画 N 日後削除)は **StorageAdapter のメソッド** として実装し、APScheduler の別ジョブで定期実行(ADR-0011)
- URL 生成:
  - ローカル(`file://`): FastAPI で静的配信、`/files/{path}` 経由でブラウザに返す
  - クラウド(`s3://`): `presigned_url` を発行、管理UI に短期 URL を返す
- バックアップ方針:
  - 楽曲・サムネ: rclone(別マシン / 別クラウド)で日次同期(ADR は別途)
  - 動画: 投稿後削除運用なので長期バックアップ対象外

## 結果

### 良い影響

- Python エコシステムでのデファクト、HuggingFace datasets / pandas / dask とも整合
- ローカル開発 ↔ クラウド本番 / 別バックアップ先 の切替が **設定変更だけ** で済む
- マウントオプション、glob、open(read/write)、URL 生成まで統一 API
- 将来 S3 互換クラウドへの移行コストが低い

### 悪い影響・トレードオフ

- 一部の特殊操作(presigned URL の有効期限制御等)はバックエンド固有 → アダプタ層で吸収
- `fsspec` の各 protocol(s3fs, gcsfs 等)は別パッケージ → 必要なものだけ依存追加
- ストリーム転送・マルチパートアップロードは内部仕様に依存
  - 緩和: 動画 100〜300MB なら一括 put でも問題なし

### 受容したリスク

- 将来 fsspec の API が破壊的変更を起こす可能性
  - 対策: アダプタ層で吸収、メジャーバージョン固定

## 検討した代替案

- **自前 Storage インターフェース + `shutil` + `boto3`:** 必要な操作のみ薄く定義、依存最小だが、エコシステムから外れて将来の拡張(glob・walk 等)が手間。不採用。
- **ローカルFS のみ実装、将来クラウド時に追加:** 「切替可能」要件と整合せず、移行コストを後払いする形になる。不採用。
- **`smart_open` 単独:** ファイル単位の URL 抽象は得意だがディレクトリ操作のカバレッジが狭い。fsspec の下位互換的位置付け。不採用。

## 関連

- ADR-0003: 動画フォーマット(動画サイズ)
- ADR-0004: 投稿規模(ストレージ消費の試算)
- ADR-0011: スケジューラ(ライフサイクルジョブ)
- ../requirements.md §データ永続化, §ストレージライフサイクル
- fsspec: <https://filesystem-spec.readthedocs.io/>
