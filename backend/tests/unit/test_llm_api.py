"""``api/llm.py`` の単体テスト (US5, T117 + credential / model 永続化)。

外部依存を一切起動しない真の単体テスト:

- DB は ``execute`` / ``flush`` / ``commit`` を記録する fake セッション。
- ``resolve_provider_config`` は本物を使い、 ``get_settings`` は monkeypatch。
- credential の Fernet は実 ``TokenCipher`` (テスト用鍵) で roundtrip。
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import Delete, Insert, Select

from ymg_backend.api import llm as api_llm
from ymg_backend.core.config import Settings
from ymg_backend.core.security import TokenCipher
from ymg_backend.llm import secrets as llm_secrets

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.asyncio

_VALID_FERNET_KEY = Fernet.generate_key().decode()


def _settings(**overrides: Any) -> Settings:
    """テスト用 Settings。 env 非依存に既定値で構築する。"""
    base: dict[str, Any] = {
        "llm_provider": "openai",
        "llm_auth_mode": "api_key",
        "openai_api_key": "sk-openai",
        "anthropic_api_key": "sk-anthropic",
        "monthly_budget_usd": 50.0,
        "admin_password": "test-admin-password",
        "fernet_key": SecretStr(_VALID_FERNET_KEY),
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


class _AppStateRow:
    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value


class _ColumnsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0][0]


class _FakeSession:
    """app_state / secrets / usage / audit を扱う fake。"""

    def __init__(
        self,
        *,
        app_state: dict[str, str] | None = None,
        secrets: dict[str, bytes] | None = None,
        usage_total: Decimal | int = 0,
        usage_rows: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.app_state: dict[str, str] = dict(app_state or {})
        self.secrets: dict[str, bytes] = dict(secrets or {})
        self.usage_total: Decimal | int = usage_total
        self.usage_rows: list[tuple[Any, ...]] = list(usage_rows or [])
        self.audit_rows: list[Mapping[str, Any]] = []
        self.commit_count = 0
        self.flush_count = 0
        self.last_delete_rowcount = 0
        self.last_insert_params: dict[str, Any] | None = None
        self.last_insert_stmt: Insert | None = None

    async def execute(self, stmt: Any) -> _ColumnsResult:
        if isinstance(stmt, Select):
            return self._handle_select(stmt)
        if isinstance(stmt, Insert):
            self._apply_insert(stmt)
            return _ColumnsResult([])
        if isinstance(stmt, Delete):
            return self._apply_delete(stmt)
        raise AssertionError(f"想定外の statement: {stmt!r}")

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        pass

    def _handle_select(self, stmt: Select[Any]) -> _ColumnsResult:
        table_name = stmt.get_final_froms()[0].name  # type: ignore[attr-defined]
        if table_name == "app_state":
            return _ColumnsResult([_AppStateRow(k, v) for k, v in self.app_state.items()])
        if table_name == "llm_provider_secrets":
            cols = list(stmt.selected_columns)
            # list_db_secret_providers: provider のみ
            if len(cols) == 1 and cols[0].name == "provider":
                return _ColumnsResult([(p,) for p in self.secrets])
            # resolve_api_key: api_key_encrypted
            provider = None
            compiled = stmt.compile()
            # WHERE provider = :provider_1 等から推定は難しいので secrets 全件から
            # 最初の一致を返す。 テストは 1 provider ずつ触る。
            for params in [compiled.params]:
                for key, value in params.items():
                    if "provider" in key and isinstance(value, str):
                        provider = value
            if provider and provider in self.secrets:
                return _ColumnsResult([(self.secrets[provider],)])
            if len(self.secrets) == 1:
                return _ColumnsResult([(next(iter(self.secrets.values())),)])
            return _ColumnsResult([])
        if table_name == "usage_log":
            n_cols = len(stmt.selected_columns)
            if n_cols == 1:
                return _ColumnsResult([(self.usage_total,)])
            return _ColumnsResult(self.usage_rows)
        raise AssertionError(f"想定外の select 元テーブル: {table_name}")

    def _apply_insert(self, stmt: Insert) -> None:
        table_name = stmt.table.name
        params = stmt.compile().params
        self.last_insert_params = dict(params)
        self.last_insert_stmt = stmt
        if table_name == "app_state":
            import json

            self.app_state[str(params["key"])] = json.dumps(params["value"])
        elif table_name == "audit_log":
            self.audit_rows.append(dict(params))
        elif table_name == "llm_provider_secrets":
            self.secrets[str(params["provider"])] = params["api_key_encrypted"]
        else:
            raise AssertionError(f"想定外の insert 先テーブル: {table_name}")

    def _apply_delete(self, stmt: Delete) -> _ColumnsResult:
        table_name = stmt.table.name
        if table_name != "llm_provider_secrets":
            raise AssertionError(f"想定外の delete 先: {table_name}")
        params = stmt.compile().params
        provider = None
        for key, value in params.items():
            if "provider" in key and isinstance(value, str):
                provider = value
        removed = 0
        if provider and provider in self.secrets:
            del self.secrets[provider]
            removed = 1
        result = _ColumnsResult([])
        result.rowcount = removed
        self.last_delete_rowcount = removed
        return result


# --- GET /llm/providers ------------------------------------------------------------


async def test_list_providers_includes_active_ollama_and_sources() -> None:
    """active は app_state の ollama、 ollama の credential_source は n/a。"""
    settings = _settings(llm_provider="openai", openai_api_key="sk-openai", anthropic_api_key="")
    session = _FakeSession(
        app_state={
            "llm_provider": '"ollama"',
            "llm_auth_mode": '"api_key"',
            "llm_model": '"qwen2.5:3b"',
        }
    )

    resp = await api_llm.list_providers(
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
    )

    assert resp.active.provider == "ollama"
    assert resp.active.auth_mode == "api_key"
    assert resp.active.model == "qwen2.5:3b"
    by_name = {c.provider: c for c in resp.providers}
    assert by_name["ollama"].credential_source == "n/a"
    assert by_name["ollama"].credential_configured is True
    assert by_name["openai"].credential_source == "env"
    assert by_name["anthropic"].credential_source == "none"
    assert by_name["openai"].unsupported_auth_modes == ["codex_oauth"]


async def test_list_providers_reports_db_source_without_key_leak() -> None:
    """DB 鍵あり・env 空 → credential_source=db。 応答に鍵文字列が無い。"""
    cipher = TokenCipher(Fernet(_VALID_FERNET_KEY.encode()))
    encrypted = cipher.encrypt("sk-db-secret-value")
    settings = _settings(openai_api_key="", anthropic_api_key="")
    session = _FakeSession(secrets={"openai": encrypted})

    resp = await api_llm.list_providers(
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
    )

    openai = next(p for p in resp.providers if p.provider == "openai")
    assert openai.credential_source == "db"
    assert openai.credential_configured is True
    dumped = resp.model_dump_json()
    assert "sk-db-secret-value" not in dumped
    assert "****" not in dumped


# --- PUT /llm/providers ------------------------------------------------------------


async def test_set_provider_codex_oauth_is_422() -> None:
    """Codex OAuth は未配線のため 422。"""
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await api_llm.set_provider(
            body=api_llm.LlmProviderPutBody(
                provider="openai",
                auth_mode="codex_oauth",
                model="gpt-4.1",
            ),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 422
    assert session.commit_count == 0


async def test_set_provider_invalid_model_is_422() -> None:
    """他 provider の model は 422。"""
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await api_llm.set_provider(
            body=api_llm.LlmProviderPutBody(
                provider="ollama",
                auth_mode="api_key",
                model="gpt-4.1",
            ),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 422
    assert session.commit_count == 0


async def test_set_provider_anthropic_subscription_is_400() -> None:
    """Anthropic + 非 api_key は 400 (codex は先に 422 になるため subscription 相当を模擬不可。
    ここでは openai 以外 + 非対応は codex 経路。 anthropic+codex も 422)。
    """
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await api_llm.set_provider(
            body=api_llm.LlmProviderPutBody(
                provider="anthropic",
                auth_mode="codex_oauth",
                model="claude-sonnet-4-6",
            ),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    # codex_oauth は全 provider で 422 (未配線)。
    assert exc.value.status_code == 422


async def test_set_provider_switch_persists_model_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ollama + モデル永続化 + audit。"""
    monkeypatch.setattr(api_llm, "get_settings", lambda: _settings(llm_provider="openai"))
    session = _FakeSession()

    state = await api_llm.set_provider(
        body=api_llm.LlmProviderPutBody(
            provider="ollama",
            auth_mode="api_key",
            model="llama3.2:3b",
        ),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.provider == "ollama"
    assert state.model == "llama3.2:3b"
    assert session.app_state["llm_provider"] == '"ollama"'
    assert session.app_state["llm_auth_mode"] == '"api_key"'
    assert session.app_state["llm_model"] == '"llama3.2:3b"'
    assert session.commit_count == 1
    assert session.audit_rows[0]["action"] == "llm_provider_changed"
    assert "llama3.2:3b" in str(session.audit_rows[0]["payload"])


async def test_set_provider_same_value_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """同値は no-op。"""
    monkeypatch.setattr(api_llm, "get_settings", lambda: _settings())
    session = _FakeSession(
        app_state={
            "llm_provider": '"openai"',
            "llm_auth_mode": '"api_key"',
            "llm_model": '"gpt-4.1"',
        }
    )

    state = await api_llm.set_provider(
        body=api_llm.LlmProviderPutBody(
            provider="openai",
            auth_mode="api_key",
            model="gpt-4.1",
        ),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.provider == "openai"
    assert session.commit_count == 0
    assert session.audit_rows == []


# --- credentials -------------------------------------------------------------------


async def test_put_credential_env_source_is_409() -> None:
    """env が SoT のとき PUT credentials は 409。"""
    settings = _settings(openai_api_key="sk-env")
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await api_llm.put_credential(
            body=api_llm.LlmCredentialPutBody(provider="openai", api_key="sk-new"),
            user="seita",
            session=session,  # type: ignore[arg-type]
            settings=settings,
        )

    assert exc.value.status_code == 409
    assert session.secrets == {}
    assert session.commit_count == 0


async def test_put_credential_encrypts_and_audits_without_leak() -> None:
    """DB へ Fernet 保存。 audit / 応答に平文なし。"""
    settings = _settings(openai_api_key="", anthropic_api_key="")
    session = _FakeSession()
    plain = "sk-test-temporary-key"

    state = await api_llm.put_credential(
        body=api_llm.LlmCredentialPutBody(provider="openai", api_key=plain),
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
    )

    assert state.credential_source == "db"
    assert state.credential_configured is True
    assert "openai" in session.secrets
    cipher = TokenCipher(Fernet(_VALID_FERNET_KEY.encode()))
    assert cipher.decrypt(session.secrets["openai"]) == plain
    audit = session.audit_rows[0]
    assert audit["action"] == "llm_credential_set"
    assert plain not in str(audit)
    assert plain not in state.model_dump_json()


async def test_put_credential_strips_whitespace_before_encrypt() -> None:
    """前後空白は strip してから暗号化。 正規化後の値のみが解決される。"""
    settings = _settings(openai_api_key="", anthropic_api_key="")
    session = _FakeSession()
    plain = "sk-normalized-key"
    padded = f"  \t{plain}  \n"

    body = api_llm.LlmCredentialPutBody(provider="openai", api_key=padded)
    assert body.api_key == plain

    state = await api_llm.put_credential(
        body=body,
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
    )

    cipher = TokenCipher(Fernet(_VALID_FERNET_KEY.encode()))
    stored = cipher.decrypt(session.secrets["openai"])
    assert stored == plain
    dumped = state.model_dump_json()
    audit_text = str(session.audit_rows[0])
    assert plain not in dumped
    assert plain not in audit_text
    assert padded not in dumped
    assert padded not in audit_text

    resolved, source = await llm_secrets.resolve_api_key(
        settings,
        "openai",
        session=session,  # type: ignore[arg-type]
        cipher=cipher,
    )
    assert source == "db"
    assert resolved == plain


async def test_put_credential_whitespace_only_is_422() -> None:
    """空白のみの api_key は Pydantic で拒否される。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        api_llm.LlmCredentialPutBody(provider="openai", api_key="   \t  ")


async def test_put_credential_rejects_over_max_length() -> None:
    """api_key が 2048 超なら拒否 (入力 DoS 防止)。"""
    from pydantic import ValidationError

    too_long = "k" * (api_llm.LLM_API_KEY_MAX_LENGTH + 1)
    with pytest.raises(ValidationError) as exc:
        api_llm.LlmCredentialPutBody(provider="openai", api_key=too_long)
    assert any(
        err.get("type") == "string_too_long" or "2048" in str(err)
        for err in exc.value.errors()
    )


async def test_upsert_api_key_stores_stripped_value() -> None:
    """secrets.upsert_api_key も strip して暗号化し、 resolve は正規化値のみ返す。"""
    settings = _settings(openai_api_key="", anthropic_api_key="")
    cipher = TokenCipher(Fernet(_VALID_FERNET_KEY.encode()))
    session = _FakeSession()
    plain = "sk-from-upsert"

    await llm_secrets.upsert_api_key(
        session,  # type: ignore[arg-type]
        settings,
        "openai",
        f"  {plain}  ",
        cipher=cipher,
    )

    resolved, source = await llm_secrets.resolve_api_key(
        settings,
        "openai",
        session=session,  # type: ignore[arg-type]
        cipher=cipher,
    )
    assert source == "db"
    assert resolved == plain


async def test_delete_credential_clears_db_only() -> None:
    """DELETE は DB 行のみ削除。"""
    settings = _settings(openai_api_key="", anthropic_api_key="")
    cipher = TokenCipher(Fernet(_VALID_FERNET_KEY.encode()))
    session = _FakeSession(secrets={"openai": cipher.encrypt("sk-x")})

    state = await api_llm.clear_credential(
        provider="openai",
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
    )

    assert state.credential_source == "none"
    assert session.secrets == {}
    assert session.audit_rows[0]["action"] == "llm_credential_deleted"


async def test_delete_credential_missing_row_is_404_without_audit() -> None:
    """DB 行が無い DELETE は 404。 audit は書かない。"""
    settings = _settings(openai_api_key="", anthropic_api_key="")
    session = _FakeSession(secrets={})

    with pytest.raises(HTTPException) as exc:
        await api_llm.clear_credential(
            provider="openai",
            user="seita",
            session=session,  # type: ignore[arg-type]
            settings=settings,
        )

    assert exc.value.status_code == 404
    assert session.audit_rows == []
    assert session.commit_count == 0


async def test_delete_credential_env_source_is_409() -> None:
    """env SoT 時の DELETE は 409。"""
    settings = _settings(openai_api_key="sk-env")
    session = _FakeSession(secrets={"openai": b"x"})

    with pytest.raises(HTTPException) as exc:
        await api_llm.clear_credential(
            provider="openai",
            user="seita",
            session=session,  # type: ignore[arg-type]
            settings=settings,
        )

    assert exc.value.status_code == 409
    assert "openai" in session.secrets


async def test_env_precedes_db_in_resolve() -> None:
    """resolve_api_key: env 非空なら DB を見ない。"""
    settings = _settings(openai_api_key="sk-from-env", anthropic_api_key="")
    cipher = TokenCipher(Fernet(_VALID_FERNET_KEY.encode()))
    session = _FakeSession(secrets={"openai": cipher.encrypt("sk-from-db")})

    key, source = await llm_secrets.resolve_api_key(
        settings,
        "openai",
        session=session,  # type: ignore[arg-type]
        cipher=cipher,
    )

    assert key == "sk-from-env"
    assert source == "env"


async def test_whitespace_only_env_key_treated_as_none() -> None:
    """空白のみの env API key は未設定扱い (source=none)。"""
    settings = _settings(openai_api_key="   \t", anthropic_api_key="")
    session = _FakeSession()

    _, source = await llm_secrets.resolve_api_key(
        settings,
        "openai",
        session=session,  # type: ignore[arg-type]
    )

    assert source == "none"


async def test_list_providers_returns_persisted_model_without_fallback() -> None:
    """GET active は strict_model=False で永続 model をそのまま返す。"""
    settings = _settings(llm_provider="openai", openai_api_key="sk-openai")
    session = _FakeSession(
        app_state={
            "llm_provider": '"ollama"',
            "llm_auth_mode": '"api_key"',
            "llm_model": '"gpt-4.1"',
        }
    )

    resp = await api_llm.list_providers(
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
    )

    assert resp.active.provider == "ollama"
    assert resp.active.model == "gpt-4.1"


async def test_set_provider_repairs_mismatched_persisted_combo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不整合 app_state でも PUT で有効組合せへ修復でき、 audit と strict resolve が通る。"""
    from ymg_backend.llm.base import LlmError
    from ymg_backend.llm.factory import resolve_provider_config

    settings = _settings(llm_provider="openai", openai_api_key="sk-openai")
    monkeypatch.setattr(api_llm, "get_settings", lambda: settings)
    session = _FakeSession(
        app_state={
            "llm_provider": '"ollama"',
            "llm_auth_mode": '"api_key"',
            "llm_model": '"gpt-4.1"',
        }
    )

    listed = await api_llm.list_providers(
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
    )
    assert listed.active.provider == "ollama"
    assert listed.active.model == "gpt-4.1"

    with pytest.raises(LlmError):
        await resolve_provider_config(settings, session=session, strict_model=True)  # type: ignore[arg-type]

    state = await api_llm.set_provider(
        body=api_llm.LlmProviderPutBody(
            provider="ollama",
            auth_mode="api_key",
            model="qwen2.5:3b",
        ),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.provider == "ollama"
    assert state.auth_mode == "api_key"
    assert state.model == "qwen2.5:3b"
    assert session.app_state["llm_provider"] == '"ollama"'
    assert session.app_state["llm_model"] == '"qwen2.5:3b"'
    assert session.commit_count == 1
    assert len(session.audit_rows) == 1
    assert session.audit_rows[0]["action"] == "llm_provider_changed"
    payload = session.audit_rows[0]["payload"]
    assert payload["from"]["provider"] == "ollama"
    assert payload["from"]["model"] == "gpt-4.1"
    assert payload["to"]["model"] == "qwen2.5:3b"

    repaired = await resolve_provider_config(
        settings,
        session=session,  # type: ignore[arg-type]
        strict_model=True,
    )
    assert repaired.provider == "ollama"
    assert repaired.model == "qwen2.5:3b"


async def test_upsert_app_state_sets_updated_at() -> None:
    """app_state upsert は updated_at を設定する。"""
    from sqlalchemy.dialects import postgresql

    session = _FakeSession()

    await api_llm._upsert_app_state(session, "llm_provider", "ollama")  # type: ignore[arg-type]

    assert session.last_insert_stmt is not None
    compiled = str(
        session.last_insert_stmt.compile(dialect=postgresql.dialect())
    ).lower()
    assert "updated_at" in compiled


# --- GET /llm/usage ----------------------------------------------------------------


async def test_get_usage_aggregates_and_computes_budget_pct() -> None:
    settings = _settings(monthly_budget_usd=50.0)
    session = _FakeSession(
        usage_total=Decimal("12.500000"),
        usage_rows=[
            ("openai", Decimal("10.000000"), Decimal("1000"), Decimal("200"), Decimal("500")),
            ("anthropic", Decimal("2.500000"), Decimal("300"), Decimal("0"), Decimal("150")),
        ],
    )

    usage = await api_llm.get_usage(
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
        month="2026-05",
    )

    assert usage.month == "2026-05"
    assert usage.total_cost_usd == pytest.approx(12.5)
    assert usage.budget_pct == pytest.approx(25.0)


async def test_get_usage_zero_budget_avoids_division_by_zero() -> None:
    settings = _settings(monthly_budget_usd=0.0)
    session = _FakeSession(usage_total=Decimal("5.000000"))

    usage = await api_llm.get_usage(
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
        month="2026-05",
    )

    assert usage.budget_pct == pytest.approx(0.0)


async def test_get_usage_rejects_out_of_range_month() -> None:
    settings = _settings()
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await api_llm.get_usage(
            user="seita",
            session=session,  # type: ignore[arg-type]
            settings=settings,
            month="2026-13",
        )

    assert exc.value.status_code == 422
