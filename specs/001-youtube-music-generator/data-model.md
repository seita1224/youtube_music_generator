# Data Model — YouTube 音楽投稿自動化システム

> PostgreSQL 16 + pgvector 拡張。 SQLAlchemy 2.x + Alembic で実装。 ADR-0010 / ADR-0011 / ADR-0012 / ADR-0023 / ADR-0024 / ADR-0025 / ADR-0028 / ADR-0031 / ADR-0032 / ADR-0006 を反映。

## ER 構造(概念図)

```text
genres (master)
  ▲
  │ FK genre
  │
plans ────── posts ────── audio_tracks
  │ FK plan_id│ FK post_id
  │           ├── music_job_id ──→ gpu_jobs
  │           ├── image_job_id ──→ gpu_jobs
  │           └── youtube_video_id ──→ videos
  │
plan_metric_snapshot

videos ────── analytics_daily (時系列)
  │
  └── comments

oauth_credentials       (Fernet 暗号化)
usage_log               (LLM 呼び出し追跡)
model_pricing           (LLM 単価表)
job_history             (実行単位 = API run_id)
  └── job_step_events   (工程イベント正本)
dryrun_outputs          (state 管理)
app_state               (scheduler_enabled 等)
```

## Enums(PostgreSQL native ENUM)

```sql
CREATE TYPE plan_cycle AS ENUM ('daily', 'weekly');
CREATE TYPE plan_status AS ENUM (
  'generated', 'approved', 'executing', 'music_generated', 'completed', 'failed'
);
CREATE TYPE post_status AS ENUM (
  'pending', 'generating', 'music_generated', 'generated', 'posting', 'posted', 'failed'
);
-- job_history.trigger は PG ENUM ではなく TEXT + CHECK (ADR-0036)。
-- 許容値: NULL | 'cron' | 'run_now' (music_generation 以外は NULL)。
CREATE TYPE gpu_job_type AS ENUM ('music', 'image');
CREATE TYPE gpu_job_status AS ENUM ('queued', 'running', 'succeeded', 'failed');
CREATE TYPE dryrun_state AS ENUM ('pending', 'approved', 'rejected', 'auto_expired', 'posted');
CREATE TYPE error_category AS ENUM ('transient', 'recoverable', 'fatal', 'compliance', 'quality');
CREATE TYPE acoustid_status AS ENUM ('not_checked', 'clear', 'hit', 'api_error');
CREATE TYPE youtube_privacy_status AS ENUM ('public', 'unlisted', 'private', 'deleted');
CREATE TYPE llm_provider AS ENUM ('openai', 'anthropic', 'ollama');
CREATE TYPE llm_auth_mode AS ENUM ('api_key', 'codex_oauth');
```

> 音楽専用実行(ADR-0006): Plan `approved → executing → music_generated|failed`、
> Post `pending → generating → music_generated|failed`。
> `completed` / `generated` / `posting` / `posted` はフル日次(動画・投稿)経路用に残す。

## Tables

### `genres` — ジャンル辞書 (ADR-0033)

```sql
CREATE TABLE genres (
  name              TEXT PRIMARY KEY,          -- 'lo-fi hip-hop' 等
  display_name      TEXT NOT NULL,             -- 'Lo-Fi Hip Hop' (タイトル用)
  bpm_min           INT,
  bpm_max           INT,
  description       TEXT NOT NULL,             -- LLM プロンプト用の特徴説明
  role              TEXT NOT NULL,             -- 'main' / 'extension' / 'experiment'
  enabled           BOOLEAN NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (bpm_min IS NULL OR bpm_max IS NULL OR bpm_min <= bpm_max)
);

-- 初期 6 件 seed
INSERT INTO genres (name, display_name, bpm_min, bpm_max, description, role) VALUES
  ('lo-fi hip-hop', 'Lo-Fi Hip Hop', 70, 90, 'chill / nostalgic, BPM 70-90', 'main'),
  ('chillhop',      'Chillhop',      80, 95, 'lo-fi の隣接、 jazz 寄り', 'main'),
  ('ambient',       'Ambient',       NULL, NULL, '構造単純、 睡眠 / 瞑想用、 ACE-Step 得意', 'main'),
  ('synthwave',     'Synthwave',     80, 110, 'retro / 80s', 'extension'),
  ('piano solo',    'Piano Solo',    NULL, NULL, 'リラックス / 勉強用', 'extension'),
  ('future garage', 'Future Garage', 130, 140, 'atmospheric、 実験枠', 'experiment');
```

