"""LLM 業務ルータ配線の単体テスト (main.py の ``_build_protected_router``, US5)。

``GET /llm/providers`` は session 依存のため、 ``get_session`` を fake に差し替える。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Select

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.main import create_app

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


class _Row:
    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0][0]


class _FakeSession:
    async def execute(self, stmt: Any) -> _Result:
        if isinstance(stmt, Select):
            table = stmt.get_final_froms()[0].name  # type: ignore[attr-defined]
            if table == "app_state":
                return _Result(
                    [
                        _Row("llm_provider", '"ollama"'),
                        _Row("llm_auth_mode", '"api_key"'),
                        _Row("llm_model", '"qwen2.5:3b"'),
                    ]
                )
            if table == "llm_provider_secrets":
                return _Result([])
            if table == "usage_log":
                return _Result([(0,)])
        return _Result([])


async def _fake_get_session() -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture
def app() -> Any:
    application = create_app(
        Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))
    )
    application.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME,
        admin_password=SecretStr(_PASSWORD),
        llm_provider="ollama",
    )
    application.dependency_overrides[get_session] = _fake_get_session
    return application


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


def test_llm_routes_are_registered(app: Any) -> None:
    paths = {route.path for route in app.routes}
    assert "/llm/providers" in paths
    assert "/llm/usage" in paths
    assert "/llm/credentials" in paths
    assert "/llm/credentials/{provider}" in paths


def test_llm_route_requires_auth(client: TestClient) -> None:
    resp = client.get("/llm/providers")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_llm_route_reaches_handler_with_valid_credentials(client: TestClient) -> None:
    resp = client.get("/llm/providers", auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"]["provider"] == "ollama"
    assert body["active"]["model"] == "qwen2.5:3b"
    assert {item["provider"] for item in body["providers"]} == {
        "openai",
        "anthropic",
        "ollama",
    }
    ollama = next(p for p in body["providers"] if p["provider"] == "ollama")
    assert ollama["credential_source"] == "n/a"
