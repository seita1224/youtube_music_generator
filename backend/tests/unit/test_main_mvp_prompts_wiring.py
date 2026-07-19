"""``/mvp-check`` / ``/prompts`` 配線の単体テスト (main.py の ``_build_protected_router``)。

検証観点 (US7 swap + Polish 配線):

- ``create_app()`` が mvp_check 業務ルータ (``/mvp-check``) と prompts 業務ルータ
  (``/prompts`` / ``/prompts/{prompt_name:path}``) を protected 親ルータ配下に登録する。
- 無認証では 401 (= 親ルータの ``require_basic_auth`` が両ルータにも効いていること、
  子ルータに認証を再付与していないこと双方を確認)。
- 正しい資格情報なら mvp_check ハンドラへ到達する (``get_checklist_evaluator`` を stub に
  override し DB を起動せず 200 まで通す)。

DB は一切起動しない。 ``get_session`` を in-memory fake に、 ``get_settings`` を固定
資格情報に、 ``get_checklist_evaluator`` を stub に override する。 lifespan (実 DB 接続
検証) を走らせないため :class:`TestClient` は ``with`` を使わず生成する (Starlette は
context manager 利用時のみ lifespan を起動する)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.api.mvp_check import get_checklist_evaluator
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.domain.mvp_check.checklist import MvpCheckItem, MvpChecklistResult
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.main import create_app

pytestmark = pytest.mark.unit

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


class _FakeSession:
    """``commit`` のみ持つ in-memory セッション (evaluator が stub なので DB I/O 無し)。"""

    async def commit(self) -> None:
        return None


async def _fake_get_session() -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


async def _stub_evaluator(session: Any) -> MvpChecklistResult:
    """``evaluate_mvp_checklist`` の stub。 1 項目 green の固定結果を返し DB に触れない。"""
    del session
    items = (
        MvpCheckItem(
            id="dryrun_success",
            label="dryrun 3本連続成功",
            status="green",
            detail=None,
        ),
    )
    return MvpChecklistResult(items=items, completed=1, total=6)


@pytest.fixture
def app() -> Any:
    application = create_app(
        Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))
    )
    application.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD)
    )
    application.dependency_overrides[get_session] = _fake_get_session
    application.dependency_overrides[get_checklist_evaluator] = lambda: _stub_evaluator
    return application


@pytest.fixture
def client(app: Any) -> TestClient:
    # ``with`` を使わない = lifespan (実 DB 接続検証) を起動しない。
    return TestClient(app)


def test_mvp_and_prompts_routes_are_registered(app: Any) -> None:
    paths = {route.path for route in app.routes}
    assert "/mvp-check" in paths
    assert "/prompts" in paths
    assert "/prompts/{prompt_name:path}" in paths


def test_mvp_check_route_requires_auth(client: TestClient) -> None:
    # mvp_check も protected 親ルータ配下 = 無認証で 401 (Basic 認証必須)。
    resp = client.get("/mvp-check")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_prompts_routes_require_auth(client: TestClient) -> None:
    # prompts も protected 親ルータ配下 = 無認証で 401 (一覧・個別とも)。
    list_resp = client.get("/prompts")
    assert list_resp.status_code == 401
    detail_resp = client.get("/prompts/planner/system")
    assert detail_resp.status_code == 401


def test_mvp_check_route_reaches_handler_with_valid_credentials(client: TestClient) -> None:
    # 親ルータの認証を通過し mvp_check ハンドラへ到達 (stub evaluator で 200)。
    resp = client.get("/mvp-check", auth=_AUTH)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["completed"] == 1
    assert payload["total"] == 6
    assert payload["items"][0]["id"] == "dryrun_success"
    assert payload["items"][0]["status"] == "green"