### `plans` — DailyPlan / WeeklyPlan (ADR-0032)

```sql
CREATE TABLE plans (
  id                    UUID PRIMARY KEY,         -- UUID v7(時系列ソート)
  cycle                 plan_cycle NOT NULL,
  target_date           DATE,                     -- DailyPlan の場合
  target_week_start     DATE,                     -- WeeklyPlan の場合(月曜)
  payload               JSONB NOT NULL,           -- Pydantic model_dump
  rationale             TEXT NOT NULL,
  status                plan_status NOT NULL DEFAULT 'generated',
  llm_provider          llm_provider NOT NULL,
  llm_model             TEXT NOT NULL,
  llm_prompt_version    TEXT NOT NULL,            -- 'planner/system_v1' 等
  llm_cost_usd          NUMERIC(10, 6) NOT NULL DEFAULT 0,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_at           TIMESTAMPTZ,
  CHECK (
    (cycle = 'daily'  AND target_date       IS NOT NULL AND target_week_start IS NULL) OR
    (cycle = 'weekly' AND target_week_start IS NOT NULL AND target_date       IS NULL)
  )
);

CREATE INDEX idx_plans_target_date       ON plans (target_date)       WHERE cycle = 'daily';
CREATE INDEX idx_plans_target_week_start ON plans (target_week_start) WHERE cycle = 'weekly';
CREATE INDEX idx_plans_status            ON plans (status);
```

### `posts` — DailyPlan.posts の個別投稿レコード (ADR-0032)

```sql
CREATE TABLE posts (
  id                    UUID PRIMARY KEY,
  plan_id               UUID NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  position              SMALLINT NOT NULL,        -- DailyPlan.posts[] index (0 or 1)
  genre                 TEXT NOT NULL REFERENCES genres(name),
  payload               JSONB NOT NULL,           -- DailyPost dump
  status                post_status NOT NULL DEFAULT 'pending',
  music_job_id          UUID REFERENCES gpu_jobs(id),
  image_job_id          UUID REFERENCES gpu_jobs(id),
  final_title           TEXT,                     -- directive parse 後
  final_description     TEXT,
  thumbnail_uri         TEXT,                     -- fsspec URI
  video_uri             TEXT,                     -- fsspec URI(投稿用 mp4)
  youtube_video_id      TEXT UNIQUE,
  scheduled_at          TIMESTAMPTZ,              -- JST 投稿予定
  posted_at             TIMESTAMPTZ,
  retention_24h         NUMERIC(5, 2),            -- 24h 後 retention %
  views_24h             INT,
  error_category        error_category,
  error_message         TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (plan_id, position)
);

CREATE INDEX idx_posts_status            ON posts (status);
CREATE INDEX idx_posts_youtube_video_id  ON posts (youtube_video_id);
CREATE INDEX idx_posts_genre             ON posts (genre);
CREATE INDEX idx_posts_posted_at         ON posts (posted_at DESC);
```

### `plan_metric_snapshot` — 改善計画 LLM 入力の生コピー (ADR-0032)

```sql
CREATE TABLE plan_metric_snapshot (
  plan_id               UUID PRIMARY KEY REFERENCES plans(id) ON DELETE CASCADE,
  metric_window_start   DATE NOT NULL,
  metric_window_end     DATE NOT NULL,
  metrics               JSONB NOT NULL,           -- LLM に渡した analytics 生 JSON
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (metric_window_start <= metric_window_end)
);
```

### `videos` — 投稿済み動画メタ

```sql
CREATE TABLE videos (
  id                       UUID PRIMARY KEY,
  youtube_video_id         TEXT NOT NULL UNIQUE,
  post_id                  UUID UNIQUE REFERENCES posts(id) ON DELETE SET NULL,
  genre                    TEXT NOT NULL REFERENCES genres(name),
  title                    TEXT NOT NULL,
  description              TEXT NOT NULL,
  duration_sec             INT NOT NULL,
  privacy_status           youtube_privacy_status NOT NULL DEFAULT 'public',
  contains_synthetic_media BOOLEAN NOT NULL,
  posted_at                TIMESTAMPTZ NOT NULL,
  thumbnail_uri            TEXT NOT NULL,
  -- 投稿後の Content ID チェック(週次取得)
  content_id_status        TEXT,                  -- null / 'clear' / 'claimed'
  content_id_checked_at    TIMESTAMPTZ,
  CHECK (contains_synthetic_media = TRUE)        -- ADR-0020 必須(DB レベルで保証)
);

CREATE INDEX idx_videos_genre            ON videos (genre);
CREATE INDEX idx_videos_posted_at        ON videos (posted_at DESC);
CREATE INDEX idx_videos_privacy_status   ON videos (privacy_status);
```

