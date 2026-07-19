"""Align job_history / job_step_events with query indexes and docs (forward-only).

003 適用済み環境向けの安全な前進マイグレーション:

- ``job_history.trigger`` を NULL 許容にし、 非 music_generation 行の誤 default を外す
- ``job_step_events.payload`` (JSONB, nullable) を追加
- クエリ用 index: ``target_date`` / step / status / ``(job_name, started_at DESC)``

``job_trigger`` ENUM は作らない (TEXT + CHECK を正とする。 ADR-0036)。

Revision ID: 004_job_history_schema_align
Revises: 003_music_generation_jobs
Create Date: 2026-07-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004_job_history_schema_align"
down_revision: str | None = "003_music_generation_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL 許容に合わせ CHECK を付け替える (旧制約は NULL を拒否する)。
    op.drop_constraint("ck_job_history_trigger", "job_history", type_="check")

    # 非 music 行は trigger 意味が無いので NULL へ寄せ、 default を外す。
    op.execute(
        "UPDATE job_history SET trigger = NULL WHERE job_name <> 'music_generation'"
    )
    op.execute("ALTER TABLE job_history ALTER COLUMN trigger DROP DEFAULT")
    op.alter_column(
        "job_history",
        "trigger",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_job_history_trigger",
        "job_history",
        "trigger IS NULL OR trigger IN ('cron', 'run_now')",
    )

    op.add_column(
        "job_step_events",
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_index(
        "idx_job_history_target_date",
        "job_history",
        [sa.text("target_date DESC")],
    )
    op.create_index(
        "idx_job_history_job_name_started_at",
        "job_history",
        ["job_name", sa.text("started_at DESC")],
    )
    op.create_index("idx_job_step_events_step", "job_step_events", ["step"])
    op.create_index("idx_job_step_events_status", "job_step_events", ["status"])


def downgrade() -> None:
    # ADR-0031: down は運用しないが、 ローカル検証用に対称操作を残す。
    op.drop_index("idx_job_step_events_status", table_name="job_step_events")
    op.drop_index("idx_job_step_events_step", table_name="job_step_events")
    op.drop_index("idx_job_history_job_name_started_at", table_name="job_history")
    op.drop_index("idx_job_history_target_date", table_name="job_history")
    op.drop_column("job_step_events", "payload")

    op.drop_constraint("ck_job_history_trigger", "job_history", type_="check")
    op.execute("UPDATE job_history SET trigger = 'cron' WHERE trigger IS NULL")
    op.alter_column(
        "job_history",
        "trigger",
        existing_type=sa.Text(),
        nullable=False,
        server_default="cron",
    )
    op.create_check_constraint(
        "ck_job_history_trigger",
        "job_history",
        "trigger IN ('cron', 'run_now')",
    )
