"""LLM provider API key の write-only 管理 (ADR-0019 / ADR-0012 拡張)。

OpenAI / Anthropic の API key を ``llm_provider_secrets`` に Fernet 暗号化して保存する。
実行時の解決順位:

1. 環境変数 (``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``) が非空 → ``env``
2. DB に暗号化行がある → ``db`` (Fernet 復号)
3. それ以外 → ``none``

Ollama は認証不要のため ``n/a``。 平文・マスク・末尾はログ / API 応答に出さない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, cast

from sqlalchemy import Column, DateTime, MetaData, String, Table, delete, select
from sqlalchemy.dialects.postgresql import BYTEA, insert

from ymg_backend.core.security import TokenCipher, build_cipher_from_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.core.config import Settings

LlmSecretProvider = Literal["openai", "anthropic"]
CredentialSource = Literal["env", "db", "none", "n/a"]

_CLOUD_PROVIDERS: Final[frozenset[str]] = frozenset({"openai", "anthropic"})

_metadata: Final[MetaData] = MetaData()

llm_provider_secrets_table: Final[Table] = Table(
    "llm_provider_secrets",
    _metadata,
    Column("provider", String, primary_key=True, nullable=False),
    Column("api_key_encrypted", BYTEA, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def _env_key(settings: Settings, provider: LlmSecretProvider) -> str:
    """env 由来の API key 平文を返す (空文字可)。 ログには出さない。"""
    if provider == "openai":
        raw = settings.openai_api_key.get_secret_value()
    else:
        raw = settings.anthropic_api_key.get_secret_value()
    stripped = raw.strip()
    return stripped if stripped else ""


def credential_source_for(
    settings: Settings,
    provider: str,
    *,
    has_db_secret: bool,
) -> CredentialSource:
    """provider の credential_source を解決する (平文を返さない)。

    ollama は常に ``n/a``。 cloud は env 非空 → ``env``、 else DB 有無 → ``db`` / ``none``。
    """
    if provider == "ollama":
        return "n/a"
    if provider not in _CLOUD_PROVIDERS:
        return "none"
    cloud = cast(LlmSecretProvider, provider)
    if _env_key(settings, cloud):
        return "env"
    if has_db_secret:
        return "db"
    return "none"


def credential_configured(source: CredentialSource) -> bool:
    """実行時に鍵が使えるか (env / db / ollama n/a)。"""
    return source in ("env", "db", "n/a")


async def list_db_secret_providers(session: AsyncSession) -> set[str]:
    """DB に暗号化行がある provider 名の集合を返す (平文は読まない)。"""
    stmt = select(llm_provider_secrets_table.c.provider)
    rows = (await session.execute(stmt)).all()
    return {str(row[0]) for row in rows}


async def resolve_api_key(
    settings: Settings,
    provider: LlmSecretProvider,
    *,
    session: AsyncSession | None,
    cipher: TokenCipher | None = None,
) -> tuple[str | None, CredentialSource]:
    """env > DB の順で API key を解決する。

    Returns:
        ``(api_key_or_none, credential_source)``。 平文は呼び出し側が provider 構築にのみ使う。
    """
    env_value = _env_key(settings, provider)
    if env_value:
        return env_value, "env"

    if session is None:
        return None, "none"

    stmt = select(llm_provider_secrets_table.c.api_key_encrypted).where(
        llm_provider_secrets_table.c.provider == provider
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None, "none"

    resolved_cipher = cipher if cipher is not None else build_cipher_from_settings(settings)
    # 復号失敗は TokenCipher が FatalError を送出する。
    return resolved_cipher.decrypt(row[0]), "db"


async def upsert_api_key(
    session: AsyncSession,
    settings: Settings,
    provider: LlmSecretProvider,
    api_key: str,
    *,
    cipher: TokenCipher | None = None,
) -> None:
    """API key を Fernet 暗号化して upsert する。 commit は呼び出し側。

    Raises:
        ValueError: ``api_key`` が空 (空白のみ含む)。
        RuntimeError: env が SoT のとき (呼び出し側で 409 に写像)。
    """
    normalized = api_key.strip()
    if not normalized:
        raise ValueError("api_key must be a non-empty string")
    if _env_key(settings, provider):
        raise RuntimeError("credential_source_is_env")

    resolved_cipher = cipher if cipher is not None else build_cipher_from_settings(settings)
    ciphertext = resolved_cipher.encrypt(normalized)
    now = datetime.now(UTC)
    stmt = (
        insert(llm_provider_secrets_table)
        .values(provider=provider, api_key_encrypted=ciphertext, updated_at=now)
        .on_conflict_do_update(
            index_elements=[llm_provider_secrets_table.c.provider],
            set_={"api_key_encrypted": ciphertext, "updated_at": now},
        )
    )
    await session.execute(stmt)
    await session.flush()


async def delete_api_key(
    session: AsyncSession,
    settings: Settings,
    provider: LlmSecretProvider,
) -> bool:
    """DB 行を削除する。 env SoT のときは RuntimeError。 戻り値は削除行の有無。"""
    if _env_key(settings, provider):
        raise RuntimeError("credential_source_is_env")
    stmt = delete(llm_provider_secrets_table).where(
        llm_provider_secrets_table.c.provider == provider
    )
    result = await session.execute(stmt)
    await session.flush()
    # SQLAlchemy Result.rowcount は CursorResult で利用可能。
    rowcount = getattr(result, "rowcount", 0) or 0
    return int(rowcount) > 0


__all__ = [
    "CredentialSource",
    "LlmSecretProvider",
    "credential_configured",
    "credential_source_for",
    "delete_api_key",
    "list_db_secret_providers",
    "llm_provider_secrets_table",
    "resolve_api_key",
    "upsert_api_key",
]
