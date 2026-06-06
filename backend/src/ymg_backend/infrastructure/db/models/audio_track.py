"""`audio_tracks` テーブル ORM。

DDL: alembic/versions/001_initial.py `_create_audio_tracks` / data-model.md §audio_tracks。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum


class AudioTrack(Base):
    """6 トラックの個別メタ。"""

    __tablename__ = "audio_tracks"
    __table_args__ = (
        UniqueConstraint("post_id", "position", name="uq_audio_tracks_post_position"),
        CheckConstraint("position BETWEEN 0 AND 5", name="ck_audio_tracks_position"),
        Index("idx_audio_tracks_acoustid_status", "acoustid_status"),
        Index("idx_audio_tracks_fingerprint", "fingerprint_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("posts.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    audio_uri: Mapped[str] = mapped_column(Text, nullable=False)
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    bpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    music_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    subtheme: Mapped[str | None] = mapped_column(Text, nullable=True)
    acoustid_status: Mapped[str] = mapped_column(
        pg_enum("acoustid_status"), nullable=False, server_default="not_checked"
    )
    acoustid_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    fingerprint_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    regenerated_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
