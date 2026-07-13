"""`llm_provider_secrets` テーブル ORM (ADR-0019 / ADR-0012 拡張)。

DDL: alembic/versions/005_llm_provider_secrets.py / data-model.md §llm_provider_secrets。
API key は Fernet 暗号化済み BYTEA。 平文・マスク・末尾は API 応答に出さない。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Text, text
from sqlalchemy.dialects.postgresql import BYTEA, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class LlmProviderSecret(Base):
    """Fernet 暗号化された LLM provider API key (openai / anthropic のみ)。"""

    __tablename__ = "llm_provider_secrets"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('openai', 'anthropic')",
            name="ck_llm_provider_secrets_provider",
        ),
    )

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    api_key_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
