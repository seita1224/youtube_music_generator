"""`dryrun_outputs` テーブル ORM (ADR-0025)。

DDL: alembic/versions/001_initial.py `_create_dryrun_outputs` / data-model.md §dryrun_outputs。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pg_enum


class DryrunOutput(Base):
    """dryrun ライフサイクル (pending → approved/rejected/auto_expired → posted)。"""

    __tablename__ = "dryrun_outputs"
    __table_args__ = (
        Index("idx_dryrun_outputs_state", "state"),
        Index("idx_dryrun_outputs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("posts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    state: Mapped[str] = mapped_column(
        pg_enum("dryrun_state"), nullable=False, server_default="pending"
    )
    video_uri: Mapped[str] = mapped_column(Text, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    auto_expired_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
