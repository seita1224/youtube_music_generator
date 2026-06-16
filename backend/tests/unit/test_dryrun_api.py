"""``/dryrun`` レビュー業務ルータの単体テスト (api/dryrun.py)。

DB は in-memory の :class:`_FakeSession` で代替し、 Basic 認証は ``require_basic_auth``
override で固定、 ``DryrunService`` / ``StorageAdapter`` は stub に差し替える。 実 DB /
実 YouTube / 実 storage / HTTP は一切起動しない。

検証観点:

- ``GET /dryrun/outputs``: 一覧返却 + ``state`` 絞り込み。
- ``POST /outputs/{id}/approve``: service.approve を呼び 200 + commit / 不在 404 / 非 pending 409。
- ``POST /outputs/{id}/reject``: reason min4 を満たせば 200 + commit / 短い reason は 422
  (FastAPI ボディ検証、 service は呼ばれない)。
- ``GET /outputs/{id}/video``: 配信可 state は 200 chunked / 削除済み state は 409 /
  不在・オブジェクト消失は 404。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.api.dryrun import get_dryrun_service, get_storage_adapter, router
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import require_basic_auth
from ymg_backend.domain.dryrun.service import (
    DryrunNotFoundError,
    DryrunStateConflictError,
)
from ymg_backend.infrastructure.db.session import get_session

pytestmark = pytest.mark.unit

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


@dataclass
class _FakeOutput:
    """ORM ``DryrunOutput`` の最小スタンドイン (from_attributes 用)。"""

    id: uuid.UUID
    post_id: uuid.UUID
    video_uri: str
    state: str = "pending"
    reject_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime(2026, 6, 1, tzinfo=UTC))
    reviewed_at: datetime | None = None
    auto_expired_at: datetime | None = None
    posted_at: datetime | None = None
    # 一覧 join 由来(Post.final_title / Post.thumbnail_uri 相当)。
    final_title: str | None = None
    thumbnail_uri: str | None = None


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


@dataclass
class _FakePost:
    """ORM ``Post`` の最小スタンドイン (サムネ endpoint の thumbnail_uri 解決用)。"""

    id: uuid.UUID
    thumbnail_uri: str | None = None


@dataclass
class _FakeSession:
    """``get`` / ``execute`` / ``commit`` のみ持つ in-memory セッション。"""

    rows: list[_FakeOutput] = field(default_factory=list)
    posts: dict[uuid.UUID, _FakePost] = field(default_factory=dict)
    commits: int = 0

    async def get(self, model: Any, pk: uuid.UUID) -> Any:
        if getattr(model, "__name__", "") == "Post":
            return self.posts.get(pk)
        return next((r for r in self.rows if r.id == pk), None)

    async def execute(self, statement: Any) -> _FakeResult:
        compiled = statement.compile()
        params = compiled.params
        rows = list(self.rows)
        state_vals = {v for k, v in params.items() if "state" in k.lower()}
        if state_vals:
            rows = [r for r in rows if r.state in state_vals]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        # 一覧 endpoint は (DryrunOutput, Post.final_title, Post.thumbnail_uri) の
        # 3 タプルを join で受け取るため、 fake もタプルで返す。
        return _FakeResult([(r, r.final_title, r.thumbnail_uri) for r in rows])

    async def commit(self) -> None:
        self.commits += 1


class _FakeHandle:
    """``read(size)`` をチャンク分割で返すファイルハンドル stub。"""

    def __init__(self, data: bytes) -> None:
        self._buf = memoryview(data)
        self._pos = 0

    def read(self, size: int) -> bytes:
        chunk = bytes(self._buf[self._pos : self._pos + size])
        self._pos += len(chunk)
        return chunk

    def __enter__(self) -> _FakeHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeStorage:
    """``open`` / ``exists`` を提供する storage stub。"""

    def __init__(self, *, data: bytes = b"VIDEOBYTES", existing: bool = True) -> None:
        self._data = data
        self._existing = existing

    def exists(self, _uri: str) -> bool:
        return self._existing

    def open(self, _uri: str, _mode: str = "rb") -> _FakeHandle:
        return _FakeHandle(self._data)


@dataclass
class _SpyService:
    """``DryrunService`` の stub。 approve/reject の戻りと例外を制御する。"""

    output: _FakeOutput | None = None
    approve_error: Exception | None = None
    reject_error: Exception | None = None
    approve_calls: list[uuid.UUID] = field(default_factory=list)
    reject_calls: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    async def approve(self, *, session: Any, output_id: uuid.UUID) -> _FakeOutput:
        del session
        self.approve_calls.append(output_id)
        if self.approve_error is not None:
            raise self.approve_error
        assert self.output is not None
        return self.output

    async def reject(self, *, session: Any, output_id: uuid.UUID, reason: str) -> _FakeOutput:
        del session
        self.reject_calls.append((output_id, reason))
        if self.reject_error is not None:
            raise self.reject_error
        assert self.output is not None
        return self.output


def _settings() -> Settings:
    return Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))


def _make_output(**overrides: Any) -> _FakeOutput:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "post_id": uuid.uuid4(),
        "video_uri": "file:///tmp/v/video.mp4",
    }
    base.update(overrides)
    return _FakeOutput(**base)


def _build_client(
    session: _FakeSession,
    *,
    service: Any | None = None,
    storage: Any | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[require_basic_auth] = lambda: _USERNAME
    if service is not None:
        app.dependency_overrides[get_dryrun_service] = lambda: service
    if storage is not None:
        app.dependency_overrides[get_storage_adapter] = lambda: storage
    return TestClient(app)


# --- auth -------------------------------------------------------------------------


def test_requires_auth() -> None:
    session = _FakeSession()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = _settings
    client = TestClient(app)
    resp = client.get("/dryrun/outputs")
    assert resp.status_code == 401


# --- list -------------------------------------------------------------------------


def test_list_returns_items_and_filters_by_state() -> None:
    pending = _make_output(
        state="pending", final_title="雨夜の Lo-Fi", thumbnail_uri="file:///t/thumb.jpg"
    )
    posted = _make_output(state="posted")
    session = _FakeSession(rows=[pending, posted])
    client = _build_client(session)

    all_resp = client.get("/dryrun/outputs", auth=_AUTH)
    assert all_resp.status_code == 200
    assert {item["state"] for item in all_resp.json()["items"]} == {"pending", "posted"}

    filtered = client.get("/dryrun/outputs", params={"state": "pending"}, auth=_AUTH)
    assert filtered.status_code == 200
    items = filtered.json()["items"]
    assert len(items) == 1
    assert items[0]["state"] == "pending"
    # US2 改善: join した title / has_thumbnail が反映される。
    assert items[0]["title"] == "雨夜の Lo-Fi"
    assert items[0]["has_thumbnail"] is True


def test_list_item_without_post_metadata_defaults() -> None:
    """final_title / thumbnail_uri が無い post は title=None / has_thumbnail=False。"""
    bare = _make_output(state="pending")
    session = _FakeSession(rows=[bare])
    client = _build_client(session)

    resp = client.get("/dryrun/outputs", auth=_AUTH)
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["title"] is None
    assert item["has_thumbnail"] is False


# --- approve ----------------------------------------------------------------------


def test_approve_returns_posted_and_commits() -> None:
    output = _make_output(state="posted", post_id=uuid.uuid4())
    session = _FakeSession()
    service = _SpyService(output=output)
    client = _build_client(session, service=service)

    resp = client.post(f"/dryrun/outputs/{output.id}/approve", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["state"] == "posted"
    assert service.approve_calls == [output.id]
    assert session.commits == 1


def test_approve_missing_returns_404() -> None:
    oid = uuid.uuid4()
    session = _FakeSession()
    service = _SpyService(approve_error=DryrunNotFoundError(oid))
    client = _build_client(session, service=service)

    resp = client.post(f"/dryrun/outputs/{oid}/approve", auth=_AUTH)
    assert resp.status_code == 404
    assert session.commits == 0


def test_approve_non_pending_returns_409() -> None:
    oid = uuid.uuid4()
    session = _FakeSession()
    service = _SpyService(
        approve_error=DryrunStateConflictError(oid, current_state="posted", action="approved")
    )
    client = _build_client(session, service=service)

    resp = client.post(f"/dryrun/outputs/{oid}/approve", auth=_AUTH)
    assert resp.status_code == 409
    assert session.commits == 0


# --- reject -----------------------------------------------------------------------


def test_reject_valid_reason_commits() -> None:
    output = _make_output(state="rejected", reject_reason="ノイズが目立つため不採用")
    session = _FakeSession()
    service = _SpyService(output=output)
    client = _build_client(session, service=service)

    resp = client.post(
        f"/dryrun/outputs/{output.id}/reject",
        json={"reason": "ノイズが目立つため不採用"},
        auth=_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "rejected"
    assert service.reject_calls == [(output.id, "ノイズが目立つため不採用")]
    assert session.commits == 1


def test_reject_short_reason_returns_422_without_calling_service() -> None:
    oid = uuid.uuid4()
    session = _FakeSession()
    service = _SpyService(output=_make_output())
    client = _build_client(session, service=service)

    resp = client.post(f"/dryrun/outputs/{oid}/reject", json={"reason": "ng"}, auth=_AUTH)
    assert resp.status_code == 422
    assert service.reject_calls == []
    assert session.commits == 0


# --- video ------------------------------------------------------------------------


def test_video_streams_for_servable_state() -> None:
    output = _make_output(state="pending")
    session = _FakeSession(rows=[output])
    storage = _FakeStorage(data=b"ABCDEFGHIJ", existing=True)
    client = _build_client(session, storage=storage)

    resp = client.get(f"/dryrun/outputs/{output.id}/video", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == b"ABCDEFGHIJ"


def test_video_409_for_deleted_state() -> None:
    output = _make_output(state="rejected")
    session = _FakeSession(rows=[output])
    storage = _FakeStorage(existing=True)
    client = _build_client(session, storage=storage)

    resp = client.get(f"/dryrun/outputs/{output.id}/video", auth=_AUTH)
    assert resp.status_code == 409


def test_video_404_when_output_missing() -> None:
    session = _FakeSession()
    storage = _FakeStorage(existing=True)
    client = _build_client(session, storage=storage)

    resp = client.get(f"/dryrun/outputs/{uuid.uuid4()}/video", auth=_AUTH)
    assert resp.status_code == 404


def test_video_404_when_object_missing() -> None:
    output = _make_output(state="posted")
    session = _FakeSession(rows=[output])
    storage = _FakeStorage(existing=False)
    client = _build_client(session, storage=storage)

    resp = client.get(f"/dryrun/outputs/{output.id}/video", auth=_AUTH)
    assert resp.status_code == 404


# --- thumbnail --------------------------------------------------------------------


def test_thumbnail_streams_image_jpeg() -> None:
    output = _make_output(state="pending")
    post = _FakePost(id=output.post_id, thumbnail_uri="file:///t/thumb.jpg")
    session = _FakeSession(rows=[output], posts={post.id: post})
    storage = _FakeStorage(data=b"JPEGDATA", existing=True)
    client = _build_client(session, storage=storage)

    resp = client.get(f"/dryrun/outputs/{output.id}/thumbnail", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == b"JPEGDATA"


def test_thumbnail_404_when_post_has_no_thumbnail() -> None:
    output = _make_output(state="pending")
    post = _FakePost(id=output.post_id, thumbnail_uri=None)
    session = _FakeSession(rows=[output], posts={post.id: post})
    storage = _FakeStorage(existing=True)
    client = _build_client(session, storage=storage)

    resp = client.get(f"/dryrun/outputs/{output.id}/thumbnail", auth=_AUTH)
    assert resp.status_code == 404


def test_thumbnail_404_when_output_missing() -> None:
    session = _FakeSession()
    storage = _FakeStorage(existing=True)
    client = _build_client(session, storage=storage)

    resp = client.get(f"/dryrun/outputs/{uuid.uuid4()}/thumbnail", auth=_AUTH)
    assert resp.status_code == 404
