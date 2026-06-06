"""`job_history` テーブル ORM (ADR-0023, ADR-0028)。

DDL: alembic/versions/001_initial.py `_create_job_history` / data-model.md §job_history。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum


class JobHistory(Base):
    """cron / scheduler 実行履歴。"""

    __tablename__ = "job_history"
    __table_args__ = (
        Index("idx_job_history_job_name", "job_name"),
        Index("idx_job_history_started_at", text("started_at DESC")),
        Index("idx_job_history_status", "status"),
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