### `audio_tracks` — 6 トラックの個別メタ

```sql
CREATE TABLE audio_tracks (
  id                   UUID PRIMARY KEY,
  post_id              UUID NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  position             SMALLINT NOT NULL,        -- 0..5 (6 トラック)
  audio_uri            TEXT NOT NULL,            -- fsspec URI
  duration_sec         INT NOT NULL,
  bpm                  INT,
  music_key            TEXT,                     -- 'C major' 等
  subtheme             TEXT,
  acoustid_status      acoustid_status NOT NULL DEFAULT 'not_checked',
  acoustid_response    JSONB,
  fingerprint_hash     TEXT,                     -- Chromaprint hash
  generated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  regenerated_count    SMALLINT NOT NULL DEFAULT 0,  -- AcoustID NG re-gen 回数
  UNIQUE (post_id, position),
  CHECK (position BETWEEN 0 AND 5)
);

CREATE INDEX idx_audio_tracks_acoustid_status ON audio_tracks (acoustid_status);
CREATE INDEX idx_audio_tracks_fingerprint     ON audio_tracks (fingerprint_hash);
```

### `gpu_jobs` — GPU worker への job (ADR-0031)

```sql
CREATE TABLE gpu_jobs (
  id                UUID PRIMARY KEY,
  job_type          gpu_job_type NOT NULL,
  status            gpu_job_status NOT NULL DEFAULT 'queued',
  request_payload   JSONB NOT NULL,             -- prompt, duration_sec, output_uri 等
  output_uri        TEXT,                       -- fsspec URI(成功時)
  vram_peak_mb      INT,
  duration_ms       INT,
  error_message     TEXT,
  worker_endpoint   TEXT NOT NULL,              -- GPU_WORKER_BASE_URL の snapshot
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ
);

CREATE INDEX idx_gpu_jobs_status      ON gpu_jobs (status);
CREATE INDEX idx_gpu_jobs_created_at  ON gpu_jobs (created_at DESC);
```

### `dryrun_outputs` — dryrun ライフサイクル (ADR-0025)

```sql
CREATE TABLE dryrun_outputs (
  id                UUID PRIMARY KEY,
  post_id           UUID NOT NULL UNIQUE REFERENCES posts(id) ON DELETE CASCADE,
  state             dryrun_state NOT NULL DEFAULT 'pending',
  video_uri         TEXT NOT NULL,              -- ローカル再生用 fsspec URI
  reject_reason     TEXT,                       -- state='rejected' 時
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_at       TIMESTAMPTZ,                -- approved/rejected
  auto_expired_at   TIMESTAMPTZ,                -- pending 7 日後
  posted_at         TIMESTAMPTZ                 -- approved → posted 完了時
);

CREATE INDEX idx_dryrun_outputs_state ON dryrun_outputs (state);
CREATE INDEX idx_dryrun_outputs_created_at ON dryrun_outputs (created_at);
```

### `oauth_credentials` — Fernet 暗号化トークン (ADR-0012)

```sql
CREATE TABLE oauth_credentials (
  id                       UUID PRIMARY KEY,
  service                  TEXT NOT NULL,        -- 'youtube'
  channel_id               TEXT,                 -- YouTube channel ID
  access_token_encrypted   BYTEA NOT NULL,       -- Fernet ciphertext
  refresh_token_encrypted  BYTEA NOT NULL,
  scopes                   TEXT[] NOT NULL,
  expires_at               TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (service, channel_id)
);
```

### `llm_provider_secrets` — LLM API key (Fernet, ADR-0019 / ADR-0012)

```sql
CREATE TABLE llm_provider_secrets (
  provider             TEXT PRIMARY KEY,          -- 'openai' | 'anthropic' のみ
  api_key_encrypted    BYTEA NOT NULL,            -- Fernet ciphertext (write-only)
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (provider IN ('openai', 'anthropic'))
);
```

