"""``/llm`` 管理エンドポイント (US5, T117, contracts/backend-api.yaml ``/llm`` 系)。

マスター LLM provider の切替・モデル永続化・write-only API key 管理とコスト可視化を担う
(いずれも Basic 認証配下):

- ``GET /llm/providers`` — :class:`LlmSettingsResponse`
  (``active`` + 各 provider の ``credential_source`` / ``credential_configured``)。
- ``PUT /llm/providers`` — active provider / auth_mode / model を ``app_state`` に upsert。
  ``codex_oauth`` は未配線のため 422。 不正 model は 422。 Anthropic + 非 api_key は 400。
- ``PUT /llm/credentials`` / ``DELETE /llm/credentials/{provider}`` — Fernet 暗号化の
  write-only 鍵管理。 env SoT 時は 409。 平文は応答・ログに出さない。
- ``GET /llm/usage`` — 月次集計 (ADR-0024)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
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
from ymg_backend.llm.base import LlmError
from ymg_backend.llm.factory import (
    _KEY_LLM_AUTH_MODE,
    _KEY_LLM_MODEL,
    _KEY_LLM_PROVIDER,
    _read_app_state_overrides,
    app_state_table,
    resolve_provider_config,
    validate_model_for_provider,
)
from ymg_backend.llm.ollama_provider import _SUPPORTED_MODELS as _OLLAMA_MODELS
from ymg_backend.llm.openai_provider import _SUPPORTED_MODELS as _OPENAI_MODELS
from ymg_backend.llm.secrets import (
    CredentialSource,
    LlmSecretProvider,
    credential_configured,
    credential_source_for,
    delete_api_key,
    list_db_secret_providers,
    upsert_api_key,
)

router: Final = APIRouter(prefix="/llm", tags=["llm"])

LlmProvider = Literal["openai", "anthropic", "ollama"]
LlmAuthMode = Literal["api_key", "codex_oauth"]

# API key 入力 DoS 防止の実務上限 (OpenAPI / UI と同一)。
LLM_API_KEY_MAX_LENGTH: Final[int] = 2048

_ACTION_PROVIDER_CHANGED: Final[str] = "llm_provider_changed"
_ACTION_CREDENTIAL_SET: Final[str] = "llm_credential_set"
_ACTION_CREDENTIAL_DELETED: Final[str] = "llm_credential_deleted"

_ANTHROPIC_ALLOWED_AUTH_MODE: Final[str] = "api_key"

# UI 選択肢には残すが、 PUT は 422 で拒否する (Codex OAuth 未配線)。
_AUTH_MODES: Final[dict[LlmProvider, tuple[LlmAuthMode, ...]]] = {
    "openai": ("api_key", "codex_oauth"),
    "anthropic": ("api_key",),
    "ollama": ("api_key",),
}

_MODELS: Final[dict[LlmProvider, tuple[str, ...]]] = {
    "openai": _OPENAI_MODELS,
    "anthropic": _ANTHROPIC_MODELS,
    "ollama": _OLLAMA_MODELS,
}

_PROVIDER_ORDER: Final[tuple[LlmProvider, ...]] = ("openai", "anthropic", "ollama")
_MONTH_PATTERN: Final[str] = r"^\d{4}-\d{2}$"

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


class LlmActiveState(BaseModel):
    """現在 active な provider / auth_mode / model。"""

    provider: LlmProvider
    auth_mode: LlmAuthMode
    model: str


class LlmProviderConfig(BaseModel):
    """``GET /llm/providers`` の providers[] 要素。"""

    provider: LlmProvider
    available: bool
    auth_modes: list[LlmAuthMode]
    models: list[str]
    credential_source: CredentialSource
    credential_configured: bool
    # Codex OAuth 等、 UI で無効表示する認証方式。
    unsupported_auth_modes: list[LlmAuthMode] = Field(default_factory=list)


class LlmSettingsResponse(BaseModel):
    """``GET /llm/providers`` のレスポンス。"""

    active: LlmActiveState
    providers: list[LlmProviderConfig]


class LlmProviderPutBody(BaseModel):
    """``PUT /llm/providers`` のリクエストボディ。"""

    provider: LlmProvider
    auth_mode: LlmAuthMode
    model: str


class LlmProviderState(BaseModel):
    """``PUT /llm/providers`` のレスポンス。"""

    provider: LlmProvider
    auth_mode: LlmAuthMode
    model: str


class LlmCredentialPutBody(BaseModel):
    """``PUT /llm/credentials`` のボディ。 平文は応答に出さない。

    前後空白は保存前に strip する。 空白のみは拒否。 最大長は
    :data:`LLM_API_KEY_MAX_LENGTH` (入力 DoS 防止)。
    """

    provider: LlmSecretProvider
    api_key: str = Field(min_length=1, max_length=LLM_API_KEY_MAX_LENGTH)

    @field_validator("api_key", mode="before")
    @classmethod
    def _strip_api_key(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class LlmCredentialState(BaseModel):
    """credential 書込/削除後のメタデータのみ (秘密値なし)。"""

    provider: LlmSecretProvider
    credential_source: CredentialSource
    credential_configured: bool


class LlmProviderUsage(BaseModel):
    """``LlmUsage.by_provider`` の値。"""

    cost_usd: float
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int


class LlmUsageResponse(BaseModel):
    """``GET /llm/usage`` のレスポンス。"""

    month: str
    total_cost_usd: float
    budget_usd: float
    budget_pct: float
    by_provider: dict[str, LlmProviderUsage]


def _unsupported_auth_modes(provider: LlmProvider) -> list[LlmAuthMode]:
    """未配線の認証方式。 openai の codex_oauth のみ。"""
    if provider == "openai":
        return ["codex_oauth"]
    return []


def _validate_combination(provider: LlmProvider, auth_mode: LlmAuthMode) -> None:
    """provider / auth_mode の組合せを永続化前に検証する。"""
    if auth_mode == "codex_oauth":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Codex OAuth (auth_mode='codex_oauth') は未実装のため保存できません。"
                " auth_mode='api_key' を使用してください。"
            ),
        )
    if provider == "anthropic" and auth_mode != _ANTHROPIC_ALLOWED_AUTH_MODE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Anthropic は api_key 認証のみサポートします "
                f"(指定された auth_mode='{auth_mode}')。 subscription は FR-022 で禁止されています。"
            ),
        )


def _validate_model(provider: LlmProvider, model: str) -> None:
    """model が provider の supported に無ければ 422。"""
    try:
        validate_model_for_provider(provider, model)
    except LlmError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.message,
        ) from exc


def _month_bounds(month: str | None, *, now: datetime) -> tuple[str, datetime, datetime]:
    """``YYYY-MM`` から ``[month_start, next_month_start)`` の半開区間を返す。"""
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
    response_model=LlmSettingsResponse,
    summary="LLM settings: active provider and credential metadata",
)
async def list_providers(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LlmSettingsResponse:
    """active 設定と各 provider の選択肢 / credential メタデータを返す。"""
    del user
    active_cfg = await resolve_provider_config(
        settings,
        session=session,
        strict_model=False,
    )
    db_providers = await list_db_secret_providers(session)

    providers: list[LlmProviderConfig] = []
    for provider in _PROVIDER_ORDER:
        source = credential_source_for(
            settings,
            provider,
            has_db_secret=provider in db_providers,
        )
        configured = credential_configured(source)
        providers.append(
            LlmProviderConfig(
                provider=provider,
                available=configured,
                auth_modes=list(_AUTH_MODES[provider]),
                models=list(_MODELS[provider]),
                credential_source=source,
                credential_configured=configured,
                unsupported_auth_modes=_unsupported_auth_modes(provider),
            )
        )

    return LlmSettingsResponse(
        active=LlmActiveState(
            provider=active_cfg.provider,
            auth_mode=_narrow_auth_mode(active_cfg.auth_mode),
            model=active_cfg.model,
        ),
        providers=providers,
    )


@router.put(
    "/providers",
    response_model=LlmProviderState,
    summary="Change active LLM provider/model (no restart, ADR-0019)",
    responses={
        400: {"description": "Invalid combination (Anthropic + non-api_key)."},
        422: {"description": "Unsupported auth_mode (codex_oauth) or invalid model."},
    },
)
async def set_provider(
    body: LlmProviderPutBody,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LlmProviderState:
    """active provider / auth_mode / model を切替える。

    リクエストは厳格検証する。 既存の永続値が provider/model 不整合でも、
    audit ``from`` 用の読取は non-strict (または LlmError を捕捉) し、
    有効な組合せへの修復 PUT が 500 にならないようにする。
    """
    _validate_combination(body.provider, body.auth_mode)
    _validate_model(body.provider, body.model)

    current_from = await _read_active_for_audit(session)
    if (
        current_from["provider"] == body.provider
        and current_from["auth_mode"] == body.auth_mode
        and current_from["model"] == body.model
    ):
        return LlmProviderState(
            provider=body.provider,
            auth_mode=body.auth_mode,
            model=body.model,
        )

    await _upsert_app_state(session, _KEY_LLM_PROVIDER, body.provider)
    await _upsert_app_state(session, _KEY_LLM_AUTH_MODE, body.auth_mode)
    await _upsert_app_state(session, _KEY_LLM_MODEL, body.model)
    await write_audit_log(
        session,
        action=_ACTION_PROVIDER_CHANGED,
        actor=user,
        target_type="app_state",
        target_id=_KEY_LLM_PROVIDER,
        payload={
            "actor": user,
            "from": current_from,
            "to": {
                "provider": body.provider,
                "auth_mode": body.auth_mode,
                "model": body.model,
            },
        },
    )
    await session.commit()

    # 検証済み body を永続化した直後なので、応答は body から決定的に返す
    # (再 resolve 失敗で 500 にしない)。
    return LlmProviderState(
        provider=body.provider,
        auth_mode=body.auth_mode,
        model=body.model,
    )


@router.put(
    "/credentials",
    response_model=LlmCredentialState,
    summary="Set/replace encrypted LLM API key (write-only)",
    responses={
        409: {"description": "Environment variable is source of truth; cannot override."},
        422: {"description": "Empty, whitespace-only, or too-long api_key."},
    },
)
async def put_credential(
    body: LlmCredentialPutBody,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LlmCredentialState:
    """API key を Fernet 暗号化して upsert する。 応答に平文は含めない。"""
    # strip / 空 / max_length は LlmCredentialPutBody で検証済み。
    try:
        await upsert_api_key(session, settings, body.provider, body.api_key)
    except RuntimeError as exc:
        if str(exc) == "credential_source_is_env":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "環境変数で API key が設定済みのため DB には保存できません(環境変数を使用)。"
                ),
            ) from exc
        raise
    await write_audit_log(
        session,
        action=_ACTION_CREDENTIAL_SET,
        actor=user,
        target_type="llm_provider_secrets",
        target_id=body.provider,
        payload={"actor": user, "provider": body.provider, "op": "set"},
    )
    await session.commit()
    return LlmCredentialState(
        provider=body.provider,
        credential_source="db",
        credential_configured=True,
    )


@router.delete(
    "/credentials/{provider}",
    response_model=LlmCredentialState,
    summary="Delete DB-stored LLM API key (env unaffected)",
    responses={
        409: {"description": "Environment variable is source of truth; cannot clear."},
        404: {"description": "No DB-stored credential for this provider."},
    },
)
async def clear_credential(
    provider: LlmSecretProvider,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LlmCredentialState:
    """DB の暗号化行のみ削除する。 env がある場合は 409。 行が無ければ 404。"""
    try:
        deleted = await delete_api_key(session, settings, provider)
    except RuntimeError as exc:
        if str(exc) == "credential_source_is_env":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "環境変数で API key が設定済みのため DB キーは削除できません(環境変数を使用)。"
                ),
            ) from exc
        raise
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"provider='{provider}' の DB 資格情報は存在しません。",
        )
    await write_audit_log(
        session,
        action=_ACTION_CREDENTIAL_DELETED,
        actor=user,
        target_type="llm_provider_secrets",
        target_id=provider,
        payload={"actor": user, "provider": provider, "op": "delete"},
    )
    await session.commit()
    return LlmCredentialState(
        provider=provider,
        credential_source="none",
        credential_configured=False,
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
    """指定月 (既定: 当月 UTC) の ``usage_log`` を集計してコストと予算進捗を返す。"""
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


async def _upsert_app_state(session: AsyncSession, key: str, value: str) -> None:
    """``app_state`` の ``key`` を ``value`` に upsert する (``updated_at`` も更新)。"""
    stmt = (
        insert(app_state_table)
        .values(key=key, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=[app_state_table.c.key],
            set_={"value": value, "updated_at": func.now()},
        )
    )
    await session.execute(stmt)
    await session.flush()


async def _read_active_for_audit(session: AsyncSession) -> dict[str, str]:
    """audit ``from`` 用に現行 active を読む。

    provider/model 不整合でも GET と同様に non-strict で読む。
    未知 provider / codex_oauth 等で ``LlmError`` になる場合は
    app_state / env の生値を audit 用に残す。
    """
    try:
        current = await resolve_provider_config(
            get_settings(),
            session=session,
            strict_model=False,
        )
    except LlmError:
        settings = get_settings()
        overrides = await _read_app_state_overrides(session)
        return {
            "provider": overrides.get(_KEY_LLM_PROVIDER, settings.llm_provider),
            "auth_mode": overrides.get(_KEY_LLM_AUTH_MODE, settings.llm_auth_mode),
            "model": overrides.get(_KEY_LLM_MODEL, ""),
        }
    return {
        "provider": current.provider,
        "auth_mode": current.auth_mode,
        "model": current.model,
    }


def _narrow_auth_mode(raw: str) -> LlmAuthMode:
    """app_state / env 由来の auth_mode を contract enum に絞り込む。"""
    return "codex_oauth" if raw == "codex_oauth" else "api_key"


def _to_float(value: object) -> float:
    """SUM 結果を float へ正規化する。"""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))


def _to_int(value: object) -> int:
    """SUM 結果を int へ正規化する。"""
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return int(str(value))


__all__ = [
    "LlmActiveState",
    "LlmCredentialPutBody",
    "LlmCredentialState",
    "LlmProviderConfig",
    "LlmProviderPutBody",
    "LlmProviderState",
    "LlmProviderUsage",
    "LlmSettingsResponse",
    "LlmUsageResponse",
    "router",
]
