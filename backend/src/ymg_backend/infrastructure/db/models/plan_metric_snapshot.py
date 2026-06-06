"""`plan_metric_snapshot` テーブル ORM (ADR-0032)。

DDL: alembic/versions/001_initial.py `_create_plan_metric_snapshot` /
data-model.md §plan_metric_snapshot。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Date

from .base import Base


class PlanMetricSnapshot(Base):
    """改善計画 LLM 入力の生コピー (再現性確保用)。"""

    __tablename__ = "plan_metric_snapshot"
    __table_args__ = (
        CheckConstraint(
            "metric_window_start <= metric_window_end",
            name="ck_plan_metric_snapshot_window",
        ),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="CASCADE"),
        primary_key=True,
    )
    metric_window_start: Mapped[date] = mapped_column(Date, nullable=False)
    metric_window_end: Mapped[date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