実行時の API key 解決順位: **環境変数非空 > 本テーブル > none**。
Ollama は対象外。 GET API は `credential_source` / `configured` のみ返し、平文・マスクは出さない。

### `usage_log` — LLM 呼び出し記録 (ADR-0024)

```sql
CREATE TABLE usage_log (
  id                  UUID PRIMARY KEY,
  provider            llm_provider NOT NULL,
  auth_mode           llm_auth_mode,
  model               TEXT NOT NULL,
  prompt_tokens       INT NOT NULL DEFAULT 0,
  cached_tokens       INT NOT NULL DEFAULT 0,    -- prompt caching hit
  completion_tokens   INT NOT NULL DEFAULT 0,
  cost_usd            NUMERIC(10, 6) NOT NULL DEFAULT 0,
  duration_ms         INT,
  context_type        TEXT,                      -- 'planner' / 'finisher' / 'analyzer' 等
  context_id          UUID,                      -- plan_id / post_id 等(任意)
  prompt_version      TEXT,                      -- ADR-0033 バージョン
  error_message       TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_usage_log_created_at ON usage_log (created_at DESC);
CREATE INDEX idx_usage_log_context    ON usage_log (context_type, context_id);
CREATE INDEX idx_usage_log_provider   ON usage_log (provider, model);
```

### `model_pricing` — LLM 単価表 (ADR-0024)

```sql
CREATE TABLE model_pricing (
  provider          llm_provider NOT NULL,
  model             TEXT NOT NULL,
  input_per_1m_usd  NUMERIC(10, 4) NOT NULL,    -- per 1M tokens
  cached_per_1m_usd NUMERIC(10, 4),             -- prompt caching 割引
  output_per_1m_usd NUMERIC(10, 4) NOT NULL,
  effective_from    DATE NOT NULL,
  source_url        TEXT,
  PRIMARY KEY (provider, model, effective_from)
);
```

### `analytics_daily` — 動画 × 日次の指標 (ADR-0021)

```sql
CREATE TABLE analytics_daily (
  youtube_video_id     TEXT NOT NULL REFERENCES videos(youtube_video_id) ON DELETE CASCADE,
  metric_date          DATE NOT NULL,
  views                INT NOT NULL DEFAULT 0,
  estimated_minutes_watched  NUMERIC(10, 2) NOT NULL DEFAULT 0,
  average_view_duration_sec  INT,
  retention_pct        NUMERIC(5, 2),
  impressions          INT,
  ctr_pct              NUMERIC(5, 2),
  traffic_sources      JSONB,                    -- {source: views} 等
  fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (youtube_video_id, metric_date)
);

CREATE INDEX idx_analytics_daily_metric_date ON analytics_daily (metric_date DESC);
```

### `comments` — YouTube コメント取得結果 (ADR-0021)

```sql
CREATE TABLE comments (
  id                UUID PRIMARY KEY,
  youtube_video_id  TEXT NOT NULL REFERENCES videos(youtube_video_id) ON DELETE CASCADE,
  youtube_comment_id TEXT NOT NULL UNIQUE,
  author            TEXT,
  text              TEXT NOT NULL,
  like_count        INT NOT NULL DEFAULT 0,
  published_at      TIMESTAMPTZ NOT NULL,
  fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  sentiment         TEXT,                       -- 週次 LLM 分析結果(任意)
  topic_tags        TEXT[]                      -- 同上
);

CREATE INDEX idx_comments_video_id     ON comments (youtube_video_id);
CREATE INDEX idx_comments_published_at ON comments (published_at DESC);
```

### `job_history` — 実行単位(API `run_id`) (ADR-0011, ADR-0023, ADR-0028)

