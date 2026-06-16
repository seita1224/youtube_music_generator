"""業務ルータ配線の単体テスト (main.py の ``_build_protected_router``)。

検証観点:

- ``create_app()`` が plans / posts / scheduler の業務ルータを protected 親ルータ
  配下に登録している (パスがルート表に存在する)。
- ``/health`` は認証不要のまま (security: [])。
- 保護ルートは無認証で 401、 正しい資格情報でハンドラに到達 (200) する
  (= 親ルータの ``require_basic_auth`` が配下に効いていること、 子ルータに認証を
  再付与していないこと双方を一度に確認する)。

DB / scheduler / 実 provider は一切起動しない。 ``get_session`` を in-memory の
fake に override し、 ``get_settings`` を固定資格情報で override する。 lifespan
(実 DB 接続検証) を走らせないため :class:`TestClient` は ``with`` を使わず生成する
(Starlette は context manager 利用時のみ lifespan を起動する)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.main import create_app

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


class _EmptyResult:
    """``session.execute`` の戻り。 空一覧/集計 0 を返す最小スタブ。

    ``GET /posts`` / ``GET /genres`` 双方の読み取り経路を満たす:

    - ``scalars().all()`` → 空一覧 (posts / genres の行取得)。
    - ``scalar_one()`` → ``0`` (genres の総件数 ``COUNT(*)``)。
    """

    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Any]:
        return []

    def scalar_one(self) -> int:
        return 0


class _FakeSession:
    """``GET /posts`` 経路で SELECT を空に解決する in-memory セッション (DB 非依存)。"""

    async def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()


async def _fake_get_session() -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture
def app() -> Any:
    application = create_app(
        Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))
    )
    application.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD)
    )
    application.dependency_overrides[get_session] = _fake_get_session
    return application


@pytest.fixture
def client(app: Any) -> TestClient:
    # ``with`` を使わない = lifespan (実 DB 接続検証) を起動しない。
    return TestClient(app)


def test_business_routers_are_registered(app: Any) -> None:
    paths = {route.path for route in app.routes}
    # plans / posts (prefix 付き) と scheduler (prefix 無し) の代表パスが登録済み。
    assert "/plans" in paths
    assert "/plans/{plan_id}" in paths
    assert "/posts" in paths
    assert "/posts/{post_id}" in paths
    assert "/scheduler" in paths
    # dryrun レビュー業務ルータ (prefix /dryrun) の 4 endpoint が protected 配下に登録済み。
    assert "/dryrun/outputs" in paths
    assert "/dryrun/outputs/{output_id}/approve" in paths
    assert "/dryrun/outputs/{output_id}/reject" in paths
    assert "/dryrun/outputs/{output_id}/video" in paths
    # genres 業務ルータ (prefix /genres) の代表パスが protected 配下に登録済み (US3)。
    assert "/genres" in paths
    assert "/genres/{name}/promote" in paths
    assert "/genres/{name}/disable" in paths
    # analytics 業務ルータ (prefix /analytics) が protected 配下に登録済み (US3)。
    assert "/analytics" in paths
    # /health は認証不要ルートとして別途登録済み。
    assert "/health" in paths


def test_health_is_unauthenticated(client: TestClient) -> None:
    resp = client.get("/health")
    # 認証要求 (401) ではないこと = health は protected 配下に居ない。
    assert resp.status_code != 401


def test_protected_route_requires_auth(client: TestClient) -> None:
    resp = client.get("/posts")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_protected_route_reaches_handler_with_valid_credentials(client: TestClient) -> None:
    resp = client.get("/posts", auth=_AUTH)
    # 親ルータの認証を通過しハンドラへ到達 (fake session で空一覧 200)。
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_protected_route_rejects_wrong_credentials(client: TestClient) -> None:
    resp = client.get("/posts", auth=(_USERNAME, "wrong"))
    assert resp.status_code == 401


def test_dryrun_route_requires_auth(client: TestClient) -> None:
    # dryrun も protected 親ルータ配下 = 無認証で 401 (動画配信含め Basic 認証必須)。
    resp = client.get("/dryrun/outputs")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_dryrun_route_reaches_handler_with_valid_credentials(client: TestClient) -> None:
    # 親ルータの認証を通過しハンドラへ到達 (fake session で空一覧 200)。
    resp = client.get("/dryrun/outputs", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_genres_route_requires_auth(client: TestClient) -> None:
    # genres も protected 親ルータ配下 = 無認証で 401 (US3)。
    resp = client.get("/genres")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_genres_route_reaches_handler_with_valid_credentials(client: TestClient) -> None:
    # 親ルータの認証を通過しハンドラへ到達 (fake session で空一覧/総件数 0 → 200)。
    resp = client.get("/genres", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_analytics_route_requires_auth(client: TestClient) -> None:
    # analytics も protected 親ルータ配下 = 無認証で 401 (US3)。
    # 認証前 (401) で短絡するため、 集計ハンドラの DB 依存には到達しない。
    resp = client.get("/analytics")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")
