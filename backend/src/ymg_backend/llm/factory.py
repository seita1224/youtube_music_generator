"""active な :class:`LlmProvider` を解決する factory (T034)。

env(:class:`~ymg_backend.core.config.Settings`)を基底に、 任意で渡された
``app_state`` テーブル(``llm_provider`` / ``llm_auth_mode`` / ``llm_model``)で上書きして
active provider / auth_mode / model を解決し、 対応する provider 実装を生成する。

API key は :mod:`ymg_backend.llm.secrets` の順位 (env 非空 > DB Fernet > none) で解決する。

参照:
- ADR-0019(provider 実装方針 / 認証方式 / 管理 UI からの再起動なし切替)
- contracts/llm-provider-interface.md(provider 一覧、 禁止組み合わせ)
- data-model.md `app_state`(``llm_provider`` / ``llm_auth_mode`` / ``llm_model``)
- spec.md FR-022(Anthropic SDK の subscription 利用を起動時に拒否)

設計方針:

- **app_state は env を上書きする**(ADR-0019「管理 UI からも切替可能、 再起動なし」)。
  ``session`` 未指定なら env のみで解決し、 起動時バリデーションにも使える。
- ORM model 層に依存しないよう、 ``app_state`` 参照は SQLAlchemy Core の
  軽量 Table 定義を本モジュールに閉じて持つ。
- **Anthropic + subscription(= api_key 以外)は起動時拒否**(FR-022)。
- **Codex OAuth は未配線**: factory は ``codex_oauth`` を fatal で拒否する (ADR-0019 追記)。
- 秘密値は生成直前に取り出し、 provider 生成後は保持しない。
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Final, cast

from sqlalchemy import Column, DateTime, MetaData, String, Table, select

from ymg_backend.llm.anthropic_provider import _SUPPORTED_MODELS as _ANTHROPIC_MODELS
from ymg_backend.llm.anthropic_provider import AnthropicProvider
from ymg_backend.llm.base import LlmError, LlmProvider, LlmProviderName
from ymg_backend.llm.ollama_provider import _SUPPORTED_MODELS as _OLLAMA_MODELS
from ymg_backend.llm.ollama_provider import OllamaProvider
from ymg_backend.llm.openai_provider import _SUPPORTED_MODELS as _OPENAI_MODELS
from ymg_backend.llm.openai_provider import OpenAIProvider
from ymg_backend.llm.secrets import resolve_api_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.core.config import Settings

# Anthropic が許可する唯一の認証方式(FR-022 / ADR-0019)。
_ANTHROPIC_ALLOWED_AUTH_MODE: Final[str] = "api_key"

# app_state テーブルのキー(data-model.md §app_state seed)。
_KEY_LLM_PROVIDER: Final[str] = "llm_provider"
_KEY_LLM_AUTH_MODE: Final[str] = "llm_auth_mode"
_KEY_LLM_MODEL: Final[str] = "llm_model"

# provider 別の既定モデル(contracts/llm-provider-interface.md / ADR-0019)。
_DEFAULT_MODELS: Final[dict[LlmProviderName, str]] = {
    "openai": "gpt-4.1",
    "anthropic": "claude-sonnet-4-6",
    "ollama": "qwen2.5:3b",
}

_SUPPORTED_MODELS: Final[dict[LlmProviderName, frozenset[str]]] = {
    "openai": frozenset(_OPENAI_MODELS),
    "anthropic": frozenset(_ANTHROPIC_MODELS),
    "ollama": frozenset(_OLLAMA_MODELS),
}

_VALID_PROVIDERS: Final[frozenset[str]] = frozenset(_DEFAULT_MODELS)

_metadata: Final[MetaData] = MetaData()


def _build_app_state_table() -> Table:
    """``app_state`` を参照する Core Table を構築する(JSONB は遅延 import)。"""
    from sqlalchemy.dialects.postgresql import JSONB as _JSONB

    return Table(
        "app_state",
        _metadata,
        Column("key", String, primary_key=True, nullable=False),
        Column("value", _JSONB, nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )


app_state_table: Final[Table] = _build_app_state_table()


class ProviderConfig:
    """解決済みの active provider 設定(不変)。"""

    __slots__ = ("auth_mode", "model", "provider")

    def __init__(self, *, provider: LlmProviderName, auth_mode: str, model: str) -> None:
        self.provider: Final[LlmProviderName] = provider
        self.auth_mode: Final[str] = auth_mode
        self.model: Final[str] = model


def _coerce_provider(raw: str) -> LlmProviderName:
    """provider 文字列を検証して :data:`_VALID_PROVIDERS` に絞り込む。"""
    if raw not in _VALID_PROVIDERS:
        raise LlmError(
            category="fatal",
            message=(f"未知の llm_provider='{raw}' です。 有効値: {sorted(_VALID_PROVIDERS)}。"),
            retryable=False,
        )
    return cast(LlmProviderName, raw)


def _decode_app_state_value(raw: object) -> str | None:
    """app_state.value(JSONB)を文字列に正規化する。"""
    value: object = raw
    if isinstance(value, str):
        with contextlib.suppress(ValueError, TypeError):
            value = json.loads(value)
    return value if isinstance(value, str) else None


def resolve_model_for_provider(
    provider: LlmProviderName,
    model: str | None,
    *,
    strict: bool = True,
) -> str:
    """model が provider の supported に含まれるなら採用、 未設定なら既定モデル。

    ``strict=True`` (factory 既定) では、 永続化済み model が provider と
    不整合なら silent fallback せず :class:`LlmError` を送出する。
    ``strict=False`` は GET active 向けに永続値をそのまま返す。
    """
    if model is None:
        return _DEFAULT_MODELS[provider]
    if model in _SUPPORTED_MODELS[provider]:
        return model
    if not strict:
        return model
    raise LlmError(
        category="fatal",
        message=(
            f"model='{model}' は provider='{provider}' の対応モデルではありません。"
            f" 有効値: {sorted(_SUPPORTED_MODELS[provider])}。"
        ),
        retryable=False,
    )


def validate_model_for_provider(provider: LlmProviderName, model: str) -> None:
    """model が provider の supported に無ければ LlmError(fatal)。"""
    if model not in _SUPPORTED_MODELS[provider]:
        raise LlmError(
            category="fatal",
            message=(
                f"model='{model}' は provider='{provider}' の対応モデルではありません。"
                f" 有効値: {sorted(_SUPPORTED_MODELS[provider])}。"
            ),
            retryable=False,
        )


async def _read_app_state_overrides(session: AsyncSession) -> dict[str, str]:
    """``app_state`` から llm_* 上書き値を読む。"""
    keys = (_KEY_LLM_PROVIDER, _KEY_LLM_AUTH_MODE, _KEY_LLM_MODEL)
    stmt = select(app_state_table.c.key, app_state_table.c.value).where(
        app_state_table.c.key.in_(keys)
    )
    rows = (await session.execute(stmt)).all()
    overrides: dict[str, str] = {}
    for row in rows:
        decoded = _decode_app_state_value(row.value)
        if decoded is not None:
            overrides[row.key] = decoded
    return overrides


async def resolve_provider_config(
    settings: Settings,
    *,
    session: AsyncSession | None = None,
    strict_model: bool = True,
) -> ProviderConfig:
    """env を基底に、 任意で app_state を上書きして active provider 設定を解決する。

    ``strict_model=True`` (factory 既定) では provider/model 不整合を fatal で拒否する。
    ``strict_model=False`` は GET active 向けに永続 model をそのまま返す。
    """
    provider_raw: str = settings.llm_provider
    auth_mode: str = settings.llm_auth_mode
    model_raw: str | None = None

    if session is not None:
        overrides = await _read_app_state_overrides(session)
        provider_raw = overrides.get(_KEY_LLM_PROVIDER, provider_raw)
        auth_mode = overrides.get(_KEY_LLM_AUTH_MODE, auth_mode)
        model_raw = overrides.get(_KEY_LLM_MODEL)

    provider = _coerce_provider(provider_raw)
    if auth_mode == "codex_oauth":
        raise LlmError(
            category="fatal",
            message=(
                "Codex OAuth (auth_mode='codex_oauth') は未実装です。"
                " API key 認証 (auth_mode='api_key') を使用してください。"
            ),
            retryable=False,
        )
    return ProviderConfig(
        provider=provider,
        auth_mode=auth_mode,
        model=resolve_model_for_provider(provider, model_raw, strict=strict_model),
    )


async def _resolve_cloud_api_key(
    settings: Settings,
    provider: LlmProviderName,
    *,
    session: AsyncSession | None,
) -> str:
    """openai / anthropic の API key を env > DB で解決する。 無ければ fatal。"""
    if provider not in ("openai", "anthropic"):
        raise LlmError(
            category="fatal",
            message=f"cloud API key は openai/anthropic のみです (provider='{provider}')。",
            retryable=False,
        )
    api_key, source = await resolve_api_key(
        settings,
        provider,  # narrowed above to openai|anthropic
        session=session,
    )
    if not api_key:
        raise LlmError(
            category="fatal",
            message=(
                f"provider='{provider}' の API key が未設定です (credential_source={source})。"
            ),
            retryable=False,
        )
    return api_key


async def _build_openai(
    settings: Settings,
    config: ProviderConfig,
    *,
    session: AsyncSession | None,
) -> OpenAIProvider:
    """``OpenAIProvider`` を生成する。 ``codex_oauth`` は未配線のため拒否。"""
    if config.auth_mode == "codex_oauth":
        raise LlmError(
            category="fatal",
            message=(
                "Codex OAuth (auth_mode='codex_oauth') は未実装です。"
                " API key 認証 (auth_mode='api_key') を使用してください。"
            ),
            retryable=False,
        )
    api_key = await _resolve_cloud_api_key(settings, "openai", session=session)
    return OpenAIProvider.from_api_key(api_key, model=config.model)


async def _build_anthropic(
    settings: Settings,
    config: ProviderConfig,
    *,
    session: AsyncSession | None,
) -> AnthropicProvider:
    """``AnthropicProvider`` を生成する。 subscription 等は FR-022 で拒否する。"""
    if config.auth_mode != _ANTHROPIC_ALLOWED_AUTH_MODE:
        raise LlmError(
            category="fatal",
            message=(
                "Anthropic は API key 認証のみサポートします"
                f"(指定された auth_mode='{config.auth_mode}')。"
                " サブスクリプション認証は 2026-02-19 に公式禁止されました(FR-022)。"
            ),
            retryable=False,
        )
    api_key = await _resolve_cloud_api_key(settings, "anthropic", session=session)
    return AnthropicProvider(
        api_key=api_key,
        default_model=config.model,
        auth_mode=config.auth_mode,
    )


def _build_ollama(settings: Settings, config: ProviderConfig) -> OllamaProvider:
    """``OllamaProvider`` を生成する(ローカル、 認証なし)。"""
    return OllamaProvider(settings.ollama_base_url, config.model)


async def create_llm_provider(
    settings: Settings,
    *,
    session: AsyncSession | None = None,
) -> LlmProvider:
    """active provider を解決して :class:`LlmProvider` 実装を生成する。"""
    config = await resolve_provider_config(settings, session=session)

    if config.provider == "openai":
        return await _build_openai(settings, config, session=session)
    if config.provider == "anthropic":
        return await _build_anthropic(settings, config, session=session)
    return _build_ollama(settings, config)


__all__ = [
    "_DEFAULT_MODELS",
    "_KEY_LLM_AUTH_MODE",
    "_KEY_LLM_MODEL",
    "_KEY_LLM_PROVIDER",
    "_SUPPORTED_MODELS",
    "ProviderConfig",
    "app_state_table",
    "create_llm_provider",
    "resolve_model_for_provider",
    "resolve_provider_config",
    "validate_model_for_provider",
]
