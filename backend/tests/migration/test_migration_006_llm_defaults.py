"""migration 006 (LLM defaults repair) の integration テスト。

実 PostgreSQL が必要。 接続不可 / migration 未適用なら skip。

方針 (ADR-0031):
- 006 の ``downgrade()`` は意図的に未サポートのため **呼ばない**
- schema は session conftest が head まで適用済みであることを前提に、
  ``alembic stamp 005`` → レガシー app_state 投入 → ``upgrade 006`` で検証する
- 新規 DB の CREATE EXTENSION vector 権限問題を避けるため、
  disposable DB のフル rebuild はしない (dev DB も drop/reset しない)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.integration

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REV_005 = "005_llm_provider_secrets"
_REV_006 = "006_llm_defaults_repair"


def _sync_url() -> str:
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return cfg


def _read_app_state(conn, key: str) -> str | None:
    row = conn.execute(
        text("SELECT value::text FROM app_state WHERE key = :key"),
        {"key": key},
    ).first()
    return row[0] if row else None


def _current_revision(conn) -> str | None:
    row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def _replace_llm_app_state(conn, rows: dict[str, str]) -> None:
    """llm_* 行を差し替え (value は JSON 文字列リテラル例: '\"openai\"')。"""
    conn.execute(text("DELETE FROM app_state WHERE key LIKE 'llm_%'"))
    for key, value in rows.items():
        conn.execute(
            text(
                "INSERT INTO app_state (key, value) VALUES "
                "(:key, CAST(:value AS jsonb))"
            ),
            {"key": key, "value": value},
        )


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(_sync_url())
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
            revision = _current_revision(conn)
    except OperationalError as exc:
        pytest.skip(f"postgres へ接続できないため skip: {exc}")
    if revision != _REV_006:
        pytest.skip(
            f"テスト DB が {_REV_006} ではないため skip (現在: {revision!r}). "
            "vector 拡張権限不足などで head 未到達の環境を想定。"
        )
    # 006 は data-only。 stamp→upgrade を繰り返す前提で schema は head のまま使う。
    yield eng
    eng.dispose()


@pytest.fixture
def migration_006_harness(engine: Engine) -> Iterator[Engine]:
    """各テスト: stamp 005 → (呼び出し側が seed) → upgrade 006。 最後に head へ戻す。"""
    cfg = _alembic_config()
    snapshot: dict[str, str] = {}
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT key, value::text FROM app_state WHERE key LIKE 'llm_%'")
        ).all()
        snapshot = {str(k): str(v) for k, v in rows}

    command.stamp(cfg, _REV_005)
    try:
        yield engine
    finally:
        # 失敗時も alembic_version と llm_* を head 整合状態へ戻す。
        # 006 は data-only のため stamp で足り、 downgrade は呼ばない。
        with engine.begin() as conn:
            _replace_llm_app_state(conn, snapshot)
        command.stamp(cfg, _REV_006)


def test_migration_006_repairs_legacy_openai_qwen_api_key(
    migration_006_harness: Engine,
) -> None:
    """001 openai + 005 qwen + api_key の三重一致のみ ollama へ修復。"""
    engine = migration_006_harness
    with engine.begin() as conn:
        _replace_llm_app_state(
            conn,
            {
                "llm_provider": '"openai"',
                "llm_auth_mode": '"api_key"',
                "llm_model": '"qwen2.5:3b"',
            },
        )

    command.upgrade(_alembic_config(), _REV_006)

    with engine.connect() as conn:
        assert _read_app_state(conn, "llm_provider") == '"ollama"'
        assert _read_app_state(conn, "llm_model") == '"qwen2.5:3b"'
        assert _read_app_state(conn, "llm_auth_mode") == '"api_key"'
        assert _current_revision(conn) == _REV_006


def test_migration_006_repairs_legacy_openai_qwen_codex_oauth(
    migration_006_harness: Engine,
) -> None:
    """openai + qwen + codex_oauth → ollama + qwen + api_key (auth 先正規化)。"""
    engine = migration_006_harness
    with engine.begin() as conn:
        _replace_llm_app_state(
            conn,
            {
                "llm_provider": '"openai"',
                "llm_auth_mode": '"codex_oauth"',
                "llm_model": '"qwen2.5:3b"',
            },
        )

    command.upgrade(_alembic_config(), _REV_006)

    with engine.connect() as conn:
        assert _read_app_state(conn, "llm_provider") == '"ollama"'
        assert _read_app_state(conn, "llm_model") == '"qwen2.5:3b"'
        assert _read_app_state(conn, "llm_auth_mode") == '"api_key"'


def test_migration_006_preserves_intentional_openai_without_qwen(
    migration_006_harness: Engine,
) -> None:
    """openai のみ (llm_model 無し) は修復対象外。"""
    engine = migration_006_harness
    with engine.begin() as conn:
        _replace_llm_app_state(
            conn,
            {
                "llm_provider": '"openai"',
                "llm_auth_mode": '"api_key"',
            },
        )

    command.upgrade(_alembic_config(), _REV_006)

    with engine.connect() as conn:
        assert _read_app_state(conn, "llm_provider") == '"openai"'
        assert _read_app_state(conn, "llm_model") is None
        assert _read_app_state(conn, "llm_auth_mode") == '"api_key"'


def test_migration_006_preserves_intentional_openai_with_cloud_model(
    migration_006_harness: Engine,
) -> None:
    """意図的な openai + gpt モデルは上書きしない。"""
    engine = migration_006_harness
    with engine.begin() as conn:
        _replace_llm_app_state(
            conn,
            {
                "llm_provider": '"openai"',
                "llm_auth_mode": '"api_key"',
                "llm_model": '"gpt-4.1"',
            },
        )

    command.upgrade(_alembic_config(), _REV_006)

    with engine.connect() as conn:
        assert _read_app_state(conn, "llm_provider") == '"openai"'
        assert _read_app_state(conn, "llm_model") == '"gpt-4.1"'
        assert _read_app_state(conn, "llm_auth_mode") == '"api_key"'


def test_migration_006_normalizes_codex_oauth_auth_mode(
    migration_006_harness: Engine,
) -> None:
    """app_state の codex_oauth は api_key へ正規化 (provider は ollama のまま)。"""
    engine = migration_006_harness
    with engine.begin() as conn:
        _replace_llm_app_state(
            conn,
            {
                "llm_provider": '"ollama"',
                "llm_auth_mode": '"codex_oauth"',
                "llm_model": '"qwen2.5:3b"',
            },
        )

    command.upgrade(_alembic_config(), _REV_006)

    with engine.connect() as conn:
        assert _read_app_state(conn, "llm_provider") == '"ollama"'
        assert _read_app_state(conn, "llm_auth_mode") == '"api_key"'
        assert _read_app_state(conn, "llm_model") == '"qwen2.5:3b"'
