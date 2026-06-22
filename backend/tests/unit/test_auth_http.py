"""Basic 認証が実 HTTP スタック(FastAPI dependency + ASGI)で 401/200 を返す検証 (A)。

関数レベルの 401 検証は tests/critical/test_security_fernet.py にあるが、 本テストは
FastAPI の依存解決〜レスポンス経路まで含めて、 保護ルートが
- 無認証 → 401 (+ WWW-Authenticate: Basic)
- 誤った資格情報 → 401
- 正しい資格情報 → 200
を返すことを保証する。 DB 非依存。 Phase 2 時点では業務エンドポイントが無く
ライブの保護ルートが存在しないため、 ダミー保護ルートで認証機構自体を検証する。
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import require_basic_auth


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_basic_auth)])
    def _protected() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[get_settings] = lambda: Settings(
        admin_username="admin",
        admin_password=SecretStr("s3cret"),
    )
    return TestClient(app)


def test_protected_route_without_credentials_returns_401(client: TestClient) -> None:
    resp = client.get("/protected")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_protected_route_with_wrong_credentials_returns_401(client: TestClient) -> None:
    resp = client.get("/protected", auth=("admin", "wrong"))
    assert resp.status_code == 401


@pytest.mark.fr("FR-085")
def test_protected_route_with_valid_credentials_returns_200(client: TestClient) -> None:
    """FR-085: 正しい資格情報で保護ルートが 200 を返す。"""
    resp = client.get("/protected", auth=("admin", "s3cret"))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
