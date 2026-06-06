"""`oauth_credentials` テーブル ORM (ADR-0012)。

DDL: alembic/versions/001_initial.py `_create_oauth_credentials` /
data-model.md §oauth_credentials。 トークンは Fernet 暗号化済み BYTEA。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OAuthCredential(Base):
    """Fernet 暗号化された OAuth トークン。"""

    __tablename__ = "oauth_credentials"
    __table_args__ = (
        UniqueConstraint("service", "channel_id", name="uq_oauth_credentials_service_channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    refresh_token_encrypted: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
