"""`job_history` テーブル ORM (ADR-0023, ADR-0028)。

DDL: alembic/versions/001_initial.py `_create_job_history` +
``003_music_generation_jobs.py`` (trigger / target_date / single-flight index)。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum

# 音楽専用実行の job_name。 partial UNIQUE (status=running) の対象。
MUSIC_GENERATION_JOB_NAME: str = "music_generation"


class JobHistory(Base):
    """cron / run-now 実行履歴 (1 run = 1 行。 ``id`` が API の ``run_id``)。"""

    __tablename__ = "job_history"
    __table_args__ = (
        CheckConstraint(
            "trigger IS NULL OR trigger IN ('cron', 'run_now')",
            name="ck_job_history_trigger",
        ),
        Index("idx_job_history_job_name", "job_name"),
        Index("idx_job_history_started_at", text("started_at DESC")),
        Index("idx_job_history_status", "status"),
        Index("idx_job_history_target_date", text("target_date DESC")),
        Index("idx_job_history_job_name_started_at", "job_name", text("started_at DESC")),
        # DB レベル single-flight: music_generation の running は高々 1 行。
        Index(
            "uq_job_history_music_generation_running",
            "job_name",
            unique=True,
            postgresql_where=text(
                f"job_name = '{MUSIC_GENERATION_JOB_NAME}' AND status = 'running'"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    context_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(pg_enum("error_category"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # music_generation のみ 'cron' | 'run_now'。 他 job_name は NULL (004 以降)。
    trigger: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
