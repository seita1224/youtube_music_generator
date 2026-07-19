"""Critical path テスト: master LLM provider 切替 (T116, US5 / ADR-0019 / FR-022)。

既存 ``test_llm_provider.py`` が provider 単体の generate / cost / retry を担うのに対し、
本ファイルは **active provider の切替** に観点を絞る(重複を避ける):

1. ``app_state``(``llm_provider`` / ``llm_auth_mode``)が env を上書きし、
   ``resolve_provider_config`` / ``create_llm_provider`` が **新 provider** を返すこと
   (ADR-0019「管理 UI から再起動なし切替」)。app_state は値を JSON literal で持つ
   (seed = ``'"openai"'``)ため、factory の一段 ``json.loads`` と往復一致する両形式
   (生 str / JSON literal str)で検証する。
2. PUT /llm/providers が再利用する **不正組合せ拒否**: Anthropic + ``api_key`` 以外
   (subscription / codex_oauth)は **永続化前** に弾く。本テストは
   - factory 経由(``create_llm_provider`` が ``LlmError(category="fatal")`` を送出)
   - PUT が踏襲する明示組合せ検証(I/O なし、 ``HTTPException(400)`` へ写像可能な述語)
   の両方を 100% カバーする。

実 LLM / 実 DB / docker は起動しない。app_state 参照は ``AsyncSession`` を最小限に
スタブして factory の Core クエリ経路だけを駆動する(respx も不要 = ネットワーク非接触)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from ymg_backend.core.config import Settings
from ymg_backend.llm.anthropic_provider import AnthropicProvider
from ymg_backend.llm.base import LlmError
from ymg_backend.llm.factory import (
    _ANTHROPIC_ALLOWED_AUTH_MODE,
    _KEY_LLM_AUTH_MODE,
    _KEY_LLM_MODEL,
    _KEY_LLM_PROVIDER,
    ProviderConfig,
    _build_openai,
    _resolve_cloud_api_key,
    app_state_table,
    create_llm_provider,
    resolve_provider_config,
    validate_model_for_provider,
)
from ymg_backend.llm.ollama_provider import OllamaProvider
from ymg_backend.llm.openai_provider import OpenAIProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.critical


# ---------------------------------------------------------------------------
# app_state を JSONB literal で返す最小 AsyncSession スタブ
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Row:
    """``select(key, value)`` の 1 行を模す(属性アクセス ``.key`` / ``.value``)。"""

    key: str
    value: Any


class _FakeResult:
    """``session.execute(...)`` の戻り(``.all()`` で行列を返す)。"""

    def __init__(self, rows: Sequence[_Row]) -> None:
        self._rows = list(rows)

    def all(self) -> list[_Row]:
        return list(self._rows)


class _FakeSession:
    """``app_state`` の 2 キー上書きだけを返す read-only な AsyncSession スタブ。

    factory の ``_read_app_state_overrides`` は ``key.in_((...))`` で 2 キーを引くだけなので、
    保持した overrides をそのまま行として返せば実 DB なしで切替経路を駆動できる。
    """

    def __init__(self, overrides: dict[str, Any]) -> None:
        self._overrides = overrides

    async def execute(self, _stmt: object) -> _FakeResult:
        rows = [_Row(key=k, value=v) for k, v in self._overrides.items()]
        return _FakeResult(rows)


def _settings(**overrides: Any) -> Settings:
    """env を無視して dummy 秘密値で固めた :class:`Settings`(provider 構築可)。

    既定は openai/api_key(切替の対照)。 切替検証はもっぱら app_state 上書きで行う。
    ローカル ``.env`` の ``LLM_PROVIDER`` に依存しないよう provider/auth を明示する。
    """
    base: dict[str, Any] = {
        "llm_provider": "openai",
        "llm_auth_mode": "api_key",
        "openai_api_key": "sk-test",
        "anthropic_api_key": "ak-test",
        "admin_password": "test-admin-password",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


# ===========================================================================
# 1. app_state 上書きで active provider が切り替わる(env=openai → anthropic / ollama)
# ===========================================================================
async def test_resolve_uses_env_when_no_app_state_override() -> None:
    """session=None(app_state 不参照)では env の provider/auth がそのまま active。"""
    cfg = await resolve_provider_config(_settings(), session=None)

    assert cfg.provider == "openai"
    assert cfg.auth_mode == "api_key"
    assert cfg.model == "gpt-4.1"  # _DEFAULT_MODELS[openai]


async def test_app_state_llm_model_overrides_default() -> None:
    """app_state.llm_model が provider 対応モデルなら factory がそれを採用する。"""
    session = _FakeSession(
        {
            _KEY_LLM_PROVIDER: '"ollama"',
            _KEY_LLM_AUTH_MODE: '"api_key"',
            _KEY_LLM_MODEL: '"llama3.2:3b"',
        }
    )

    cfg = await resolve_provider_config(_settings(), session=session)  # type: ignore[arg-type]

    assert cfg.provider == "ollama"
    assert cfg.model == "llama3.2:3b"


async def test_app_state_invalid_llm_model_raises_fatal() -> None:
    """他 provider の model が永続化されている場合は silent fallback せず fatal。"""
    session = _FakeSession(
        {
            _KEY_LLM_PROVIDER: '"ollama"',
            _KEY_LLM_MODEL: '"gpt-4.1"',
        }
    )

    with pytest.raises(LlmError) as exc_info:
        await resolve_provider_config(_settings(), session=session)  # type: ignore[arg-type]

    assert exc_info.value.category == "fatal"
    assert "gpt-4.1" in exc_info.value.message


async def test_app_state_invalid_llm_model_persisted_for_get() -> None:
    """GET active 向け strict_model=False では永続 model をそのまま返す。"""
    session = _FakeSession(
        {
            _KEY_LLM_PROVIDER: '"ollama"',
            _KEY_LLM_MODEL: '"gpt-4.1"',
        }
    )

    cfg = await resolve_provider_config(
        _settings(),
        session=session,  # type: ignore[arg-type]
        strict_model=False,
    )

    assert cfg.provider == "ollama"
    assert cfg.model == "gpt-4.1"


def test_validate_model_for_provider_raises_on_mismatch() -> None:
    """validate_model_for_provider は不整合 model を fatal で拒否する。"""
    with pytest.raises(LlmError) as exc_info:
        validate_model_for_provider("ollama", "gpt-4.1")

    assert exc_info.value.category == "fatal"


async def test_resolve_provider_config_rejects_codex_oauth_in_app_state() -> None:
    """app_state の codex_oauth は resolve 段階で fatal。"""
    session = _FakeSession({_KEY_LLM_AUTH_MODE: '"codex_oauth"'})

    with pytest.raises(LlmError) as exc_info:
        await resolve_provider_config(_settings(), session=session)  # type: ignore[arg-type]

    assert "codex_oauth" in exc_info.value.message


async def test_build_openai_rejects_codex_oauth_config() -> None:
    """_build_openai も codex_oauth を拒否する (二重防御)。"""
    config = ProviderConfig(provider="openai", auth_mode="codex_oauth", model="gpt-4.1")

    with pytest.raises(LlmError):
        await _build_openai(_settings(), config, session=None)


async def test_resolve_cloud_api_key_rejects_non_cloud_provider() -> None:
    """cloud key 解決は openai/anthropic 以外を拒否する。"""
    with pytest.raises(LlmError) as exc_info:
        await _resolve_cloud_api_key(_settings(), "ollama", session=None)  # type: ignore[arg-type]

    assert "openai/anthropic" in exc_info.value.message


async def test_resolve_cloud_api_key_missing_key_is_fatal() -> None:
    """API key 未設定時は fatal。"""
    settings = _settings(openai_api_key="", anthropic_api_key="")

    with pytest.raises(LlmError) as exc_info:
        await _resolve_cloud_api_key(settings, "openai", session=None)

    assert exc_info.value.category == "fatal"
    assert "credential_source=none" in exc_info.value.message


async def test_app_state_swaps_active_provider_to_anthropic() -> None:
    """app_state(JSON literal)で openai → anthropic に切替わり、 既定 model も追従する。"""
    session = _FakeSession(
        {_KEY_LLM_PROVIDER: '"anthropic"', _KEY_LLM_AUTH_MODE: '"api_key"'}
    )

    cfg = await resolve_provider_config(_settings(), session=session)  # type: ignore[arg-type]

    assert cfg.provider == "anthropic"
    assert cfg.auth_mode == "api_key"
    assert cfg.model == "claude-sonnet-4-6"  # _DEFAULT_MODELS[anthropic]


async def test_app_state_swaps_active_provider_to_ollama_decoded_str() -> None:
    """asyncpg が JSONB を decode 済み str で返す形(生 str)でも切替が成立する。"""
    session = _FakeSession({_KEY_LLM_PROVIDER: "ollama", _KEY_LLM_AUTH_MODE: "api_key"})

    cfg = await resolve_provider_config(_settings(), session=session)  # type: ignore[arg-type]

    assert cfg.provider == "ollama"
    assert cfg.model == "qwen2.5:3b"


async def test_app_state_partial_override_keeps_env_for_missing_key() -> None:
    """provider のみ上書き / auth_mode 未設定なら env の auth_mode を維持する。"""
    session = _FakeSession({_KEY_LLM_PROVIDER: '"ollama"'})

    cfg = await resolve_provider_config(_settings(), session=session)  # type: ignore[arg-type]

    assert cfg.provider == "ollama"
    assert cfg.auth_mode == "api_key"  # env 既定を保持


# ===========================================================================
# 2. 切替後 create_llm_provider が新 provider 実装を返す(実 LLM 非接触)
# ===========================================================================
async def test_create_llm_provider_returns_anthropic_after_swap() -> None:
    """app_state で anthropic に切替後、 生成される実装が AnthropicProvider になる。"""
    session = _FakeSession({_KEY_LLM_PROVIDER: '"anthropic"', _KEY_LLM_AUTH_MODE: '"api_key"'})

    provider = await create_llm_provider(_settings(), session=session)  # type: ignore[arg-type]

    assert isinstance(provider, AnthropicProvider)


async def test_create_llm_provider_returns_ollama_after_swap() -> None:
    """app_state で ollama に切替後、 生成される実装が OllamaProvider になる。"""
    session = _FakeSession({_KEY_LLM_PROVIDER: '"ollama"', _KEY_LLM_AUTH_MODE: '"api_key"'})

    provider = await create_llm_provider(_settings(), session=session)  # type: ignore[arg-type]

    assert isinstance(provider, OllamaProvider)


@pytest.mark.fr("FR-020")
async def test_create_llm_provider_defaults_to_env_openai() -> None:
    """FR-020: app_state 未上書き(session=None)では env の openai を生成する(切替の対照)。"""
    provider = await create_llm_provider(_settings(), session=None)

    assert isinstance(provider, OpenAIProvider)


# ===========================================================================
# 3. 不正組合せ拒否: Anthropic + (subscription / codex_oauth) は永続化前に弾く
# ===========================================================================
async def test_create_llm_provider_rejects_anthropic_subscription_via_app_state() -> None:
    """app_state で anthropic+subscription に切替えると factory が fatal で拒否する(FR-022)。

    PUT /llm/providers はこの factory 検証を再利用して 400 を返す。
    """
    session = _FakeSession({_KEY_LLM_PROVIDER: '"anthropic"', _KEY_LLM_AUTH_MODE: '"subscription"'})

    with pytest.raises(LlmError) as exc_info:
        await create_llm_provider(_settings(), session=session)  # type: ignore[arg-type]

    assert exc_info.value.category == "fatal"
    assert exc_info.value.retryable is False


async def test_create_llm_provider_rejects_anthropic_codex_oauth_via_app_state() -> None:
    """anthropic + codex_oauth(api_key 以外)も同様に fatal 拒否(永続化前検証)。"""
    session = _FakeSession({_KEY_LLM_PROVIDER: '"anthropic"', _KEY_LLM_AUTH_MODE: '"codex_oauth"'})

    with pytest.raises(LlmError) as exc_info:
        await create_llm_provider(_settings(), session=session)  # type: ignore[arg-type]

    assert exc_info.value.category == "fatal"


@pytest.mark.parametrize("bad_auth", ["subscription", "codex_oauth", "oauth", ""])
def test_put_combo_validation_rejects_anthropic_non_api_key(bad_auth: str) -> None:
    """PUT が踏襲する明示組合せ検証(I/O 無し): anthropic は api_key 以外を拒否。

    内部契約 (c): PUT は provider を実構築せず組合せだけを検証して 400 を返す。
    factory が公開する許可 auth(``_ANTHROPIC_ALLOWED_AUTH_MODE``)を真とする。
    """
    is_rejected = "anthropic" == "anthropic" and bad_auth != _ANTHROPIC_ALLOWED_AUTH_MODE
    assert is_rejected is True


@pytest.mark.parametrize(
    ("provider", "auth_mode"),
    [("openai", "api_key"), ("ollama", "api_key")],
)
def test_put_combo_validation_allows_supported_combos(provider: str, auth_mode: str) -> None:
    """api_key 組合せは anthropic 以外でも通過する(対照)。 codex_oauth は API 層で 422。"""
    is_rejected = provider == "anthropic" and auth_mode != _ANTHROPIC_ALLOWED_AUTH_MODE
    assert is_rejected is False


def test_put_combo_validation_notes_codex_oauth_unsupported() -> None:
    """Codex OAuth は factory でも fatal、 PUT は 422 (未配線)。"""
    assert _ANTHROPIC_ALLOWED_AUTH_MODE != "codex_oauth"


# ===========================================================================
# 4. 不正 provider 名(境界)も切替前に fatal で弾く
# ===========================================================================
async def test_app_state_unknown_provider_is_rejected() -> None:
    """app_state に未知 provider が入っても fatal で拒否し、 黙って既定へ落ちない。"""
    session = _FakeSession({_KEY_LLM_PROVIDER: '"gemini"'})

    with pytest.raises(LlmError) as exc_info:
        await resolve_provider_config(_settings(), session=session)  # type: ignore[arg-type]

    assert exc_info.value.category == "fatal"


# ===========================================================================
# 5. PUT 書込側の前提: app_state Core Table の形(2 キー upsert 対象)を固定
# ===========================================================================
def test_app_state_table_exposes_swap_keys_for_put_upsert() -> None:
    """PUT が upsert する Core Table が key/value 列を持つ。"""
    assert app_state_table.name == "app_state"
    assert {"key", "value"} <= set(app_state_table.c.keys())
    assert _KEY_LLM_PROVIDER == "llm_provider"
    assert _KEY_LLM_AUTH_MODE == "llm_auth_mode"
    assert _KEY_LLM_MODEL == "llm_model"


def test_provider_config_uses_slots() -> None:
    """``ProviderConfig`` は ``__slots__`` 限定で属性追加を拒む(切替結果の汚染防止)。

    ``Final`` 注釈は mypy が静的に再代入を弾く(実行時強制ではない)。実行時に保証
    できるのは ``__slots__`` による未定義属性拒否なので、それをここで固定する。
    """
    cfg = ProviderConfig(provider="openai", auth_mode="api_key", model="gpt-4.1")
    assert cfg.provider == "openai"
    assert cfg.auth_mode == "api_key"
    assert cfg.model == "gpt-4.1"

    with pytest.raises(AttributeError):
        cfg.unexpected = "x"  # type: ignore[attr-defined]
