"""``/posts`` 業務ルータの単体テスト (api/posts.py)。

DB は in-memory の :class:`FakeSession` で代替し、 Basic 認証は ``get_settings``
override で固定、 retry launcher / storage は stub に差し替える。 実 DB / 実
オーケストレータ / 実 storage / HTTP は一切起動しない。

検証観点:

- 認証: 無認証は 401。
- ``GET /posts``: 一覧返却 + ``plan_id`` / ``status`` / ``genre`` / ``from_date`` /
  ``to_date`` 絞り込み。
- ``GET /posts/{id}``: 取得 (200) / 不在 (404)。
- ``POST /posts/{id}/retry``: failed のみ 202 + post を pending に戻し launcher を呼ぶ /
  非 failed は 409 / 不在は 404。
- ``GET /posts/{id}/tracks``: メタ一覧 (``audio_uri`` 非露出) / Post 不在 404。
- ``GET .../audio`` / ``.../download``: WAV ストリーム / attachment / 404 系。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.api import posts as posts_module
from ymg_backend.api.posts import get_retry_launcher, get_storage_adapter, router
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import require_basic_auth
from ymg_backend.infrastructure.db.session import get_session

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


@dataclass
class _FakePost:
    """ORM ``Post`` の最小スタンドイン (PostResponse.from_attributes 用)。"""

    id: uuid.UUID
    plan_id: uuid.UUID
    position: int
    genre: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
    final_title: str | None = None
    final_description: str | None = None
    thumbnail_uri: str | None = None
    video_uri: str | None = None
    youtube_video_id: str | None = None
    scheduled_at: datetime | None = None
    posted_at: datetime | None = None
    retention_24h: Any | None = None
    views_24h: int | None = None
    error_category: str | None = None
    error_message: str | None = None


@dataclass
class _FakeTrack:
    """ORM ``AudioTrack`` の最小スタンドイン (AudioTrackResponse.from_attributes 用)。"""

    id: uuid.UUID
    post_id: uuid.UUID
    position: int
    audio_uri: str
    duration_sec: int
    generated_at: datetime
    bpm: int | None = None
    music_key: str | None = None
    subtheme: str | None = None
    acoustid_status: str = "not_checked"
    regenerated_count: int = 0


class _FakeResult:
    """``session.execute`` の戻り。 ``.scalars().all()`` / ``scalar_one_or_none`` を提供。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


@dataclass
class _FakeSession:
    """list/get/commit のみ実装する in-memory セッション。

    ``execute`` は受け取った statement を解釈し、 Post 一覧 / AudioTrack 一覧・単票を
    再現する (本物の SQL は走らせない)。
    """

    rows: list[_FakePost] = field(default_factory=list)
    tracks: list[_FakeTrack] = field(default_factory=list)
    commits: int = 0

    async def get(self, _model: Any, pk: uuid.UUID) -> _FakePost | None:
        return next((r for r in self.rows if r.id == pk), None)

    async def execute(self, statement: Any) -> _FakeResult:
        compiled = statement.compile()
        sql = str(compiled).lower()
        if "audio_tracks" in sql:
            return _FakeResult(self._apply_track_filters(statement))
        return _FakeResult(self._apply_filters(statement))

    def _apply_filters(self, statement: Any) -> list[_FakePost]:
        compiled = statement.compile()
        sql = str(compiled).lower()
        params = compiled.params
        rows = list(self.rows)

        plan_id_vals = {v for k, v in params.items() if "plan_id" in k.lower()}
        if plan_id_vals:
            rows = [r for r in rows if r.plan_id in plan_id_vals]

        status_vals = {v for k, v in params.items() if "status" in k.lower()}
        if status_vals:
            rows = [r for r in rows if r.status in status_vals]

        genre_vals = {v for k, v in params.items() if "genre" in k.lower()}
        if genre_vals:
            rows = [r for r in rows if r.genre in genre_vals]

        # created_at 範囲: ルータは下限 (>=) を from、 排他上界 (<) を to で組む。
        # 両者とも created_at バインド (datetime) なので、 出現順で下限/上界を割り当てる。
        dt_bounds = [v for v in params.values() if isinstance(v, datetime)]
        has_lower = ">=" in sql
        has_upper = "created_at <" in sql or " < " in sql
        if has_lower and dt_bounds:
            rows = [r for r in rows if r.created_at >= min(dt_bounds)]
        if has_upper and dt_bounds:
            rows = [r for r in rows if r.created_at < max(dt_bounds)]

        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    def _apply_track_filters(self, statement: Any) -> list[_FakeTrack]:
        compiled = statement.compile()
        params = compiled.params
        rows = list(self.tracks)

        post_id_vals = {
            v for k, v in params.items() if "post_id" in k.lower() and isinstance(v, uuid.UUID)
        }
        if post_id_vals:
            rows = [r for r in rows if r.post_id in post_id_vals]

        position_vals = {
            v
            for k, v in params.items()
            if "position" in k.lower() and isinstance(v, int) and not isinstance(v, bool)
        }
        if position_vals:
            rows = [r for r in rows if r.position in position_vals]

        rows.sort(key=lambda r: r.position)
        return rows

    async def commit(self) -> None:
        self.commits += 1


