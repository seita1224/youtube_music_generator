"""`genres` テーブル ORM (ADR-0033)。

DDL: alembic/versions/001_initial.py `_create_genres` / data-model.md §genres。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, Text, text, true
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Genre(Base):
    """ジャンル辞書 (主力 / 拡張 / 実験)。"""

    __tablename__ = "genres"
    __table_args__ = (
        CheckConstraint(
            "bpm_min IS NULL OR bpm_max IS NULL OR bpm_min <= bpm_max",
            name="ck_genres_bpm_range",
        ),
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    bpm_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bpm_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
