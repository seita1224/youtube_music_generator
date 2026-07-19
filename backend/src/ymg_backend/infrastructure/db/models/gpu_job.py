"""`gpu_jobs` テーブル ORM (ADR-0031)。

DDL: alembic/versions/001_initial.py `_create_gpu_jobs` / data-model.md §gpu_jobs。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum


class GpuJob(Base):
    """GPU worker への音楽 / 画像生成ジョブ。"""

    __tablename__ = "gpu_jobs"
    __table_args__ = (
        Index("idx_gpu_jobs_status", "status"),
        Index("idx_gpu_jobs_created_at", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_type: Mapped[str] = mapped_column(pg_enum("gpu_job_type"), nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("gpu_job_status"), nullable=False, server_default="queued"
    )
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    vram_peak_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
