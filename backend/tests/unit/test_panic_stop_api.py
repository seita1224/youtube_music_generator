"""``/scheduler/panic-stop`` ルータの単体テスト (api/panic_stop.py, US4 T113)。

DB は触れず (service を stub に差し替え)、 Basic 認証は ``require_basic_auth`` override で
固定する。 実 DB / 実 YouTube / 実 scheduler / HTTP は一切起動しない。

検証観点:

- 認証必須 (未認証は 401)。
- 正常系: service.panic_stop を ``window_hours`` / ``set_private`` / actor 付きで呼び、
  200 + commit。 レスポンスは ``scheduler_enabled`` / ``recent_videos`` / ``updated_count``。
- ``set_private`` は **文字列配列** としてそのまま service に渡る (bool ではない)。
- ``window_hours`` 境界: 0 以下は ``Field(ge=1)`` で 422 (service は呼ばれない)。
- 業務例外写像: RecoverableError → 502 / TransientError → 503。
- ``scheduler_service`` 未配線 (app.state 無し) でも ``None`` が service に渡り 500 にならない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.api.panic_stop import get_panic_stop_service, router
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import require_basic_auth
from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.domain.panic_stop.service import PanicStopResult
from ymg_backend.infrastructure.db.session import get_session

pytestmark = pytest.mark.unit

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


@dataclass
class _FakeVideo:
    """ORM ``Video`` の最小スタンドイン (VideoOut.from_attributes 用)。"""

    youtube_video_id: str
    title: str = "雨夜の Lo-Fi"
    posted_at: datetime = field(default_factory=lambda: datetime(2026, 6, 15, tzinfo=UTC))
    privacy_status: str = "public"
    contains_synthetic_media: bool = True
    genre: str | None = "lofi"
    duration_sec: int | None = 120
    thumbnail_uri: str | None = "file:///t/thumb.jpg"


@dataclass
class _FakeSession:
    """``commit`` のみ持つ in-memory セッション (service が stub なので DB I/O 無し)。"""

    commits: int = 0

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class _SpyService:
    """``PanicStopService`` の stub。 戻りと例外、 受け取った引数を記録する。"""

    result: PanicStopResult | None = None
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def panic_stop(
        self,
        *,
        session: Any,
        scheduler_service: Any,
        window_hours: int = 24,
        set_private: list[str] | None = None,
        actor: str = "seita",
    ) -> PanicStopResult:
        del session
        self.calls.append(
            {
                "scheduler_service": scheduler_service,
                "window_hours": window_hours,
                "set_private": set_private,
                "actor": actor,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _settings() -> Settings:
    return Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))


def _build_client(session: _FakeSession, *, service: Any | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[require_basic_auth] = lambda: _USERNAME
    if service is not None:
        app.dependency_overrides[get_panic_stop_service] = lambda: service
    return TestClient(app)


# --- auth -------------------------------------------------------------------------


def test_requires_auth() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: _FakeSession()
    app.dependency_overrides[get_settings] = _settings
    client = TestClient(app)
    resp = client.post("/scheduler/panic-stop", json={})
    assert resp.status_code == 401


# --- happy path -------------------------------------------------------------------


def test_panic_stop_invokes_service_and_commits() -> None:
    video = _FakeVideo(youtube_video_id="vid_1", privacy_status="private")
    service = _SpyService(
        result=PanicStopResult(
            scheduler_enabled=False,
            recent_videos=[video],  # type: ignore[list-item]
            updated_count=1,
        )
    )
    session = _FakeSession()
    client = _build_client(session, service=service)

    resp = client.post(
        "/scheduler/panic-stop",
        json={"window_hours": 12, "set_private": ["vid_1"]},
        auth=_AUTH,
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scheduler_enabled"] is False
    assert payload["updated_count"] == 1
    assert len(payload["recent_videos"]) == 1
    assert payload["recent_videos"][0]["youtube_video_id"] == "vid_1"
    assert payload["recent_videos"][0]["privacy_status"] == "private"
    # commit は本ルータの責務。
    assert session.commits == 1
    # window_hours / set_private (文字列配列) / actor が service にそのまま渡る。
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["window_hours"] == 12
    assert call["set_private"] == ["vid_1"]
    assert call["actor"] == _USERNAME
    # scheduler_service は app.state に未配線なので None が渡る (500 にならない)。
    assert call["scheduler_service"] is None


def test_set_private_defaults_to_none_when_omitted() -> None:
    """``set_private`` 省略時は None (候補列挙のみ)。 既定 window_hours=24。"""
    service = _SpyService(
        result=PanicStopResult(scheduler_enabled=False, recent_videos=[], updated_count=0)
    )
    client = _build_client(_FakeSession(), service=service)

    resp = client.post("/scheduler/panic-stop", json={}, auth=_AUTH)

    assert resp.status_code == 200
    assert resp.json()["recent_videos"] == []
    call = service.calls[0]
    assert call["window_hours"] == 24
    assert call["set_private"] is None


# --- validation -------------------------------------------------------------------


def test_window_hours_below_one_is_422() -> None:
    """``window_hours <= 0`` は Field(ge=1) で 422 (service は呼ばれない)。"""
    service = _SpyService(
        result=PanicStopResult(scheduler_enabled=False, recent_videos=[], updated_count=0)
    )
    client = _build_client(_FakeSession(), service=service)

    resp = client.post("/scheduler/panic-stop", json={"window_hours": 0}, auth=_AUTH)

    assert resp.status_code == 422
    assert service.calls == []


def test_set_private_rejects_bool() -> None:
    """``set_private`` は文字列配列。 bool を渡すと 422 (契約厳守: 配列であって bool ではない)。"""
    service = _SpyService(
        result=PanicStopResult(scheduler_enabled=False, recent_videos=[], updated_count=0)
    )
    client = _build_client(_FakeSession(), service=service)

    resp = client.post("/scheduler/panic-stop", json={"set_private": True}, auth=_AUTH)

    assert resp.status_code == 422
    assert service.calls == []


# --- error mapping ----------------------------------------------------------------


def test_recoverable_error_maps_to_502() -> None:
    service = _SpyService(error=RecoverableError("youtube rejected"))
    session = _FakeSession()
    client = _build_client(session, service=service)

    resp = client.post("/scheduler/panic-stop", json={"set_private": ["vid_x"]}, auth=_AUTH)

    assert resp.status_code == 502
    # 例外時は commit しない。
    assert session.commits == 0


def test_transient_error_maps_to_503() -> None:
    service = _SpyService(error=TransientError("youtube 5xx"))
    session = _FakeSession()
    client = _build_client(session, service=service)

    resp = client.post("/scheduler/panic-stop", json={"set_private": ["vid_y"]}, auth=_AUTH)

    assert resp.status_code == 503
    assert session.commits == 0
