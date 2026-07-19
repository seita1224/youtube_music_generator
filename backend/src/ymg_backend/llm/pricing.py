"""LLM コスト計算: `model_pricing` テーブル参照で `cost_usd` を算出 (T032)。

ADR-0024(コストトラッキング)+ data-model.md `model_pricing` を反映する。
prompt caching 割引(cached input tokens の単価差)に対応する。

設計方針:

- ORM model 層(T021)に依存しないよう、 SQLAlchemy Core の軽量 Table 定義を
  本モジュールに閉じて持ち、 呼び出し側から渡された ``AsyncSession`` で参照する
  (``infrastructure/audit.py`` と同じ疎結合方針)。
- 金額は ``Decimal`` で扱う。 単価は ``NUMERIC(10, 4)``、 算出コストは
  ``NUMERIC(10, 6)``(``usage_log.cost_usd`` / ``plans.llm_cost_usd``)に丸める。
- 不変性: 解決した単価は frozen な :class:`ModelPricing` に保持する。
- ``effective_from`` は ``(provider, model, effective_from)`` 複合 PK のうち、
  ``request_date`` 以前で最新のものを採用する(時点単価)。
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Date, MetaData, Numeric, String, Table, select
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.llm.base import LlmProviderName

# 1M tokens あたり単価で表現するため、 トークン数をこの値で割って単価を掛ける。
_TOKENS_PER_UNIT: Final[Decimal] = Decimal(1_000_000)

# usage_log.cost_usd / plans.llm_cost_usd は NUMERIC(10, 6)。 丸め桁を合わせる。
_COST_QUANTUM: Final[Decimal] = Decimal("0.000001")

# llm_provider native ENUM(data-model.md)。 create_type=False で既存型を再利用し、
# マイグレーションが所有する型を二重定義しない。
_llm_provider_enum: Final[ENUM] = ENUM(
    "openai",
    "anthropic",
    "ollama",
    name="llm_provider",
    create_type=False,
)

# model_pricing への Core Table 定義(data-model.md `model_pricing` と整合)。
_metadata: Final[MetaData] = MetaData()

model_pricing_table: Final[Table] = Table(
    "model_pricing",
    _metadata,
    Column("provider", _llm_provider_enum, primary_key=True, nullable=False),
    Column("model", String, primary_key=True, nullable=False),
    Column("input_per_1m_usd", Numeric(10, 4), nullable=False),
    Column("cached_per_1m_usd", Numeric(10, 4), nullable=True),
    Column("output_per_1m_usd", Numeric(10, 4), nullable=False),
    Column("effective_from", Date, primary_key=True, nullable=False),
    Column("source_url", String, nullable=True),
)


class ModelPricing(BaseModel):
    """ある時点で有効な 1 モデルの単価(1M tokens あたり、 USD)。

    ``cached_per_1m_usd`` が ``None`` の provider/model では、 cached tokens にも
    ``input_per_1m_usd`` を適用する(割引なし)。
    """

    model_config = ConfigDict(frozen=True)

    provider: LlmProviderName
    model: str
    input_per_1m_usd: Decimal
    cached_per_1m_usd: Decimal | None
    output_per_1m_usd: Decimal
    effective_from: dt.date


class PricingNotFoundError(Exception):
    """指定 ``(provider, model, request_date)`` に有効な単価行が無い場合に送出する。

    呼び出し側(usage_writer / factory)は安全側に倒して ``cost_usd=0`` で記録するか、
    運用ルールで ``model_pricing`` を更新する判断材料にする。
    """

    def __init__(self, provider: str, model: str, request_date: dt.date) -> None:
        self.provider: Final[str] = provider
        self.model: Final[str] = model
        self.request_date: Final[dt.date] = request_date
        super().__init__(
            f"no model_pricing row for provider={provider!r} model={model!r} "
            f"effective on or before {request_date.isoformat()}"
        )


def _quantize_cost(value: Decimal) -> Decimal:
    """算出コストを ``NUMERIC(10, 6)`` に合わせて丸める(half-up)。"""
    return value.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)


def compute_cost(
    pricing: ModelPricing,
    *,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
) -> Decimal:
    """トークン数と単価から推定コスト(USD)を ``Decimal`` で算出する。

    prompt caching の扱い:

    - ``cached_tokens`` は ``prompt_tokens`` のうちキャッシュヒットした分とみなし、
      非キャッシュ分 ``prompt_tokens - cached_tokens`` には ``input_per_1m_usd`` を、
      キャッシュ分には ``cached_per_1m_usd``(無ければ ``input_per_1m_usd``)を適用する。
    - ``completion_tokens`` には ``output_per_1m_usd`` を適用する。

    Args:
        pricing: 解決済みの :class:`ModelPricing`。
        prompt_tokens: 入力トークン総数(キャッシュ分を含む)。
        cached_tokens: prompt caching でヒットした入力トークン数。
        completion_tokens: 出力トークン数。

    Returns:
        ``NUMERIC(10, 6)`` に丸めた推定コスト(USD)。

    Raises:
        ValueError: トークン数が負、 または ``cached_tokens > prompt_tokens`` の場合
            (境界での入力検証)。
    """
    if prompt_tokens < 0 or cached_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must be non-negative")
    if cached_tokens > prompt_tokens:
        raise ValueError(
            f"cached_tokens ({cached_tokens}) must not exceed prompt_tokens ({prompt_tokens})"
        )

    uncached_prompt_tokens = prompt_tokens - cached_tokens
    cached_rate = pricing.cached_per_1m_usd
    if cached_rate is None:
        cached_rate = pricing.input_per_1m_usd

    uncached_cost = (Decimal(uncached_prompt_tokens) / _TOKENS_PER_UNIT) * pricing.input_per_1m_usd
    cached_cost = (Decimal(cached_tokens) / _TOKENS_PER_UNIT) * cached_rate
    output_cost = (Decimal(completion_tokens) / _TOKENS_PER_UNIT) * pricing.output_per_1m_usd

    return _quantize_cost(uncached_cost + cached_cost + output_cost)


async def resolve_pricing(
    session: AsyncSession,
    *,
    provider: LlmProviderName,
    model: str,
    request_date: dt.date | None = None,
) -> ModelPricing:
    """``model_pricing`` から有効な単価行を解決する。

    ``(provider, model)`` で ``effective_from <= request_date`` を満たす行のうち、
    ``effective_from`` が最大(最新)の 1 行を採用する。

    Args:
        session: 呼び出し側が管理する SQLAlchemy AsyncSession。
        provider: LLM provider(``openai`` / ``anthropic`` / ``ollama``)。
        model: モデル ID。
        request_date: 単価を評価する基準日。 ``None`` なら UTC の今日を使う。

    Returns:
        解決した :class:`ModelPricing`。

    Raises:
        ValueError: ``model`` が空文字の場合(境界での入力検証)。
        PricingNotFoundError: 該当する単価行が存在しない場合。
    """
    if not model:
        raise ValueError("model must be a non-empty string")

    effective_date = request_date or dt.datetime.now(dt.UTC).date()

    stmt = (
        select(
            model_pricing_table.c.input_per_1m_usd,
            model_pricing_table.c.cached_per_1m_usd,
            model_pricing_table.c.output_per_1m_usd,
            model_pricing_table.c.effective_from,
        )
        .where(
            model_pricing_table.c.provider == provider,
            model_pricing_table.c.model == model,
            model_pricing_table.c.effective_from <= effective_date,
        )
        .order_by(model_pricing_table.c.effective_from.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise PricingNotFoundError(provider, model, effective_date)

    return ModelPricing(
        provider=provider,
        model=model,
        input_per_1m_usd=row.input_per_1m_usd,
        cached_per_1m_usd=row.cached_per_1m_usd,
        output_per_1m_usd=row.output_per_1m_usd,
        effective_from=row.effective_from,
    )


async def calculate_cost_usd(
    session: AsyncSession,
    *,
    provider: LlmProviderName,
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    request_date: dt.date | None = None,
) -> Decimal:
    """``model_pricing`` を参照してコストを算出するワンショットヘルパ。

    :func:`resolve_pricing` で単価を解決し、 :func:`compute_cost` で推定コストを返す。

    Raises:
        ValueError: 入力検証に失敗した場合。
        PricingNotFoundError: 単価行が存在しない場合。
    """
    pricing = await resolve_pricing(
        session,
        provider=provider,
        model=model,
        request_date=request_date,
    )
    return compute_cost(
        pricing,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        completion_tokens=completion_tokens,
    )
