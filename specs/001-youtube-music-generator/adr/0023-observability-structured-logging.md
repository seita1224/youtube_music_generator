# ADR-0023: 観測性 = 構造化ログ(loguru)+ 実行進捗の永続化と管理UI 閲覧

- **ステータス:** Accepted
- **日付:** 2026-05-25 (改訂: 2026-07-11 — job_history / job_step_events / SSE 契約を固定)
- **決定者:** @seita
- **タグ:** backend, ops

## 背景

要件「失敗は Slack 通知のみ・自動リトライなし」に対し、何が起きていたかを後追いできる状態が必要。
1人運用・1台マシンの規模で、観測性の妥当なレベルを決める。

規模:

- 1日 1〜2 サイクル(各 10 ステップ程度)= 月数百ジョブ
- 後追いの 2 軸: 「直近サイクルで何が起きたか」「特定の動画 ID / ジャンル で何が失敗したか」

音楽専用実行では、ブラウザをリロードしても **実行 ID 単位で進捗・失敗理由を復元**できる必要がある。
メモリ上の EventBus / SSE だけでは不足し、実行単位と工程イベントの正本を DB に置く。

## 決定

### 構造化ログ

- ロギングライブラリ = **`loguru`**(API 簡潔、構造化・ローテーション・カラー標準装備、依存最小)
- フォーマット = **JSON Lines**(構造化ログ)
- 出力先 = 標準出力(コンテナ・systemd ログ) + ファイル(日次ローテーション、retention 90 日)
- 主要フィールド(全ログ共通の context):
  - `timestamp`, `level`, `event`, `job_id` / `run_id`, `cycle_id`, `genre`, `video_id`, `step`, `duration_ms`
  - LLM 呼び出し時: `provider`, `model`, `input_tokens`, `output_tokens`, `cost_estimate`
- Slack 通知は **失敗時 + 重要マイルストーン** に絞る(投稿完了・週次サイクル完了等)
- Prometheus / Grafana / OpenTelemetry は **採用しない**(1人運用にはオーバーキル、必要になれば後付け可能)

### 実行進捗の正本

- **`job_history` = 実行単位の正本。** 1 行が 1 回の実行。API の `run_id` は `job_history.id`
  - `job_name`(例: `music_generation`)、`status`(`running` / `succeeded` / `failed` / `skipped`)
  - `trigger`(`cron` | `run_now`)、`target_date`、対象 Plan への context、開始/終了、失敗理由
- **`job_step_events` = 工程イベントの正本。** `run_id` / `step` / `status` / `genre` / context / error / timestamp を保存
- 記録手順: `JobProgressRecorder` が短い独立トランザクションで `job_step_events` に書いた後、既存 EventBus へ publish する
- 音楽生成では `cycle` / `post` / `music` の running と終端(`succeeded` | `failed`)を必ず対で記録し、失敗工程が running のまま残らないようにする

### 管理UI / API

- `GET /jobs/runs` — 直近実行一覧(DB snapshot)
- `GET /jobs/runs/{run_id}/events` — 永続化された工程履歴
- `GET /jobs/stream?run_id=...` — 選択実行のライブ差分(SSE)。初期表示は DB、以後 SSE
- `/jobs?run_id=...` で対象実行を復元する
- 構造化ログの補助閲覧: `video_id` / `genre` / `step` フィルタはファイル / 将来取り込みで継続

## 結果

### 良い影響

- 構造化ログにより事後の検索・集計が容易
- 管理UI から実行履歴と工程をリロード後も追跡できる
- SSE は差分配信に留め、正本は DB のため再接続に強い
- 依存追加は loguru のみ(進捗は既存 Postgres)、運用コストが低い
- 将来 Prometheus / OpenTelemetry を追加したくなっても、構造化ログと step events があれば移行容易

### 悪い影響・トレードオフ

- メトリクス時系列(集計値の長期トレンド)は取れない
  - 緩和: 必要なメトリクス(各ステップ所要時間・成功率・トークン消費)はログ / step events から事後集計可能
- ログと step events のストレージ消費
  - 緩和: 日次ローテーション + retention 90 日(`job_history` / `job_step_events` も同方針)+ 圧縮
- EventBus と DB の二重書き込み
  - 緩和: DB 書き込み成功後にのみ publish し、正本を DB に固定

### 受容したリスク

- 高頻度のサンプリング・ダッシュボード型の運用は実現しないが、本規模では不要

## 検討した代替案

- **Prometheus + Grafana:** 1人運用にはオーバーキル、追加コンテナ管理コスト。不採用。
- **OpenTelemetry:** 分散化への布石として有用だが現時点では過剰。不採用。
- **標準 `logging` モジュール + 平文ファイル:** 後追い検索が手間、構造化なしで管理UI 統合がしにくい。不採用。
- **SSE / メモリのみの進捗:** リロードで失われる。DB 永続化を正とする。不採用。

## 関連

- ADR-0006: 日次/週次サイクル・音楽専用実行(ログの context 軸)
- ADR-0011: APScheduler・run_id・single-flight
- ../requirements.md §失敗時の挙動, §機能要件 6.1
- loguru: <https://github.com/Delgan/loguru>
