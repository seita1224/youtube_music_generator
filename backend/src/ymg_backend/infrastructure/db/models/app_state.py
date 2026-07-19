"""`app_state` テーブル ORM (ADR-0031)。

DDL: alembic/versions/001_initial.py `_create_app_state` / data-model.md §app_state。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AppState(Base):
    """グローバル状態 (scheduler_enabled / llm_provider 等)。"""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
