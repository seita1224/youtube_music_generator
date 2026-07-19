"""`analytics_daily` テーブル ORM (ADR-0021)。

DDL: alembic/versions/001_initial.py `_create_analytics_daily` / data-model.md §analytics_daily。
複合主キー (youtube_video_id, metric_date)。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Numeric, PrimaryKeyConstraint, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Date

from .base import Base


class AnalyticsDaily(Base):
    """動画ごと日次の指標。"""

    __tablename__ = "analytics_daily"
    __table_args__ = (
        PrimaryKeyConstraint("youtube_video_id", "metric_date", name="pk_analytics_daily"),
        Index("idx_analytics_daily_metric_date", text("metric_date DESC")),
    )

    youtube_video_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("videos.youtube_video_id", ondelete="CASCADE"),
        nullable=False,
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    views: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    estimated_minutes_watched: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("0")
    )
    average_view_duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ctr_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    traffic_sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
