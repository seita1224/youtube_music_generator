"""initial schema: enums + 16 tables + seed (T016/T017/T018).

全 ENUM / 全テーブル / seed (genres x6, app_state x4, model_pricing) を展開する
初期マイグレーション。 data-model.md (PostgreSQL 16 + pgvector) と完全整合。

ADR-0010 (pgvector) / ADR-0012 (Fernet) / ADR-0020 (containsSyntheticMedia) /
ADR-0024 (LLM コスト) / ADR-0031 (デプロイ) / ADR-0032 (plan スキーマ) /
ADR-0033 (初期ジャンル) を反映。

注: ADR-0031 は本番運用で down migration を書かない方針だが、 初期スキーマの
開発時ロールバック / CI 検証用に downgrade も実装する。

Revision ID: 001_initial
Revises:
Create Date: 2026-06-01

"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- ENUM 定義 (data-model.md §Enums) ---
_ENUMS: dict[str, tuple[str, ...]] = {
    "plan_cycle": ("daily", "weekly"),
    "plan_status": ("generated", "approved", "executing", "completed", "failed"),
    "post_status": ("pending", "generating", "generated", "posting", "posted", "failed"),
    "gpu_job_type": ("music", "image"),
    "gpu_job_status": ("queued", "running", "succeeded", "failed"),
    "dryrun_state": ("pending", "approved", "rejected", "auto_expired", "posted"),
    "error_category": ("transient", "recoverable", "fatal", "compliance", "quality"),
    "acoustid_status": ("not_checked", "clear", "hit", "api_error"),
    "youtube_privacy_status": ("public", "unlisted", "private", "deleted"),
    "llm_provider": ("openai", "anthropic", "ollama"),
    "llm_auth_mode": ("api_key", "codex_oauth"),
}


def _enum(name: str) -> postgresql.ENUM:
    """既存の ENUM 型を参照する (create_table 時の重複 CREATE TYPE を抑止)。"""
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    _create_extension()
    _create_enums()
    _create_genres()
    _create_plans()
    _create_gpu_jobs()  # posts.music_job_id/image_job_id が FK 参照するため posts より前に作成
    _create_posts()
    _create_plan_metric_snapshot()
    _create_videos()
    _create_audio_tracks()
    _create_dryrun_outputs()
    _create_oauth_credentials()
    _create_usage_log()
    _create_model_pricing()
    _create_analytics_daily()
    _create_comments()
    _create_job_history()
    _create_app_state()
    _create_audit_log()
    _seed()


def downgrade() -> None:
    for table in (
        "audit_log",
        "app_state",
        "job_history",
        "comments",
        "analytics_daily",
        "model_pricing",
        "usage_log",
        "oauth_credentials",
        "dryrun_outputs",
        "audio_tracks",
        "videos",
        "plan_metric_snapshot",
        "posts",
        "gpu_jobs",  # posts が FK 参照するため posts を drop した後に drop
        "plans",
        "genres",
    ):
        op.drop_table(table)
    for name in _ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
    # vector 拡張は他テーブルが依存し得るため残す (べき等な CREATE EXTENSION のみ管理)。


# ---------------------------------------------------------------------------
# Extension / Enums
# ---------------------------------------------------------------------------
def _create_extension() -> None:
    # pgvector (ADR-0010): 将来 RAG 用。 idempotent。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def _create_enums() -> None:
    for name, values in _ENUMS.items():
        labels = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({labels})")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def _create_genres() -> None:
    op.create_table(
        "genres",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("bpm_min", sa.Integer(), nullable=True),
        sa.Column("bpm_max", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "bpm_min IS NULL OR bpm_max IS NULL OR bpm_min <= bpm_max",
            name="ck_genres_bpm_range",
        ),
    )


def _create_plans() -> None:
    op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cycle", _enum("plan_cycle"), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("target_week_start", sa.Date(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "status",
            _enum("plan_status"),
            nullable=False,
            server_default="generated",
        ),
        sa.Column("llm_provider", _enum("llm_provider"), nullable=False),
        sa.Column("llm_model", sa.Text(), nullable=False),
        sa.Column("llm_prompt_version", sa.Text(), nullable=False),
        sa.Column(
            "llm_cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(cycle = 'daily'  AND target_date       IS NOT NULL AND target_week_start IS NULL) OR "
            "(cycle = 'weekly' AND target_week_start IS NOT NULL AND target_date       IS NULL)",
            name="ck_plans_cycle_target",
        ),
    )
    op.create_index(
        "idx_plans_target_date",
        "plans",
        ["target_date"],
        postgresql_where=sa.text("cycle = 'daily'"),
    )
    op.create_index(
        "idx_plans_target_week_start",
        "plans",
        ["target_week_start"],
        postgresql_where=sa.text("cycle = 'weekly'"),
    )
    op.create_index("idx_plans_status", "plans", ["status"])


def _create_posts() -> None:
    op.create_table(
        "posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "genre",
            sa.Text(),
            sa.ForeignKey("genres.name"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            _enum("post_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "music_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gpu_jobs.id"),
            nullable=True,
        ),
        sa.Column(
            "image_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gpu_jobs.id"),
            nullable=True,
        ),
        sa.Column("final_title", sa.Text(), nullable=True),
        sa.Column("final_description", sa.Text(), nullable=True),
        sa.Column("thumbnail_uri", sa.Text(), nullable=True),
        sa.Column("video_uri", sa.Text(), nullable=True),
        sa.Column("youtube_video_id", sa.Text(), nullable=True, unique=True),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retention_24h", sa.Numeric(5, 2), nullable=True),
        sa.Column("views_24h", sa.Integer(), nullable=True),
        sa.Column("error_category", _enum("error_category"), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("plan_id", "position", name="uq_posts_plan_position"),
    )
    op.create_index("idx_posts_status", "posts", ["status"])
    op.create_index("idx_posts_youtube_video_id", "posts", ["youtube_video_id"])
    op.create_index("idx_posts_genre", "posts", ["genre"])
    op.create_index("idx_posts_posted_at", "posts", [sa.text("posted_at DESC")])


def _create_plan_metric_snapshot() -> None:
    op.create_table(
        "plan_metric_snapshot",
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("plans.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("metric_window_start", sa.Date(), nullable=False),
        sa.Column("metric_window_end", sa.Date(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "metric_window_start <= metric_window_end",
            name="ck_plan_metric_snapshot_window",
        ),
    )


def _create_videos() -> None:
    op.create_table(
        "videos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("youtube_video_id", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "genre",
            sa.Text(),
            sa.ForeignKey("genres.name"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("duration_sec", sa.Integer(), nullable=False),
        sa.Column(
            "privacy_status",
            _enum("youtube_privacy_status"),
            nullable=False,
            server_default="public",
        ),
        sa.Column("contains_synthetic_media", sa.Boolean(), nullable=False),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("thumbnail_uri", sa.Text(), nullable=False),
        sa.Column("content_id_status", sa.Text(), nullable=True),
        sa.Column("content_id_checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        # ADR-0020: 合成メディア開示を DB レベルで保証
        sa.CheckConstraint(
            "contains_synthetic_media = TRUE",
            name="ck_videos_contains_synthetic_media",
        ),
    )
    op.create_index("idx_videos_genre", "videos", ["genre"])
    op.create_index("idx_videos_posted_at", "videos", [sa.text("posted_at DESC")])
    op.create_index("idx_videos_privacy_status", "videos", ["privacy_status"])


def _create_audio_tracks() -> None:
    op.create_table(
        "audio_tracks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("audio_uri", sa.Text(), nullable=False),
        sa.Column("duration_sec", sa.Integer(), nullable=False),
        sa.Column("bpm", sa.Integer(), nullable=True),
        sa.Column("music_key", sa.Text(), nullable=True),
        sa.Column("subtheme", sa.Text(), nullable=True),
        sa.Column(
            "acoustid_status",
            _enum("acoustid_status"),
            nullable=False,
            server_default="not_checked",
        ),
        sa.Column("acoustid_response", postgresql.JSONB(), nullable=True),
        sa.Column("fingerprint_hash", sa.Text(), nullable=True),
        sa.Column(
            "generated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "regenerated_count",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.UniqueConstraint("post_id", "position", name="uq_audio_tracks_post_position"),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 5",
            name="ck_audio_tracks_position",
        ),
    )
    op.create_index("idx_audio_tracks_acoustid_status", "audio_tracks", ["acoustid_status"])
    op.create_index("idx_audio_tracks_fingerprint", "audio_tracks", ["fingerprint_hash"])


def _create_gpu_jobs() -> None:
    op.create_table(
        "gpu_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", _enum("gpu_job_type"), nullable=False),
        sa.Column(
            "status",
            _enum("gpu_job_status"),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("output_uri", sa.Text(), nullable=True),
        sa.Column("vram_peak_mb", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("worker_endpoint", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("idx_gpu_jobs_status", "gpu_jobs", ["status"])
    op.create_index("idx_gpu_jobs_created_at", "gpu_jobs", [sa.text("created_at DESC")])


def _create_dryrun_outputs() -> None:
    op.create_table(
        "dryrun_outputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "state",
            _enum("dryrun_state"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("video_uri", sa.Text(), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("auto_expired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("idx_dryrun_outputs_state", "dryrun_outputs", ["state"])
    op.create_index("idx_dryrun_outputs_created_at", "dryrun_outputs", ["created_at"])


def _create_oauth_credentials() -> None:
    op.create_table(
        "oauth_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=True),
        sa.Column("access_token_encrypted", postgresql.BYTEA(), nullable=False),
        sa.Column("refresh_token_encrypted", postgresql.BYTEA(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("service", "channel_id", name="uq_oauth_credentials_service_channel"),
    )


def _create_usage_log() -> None:
    op.create_table(
        "usage_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", _enum("llm_provider"), nullable=False),
        sa.Column("auth_mode", _enum("llm_auth_mode"), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("context_type", sa.Text(), nullable=True),
        sa.Column("context_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_usage_log_created_at", "usage_log", [sa.text("created_at DESC")])
    op.create_index("idx_usage_log_context", "usage_log", ["context_type", "context_id"])
    op.create_index("idx_usage_log_provider", "usage_log", ["provider", "model"])


def _create_model_pricing() -> None:
    op.create_table(
        "model_pricing",
        sa.Column("provider", _enum("llm_provider"), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_per_1m_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("cached_per_1m_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("output_per_1m_usd", sa.Numeric(10, 4), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("provider", "model", "effective_from", name="pk_model_pricing"),
    )


def _create_analytics_daily() -> None:
    op.create_table(
        "analytics_daily",
        sa.Column(
            "youtube_video_id",
            sa.Text(),
            sa.ForeignKey("videos.youtube_video_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "estimated_minutes_watched",
            sa.Numeric(10, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("average_view_duration_sec", sa.Integer(), nullable=True),
        sa.Column("retention_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("ctr_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("traffic_sources", postgresql.JSONB(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("youtube_video_id", "metric_date", name="pk_analytics_daily"),
    )
    op.create_index(
        "idx_analytics_daily_metric_date",
        "analytics_daily",
        [sa.text("metric_date DESC")],
    )


def _create_comments() -> None:
    op.create_table(
        "comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "youtube_video_id",
            sa.Text(),
            sa.ForeignKey("videos.youtube_video_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("youtube_comment_id", sa.Text(), nullable=False, unique=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("sentiment", sa.Text(), nullable=True),
        sa.Column("topic_tags", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.create_index("idx_comments_video_id", "comments", ["youtube_video_id"])
    op.create_index("idx_comments_published_at", "comments", [sa.text("published_at DESC")])


def _create_job_history() -> None:
    op.create_table(
        "job_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("context_type", sa.Text(), nullable=True),
        sa.Column("context_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_category", _enum("error_category"), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("idx_job_history_job_name", "job_history", ["job_name"])
    op.create_index("idx_job_history_started_at", "job_history", [sa.text("started_at DESC")])
    op.create_index("idx_job_history_status", "job_history", ["status"])


def _create_app_state() -> None:
    op.create_table(
        "app_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def _create_audit_log() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_audit_log_action", "audit_log", ["action"])
    op.create_index("idx_audit_log_created_at", "audit_log", [sa.text("created_at DESC")])


# ---------------------------------------------------------------------------
# Seed (T018)
# ---------------------------------------------------------------------------
def _seed() -> None:
    _seed_genres()
    _seed_app_state()
    _seed_model_pricing()


def _seed_genres() -> None:
    """初期 6 ジャンル (ADR-0033)。 主力 3 / 拡張 2 / 実験 1。"""
    genres = sa.table(
        "genres",
        sa.column("name", sa.Text),
        sa.column("display_name", sa.Text),
        sa.column("bpm_min", sa.Integer),
        sa.column("bpm_max", sa.Integer),
        sa.column("description", sa.Text),
        sa.column("role", sa.Text),
    )
    op.bulk_insert(
        genres,
        [
            {
                "name": "lo-fi hip-hop",
                "display_name": "Lo-Fi Hip Hop",
                "bpm_min": 70,
                "bpm_max": 90,
                "description": "chill / nostalgic, BPM 70-90",
                "role": "main",
            },
            {
                "name": "chillhop",
                "display_name": "Chillhop",
                "bpm_min": 80,
                "bpm_max": 95,
                "description": "lo-fi の隣接、 jazz 寄り",
                "role": "main",
            },
            {
                "name": "ambient",
                "display_name": "Ambient",
                "bpm_min": None,
                "bpm_max": None,
                "description": "構造単純、 睡眠 / 瞑想用、 ACE-Step 得意",
                "role": "main",
            },
            {
                "name": "synthwave",
                "display_name": "Synthwave",
                "bpm_min": 80,
                "bpm_max": 110,
                "description": "retro / 80s",
                "role": "extension",
            },
            {
                "name": "piano solo",
                "display_name": "Piano Solo",
                "bpm_min": None,
                "bpm_max": None,
                "description": "リラックス / 勉強用",
                "role": "extension",
            },
            {
                "name": "future garage",
                "display_name": "Future Garage",
                "bpm_min": 130,
                "bpm_max": 140,
                "description": "atmospheric、 実験枠",
                "role": "experiment",
            },
        ],
    )


def _seed_app_state() -> None:
    """初期グローバル状態 (data-model.md §app_state)。

    JSONB のため値は JSON エンコード文字列で挿入する
    (scheduler_enabled=false, dryrun_enabled=true 等)。
    """
    rows = (
        ("scheduler_enabled", "false"),  # ADR-0031: reboot 後は手動 enable
        ("llm_provider", '"openai"'),  # ADR-0019
        ("llm_auth_mode", '"api_key"'),
        ("monthly_budget_usd", "50"),  # ADR-0024
        ("dryrun_enabled", "true"),  # 初期は dryrun 推奨
    )
    for key, value in rows:
        op.execute(
            sa.text(
                "INSERT INTO app_state (key, value) VALUES (:key, CAST(:value AS jsonb))"
            ).bindparams(key=key, value=value)
        )


def _seed_model_pricing() -> None:
    """LLM 単価表の初期投入 (ADR-0024)。 per 1M tokens / USD。

    出典は各社公開価格 (2026-06 時点)。 新モデルは運用ルールで追補する。
    """
    pricing = sa.table(
        "model_pricing",
        sa.column(
            "provider", _enum("llm_provider")
        ),  # ENUM 列。 Text 宣言だと varchar 扱いで型不一致になる
        sa.column("model", sa.Text),
        sa.column("input_per_1m_usd", sa.Numeric),
        sa.column("cached_per_1m_usd", sa.Numeric),
        sa.column("output_per_1m_usd", sa.Numeric),
        sa.column("effective_from", sa.Date),
        sa.column("source_url", sa.Text),
    )
    effective_from = "2026-06-01"
    op.bulk_insert(
        pricing,
        [
            {
                "provider": "openai",
                "model": "gpt-4o",
                "input_per_1m_usd": Decimal("2.5000"),
                "cached_per_1m_usd": Decimal("1.2500"),
                "output_per_1m_usd": Decimal("10.0000"),
                "effective_from": effective_from,
                "source_url": "https://openai.com/api/pricing/",
            },
            {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "input_per_1m_usd": Decimal("0.1500"),
                "cached_per_1m_usd": Decimal("0.0750"),
                "output_per_1m_usd": Decimal("0.6000"),
                "effective_from": effective_from,
                "source_url": "https://openai.com/api/pricing/",
            },
            {
                "provider": "anthropic",
                "model": "claude-3-5-sonnet-latest",
                "input_per_1m_usd": Decimal("3.0000"),
                "cached_per_1m_usd": Decimal("0.3000"),
                "output_per_1m_usd": Decimal("15.0000"),
                "effective_from": effective_from,
                "source_url": "https://www.anthropic.com/pricing",
            },
            {
                "provider": "anthropic",
                "model": "claude-3-5-haiku-latest",
                "input_per_1m_usd": Decimal("0.8000"),
                "cached_per_1m_usd": Decimal("0.0800"),
                "output_per_1m_usd": Decimal("4.0000"),
                "effective_from": effective_from,
                "source_url": "https://www.anthropic.com/pricing",
            },
            {
                "provider": "ollama",
                "model": "llama3.1",
                "input_per_1m_usd": Decimal("0.0000"),
                "cached_per_1m_usd": None,
                "output_per_1m_usd": Decimal("0.0000"),
                "effective_from": effective_from,
                "source_url": None,
            },
        ],
    )
