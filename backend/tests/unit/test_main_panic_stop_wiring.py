"""``POST /scheduler/panic-stop`` 配線の単体テスト (main.py の ``_build_protected_router``)。

検証観点 (US4 T113 配線):

- ``create_app()`` が panic_stop 業務ルータを protected 親ルータ配下に登録している
  (``/scheduler/panic-stop`` がルート表に存在する)。
- 無認証では 401 (= 親ルータの ``require_basic_auth`` が panic_stop にも効いていること、
  子ルータに認証を再付与していないこと双方を確認)。
- 正しい資格情報ならハンドラへ到達する (service を override し DB / YouTube / scheduler を
  起動せず 200 まで通す)。

DB / scheduler / 実 provider は一切起動しない。 ``get_session`` を in-memory fake に、
``get_settings`` を固定資格情報に、 ``get_panic_stop_service`` を stub に override する。
lifespan (実 DB 接続検証) を走らせないため :class:`TestClient` は ``with`` を使わず生成する
(Starlette は context manager 利用時のみ lifespan を起動する)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.api.panic_stop import get_panic_stop_service
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.domain.panic_stop.service import PanicStopResult
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.main import create_app

pytestmark = pytest.mark.unit

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


class _FakeSession:
    """``commit`` のみ持つ in-memory セッション (service が stub なので DB I/O 無し)。"""

    async def commit(self) -> None:
        return None


async def _fake_get_session() -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


class _StubService:
    """``PanicStopService`` の stub。 固定の停止結果を返し DB / YouTube に触れない。"""

    async def panic_stop(
        self,
        *,
        session: Any,
        scheduler_service: Any,
        window_hours: int = 24,
        set_private: list[str] | None = None,
        actor: str = "seita",
    ) -> PanicStopResult:
        del session, scheduler_service, window_hours, set_private, actor
        return PanicStopResult(scheduler_enabled=False, recent_videos=[], updated_count=0)


@pytest.fixture
def app() -> Any:
    application = create_app(
        Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))
    )
    application.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD)
    )
    application.dependency_overrides[get_session] = _fake_get_session
    application.dependency_overrides[get_panic_stop_service] = lambda: _StubService()
    return application


@pytest.fixture
def client(app: Any) -> TestClient:
    # ``with`` を使わない = lifespan (実 DB 接続検証) を起動しない。
    return TestClient(app)


def test_panic_stop_route_is_registered(app: Any) -> None:
    paths = {route.path for route in app.routes}
    assert "/scheduler/panic-stop" in paths


def test_panic_stop_route_requires_auth(client: TestClient) -> None:
    # panic_stop も protected 親ルータ配下 = 無認証で 401 (Basic 認証必須)。
    resp = client.post("/scheduler/panic-stop", json={})
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_panic_stop_route_reaches_handler_with_valid_credentials(client: TestClient) -> None:
    # 親ルータの認証を通過しハンドラへ到達 (stub service で停止結果 200)。
    resp = client.post("/scheduler/panic-stop", json={}, auth=_AUTH)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload == {
        "scheduler_enabled": False,
        "recent_videos": [],
        "updated_count": 0,
    }
