"""`comments` テーブル ORM (ADR-0021)。

DDL: alembic/versions/001_initial.py `_create_comments` / data-model.md §comments。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Comment(Base):
    """YouTube コメント取得結果。"""

    __tablename__ = "comments"
    __table_args__ = (
        Index("idx_comments_video_id", "youtube_video_id"),
        Index("idx_comments_published_at", sa_text("published_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    youtube_video_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("videos.youtube_video_id", ondelete="CASCADE"),
        nullable=False,
    )
    youtube_comment_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    like_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    published_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=sa_text("now()")
    )
    sentiment: Mapped[str | None] = mapped_column(Text, nullable=True)
    topic_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
