"""LLM 呼び出しを `usage_log` テーブルへ永続化する (T033)。

ADR-0024(コストトラッキング)+ data-model.md `usage_log` を反映する。
全 LLM 呼び出し(成功 / 失敗)を provider / model / tokens / cost / cached /
timestamp 付きで 1 行 insert する。 :class:`~ymg_backend.llm.base.LlmResponse`
からの記録と、 失敗時(usage 不明)の記録の両方を扱う。

設計方針:

- ORM model 層(T021)に依存しないよう、 SQLAlchemy Core の軽量 Table 定義を
  本モジュールに閉じて持つ(``infrastructure/audit.py`` / ``llm/pricing.py`` と同方針)。
- 金額は ``Decimal``(``NUMERIC(10, 6)``)で保持する。 ``LlmResponse.usage`` の
  ``cost_usd`` は ``float`` なので ``Decimal(str(...))`` で安全に変換する。
- commit は呼び出し側のトランザクション境界に委ね、 ここでは flush のみ行う。
- 不変性: 渡された context dict はコピーして保持しない(そのまま JSONB へ渡すだけ)。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table, insert
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.llm.base import LlmProviderName, LlmResponse, LlmUsage

# llm_provider / llm_auth_mode native ENUM(data-model.md)。 create_type=False で
# マイグレーション所有の型を再利用する。
_llm_provider_enum: Final[ENUM] = ENUM(
    "openai",
    "anthropic",
    "ollama",
    name="llm_provider",
    create_type=False,
)

_llm_auth_mode_enum: Final[ENUM] = ENUM(
    "api_key",
    "codex_oauth",
    name="llm_auth_mode",
    create_type=False,
)

LlmAuthMode = str  # 'api_key' / 'codex_oauth'(data-model.md llm_auth_mode)

# usage_log への Core Table 定義(data-model.md `usage_log` と整合)。
# id / created_at は明示挿入(id は uuid4、 created_at は DB 側 default を使うため非宣言)。
_metadata: Final[MetaData] = MetaData()

usage_log_table: Final[Table] = Table(
    "usage_log",
    _metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("provider", _llm_provider_enum, nullable=False),
    Column("auth_mode", _llm_auth_mode_enum, nullable=True),
    Column("model", String, nullable=False),
    Column("prompt_tokens", Integer, nullable=False),
    Column("cached_tokens", Integer, nullable=False),
    Column("completion_tokens", Integer, nullable=False),
    Column("cost_usd", Numeric(10, 6), nullable=False),
    Column("duration_ms", Integer, nullable=True),
    Column("context_type", String, nullable=True),
    Column("context_id", UUID(as_uuid=True), nullable=True),
    Column("prompt_version", String, nullable=True),
    Column("error_message", String, nullable=True),
)


def _parse_context_id(context_id: str | uuid.UUID | None) -> uuid.UUID | None:
    """``context_id`` を UUID へ正規化する(文字列許容)。 空 / None は None。"""
    if context_id is None:
        return None
    if isinstance(context_id, uuid.UUID):
        return context_id
    if not context_id:
        return None
    return uuid.UUID(context_id)


async def write_usage_log(
    session: AsyncSession,
    *,
    provider: LlmProviderName,
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    cost_usd: Decimal,
    auth_mode: LlmAuthMode | None = None,
    duration_ms: int | None = None,
    context_type: str | None = None,
    context_id: str | uuid.UUID | None = None,
    prompt_version: str | None = None,
    error_message: str | None = None,
) -> uuid.UUID:
    """`usage_log` に 1 行 insert し、 生成した行の UUID を返す。

    成功・失敗のどちらでも呼べる。 失敗時(usage 取得不可)は token を 0、
    ``cost_usd=Decimal(0)`` とし、 ``error_message`` に理由を渡す運用を想定する。

    Args:
        session: 呼び出し側が管理する SQLAlchemy AsyncSession。 commit は
            呼び出し側のトランザクション境界に委ねる(ここでは flush のみ)。
        provider: LLM provider(``openai`` / ``anthropic`` / ``ollama``)。
        model: モデル ID。
        prompt_tokens: 入力トークン数(キャッシュ分を含む)。
        cached_tokens: prompt caching でヒットした入力トークン数。
        completion_tokens: 出力トークン数。
        cost_usd: ``model_pricing`` から算出した推定コスト(``Decimal``)。
        auth_mode: 認証方式(``api_key`` / ``codex_oauth``)。 任意。
        duration_ms: 呼び出しレイテンシ(ms)。 任意。
        context_type: 呼び出し文脈(``planner`` / ``finisher`` / ``analyzer`` 等)。 任意。
        context_id: 文脈 ID(plan_id / post_id 等)。 文字列または UUID。 任意。
        prompt_version: prompt バージョン(``planner/system_v1`` 等)。 任意。
        error_message: 失敗時のエラーメッセージ。 任意。

    Returns:
        挿入した usage_log 行の id(UUID)。

    Raises:
        ValueError: ``model`` が空、 トークン数が負、 または ``cached_tokens >
            prompt_tokens`` の場合(境界での入力検証)。
    """
    if not model:
        raise ValueError("model must be a non-empty string")
    if prompt_tokens < 0 or cached_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must be non-negative")
    if cached_tokens > prompt_tokens:
        raise ValueError(
            f"cached_tokens ({cached_tokens}) must not exceed prompt_tokens ({prompt_tokens})"
        )

    row_id = uuid.uuid4()
    values: dict[str, Any] = {
        "id": row_id,
        "provider": provider,
        "auth_mode": auth_mode,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "context_type": context_type,
        "context_id": _parse_context_id(context_id),
        "prompt_version": prompt_version,
        "error_message": error_message,
    }
    await session.execute(insert(usage_log_table).values(**values))
    await session.flush()
    return row_id


async def write_usage_from_response(
    session: AsyncSession,
    response: LlmResponse[Any],
    *,
    auth_mode: LlmAuthMode | None = None,
    context_type: str | None = None,
    context_id: str | uuid.UUID | None = None,
    prompt_version: str | None = None,
) -> uuid.UUID:
    """成功した :class:`LlmResponse` から `usage_log` 行を記録する。

    ``response.usage``(:class:`LlmUsage`)の token / cost / duration をそのまま転記する。
    ``cost_usd`` は ``float`` なので精度劣化を避けるため ``Decimal(str(...))`` で変換する。

    Args:
        session: 呼び出し側が管理する AsyncSession。
        response: provider が返した構造化応答。
        auth_mode: 認証方式。 任意(``LlmResponse`` には含まれないため別途指定)。
        context_type: 呼び出し文脈。 任意。
        context_id: 文脈 ID。 任意。
        prompt_version: prompt バージョン。 任意。

    Returns:
        挿入した usage_log 行の id(UUID)。
    """
    usage: LlmUsage = response.usage
    return await write_usage_log(
        session,
        provider=response.provider,
        model=response.model,
        prompt_tokens=usage.prompt_tokens,
        cached_tokens=usage.cached_tokens,
        completion_tokens=usage.completion_tokens,
        cost_usd=Decimal(str(usage.cost_usd)),
        auth_mode=auth_mode,
        duration_ms=usage.duration_ms,
        context_type=context_type,
        context_id=context_id,
        prompt_version=prompt_version,
    )
