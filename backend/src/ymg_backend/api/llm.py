"""``/llm`` 管理エンドポイント (US5, T117, contracts/backend-api.yaml ``/llm`` 系)。

マスター LLM provider の切替とコスト可視化を担う 3 route を提供する (いずれも Basic 認証
配下。 配線は ``main.py:_build_protected_router`` が ``include_router(router)`` する後段の
責務):

- ``GET /llm/providers`` — 有効な provider 選択肢 (:class:`LlmProviderConfig` のリスト)。
  各要素は ``available`` (API key / 接続が設定済みか)、 ``auth_modes`` (許可された認証方式)、
  ``models`` (対応モデル) を持つ。 現在 active な provider/auth_mode は contract 上の
  別フィールドを持たないため、 UI 側が別途判定する (ADR-0019)。
- ``PUT /llm/providers`` — active provider を切替える (ADR-0019: 管理 UI から再起動なし)。
  ``provider`` / ``auth_mode`` を ``app_state`` に 2 キー upsert し、 切替を ``audit_log`` に
  記録する。 ``Anthropic + 非 api_key`` (subscription) は永続化前に 400 で弾く (FR-022)。
- ``GET /llm/usage`` — 当月 (or ``month=YYYY-MM`` 指定月) の ``usage_log`` を集計し、
  合計コスト + provider 別内訳 + 月次予算と進捗% を返す (:class:`LlmUsageResponse`, ADR-0024)。

設計方針:

- ``app_state`` の active provider 読み出し (GET の現在値) と PUT の妥当性検証は
  ``llm.factory`` を再利用し、 本モジュールでは重複定義しない (``resolve_provider_config`` /
  ``app_state_table``)。 PUT の upsert は ``api/scheduler.py:_upsert_flag`` を踏襲する。
- ``app_state.value`` は **JSON literal** で保持する (factory の ``_decode_app_state_value`` が
  一段 ``json.loads`` する前提)。 JSONB 列に Python ``str`` を渡すと SQLAlchemy が JSON
  エンコードして ``'"openai"'`` 相当を格納するため、 読取側と往復一致する。
- ``commit`` は本ハンドラの責務 (HTTP リクエスト = トランザクション境界)。 ``write_audit_log``
  は flush までなので、 app_state upsert と audit を 1 トランザクションでまとめて commit する。
- usage 集計は ``usage_log_table`` (``llm/usage_writer.py``) を流用するが、 同テーブルは
  ``created_at`` を非宣言なので、 月境界フィルタ用に ``created_at`` 付きの軽量 Core Table を
  本モジュールに閉じて再宣言する (pricing / usage_writer と同じ疎結合方針)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    Numeric,
    String,
    Table,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import BasicAuthUser
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.llm.anthropic_provider import _SUPPORTED_MODELS as _ANTHROPIC_MODELS
from ymg_backend.llm.factory import app_state_table, resolve_provider_config
from ymg_backend.llm.ollama_provider import _SUPPORTED_MODELS as _OLLAMA_MODELS
from ymg_backend.llm.openai_provider import _SUPPORTED_MODELS as _OPENAI_MODELS

router: Final = APIRouter(prefix="/llm", tags=["llm"])

# contract enum 型 (backend-api.yaml LlmProviderConfig / PUT body)。
LlmProvider = Literal["openai", "anthropic", "ollama"]
LlmAuthMode = Literal["api_key", "codex_oauth"]

# app_state のキー (data-model.md §app_state seed / factory._KEY_*)。
_KEY_LLM_PROVIDER: Final[str] = "llm_provider"
_KEY_LLM_AUTH_MODE: Final[str] = "llm_auth_mode"

# audit_log の action 名 (provider 切替, ADR-0019)。
_ACTION_PROVIDER_CHANGED: Final[str] = "llm_provider_changed"

# Anthropic が許可する唯一の認証方式 (FR-022 / ADR-0019 / factory と整合)。
_ANTHROPIC_ALLOWED_AUTH_MODE: Final[str] = "api_key"

# provider 別の許可された認証方式 (contract LlmProviderConfig.auth_modes)。
# openai のみ codex_oauth 併用可 (config.py:94 の settings バリデータと同趣旨)。
_AUTH_MODES: Final[dict[LlmProvider, tuple[LlmAuthMode, ...]]] = {
    "openai": ("api_key", "codex_oauth"),
    "anthropic": ("api_key",),
    "ollama": ("api_key",),
}

# provider 別の対応モデル一覧 (各 provider の ``_SUPPORTED_MODELS`` を参照、 実構築しない)。
_MODELS: Final[dict[LlmProvider, tuple[str, ...]]] = {
    "openai": _OPENAI_MODELS,
    "anthropic": _ANTHROPIC_MODELS,
    "ollama": _OLLAMA_MODELS,
}

# provider 表示順 (contract のリスト順、 UI の安定描画用)。
_PROVIDER_ORDER: Final[tuple[LlmProvider, ...]] = ("openai", "anthropic", "ollama")

# usage 集計用の月境界フィルタ正規表現 (Query パターン検証で 422 を返す)。
_MONTH_PATTERN: Final[str] = r"^\d{4}-\d{2}$"

# usage_log 集計用の Core Table (writer 側は created_at 非宣言のため本モジュールで再宣言)。
# 月境界フィルタに created_at が必要。 ORM 層には依存しない (疎結合方針)。
_metadata: Final[MetaData] = MetaData()

usage_log_table: Final[Table] = Table(
    "usage_log",
    _metadata,
    Column("provider", String, nullable=False),
    Column("prompt_tokens", Numeric, nullable=False),
    Column("cached_tokens", Numeric, nullable=False),
    Column("completion_tokens", Numeric, nullable=False),
    Column("cost_usd", Numeric(10, 6), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class LlmProviderConfig(BaseModel):
    """``GET /llm/providers`` の 1 要素 (backend-api.yaml LlmProviderConfig)。"""

    provider: LlmProvider
    available: bool
    auth_modes: list[LlmAuthMode]
    models: list[str]


class LlmProviderPutBody(BaseModel):
    """``PUT /llm/providers`` のリクエストボディ (contract: ``required: [provider, auth_mode]``)。"""

    provider: LlmProvider
    auth_mode: LlmAuthMode


class LlmProviderState(BaseModel):
    """``PUT /llm/providers`` のレスポンス (切替後の現在値)。"""

    provider: LlmProvider
    auth_mode: LlmAuthMode
    model: str


class LlmProviderUsage(BaseModel):
    """``LlmUsage.by_provider`` の値 (provider 別コスト / トークン内訳)。"""

    cost_usd: float
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int


class LlmUsageResponse(BaseModel):
    """``GET /llm/usage`` のレスポンス (backend-api.yaml LlmUsage)。"""

    month: str
    total_cost_usd: float
    budget_usd: float
    budget_pct: float
    by_provider: dict[str, LlmProviderUsage]


def _provider_available(provider: LlmProvider, settings: Settings) -> bool:
    """provider が利用可能か (対応する API key / 接続が設定済みか) を返す。

    openai / anthropic は対応 API key が非空かで判定する。 ollama はローカル実行で
    認証不要のため常に True (接続可否は health_check 側の責務)。
    """
    if provider == "openai":
        return bool(settings.openai_api_key.get_secret_value())
    if provider == "anthropic":
        return bool(settings.anthropic_api_key.get_secret_value())
    return True  # ollama


def _validate_combination(provider: LlmProvider, auth_mode: LlmAuthMode) -> None:
    """provider / auth_mode の組合せを永続化前に検証する (不正は 400)。

    - Anthropic + 非 api_key (subscription) は FR-022 で禁止 (factory._build_anthropic と同趣旨)。
    - codex_oauth は openai のみ (config.py:94 の settings バリデータと同趣旨。 PUT は
      app_state 経路なので独自に弾く)。

    provider を実構築せず組合せだけ検証するため I/O / 秘密値参照を伴わない (API key 未設定でも
    正しく 400 を返せる)。

    Raises:
        HTTPException: 不正な組合せの場合 (status 400)。
    """
    if provider == "anthropic" and auth_mode != _ANTHROPIC_ALLOWED_AUTH_MODE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Anthropic は api_key 認証のみサポートします "
                f"(指定された auth_mode='{auth_mode}')。 subscription は FR-022 で禁止されています。"
            ),
        )
    if auth_mode == "codex_oauth" and provider != "openai":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"auth_mode='codex_oauth' は provider='openai' のみサポートします "
                f"(指定された provider='{provider}')。"
            ),
        )


def _month_bounds(month: str | None, *, now: datetime) -> tuple[str, datetime, datetime]:
    """``YYYY-MM`` から ``[month_start, next_month_start)`` の半開区間を返す。

    ``month`` 省略時は ``now`` (UTC) の当月。 月境界は to_char 依存を避け index が効く
    範囲フィルタにする。 戻り値は ``(正規化月文字列, 月初, 翌月初)``。

    Raises:
        HTTPException: ``month`` が ``YYYY-MM`` だが月が 1..12 の範囲外の場合 (422)。
    """
    if month is None:
        year, mon = now.year, now.month
    else:
        year, mon = int(month[:4]), int(month[5:7])
        if not 1 <= mon <= 12:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"month の月部分は 01..12 である必要があります (指定: '{month}')。",
            )
    month_start = datetime(year, mon, 1, tzinfo=UTC)
    next_year, next_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    next_start = datetime(next_year, next_mon, 1, tzinfo=UTC)
    return f"{year:04d}-{mon:02d}", month_start, next_start


@router.get(
    "/providers",
    response_model=list[LlmProviderConfig],
    summary="List available LLM providers and auth modes",
)
async def list_providers(
    user: BasicAuthUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[LlmProviderConfig]:
    """有効な provider 選択肢を返す (ADR-0019)。

    各 provider の ``available`` (API key / 接続設定済みか)、 許可された ``auth_modes``、
    対応 ``models`` を contract 順 (openai → anthropic → ollama) で列挙する。 provider は
    実構築せず定数を参照する。
    """
    del user  # 認証のみ目的。
    return [
        LlmProviderConfig(
            provider=provider,
            available=_provider_available(provider, settings),
            auth_modes=list(_AUTH_MODES[provider]),
            models=list(_MODELS[provider]),
        )
        for provider in _PROVIDER_ORDER
    ]


@router.put(
    "/providers",
    response_model=LlmProviderState,
    summary="Change active LLM provider (no restart, ADR-0019)",
    responses={400: {"description": "Invalid combination (Anthropic + subscription forbidden)."}},
)
async def set_provider(
    body: LlmProviderPutBody,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LlmProviderState:
    """active provider / auth_mode を切替える (ADR-0019: 再起動なし切替)。

    永続化前に組合せを検証し (Anthropic + 非 api_key は 400)、 ``app_state`` の
    ``llm_provider`` / ``llm_auth_mode`` を 2 キー upsert、 切替を ``audit_log`` に記録する。
    同値要求は no-op (audit / 書込なし) で現状を返す。

    Args:
        body: ``{"provider": ..., "auth_mode": ...}``。
        user: Basic 認証済みユーザー名 (audit actor)。
        session: DB セッション (本ハンドラが commit する)。

    Returns:
        切替後の :class:`LlmProviderState` (provider / auth_mode / 既定 model)。

    Raises:
        HTTPException: provider / auth_mode の組合せが不正な場合 (400, FR-022)。
    """
    _validate_combination(body.provider, body.auth_mode)

    current = await resolve_provider_config(get_settings(), session=session)
    if current.provider == body.provider and current.auth_mode == body.auth_mode:
        # 同値: 書込 / audit せず現状を返す (scheduler.py と同方針)。
        return LlmProviderState(
            provider=current.provider,
            auth_mode=_narrow_auth_mode(current.auth_mode),
            model=current.model,
        )

    await _upsert_app_state(session, _KEY_LLM_PROVIDER, body.provider)
    await _upsert_app_state(session, _KEY_LLM_AUTH_MODE, body.auth_mode)
    await write_audit_log(
        session,
        action=_ACTION_PROVIDER_CHANGED,
        actor=user,
        target_type="app_state",
        target_id=_KEY_LLM_PROVIDER,
        payload={
            "actor": user,
            "from": {"provider": current.provider, "auth_mode": current.auth_mode},
            "to": {"provider": body.provider, "auth_mode": body.auth_mode},
        },
    )
    await session.commit()

    # 切替後の現在値を返す (model は factory が provider から既定解決)。
    updated = await resolve_provider_config(get_settings(), session=session)
    return LlmProviderState(
        provider=updated.provider,
        auth_mode=_narrow_auth_mode(updated.auth_mode),
        model=updated.model,
    )


@router.get(
    "/usage",
    response_model=LlmUsageResponse,
    summary="Monthly LLM usage and cost (ADR-0024)",
)
async def get_usage(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    month: Annotated[str | None, Query(pattern=_MONTH_PATTERN, examples=["2026-05"])] = None,
) -> LlmUsageResponse:
    """指定月 (既定: 当月 UTC) の ``usage_log`` を集計してコストと予算進捗を返す (ADR-0024)。

    ``cost_usd`` の合計と provider 別内訳 (cost + token) を ``[月初, 翌月初)`` の半開区間で
    SUM する。 予算進捗% は ``total / monthly_budget_usd * 100`` (budget<=0 は 0 でゼロ除算回避)。

    Args:
        user: Basic 認証済みユーザー名 (認証のみ目的)。
        session: DB セッション。
        settings: ``monthly_budget_usd`` 参照用。
        month: ``YYYY-MM``。 省略時は当月。 形式不一致は 422 (``Query(pattern=...)``)。

    Returns:
        :class:`LlmUsageResponse` (合計コスト + provider 別内訳 + 予算 + 進捗%)。
    """
    del user
    normalized, month_start, next_start = _month_bounds(month, now=datetime.now(UTC))

    in_range = (
        usage_log_table.c.created_at >= month_start,
        usage_log_table.c.created_at < next_start,
    )

    total_stmt = select(func.coalesce(func.sum(usage_log_table.c.cost_usd), 0)).where(*in_range)
    total_raw = (await session.execute(total_stmt)).scalar_one()
    total_cost = _to_float(total_raw)

    by_provider_stmt = (
        select(
            usage_log_table.c.provider,
            func.coalesce(func.sum(usage_log_table.c.cost_usd), 0),
            func.coalesce(func.sum(usage_log_table.c.prompt_tokens), 0),
            func.coalesce(func.sum(usage_log_table.c.cached_tokens), 0),
            func.coalesce(func.sum(usage_log_table.c.completion_tokens), 0),
        )
        .where(*in_range)
        .group_by(usage_log_table.c.provider)
    )
    by_provider: dict[str, LlmProviderUsage] = {}
    for provider, cost, prompt, cached, completion in (
        await session.execute(by_provider_stmt)
    ).all():
        by_provider[str(provider)] = LlmProviderUsage(
            cost_usd=_to_float(cost),
            prompt_tokens=_to_int(prompt),
            cached_tokens=_to_int(cached),
            completion_tokens=_to_int(completion),
        )

    budget = settings.monthly_budget_usd
    budget_pct = (total_cost / budget * 100.0) if budget > 0 else 0.0

    return LlmUsageResponse(
        month=normalized,
        total_cost_usd=total_cost,
        budget_usd=budget,
        budget_pct=budget_pct,
        by_provider=by_provider,
    )


# ---------------------------------------------------------------------------------
# 内部ヘルパ (app_state upsert / 型正規化)
# ---------------------------------------------------------------------------------
async def _upsert_app_state(session: AsyncSession, key: str, value: str) -> None:
    """``app_state`` の ``key`` を ``value`` に upsert する (JSONB 列 → JSON literal で格納)。

    ``api/scheduler.py:_upsert_flag`` を踏襲。 ``value`` (str) を JSONB 列に渡すと SQLAlchemy が
    JSON エンコードして ``'"openai"'`` 相当を格納するため、 factory の ``_decode_app_state_value``
    による一段デコードと往復一致する。 commit は呼び出し側の責務 (ここでは flush)。
    """
    stmt = (
        insert(app_state_table)
        .values(key=key, value=value)
        .on_conflict_do_update(
            index_elements=[app_state_table.c.key],
            set_={"value": value},
        )
    )
    await session.execute(stmt)
    await session.flush()


def _narrow_auth_mode(raw: str) -> LlmAuthMode:
    """app_state / env 由来の auth_mode (生 str) を contract enum に絞り込む。

    factory の ``ProviderConfig.auth_mode`` は検証前の生値もありうる (``str``)。 contract enum
    外の値は ``api_key`` に丸める (PUT 経路は ``_validate_combination`` で弾くため通常到達しない)。
    """
    return "codex_oauth" if raw == "codex_oauth" else "api_key"


def _to_float(value: object) -> float:
    """SUM 結果 (Decimal / int / None) を float へ正規化する (None は 0.0)。"""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))


def _to_int(value: object) -> int:
    """SUM 結果 (Decimal / int / None) を int へ正規化する (None は 0)。"""
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return int(str(value))


__all__ = [
    "LlmProviderConfig",
    "LlmProviderPutBody",
    "LlmProviderState",
    "LlmProviderUsage",
    "LlmUsageResponse",
    "router",
]