```sql
CREATE TABLE job_history (
  id                UUID PRIMARY KEY,           -- API の run_id
  job_name          TEXT NOT NULL,              -- 'music_generation' / 'weekly_cycle' / 'dryrun_retention' 等
  status            TEXT NOT NULL,              -- 'running' / 'succeeded' / 'failed' / 'skipped'
  trigger           TEXT,                       -- music_generation: 'cron' | 'run_now' (他は NULL)
  target_date       DATE,                       -- music_generation の対象日(JST)
  context_type      TEXT,                       -- 'plan' / 'post' 等
  context_id        UUID,                       -- 対象 Plan.id 等
  error_category    error_category,
  error_message     TEXT,
  started_at        TIMESTAMPTZ NOT NULL,
  finished_at       TIMESTAMPTZ,
  duration_ms       INT,
  CONSTRAINT ck_job_history_trigger
    CHECK (trigger IS NULL OR trigger IN ('cron', 'run_now'))
);

CREATE INDEX idx_job_history_job_name   ON job_history (job_name);
CREATE INDEX idx_job_history_started_at ON job_history (started_at DESC);
CREATE INDEX idx_job_history_status     ON job_history (status);
CREATE INDEX idx_job_history_target_date ON job_history (target_date DESC);
CREATE INDEX idx_job_history_job_name_started_at ON job_history (job_name, started_at DESC);

-- music_generation の single-flight(ADR-0011): cron / run_now 共有
CREATE UNIQUE INDEX uq_job_history_music_generation_running
  ON job_history (job_name)
  WHERE job_name = 'music_generation' AND status = 'running';
```

### `job_step_events` — 工程イベント正本 (ADR-0023)

```sql
CREATE TABLE job_step_events (
  id                UUID PRIMARY KEY,
  run_id            UUID NOT NULL REFERENCES job_history(id) ON DELETE CASCADE,
  step              TEXT NOT NULL,              -- 'cycle' / 'post' / 'music' 等
  status            TEXT NOT NULL,              -- 'running' / 'succeeded' / 'failed'
  genre             TEXT,                       -- genres.name 参照(任意)
  context_type      TEXT,                       -- 'plan' / 'post' 等
  context_id        UUID,
  error_category    error_category,
  error_message     TEXT,
  payload           JSONB,                      -- 補助 context(任意)
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_job_step_events_run_id     ON job_step_events (run_id, created_at);
CREATE INDEX idx_job_step_events_step       ON job_step_events (step);
CREATE INDEX idx_job_step_events_status     ON job_step_events (status);
```

### `app_state` — グローバル状態 (ADR-0031)

```sql
CREATE TABLE app_state (
  key               TEXT PRIMARY KEY,
  value             JSONB NOT NULL,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 初期 seed
INSERT INTO app_state (key, value) VALUES
  ('scheduler_enabled', 'false'::jsonb),           -- ADR-0031: reboot 後は手動 enable
  ('llm_provider', '"ollama"'::jsonb),             -- ADR-0019: clean install 既定
  ('llm_auth_mode', '"api_key"'::jsonb),
  ('llm_model', '"qwen2.5:3b"'::jsonb),           -- ADR-0019: ollama 既定モデル
  ('monthly_budget_usd', '50'::jsonb),             -- ADR-0024
  ('dryrun_enabled', 'true'::jsonb);               -- 初期は dryrun 推奨
```

### `audit_log` — panic-stop 等の運用操作監査 (ADR-0028, ADR-0031)

```sql
CREATE TABLE audit_log (
  id                UUID PRIMARY KEY,
  actor             TEXT NOT NULL,              -- 'system' / 'admin' / 'panic_stop' 等
  action            TEXT NOT NULL,              -- 'panic_stop_invoked' / 'video_set_private' 等
  target_type       TEXT,                       -- 'video' / 'post' / 'plan' 等
  target_id         TEXT,
  payload           JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_action     ON audit_log (action);
CREATE INDEX idx_audit_log_created_at ON audit_log (created_at DESC);
```

