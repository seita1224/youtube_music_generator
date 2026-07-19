"""`usage_log` テーブル ORM (ADR-0024)。

DDL: alembic/versions/001_initial.py `_create_usage_log` / data-model.md §usage_log。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum


class UsageLog(Base):
    """LLM 呼び出し記録 (コスト集約用)。"""

    __tablename__ = "usage_log"
    __table_args__ = (
        Index("idx_usage_log_created_at", text("created_at DESC")),
        Index("idx_usage_log_context", "context_type", "context_id"),
        Index("idx_usage_log_provider", "provider", "model"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(pg_enum("llm_provider"), nullable=False)
    auth_mode: Mapped[str | None] = mapped_column(pg_enum("llm_auth_mode"), nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, server_default=text("0")
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
