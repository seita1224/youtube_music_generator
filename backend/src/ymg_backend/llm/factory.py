"""active な :class:`LlmProvider` を解決する factory (T034)。

env(:class:`~ymg_backend.core.config.Settings`)を基底に、 任意で渡された
``app_state`` テーブル(``llm_provider`` / ``llm_auth_mode``)で上書きして
active provider / auth_mode を解決し、 対応する provider 実装を生成する。

参照:
- ADR-0019(provider 実装方針 / 認証方式 / 管理 UI からの再起動なし切替)
- contracts/llm-provider-interface.md(provider 一覧、 禁止組み合わせ)
- data-model.md `app_state`(``llm_provider`` / ``llm_auth_mode`` seed)
- spec.md FR-022(Anthropic SDK の subscription 利用を起動時に拒否)

設計方針:

- **app_state は env を上書きする**(ADR-0019「管理 UI からも切替可能、 再起動なし」)。
  ``session`` 未指定なら env のみで解決し、 起動時バリデーションにも使える。
- ORM model 層(T021)に依存しないよう、 ``app_state`` 参照は SQLAlchemy Core の
  軽量 Table 定義を本モジュールに閉じて持つ(``llm/pricing.py`` / ``llm/usage_writer.py``
  と同じ疎結合方針)。
- **Anthropic + subscription(= api_key 以外)は起動時拒否**(FR-022): factory 時点で
  :class:`~ymg_backend.llm.base.LlmError`(category="fatal")を送出する。
  ``AnthropicProvider`` 自身も同じ検証を持つが、 factory で早期に拒否してメッセージを統一する。
- 秘密値(API key)は ``Settings`` の ``SecretStr`` から生成直前に取り出し、
  provider 生成後は保持しない(各 provider が漏洩面を最小化する設計)。
- provider 別の既定モデルは各 provider モジュールの private 定数に依存せず、
  本モジュールの :data:`_DEFAULT_MODELS` で明示管理する(関心の分離)。
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Final, cast

from sqlalchemy import Column, MetaData, String, Table, select

from ymg_backend.llm.anthropic_provider import AnthropicProvider
from ymg_backend.llm.base import LlmError, LlmProvider, LlmProviderName
from ymg_backend.llm.ollama_provider import OllamaProvider
from ymg_backend.llm.openai_provider import OpenAIProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.core.config import Settings

# Anthropic が許可する唯一の認証方式(FR-022 / ADR-0019)。
_ANTHROPIC_ALLOWED_AUTH_MODE: Final[str] = "api_key"

# app_state テーブルのキー(data-model.md §app_state seed)。
_KEY_LLM_PROVIDER: Final[str] = "llm_provider"
_KEY_LLM_AUTH_MODE: Final[str] = "llm_auth_mode"

# provider 別の既定モデル(contracts/llm-provider-interface.md / ADR-0019)。
# 各 provider の supported_models 先頭(推奨既定)に合わせる。
_DEFAULT_MODELS: Final[dict[LlmProviderName, str]] = {
    "openai": "gpt-4.1",
    "anthropic": "claude-sonnet-4-6",
    "ollama": "qwen2.5:3b",
}

# 有効な provider 名(境界での入力検証用)。
_VALID_PROVIDERS: Final[frozenset[str]] = frozenset(_DEFAULT_MODELS)

# app_state.value への疎結合参照用 Core Table(ORM 層非依存)。
# JSONB は実体スキーマに合わせるが、 読み取りは生 JSON 文字列として扱う。
_metadata: Final[MetaData] = MetaData()


def _build_app_state_table() -> Table:
    """``app_state`` を参照する Core Table を構築する(JSONB は遅延 import)。"""
    from sqlalchemy.dialects.postgresql import JSONB as _JSONB

    return Table(
        "app_state",
        _metadata,
        Column("key", String, primary_key=True, nullable=False),
        Column("value", _JSONB, nullable=False),
    )


app_state_table: Final[Table] = _build_app_state_table()


class ProviderConfig:
    """解決済みの active provider 設定(不変)。

    ``provider`` は :data:`_VALID_PROVIDERS` のいずれか、 ``auth_mode`` は env / app_state
    から得た認証方式(検証前の生値もありうるため ``str``)、 ``model`` は既定モデル ID。
    """

    __slots__ = ("auth_mode", "model", "provider")

    def __init__(self, *, provider: LlmProviderName, auth_mode: str, model: str) -> None:
        self.provider: Final[LlmProviderName] = provider
        self.auth_mode: Final[str] = auth_mode
        self.model: Final[str] = model


def _coerce_provider(raw: str) -> LlmProviderName:
    """provider 文字列を検証して :data:`LlmProviderName` に絞り込む。

    Raises:
        LlmError: 未知の provider 名の場合(category="fatal")。
    """
    if raw not in _VALID_PROVIDERS:
        raise LlmError(
            category="fatal",
            message=(f"未知の llm_provider='{raw}' です。 有効値: {sorted(_VALID_PROVIDERS)}。"),
            retryable=False,
        )
    return cast(LlmProviderName, raw)


def _decode_app_state_value(raw: object) -> str | None:
    """app_state.value(JSONB)を文字列に正規化する。

    asyncpg は JSONB を Python オブジェクトに既にデコードして返す場合と、
    生 JSON 文字列で返す場合がある。 文字列に JSON literal が入っていれば
    一段デコードし、 最終的に ``str`` のみを採用する(それ以外は ``None``)。
    """
    value: object = raw
    if isinstance(value, str):
        # 生文字列がそのまま値(例: "openai")のケースは json.loads が失敗するため許容する。
        with contextlib.suppress(ValueError, TypeError):
            value = json.loads(value)
    return value if isinstance(value, str) else None


async def _read_app_state_overrides(session: AsyncSession) -> dict[str, str]:
    """``app_state`` から ``llm_provider`` / ``llm_auth_mode`` の上書き値を読む。

    取得できなかったキーは結果 dict に含めない(= env 値を採用する)。
    """
    stmt = select(app_state_table.c.key, app_state_table.c.value).where(
        app_state_table.c.key.in_((_KEY_LLM_PROVIDER, _KEY_LLM_AUTH_MODE))
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
) -> ProviderConfig:
    """env を基底に、 任意で app_state を上書きして active provider 設定を解決する。

    Args:
        settings: env / ``.env`` から読み込んだ :class:`Settings`。
        session: ``app_state`` を参照する AsyncSession。 ``None`` なら env のみで解決する
            (起動時バリデーション用途)。

    Returns:
        解決済みの :class:`ProviderConfig`(provider / auth_mode / 既定 model)。

    Raises:
        LlmError: provider 名が未知の場合(category="fatal")。
    """
    provider_raw: str = settings.llm_provider
    auth_mode: str = settings.llm_auth_mode

    if session is not None:
        overrides = await _read_app_state_overrides(session)
        provider_raw = overrides.get(_KEY_LLM_PROVIDER, provider_raw)
        auth_mode = overrides.get(_KEY_LLM_AUTH_MODE, auth_mode)

    provider = _coerce_provider(provider_raw)
    return ProviderConfig(
        provider=provider,
        auth_mode=auth_mode,
        model=_DEFAULT_MODELS[provider],
    )


def _build_openai(settings: Settings, config: ProviderConfig) -> OpenAIProvider:
    """``OpenAIProvider`` を生成する(api_key / codex_oauth 共通の API key ルート)。"""
    return OpenAIProvider.from_api_key(
        settings.openai_api_key.get_secret_value(),
        model=config.model,
    )


def _build_anthropic(settings: Settings, config: ProviderConfig) -> AnthropicProvider:
    """``AnthropicProvider`` を生成する。 subscription 等は FR-022 で拒否する。

    Raises:
        LlmError: ``auth_mode`` が ``api_key`` 以外の場合(category="fatal")。
    """
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
    return AnthropicProvider(
        api_key=settings.anthropic_api_key.get_secret_value(),
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
    """active provider を解決して :class:`LlmProvider` 実装を生成する。

    env(:class:`Settings`)を基底に、 ``session`` が与えられれば ``app_state`` の
    ``llm_provider`` / ``llm_auth_mode`` で上書きして active provider を決める(ADR-0019:
    管理 UI からの再起動なし切替)。 ``session`` 省略時は env のみで解決し、
    起動時バリデーション(FR-022 の早期拒否)に使える。

    Args:
        settings: env / ``.env`` から読み込んだ :class:`Settings`。
        session: ``app_state`` を参照する AsyncSession(任意)。

    Returns:
        生成済みの :class:`LlmProvider`(``openai`` / ``anthropic`` / ``ollama``)。

    Raises:
        LlmError: provider 名が未知、 もしくは Anthropic に ``api_key`` 以外の
            ``auth_mode`` が指定された場合(category="fatal"、 FR-022)。
    """
    config = await resolve_provider_config(settings, session=session)

    if config.provider == "openai":
        return _build_openai(settings, config)
    if config.provider == "anthropic":
        return _build_anthropic(settings, config)
    return _build_ollama(settings, config)