## ベクタ拡張(将来 RAG 用、 ADR-0010)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- 例: コメント / プロンプト等のベクトル化テーブル(将来追加)
-- CREATE TABLE comment_embeddings (
--   comment_id UUID PRIMARY KEY REFERENCES comments(id) ON DELETE CASCADE,
--   embedding  vector(1536) NOT NULL,
--   model      TEXT NOT NULL,
--   created_at TIMESTAMPTZ NOT NULL DEFAULT now()
-- );
```

## Validation Rules(ADR-0032 + Pydantic 側)

DB レベル制約と Pydantic レベル制約の責務分担:

| ルール | DB | Pydantic |
| --- | --- | --- |
| `videos.contains_synthetic_media = TRUE` | CHECK constraint | model_validator |
| `genres.name` 辞書照合 | FK constraint(posts.genre) | field_validator with context |
| `plans.cycle` discriminator | CHECK constraint | Literal["daily"/"weekly"] |
| DailyPlan.posts の長さ 1〜2 | アプリ層 | Field(min_length=1, max_length=2) |
| WeeklyPlan.genre_distribution 合計 1.0 | アプリ層 | field_validator |
| `mood` ≥ 4 字 / `visual_direction` ≥ 10 字 | アプリ層 | Field(min_length=...) |
| `rationale` ≥ 20 字 / 50 字 | アプリ層 | Field(min_length=...) |
| BPM 範囲整合 | CHECK constraint(genres) | アプリ層 |

## Alembic マイグレーション戦略 (ADR-0031)

- バージョン: `alembic/versions/<rev>_*.py`
- down migration は書かない(ADR-0031)、 後退時は手動 SQL
- `make migrate` 実行前に自動 `pg_dump`(`backups/pre-migrate-<ts>.sql`)
- 初期マイグレーション(`001_initial.py`): 全 ENUM + 全テーブル + seed(`genres` × 6, `app_state` 6 件 [llm_provider/llm_auth_mode/llm_model 含む], `model_pricing` 初期単価)
- LLM credential / model seed (`005_llm_provider_secrets.py`): `llm_provider_secrets` テーブル追加。 既存 DB 向けに `llm_model` を **未設定時のみ** `qwen2.5:3b` で seed (`ON CONFLICT DO NOTHING`)
- LLM 既定修復 (`006_llm_defaults_repair.py`): 先に `codex_oauth` → `api_key`。 その後 001 openai + 005 qwen + api_key の三重一致のみ `ollama` へ修復 (`openai + qwen + codex_oauth` も coherent な `ollama + qwen + api_key` へ到達)。 **意図的な provider/model 選択は上書きしない**
- **down migration**: 運用では書かない (ADR-0031)。 secrets を含む行の downgrade は復元不可のため禁止
- **FERNET_KEY ローテーション**: 再暗号化パスが無い限り実施しない (本スコープ外)
- 音楽生成実行基盤(`003_music_generation_jobs.py`): `plan_status` / `post_status` に `music_generated`、`job_history.trigger` (TEXT+CHECK) / `target_date`、`job_step_events`、music_generation single-flight partial UNIQUE
- スキーマ整合(`004_job_history_schema_align.py`): `trigger` NULL 許容、`job_step_events.payload`、`idx_job_history_target_date` / step・status / `(job_name, started_at DESC)` (ADR-0036)

## インデックス戦略まとめ

- 時系列クエリ: `posts.posted_at DESC` / `analytics_daily.metric_date DESC` / `usage_log.created_at DESC` / `audit_log.created_at DESC`
- 状態フィルタ: `posts.status` / `dryrun_outputs.state` / `gpu_jobs.status` / `videos.privacy_status` / `job_history.status`
- 実行追跡: `job_history.id(=run_id)` / `job_step_events.run_id` / single-flight partial UNIQUE
- 外部 ID 検索: `posts.youtube_video_id` / `audio_tracks.fingerprint_hash`
- 多次元: `usage_log.(context_type, context_id)`(コスト集約用)

## Retention / Storage 戦略 (ADR-0022, ADR-0025, ADR-0026)

| データ | 保存先 | 寿命 |
| --- | --- | --- |
| `plans` / `posts` / `plan_metric_snapshot` | DB | 永続(集計用) |
| `audio_tracks` メタ | DB | 永続 |
| 音楽ファイル(audio_uri 先) | fsspec(初期 file://) | 永続 |
| サムネファイル | fsspec | 永続 |
| 動画ファイル(投稿用) | fsspec | posted 後 N=30 日でローカル削除(将来安価ストレージへ) |
| 動画ファイル(dryrun) | fsspec | state=approved/rejected → 即削除、 pending → 7 日後 auto_expired + 削除 |
| `usage_log` / `model_pricing` | DB | 永続 |
| `analytics_daily` / `comments` | DB | 永続(コスト軽微) |
| `job_history` / `job_step_events` | DB | 90 日 retention(別ジョブで削除、 cascade) |
| `audit_log` | DB | 永続(コンプラ監査) |
| Fernet 鍵 | `.env`(host) | バックアップ対象外(ADR-0026 防衛線) |

## Open Questions(将来 ADR 候補)

- analytics_daily の retention 期間延長 vs 月次集約への移管(運用 1 年後)
- `comments.sentiment` のモデル選定(現状 NULL 許容)
- video ファイルの長期アーカイブ先(R2 / B2 / Glacier)
- pgvector を使った週次「過去 plan 類似検索」の本実装
