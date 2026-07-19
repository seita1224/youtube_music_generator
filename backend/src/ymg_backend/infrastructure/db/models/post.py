"""`posts` テーブル ORM (ADR-0032)。

DDL: alembic/versions/001_initial.py `_create_posts` / data-model.md §posts。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum


class Post(Base):
    """DailyPlan.posts の個別投稿レコード。"""

    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("plan_id", "position", name="uq_posts_plan_position"),
        Index("idx_posts_status", "status"),
        Index("idx_posts_youtube_video_id", "youtube_video_id"),
        Index("idx_posts_genre", "genre"),
        Index("idx_posts_posted_at", text("posted_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    genre: Mapped[str] = mapped_column(Text, ForeignKey("genres.name"), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("post_status"), nullable=False, server_default="pending"
    )
    music_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gpu_jobs.id"), nullable=True
    )
    image_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gpu_jobs.id"), nullable=True
    )
    final_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    youtube_video_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    retention_24h: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    views_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_category: Mapped[str | None] = mapped_column(pg_enum("error_category"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
