"""Unit テスト: Settings の LLM 認証組合せ検証 (core/config.py, FR-021)。

FR-021: codex_oauth 認証は OpenAI provider のみサポートする (ADR-0019)。 不整合な
provider/auth_mode の組合せは **起動時** (``Settings`` 構築時) に拒否する。

``Settings._validate_llm_auth_mode`` (``@model_validator(mode="after")``) を直接駆動して
検証する。 実装を確認した結果:

- 例外型は **pydantic の ``ValidationError``** (専用 ``FatalError`` ではない)。
  ``_validate_llm_auth_mode`` が素の ``ValueError`` を送出し、 pydantic がそれを
  ``ValidationError`` に包む (``mode="after"`` バリデータの標準挙動)。
- env フィールド名は ``llm_provider`` / ``llm_auth_mode`` (``core/config.py`` の Field 名)。
- ``llm_auth_mode`` は ``Literal["api_key", "codex_oauth"]`` のため、 ``subscription`` の
  ような未知値はフィールド型 (literal) 段階で ``ValidationError`` になる
  (provider 組合せ判定に到達する前に弾かれる)。

``.env`` 由来の汚染を避けるため ``_env_file=None`` を渡して env ファイルを無効化し、
provider key はダミーで埋める。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from ymg_backend.core.config import Settings


def _settings(**overrides: Any) -> Settings:
    """env ファイル非依存に Settings を構築する (provider key はダミー)。"""
    base: dict[str, Any] = {
        "openai_api_key": SecretStr("sk-openai"),
        "anthropic_api_key": SecretStr("sk-anthropic"),
        "_env_file": None,  # .env を読み込まない (テスト隔離)
    }
    base.update(overrides)
    return Settings(**base)


# ===========================================================================
# 拒否: anthropic + codex_oauth (codex_oauth は openai 専用)
# ===========================================================================
@pytest.mark.fr("FR-021")
def test_anthropic_with_codex_oauth_is_rejected_at_startup() -> None:
    """FR-021: anthropic + codex_oauth は起動時に ValidationError で拒否する。"""
    with pytest.raises(ValidationError) as exc_info:
        _settings(llm_provider="anthropic", llm_auth_mode="codex_oauth")

    # _validate_llm_auth_mode が送出する value_error に provider/openai のヒントが含まれる。
    assert any("codex_oauth" in str(err["msg"]) for err in exc_info.value.errors())


# ===========================================================================
# 拒否: ollama + codex_oauth (openai 以外は全て拒否)
# ===========================================================================
@pytest.mark.fr("FR-021")
def test_ollama_with_codex_oauth_is_rejected_at_startup() -> None:
    """FR-021: ollama + codex_oauth も openai 以外として拒否する。"""
    with pytest.raises(ValidationError):
        _settings(llm_provider="ollama", llm_auth_mode="codex_oauth")


# ===========================================================================
# 拒否: anthropic + subscription (subscription は Literal 外の未知値)
# ===========================================================================
@pytest.mark.fr("FR-021")
def test_anthropic_with_subscription_is_rejected_at_startup() -> None:
    """FR-021: anthropic + subscription は起動時に拒否する。

    ``subscription`` は ``LLMAuthMode = Literal["api_key", "codex_oauth"]`` に無いため、
    フィールド型 (literal) 段階で ``ValidationError`` (``literal_error``) になる。
    """
    with pytest.raises(ValidationError) as exc_info:
        _settings(llm_provider="anthropic", llm_auth_mode="subscription")

    assert any(
        err["loc"] == ("llm_auth_mode",) and err["type"] == "literal_error"
        for err in exc_info.value.errors()
    )


# ===========================================================================
# 許可: openai + codex_oauth (対照)
# ===========================================================================
@pytest.mark.fr("FR-021")
def test_openai_with_codex_oauth_is_allowed() -> None:
    """FR-021: openai + codex_oauth は許可される(対照)。"""
    settings = _settings(llm_provider="openai", llm_auth_mode="codex_oauth")

    assert settings.llm_provider == "openai"
    assert settings.llm_auth_mode == "codex_oauth"


@pytest.mark.fr("FR-021")
@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama"])
def test_api_key_auth_is_allowed_for_all_providers(provider: str) -> None:
    """FR-021: api_key 認証は全 provider で許可される(codex_oauth 制約は api_key に無関係)。"""
    settings = _settings(llm_provider=provider, llm_auth_mode="api_key")

    assert settings.llm_provider == provider
    assert settings.llm_auth_mode == "api_key"
