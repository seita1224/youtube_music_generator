"""`videos` テーブル ORM。

DDL: alembic/versions/001_initial.py `_create_videos` / data-model.md §videos。
ADR-0020: `contains_synthetic_media = TRUE` を DB レベル CHECK で保証する。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum


class Video(Base):
    """投稿済み動画メタ。"""

    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint(
            "contains_synthetic_media = TRUE",
            name="ck_videos_contains_synthetic_media",
        ),
        Index("idx_videos_genre", "genre"),
        Index("idx_videos_posted_at", text("posted_at DESC")),
        Index("idx_videos_privacy_status", "privacy_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    youtube_video_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    post_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("posts.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    genre: Mapped[str] = mapped_column(Text, ForeignKey("genres.name"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    privacy_status: Mapped[str] = mapped_column(
        pg_enum("youtube_privacy_status"), nullable=False, server_default="public"
    )
    contains_synthetic_media: Mapped[bool] = mapped_column(Boolean, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    thumbnail_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_id_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_id_checked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
