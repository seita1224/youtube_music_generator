"""`plans` テーブル ORM (ADR-0032)。

DDL: alembic/versions/001_initial.py `_create_plans` / data-model.md §plans。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, Index, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Date

from .base import Base, pg_enum


class Plan(Base):
    """DailyPlan / WeeklyPlan の永続レコード。"""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(
            "(cycle = 'daily'  AND target_date       IS NOT NULL AND target_week_start IS NULL) OR "
            "(cycle = 'weekly' AND target_week_start IS NOT NULL AND target_date       IS NULL)",
            name="ck_plans_cycle_target",
        ),
        Index(
            "idx_plans_target_date",
            "target_date",
            postgresql_where=text("cycle = 'daily'"),
        ),
        Index(
            "idx_plans_target_week_start",
            "target_week_start",
            postgresql_where=text("cycle = 'weekly'"),
        ),
        Index("idx_plans_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    cycle: Mapped[str] = mapped_column(pg_enum("plan_cycle"), nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    target_week_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("plan_status"), nullable=False, server_default="generated"
    )
    llm_provider: Mapped[str] = mapped_column(pg_enum("llm_provider"), nullable=False)
    llm_model: Mapped[str] = mapped_column(Text, nullable=False)
    llm_prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    llm_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
