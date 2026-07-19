# ADR-0036: job_history.trigger は TEXT + CHECK

- **ステータス:** Accepted
- **日付:** 2026-07-11
- **決定者:** @seita
- **タグ:** backend / infra

## 背景

音楽生成実行基盤 (migration 003) は `job_history.trigger` を `TEXT NOT NULL` +
`CHECK (trigger IN ('cron', 'run_now'))` と `server_default='cron'` で追加した。
一方 data-model 初稿は `job_trigger` ENUM と NULL 可を記載しており、実装と文書が食い違った。

003 は既にローカルへ適用済みの可能性があるため、 ENUM への型変更やデータ破棄は避け、
前進マイグレーションで文書と実装を揃える必要がある。

## 決定

1. **`job_trigger` ENUM は作らない。** `trigger` は `TEXT` + CHECK を正とする。
2. **NULL を許容する。** `music_generation` 以外の行は `trigger = NULL`。
   CHECK は `trigger IS NULL OR trigger IN ('cron', 'run_now')`。
3. **`server_default` は外す。** music 経路は INSERT 時に必ず明示する。
4. **`job_step_events.payload` (JSONB, nullable)** と一覧用 index
   (`target_date` / step / status / `(job_name, started_at DESC)`) を 004 で追加する。

## 結果

### 良い影響

- 003 適用済み DB を壊さず整合できる
- 非 music ジョブ履歴に誤った `cron` ラベルが付かない
- ENUM 追加・付け替えの運用コストを避けられる

### 悪い影響・トレードオフ

- PG カタログ上の ENUM 一覧には `job_trigger` が無い (アプリ / CHECK が正)
- 003 単体適用時点では一時的に NOT NULL + default のまま (004 で解消)

### 受容したリスク

- アプリが music 行で `trigger` を省略すると NULL になり得る → MusicRunService が常に明示

## 検討した代替案

- **`job_trigger` ENUM へ ALTER:** 003 適用済み環境で型再作成が必要。不採用。
- **data-model だけ NOT NULL に合わせる:** 非 music 行の意味歪みを残す。不採用。

## 関連

- ADR-0011: scheduler / music_generation single-flight
- ADR-0023: job_history / job_step_events
- data-model.md: `job_history` / `job_step_events`
- alembic: `003_music_generation_jobs.py` / `004_job_history_schema_align.py`
