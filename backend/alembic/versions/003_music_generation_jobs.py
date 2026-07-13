"""music generation run metadata + step events + single-flight (migration-models).

音楽専用実行の永続化基盤:

- ``plan_status`` / ``post_status`` に ``music_generated`` を追加
- ``job_history`` に ``trigger`` (cron|run_now) と ``target_date`` を追加
- ``job_step_events`` を新設 (run 単位の工程イベント正本)
- ``job_name='music_generation' AND status='running'`` の部分 UNIQUE で
  cron / run-now の DB レベル single-flight

孤立 running / executing / generating の確定はアプリ起動時の
``orphan_reconciliation`` が担う (本マイグレーションはスキーマのみ)。

Revision ID: 003_music_generation_jobs
Revises: 002_normalize_genre_roles
Create Date: 2026-07-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003_music_generation_jobs"
down_revision: str | None = "002_normalize_genre_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MUSIC_GENERATION_JOB_NAME = "music_generation"


def upgrade() -> None:
    # ENUM 末尾へ追加 (PG 16 はトランザクション内 ADD VALUE 可)。
    op.execute("ALTER TYPE plan_status ADD VALUE 'music_generated'")
    op.execute("ALTER TYPE post_status ADD VALUE 'music_generated'")

    op.add_column(
        "job_history",
        sa.Column(
            "trigger",
            sa.Text(),
            nullable=False,
            server_default="cron",
        ),
    )
    op.add_column(
        "job_history",
        sa.Column("target_date", sa.Date(), nullable=True),
    )
    op.create_check_constraint(
        "ck_job_history_trigger",
        "job_history",
        "trigger IN ('cron', 'run_now')",
    )
    # 同一 job_name で status=running は高々 1 行 (cron / run-now 共用)。
    op.create_index(
        "uq_job_history_music_generation_running",
        "job_history",
        ["job_name"],
        unique=True,
        postgresql_where=sa.text(
            f"job_name = '{_MUSIC_GENERATION_JOB_NAME}' AND status = 'running'"
        ),
    )

    op.create_table(
        "job_step_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("job_history.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("genre", sa.Text(), nullable=True),
        sa.Column("context_type", sa.Text(), nullable=True),
        sa.Column("context_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "error_category",
            postgresql.ENUM(
                "transient",
                "recoverable",
                "fatal",
                "compliance",
                "quality",
                name="error_category",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_job_step_events_status",
        ),
    )
    op.create_index(
        "idx_job_step_events_run_id",
        "job_step_events",
        ["run_id", "created_at"],
    )
    op.create_index(
        "idx_job_step_events_created_at",
        "job_step_events",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_job_step_events_created_at", table_name="job_step_events")
    op.drop_index("idx_job_step_events_run_id", table_name="job_step_events")
    op.drop_table("job_step_events")

    op.drop_index(
        "uq_job_history_music_generation_running",
        table_name="job_history",
    )
    op.drop_constraint("ck_job_history_trigger", "job_history", type_="check")
    op.drop_column("job_history", "target_date")
    op.drop_column("job_history", "trigger")

    # ENUM 値の削除は型再作成が必要。 music_generated 行を先に failed へ寄せる。
    op.execute("UPDATE plans SET status = 'failed' WHERE status = 'music_generated'")
    op.execute("UPDATE posts SET status = 'failed' WHERE status = 'music_generated'")
    _rebuild_enum_without_music_generated(
        "plan_status",
        ("generated", "approved", "executing", "completed", "failed"),
        table_column_pairs=(("plans", "status"),),
        default="'generated'",
    )
    _rebuild_enum_without_music_generated(
        "post_status",
        ("pending", "generating", "generated", "posting", "posted", "failed"),
        table_column_pairs=(("posts", "status"),),
        default="'pending'",
    )


def _rebuild_enum_without_music_generated(
    enum_name: str,
    values: tuple[str, ...],
    *,
    table_column_pairs: tuple[tuple[str, str], ...],
    default: str,
) -> None:
    """``music_generated`` を除いた ENUM 型へ列を付け替える (downgrade 用)。"""
    tmp = f"{enum_name}_old"
    values_sql = ", ".join(f"'{v}'" for v in values)
    op.execute(f"ALTER TYPE {enum_name} RENAME TO {tmp}")
    op.execute(f"CREATE TYPE {enum_name} AS ENUM ({values_sql})")
    for table, column in table_column_pairs:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER COLUMN {column} TYPE {enum_name} "
            f"USING {column}::text::{enum_name}"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {default}::{enum_name}")
    op.execute(f"DROP TYPE {tmp}")
