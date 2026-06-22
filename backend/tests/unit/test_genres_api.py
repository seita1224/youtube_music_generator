"""``/genres`` 業務ルータの単体テスト (api/genres.py, T107)。

DB は in-memory の :class:`_FakeSession` で代替し、 Basic 認証は ``require_basic_auth``
配下 router へ束ねる。 audit の Core insert は stub 化し、 実 DB / 実 LLM / HTTP は
一切起動しない。

検証観点:

- 認証: 無認証は 401。
- ``GET /genres``: 一覧 + ``total``、 ``enabled`` / ``role`` 絞り込み。
- ``POST /genres/{name}/promote``: 1 段昇格 (experimental→extended) + enabled=true /
  target_role 明示 (experimental→primary) / primary の更なる promote は 409 /
  不在は 404 / audit + commit。
- ``POST /genres/{name}/disable``: enabled=false / audit + commit / 不在は 404。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.api import genres as genres_module
from ymg_backend.api.genres import router
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import require_basic_auth
from ymg_backend.infrastructure.db.session import get_session

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


@dataclass
class _FakeGenre:
    """ORM ``Genre`` の最小スタンドイン (GenreResponse 写像用 / promote-disable 可変)。"""

    name: str
    display_name: str
    role: str
    enabled: bool
    description: str | None = None
    bpm_min: int | None = None
    bpm_max: int | None = None
    created_at: datetime = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    updated_at: datetime = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class _FakeResult:
    """``session.execute`` の戻り。 scalar 値 / scalars().all() を満たす。"""

    def __init__(self, rows: list[Any], *, scalar: Any | None = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


@dataclass
class _FakeSession:
    """get/count/list/insert(audit)/commit のみ実装する in-memory セッション。

    ``execute`` は組まれた ``select`` を ``str()`` / compile().params 検査して
    count / 一覧 / audit insert を判別する (本物の SQL は実行しない)。
    """

    rows: list[_FakeGenre] = field(default_factory=list)
    commits: int = 0
    flushes: int = 0
    audit_actions: list[str] = field(default_factory=list)

    async def get(self, _model: Any, pk: str) -> _FakeGenre | None:
        return next((r for r in self.rows if r.name == pk), None)

    async def execute(self, statement: Any) -> _FakeResult:
        sql = str(statement).lower()
        if sql.startswith("insert"):
            return _FakeResult([])
        filtered = self._filtered(statement)
        if "count" in sql:
            return _FakeResult([], scalar=len(filtered))
        return _FakeResult(filtered)

    def _filtered(self, statement: Any) -> list[_FakeGenre]:
        compiled = statement.compile()
        sql = str(compiled).lower()
        params = compiled.params
        rows = list(self.rows)

        # ``Genre.enabled.is_(bool)`` は ``enabled IS true/false`` をリテラル展開する
        # (bound param にならない) ため、 compile 文字列から判別する。
        if "enabled is true" in sql:
            rows = [r for r in rows if r.enabled]
        elif "enabled is false" in sql:
            rows = [r for r in rows if not r.enabled]
        role_vals = {v for k, v in params.items() if "role" in k.lower()}
        if role_vals:
            rows = [r for r in rows if r.role in role_vals]

        rows.sort(key=lambda r: r.name)
        return rows

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj: Any) -> None:
        return None


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def client(session: _FakeSession, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _fake_audit(_session: Any, *, action: str, **_kwargs: Any) -> str:
        session.audit_actions.append(action)
        return "00000000-0000-0000-0000-000000000000"

    monkeypatch.setattr(genres_module, "write_audit_log", _fake_audit)

    app = FastAPI()
    protected = APIRouter(dependencies=[Depends(require_basic_auth)])
    protected.include_router(router)
    app.include_router(protected)

    app.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME,
        admin_password=SecretStr(_PASSWORD),
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _genre(name: str, role: str = "experimental", *, enabled: bool = True) -> _FakeGenre:
    return _FakeGenre(name=name, display_name=name.title(), role=role, enabled=enabled)


# --- auth ------------------------------------------------------------------
def test_list_genres_requires_auth(client: TestClient) -> None:
    assert client.get("/genres").status_code == 401


# --- GET /genres -----------------------------------------------------------
def test_list_genres_returns_items_and_total(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_genre("lo-fi"), _genre("synthwave")]
    resp = client.get("/genres", auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {g["name"] for g in body["items"]} == {"lo-fi", "synthwave"}


def test_list_genres_filters_by_enabled(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_genre("a", enabled=True), _genre("b", enabled=False)]
    resp = client.get("/genres", params={"enabled": "false"}, auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "b"


def test_list_genres_filters_by_role(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_genre("a", role="primary"), _genre("b", role="experimental")]
    resp = client.get("/genres", params={"role": "primary"}, auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["role"] == "primary"


# --- POST /genres/{name}/promote -------------------------------------------
@pytest.mark.fr("FR-038")
def test_promote_steps_up_one_level_and_enables(client: TestClient, session: _FakeSession) -> None:
    """FR-038: experimental→extended へ 1 段昇格し enabled=True にする。"""
    session.rows = [_genre("lo-fi", role="experimental", enabled=False)]
    resp = client.post("/genres/lo-fi/promote", json={}, auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "extended"
    assert body["enabled"] is True
    assert session.commits == 1
    assert "genre_promoted" in session.audit_actions


def test_promote_with_explicit_target_role(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_genre("lo-fi", role="experimental")]
    resp = client.post("/genres/lo-fi/promote", json={"target_role": "primary"}, auth=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["role"] == "primary"


def test_promote_primary_conflicts(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_genre("lo-fi", role="primary")]
    resp = client.post("/genres/lo-fi/promote", json={}, auth=_AUTH)
    assert resp.status_code == 409
    assert resp.json()["detail"]["category"] == "recoverable"
    assert session.commits == 0


def test_promote_lower_target_conflicts(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_genre("lo-fi", role="primary")]
    resp = client.post("/genres/lo-fi/promote", json={"target_role": "extended"}, auth=_AUTH)
    assert resp.status_code == 409


def test_promote_missing_genre_404(client: TestClient) -> None:
    resp = client.post("/genres/unknown/promote", json={}, auth=_AUTH)
    assert resp.status_code == 404
    assert resp.json()["detail"]["category"] == "recoverable"


# --- POST /genres/{name}/disable -------------------------------------------
def test_disable_sets_enabled_false(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_genre("lo-fi", role="extended", enabled=True)]
    resp = client.post("/genres/lo-fi/disable", json={"reason": "low retention"}, auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["role"] == "extended"  # role は保持
    assert session.commits == 1
    assert "genre_disabled" in session.audit_actions


def test_disable_missing_genre_404(client: TestClient) -> None:
    resp = client.post("/genres/unknown/disable", json={}, auth=_AUTH)
    assert resp.status_code == 404
