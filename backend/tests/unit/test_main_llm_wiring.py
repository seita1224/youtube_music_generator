"""LLM 業務ルータ配線の単体テスト (main.py の ``_build_protected_router``, US5)。

検証観点:

- ``create_app()`` が llm 業務ルータ (prefix ``/llm``) を protected 親ルータ配下に
  登録している (``/llm/providers`` / ``/llm/usage`` がルート表に存在する)。
- 保護ルートは無認証で 401 (= 親ルータの ``require_basic_auth`` が配下に効く)、
  正しい資格情報でハンドラに到達 (200) する (= 子ルータに認証を再付与していない)。

``GET /llm/providers`` は settings のみ参照し DB 非依存のため、 正資格情報で 200 まで
到達できる。 DB / scheduler / 実 provider は一切起動しない。 ``get_settings`` を固定
資格情報で override し、 lifespan (実 DB 接続検証) を走らせないため :class:`TestClient`
は ``with`` を使わず生成する (Starlette は context manager 利用時のみ lifespan を起動)。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.main import create_app

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


@pytest.fixture
def app() -> Any:
    application = create_app(
        Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))
    )
    application.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD)
    )
    return application


@pytest.fixture
def client(app: Any) -> TestClient:
    # ``with`` を使わない = lifespan (実 DB 接続検証) を起動しない。
    return TestClient(app)


def test_llm_routes_are_registered(app: Any) -> None:
    paths = {route.path for route in app.routes}
    # llm 業務ルータ (prefix /llm) の代表パスが protected 配下に登録済み (US5)。
    assert "/llm/providers" in paths
    assert "/llm/usage" in paths


def test_llm_route_requires_auth(client: TestClient) -> None:
    # llm も protected 親ルータ配下 = 無認証で 401。
    resp = client.get("/llm/providers")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_llm_route_reaches_handler_with_valid_credentials(client: TestClient) -> None:
    # 親ルータの認証を通過しハンドラへ到達 (settings 参照のみ、 DB 非依存で 200)。
    resp = client.get("/llm/providers", auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert {item["provider"] for item in body} == {"openai", "anthropic", "ollama"}