def _make_post(**overrides: Any) -> _FakePost:
    base = {
        "id": uuid.uuid4(),
        "plan_id": uuid.uuid4(),
        "position": 0,
        "genre": "lo-fi-hip-hop",
        "payload": {"mood": "calm"},
        "status": "generated",
        "created_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return _FakePost(**base)  # type: ignore[arg-type]


def _make_track(*, post_id: uuid.UUID, position: int = 0, **overrides: Any) -> _FakeTrack:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "post_id": post_id,
        "position": position,
        "audio_uri": f"file:///audio/{post_id}/{position}.wav",
        "duration_sec": 300,
        "generated_at": datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
        "bpm": 90,
        "music_key": "Am",
        "subtheme": "rain",
        "acoustid_status": "not_checked",
        "regenerated_count": 0,
    }
    base.update(overrides)
    return _FakeTrack(**base)


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
    """``open`` / ``exists`` / ``is_under_base`` を提供する storage stub。"""

    def __init__(
        self,
        *,
        data: bytes = b"WAVBYTES",
        existing: bool = True,
        under_base: bool = True,
    ) -> None:
        self._data = data
        self._existing = existing
        self._under_base = under_base

    def configure(
        self,
        *,
        data: bytes | None = None,
        existing: bool | None = None,
        under_base: bool | None = None,
    ) -> None:
        """テスト側から返却バイト / 存在フラグ / 閉じ込め判定を差し替える。"""
        if data is not None:
            self._data = data
        if existing is not None:
            self._existing = existing
        if under_base is not None:
            self._under_base = under_base

    def exists(self, _uri: str) -> bool:
        return self._existing

    def is_under_base(self, _uri: str) -> bool:
        return self._under_base

    def open(self, _uri: str, _mode: str = "rb") -> _FakeHandle:
        return _FakeHandle(self._data)


@dataclass
class _LauncherSpy:
    """retry launcher の呼び出しを記録する stub。"""

    calls: list[uuid.UUID] = field(default_factory=list)

    def __call__(self, *, post_id: uuid.UUID, background: Any) -> None:
        del background
        self.calls.append(post_id)


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def launcher() -> _LauncherSpy:
    return _LauncherSpy()


@pytest.fixture
def storage() -> _FakeStorage:
    return _FakeStorage()


@pytest.fixture
def client(
    session: _FakeSession,
    launcher: _LauncherSpy,
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    # audit の Core insert は本テストの対象外。 呼び出しが起きたことだけ保証できれば
    # 十分なので no-op に差し替える (FakeSession に audit テーブルを持たせない)。
    async def _fake_audit(*_args: Any, **_kwargs: Any) -> uuid.UUID:
        return uuid.uuid4()

    monkeypatch.setattr(posts_module, "write_audit_log", _fake_audit)

    app = FastAPI()
    protected = APIRouter(dependencies=[Depends(require_basic_auth)])
    protected.include_router(router)
    app.include_router(protected)

    app.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME,
        admin_password=SecretStr(_PASSWORD),
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_retry_launcher] = lambda: launcher
    app.dependency_overrides[get_storage_adapter] = lambda: storage
    return TestClient(app)


def test_list_posts_requires_auth(client: TestClient) -> None:
    resp = client.get("/posts")
    assert resp.status_code == 401


def test_list_posts_returns_items(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_make_post(), _make_post()]
    resp = client.get("/posts", auth=_AUTH)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2


def test_list_posts_filters_by_plan_id(client: TestClient, session: _FakeSession) -> None:
    plan_a = uuid.uuid4()
    plan_b = uuid.uuid4()
    session.rows = [
        _make_post(plan_id=plan_a, status="music_generated"),
        _make_post(plan_id=plan_b, status="music_generated"),
        _make_post(plan_id=plan_a, status="pending"),
    ]
    resp = client.get("/posts", params={"plan_id": str(plan_a)}, auth=_AUTH)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert {item["plan_id"] for item in items} == {str(plan_a)}


