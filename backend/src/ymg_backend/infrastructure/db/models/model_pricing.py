"""`model_pricing` テーブル ORM (ADR-0024)。

DDL: alembic/versions/001_initial.py `_create_model_pricing` / data-model.md §model_pricing。
複合主キー (provider, model, effective_from)。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Numeric, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Date

from .base import Base, pg_enum


class ModelPricing(Base):
    """LLM 単価表 (per 1M tokens / USD)。"""

    __tablename__ = "model_pricing"
    __table_args__ = (
        PrimaryKeyConstraint("provider", "model", "effective_from", name="pk_model_pricing"),
    )

    provider: Mapped[str] = mapped_column(pg_enum("llm_provider"), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_per_1m_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    cached_per_1m_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    output_per_1m_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
