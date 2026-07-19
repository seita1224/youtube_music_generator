"""`job_step_events` テーブル ORM (音楽生成進捗の工程イベント正本)。

DDL: alembic/versions/003_music_generation_jobs.py。
``job_history.id`` (= run_id) に紐づく step 単位のイベント行。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum


class JobStepEvent(Base):
    """1 実行 (run) 内の工程イベント (running / succeeded / failed を対で記録)。"""

    __tablename__ = "job_step_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_job_step_events_status",
        ),
        Index("idx_job_step_events_run_id", "run_id", "created_at"),
        Index("idx_job_step_events_created_at", text("created_at DESC")),
        Index("idx_job_step_events_step", "step"),
        Index("idx_job_step_events_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_history.id", ondelete="CASCADE"),
        nullable=False,
    )
    step: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    genre: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(pg_enum("error_category"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