def test_list_posts_filters_by_status(client: TestClient, session: _FakeSession) -> None:
    session.rows = [
        _make_post(status="failed"),
        _make_post(status="posted"),
    ]
    resp = client.get("/posts", params={"status": "failed"}, auth=_AUTH)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"


def test_list_posts_accepts_music_generated_status(
    client: TestClient, session: _FakeSession
) -> None:
    session.rows = [
        _make_post(status="music_generated"),
        _make_post(status="generated"),
    ]
    resp = client.get("/posts", params={"status": "music_generated"}, auth=_AUTH)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "music_generated"


def test_list_posts_filters_by_genre(client: TestClient, session: _FakeSession) -> None:
    session.rows = [
        _make_post(genre="lo-fi-hip-hop"),
        _make_post(genre="synthwave"),
    ]
    resp = client.get("/posts", params={"genre": "synthwave"}, auth=_AUTH)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["genre"] == "synthwave"


def test_list_posts_filters_by_date_range(client: TestClient, session: _FakeSession) -> None:
    base = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)
    session.rows = [
        _make_post(created_at=base - timedelta(days=5)),  # 06-05 範囲外
        _make_post(created_at=base),  # 06-10 範囲内
        _make_post(created_at=base + timedelta(days=5)),  # 06-15 範囲外
    ]
    resp = client.get(
        "/posts",
        params={
            "from_date": date(2026, 6, 8).isoformat(),
            "to_date": date(2026, 6, 12).isoformat(),
        },
        auth=_AUTH,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["created_at"].startswith("2026-06-10")


def test_get_post_returns_post(client: TestClient, session: _FakeSession) -> None:
    post = _make_post(final_title="Chill Beats")
    session.rows = [post]
    resp = client.get(f"/posts/{post.id}", auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(post.id)
    assert body["final_title"] == "Chill Beats"


def test_get_post_not_found_returns_404(client: TestClient) -> None:
    resp = client.get(f"/posts/{uuid.uuid4()}", auth=_AUTH)
    assert resp.status_code == 404


def test_retry_failed_post_returns_202_and_resets_state(
    client: TestClient,
    session: _FakeSession,
    launcher: _LauncherSpy,
) -> None:
    post = _make_post(
        status="failed",
        error_category="recoverable",
        error_message="boom",
    )
    session.rows = [post]

    resp = client.post(f"/posts/{post.id}/retry", auth=_AUTH)

    assert resp.status_code == 202
    body = resp.json()
    assert body["post_id"] == str(post.id)
    assert body["status"] == "pending"
    # 状態がリセットされ commit され、 launcher が起動されている。
    assert post.status == "pending"
    assert post.error_category is None
    assert post.error_message is None
    assert session.commits == 1
    assert launcher.calls == [post.id]


def test_retry_non_failed_post_returns_409(
    client: TestClient,
    session: _FakeSession,
    launcher: _LauncherSpy,
) -> None:
    post = _make_post(status="posted")
    session.rows = [post]
    resp = client.post(f"/posts/{post.id}/retry", auth=_AUTH)
    assert resp.status_code == 409
    # 非 failed は状態を変えず launcher も呼ばない。
    assert post.status == "posted"
    assert launcher.calls == []


def test_retry_missing_post_returns_404(client: TestClient, launcher: _LauncherSpy) -> None:
    resp = client.post(f"/posts/{uuid.uuid4()}/retry", auth=_AUTH)
    assert resp.status_code == 404
    assert launcher.calls == []


# --- tracks / audio / download ----------------------------------------------------


def test_list_tracks_requires_auth(client: TestClient, session: _FakeSession) -> None:
    post = _make_post()
    session.rows = [post]
    resp = client.get(f"/posts/{post.id}/tracks")
    assert resp.status_code == 401


def test_list_tracks_returns_meta_without_audio_uri(
    client: TestClient, session: _FakeSession
) -> None:
    post = _make_post(status="music_generated")
    tracks = [_make_track(post_id=post.id, position=i) for i in range(6)]
    session.rows = [post]
    session.tracks = tracks

    resp = client.get(f"/posts/{post.id}/tracks", auth=_AUTH)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 6
    assert [item["position"] for item in items] == list(range(6))
    for item in items:
        assert "audio_uri" not in item
        assert item["post_id"] == str(post.id)
        assert item["duration_sec"] == 300
        assert item["acoustid_status"] == "not_checked"


def test_list_tracks_empty_when_no_tracks(client: TestClient, session: _FakeSession) -> None:
    post = _make_post(status="pending")
    session.rows = [post]
    resp = client.get(f"/posts/{post.id}/tracks", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_list_tracks_post_missing_returns_404(client: TestClient) -> None:
    resp = client.get(f"/posts/{uuid.uuid4()}/tracks", auth=_AUTH)
    assert resp.status_code == 404


def test_stream_audio_returns_wav(
    client: TestClient, session: _FakeSession, storage: _FakeStorage
) -> None:
    post = _make_post(status="music_generated")
    track = _make_track(post_id=post.id, position=2)
    session.rows = [post]
    session.tracks = [track]
    storage.configure(data=b"RIFF....WAVE")

    resp = client.get(f"/posts/{post.id}/tracks/2/audio", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content == b"RIFF....WAVE"
    assert "content-disposition" not in {k.lower() for k in resp.headers}


def test_download_audio_returns_attachment(
    client: TestClient, session: _FakeSession, storage: _FakeStorage
) -> None:
    post = _make_post(status="music_generated")
    track = _make_track(post_id=post.id, position=0)
    session.rows = [post]
    session.tracks = [track]
    storage.configure(data=b"WAVDATA")

    resp = client.get(f"/posts/{post.id}/tracks/0/download", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.headers["content-disposition"] == 'attachment; filename="track-0.wav"'
    assert resp.content == b"WAVDATA"


def test_stream_audio_requires_auth(client: TestClient, session: _FakeSession) -> None:
    post = _make_post()
    session.rows = [post]
    session.tracks = [_make_track(post_id=post.id, position=0)]
    resp = client.get(f"/posts/{post.id}/tracks/0/audio")
    assert resp.status_code == 401


def test_stream_audio_404_when_post_missing(client: TestClient) -> None:
    resp = client.get(f"/posts/{uuid.uuid4()}/tracks/0/audio", auth=_AUTH)
    assert resp.status_code == 404


def test_stream_audio_404_when_track_missing(client: TestClient, session: _FakeSession) -> None:
    post = _make_post()
    session.rows = [post]
    resp = client.get(f"/posts/{post.id}/tracks/3/audio", auth=_AUTH)
    assert resp.status_code == 404


def test_stream_audio_404_when_object_missing(
    client: TestClient, session: _FakeSession, storage: _FakeStorage
) -> None:
    post = _make_post()
    session.rows = [post]
    session.tracks = [_make_track(post_id=post.id, position=1)]
    storage.configure(existing=False)

    resp = client.get(f"/posts/{post.id}/tracks/1/audio", auth=_AUTH)
    assert resp.status_code == 404


def test_download_audio_404_when_object_missing(
    client: TestClient, session: _FakeSession, storage: _FakeStorage
) -> None:
    post = _make_post()
    session.rows = [post]
    session.tracks = [_make_track(post_id=post.id, position=1)]
    storage.configure(existing=False)

    resp = client.get(f"/posts/{post.id}/tracks/1/download", auth=_AUTH)
    assert resp.status_code == 404


def test_stream_audio_404_when_uri_outside_storage_base(
    client: TestClient, session: _FakeSession, storage: _FakeStorage
) -> None:
    """storage base 外の audio_uri は open せず 404。"""
    post = _make_post()
    session.rows = [post]
    session.tracks = [
        _make_track(post_id=post.id, position=0, audio_uri="file:///etc/passwd")
    ]
    storage.configure(under_base=False, existing=True)

    resp = client.get(f"/posts/{post.id}/tracks/0/audio", auth=_AUTH)
    assert resp.status_code == 404


def test_download_audio_404_when_uri_outside_storage_base(
    client: TestClient, session: _FakeSession, storage: _FakeStorage
) -> None:
    post = _make_post()
    session.rows = [post]
    session.tracks = [
        _make_track(post_id=post.id, position=0, audio_uri="file:///etc/passwd")
    ]
    storage.configure(under_base=False, existing=True)

    resp = client.get(f"/posts/{post.id}/tracks/0/download", auth=_AUTH)
    assert resp.status_code == 404


def test_track_position_out_of_range_returns_422(client: TestClient, session: _FakeSession) -> None:
    post = _make_post()
    session.rows = [post]
    resp = client.get(f"/posts/{post.id}/tracks/6/audio", auth=_AUTH)
    assert resp.status_code == 422
