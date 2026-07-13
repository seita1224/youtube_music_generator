"""migration 006 (LLM defaults repair) の単体テスト。

DB 不要。 ソース順序と downgrade 未サポートを固定する。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "006_llm_defaults_repair.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_006_llm_defaults_repair", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_006_downgrade_is_unsupported() -> None:
    """ADR-0031: 006 downgrade は NotImplementedError。"""
    module = _load_migration()
    with pytest.raises(NotImplementedError, match="not supported"):
        module.downgrade()


def test_migration_006_normalizes_auth_before_provider_repair() -> None:
    """codex→api_key 正規化が openai→ollama 修復より先 (ソース順序)。"""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    auth_idx = source.index('value = \'"api_key"\'::jsonb')
    provider_idx = source.index('value = \'"ollama"\'::jsonb')
    assert auth_idx < provider_idx
    assert "codex_oauth" in source[auth_idx - 200 : auth_idx]
    assert "qwen2.5:3b" in source[provider_idx:]
