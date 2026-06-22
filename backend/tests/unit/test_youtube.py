"""YouTube OAuth / uploader / compliance_gate の単体テスト (US1).

外部依存 (Google OAuth トークンエンドポイント / YouTube Data API / DB) はすべて
mock / fake で置き換え、 実 HTTP・実 DB を打たない。 契約:

- ``YouTubeOAuth.get_access_token`` — 失効間近なら refresh → 再暗号化保存 → 平文返却。
- ``YouTubeUploader.upload`` — body に ``containsSyntheticMedia=true`` を必ず付与し、
  ``videos.insert`` 実行後に ``Post.youtube_video_id`` / ``posted_at`` を更新。
- ``compliance_gate`` — body が合成メディアフラグ無しなら ``ComplianceError`` で停止。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from google.oauth2.credentials import Credentials
from pydantic import SecretStr

from ymg_backend.core.config import Settings
from ymg_backend.core.security import build_cipher_from_settings
from ymg_backend.domain.errors.errors import ComplianceError, FatalError, RecoverableError
from ymg_backend.infrastructure.db.models import OAuthCredential, Post
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter
from ymg_backend.infrastructure.youtube.compliance_gate import (
    _build_upload_status_body,
    compliance_gate,
)
from ymg_backend.infrastructure.youtube.oauth import (
    SERVICE_YOUTUBE,
    YOUTUBE_UPLOAD_SCOPE,
    YouTubeOAuth,
)
from ymg_backend.infrastructure.youtube.uploader import (
    YouTubeUploader,
    _extract_video_id,
    _to_rfc3339,
)

# asyncio_mode = "auto" (pyproject) のため、 async テストはマーカ不要で自動収集される。
# 同一ファイル内に同期テストも含むため、 ブランケットの pytestmark は付けない。

# Fernet 鍵 (テスト用固定値、 urlsafe base64 32 byte)。
_FERNET_KEY = "x" * 43 + "="
_VALID_FERNET_KEY = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="


def _settings() -> Settings:
    return Settings(
        fernet_key=SecretStr(_VALID_FERNET_KEY),
        youtube_client_id="cid.apps.googleusercontent.com",
        youtube_client_secret=SecretStr("client-secret"),
        youtube_channel_id="UC_test_channel",
    )


# --------------------------------------------------------------------------- #
# fake AsyncSession (oauth / uploader 用)。 必要メソッドのみ実装する。
# --------------------------------------------------------------------------- #
class _ScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def first(self) -> Any | None:
        return self._items[0] if self._items else None


class _ExecuteResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._items)


class FakeSession:
    """``execute`` で固定レコードを返し ``flush`` を記録する最小 fake セッション。"""

    def __init__(self, records: list[Any] | None = None) -> None:
        self._records = records or []
        self.flush_count = 0
        self.added: list[Any] = []
        self.executed: list[Any] = []

    async def execute(self, _stmt: Any) -> _ExecuteResult:
        self.executed.append(_stmt)
        return _ExecuteResult(list(self._records))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1


def _make_credential(cipher: Any, *, expires_at: datetime | None) -> OAuthCredential:
    return OAuthCredential(
        id=uuid.uuid4(),
        service=SERVICE_YOUTUBE,
        channel_id="UC_test_channel",
        access_token_encrypted=cipher.encrypt("old-access"),
        refresh_token_encrypted=cipher.encrypt("refresh-token"),
        scopes=[YOUTUBE_UPLOAD_SCOPE],
        expires_at=expires_at,
    )


# --------------------------------------------------------------------------- #
# YouTubeOAuth
# --------------------------------------------------------------------------- #
async def test_get_access_token_returns_existing_when_valid() -> None:
    """失効まで十分あれば refresh せず既存トークンをそのまま返す。"""
    settings = _settings()
    cipher = build_cipher_from_settings(settings)
    cred = _make_credential(cipher, expires_at=datetime.now(UTC) + timedelta(hours=1))
    session = FakeSession([cred])

    refreshed: list[bool] = []

    def _refresher(_c: Credentials) -> None:
        refreshed.append(True)

    oauth = YouTubeOAuth(settings, cipher, refresher=_refresher)
    token = await oauth.get_access_token(session=session)  # type: ignore[arg-type]

    assert token == "old-access"
    assert refreshed == []
    assert session.flush_count == 0


async def test_get_access_token_refreshes_when_expired() -> None:
    """失効間近なら refresher を呼び、 新トークンを再暗号化保存して返す。"""
    settings = _settings()
    cipher = build_cipher_from_settings(settings)
    cred = _make_credential(cipher, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    session = FakeSession([cred])

    def _refresher(credentials: Credentials) -> None:
        credentials.token = "new-access"
        credentials.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    oauth = YouTubeOAuth(settings, cipher, refresher=_refresher)
    token = await oauth.get_access_token(session=session)  # type: ignore[arg-type]

    assert token == "new-access"
    assert session.flush_count == 1
    # 再暗号化されて保存されたことを復号で確認する。
    assert cipher.decrypt(cred.access_token_encrypted) == "new-access"


async def test_get_access_token_missing_credential_raises_recoverable() -> None:
    """認証情報未登録なら RecoverableError (再認証が必要)。"""
    settings = _settings()
    cipher = build_cipher_from_settings(settings)
    session = FakeSession([])

    oauth = YouTubeOAuth(settings, cipher)
    with pytest.raises(RecoverableError):
        await oauth.get_access_token(session=session)  # type: ignore[arg-type]


async def test_get_access_token_refresh_failure_raises_recoverable() -> None:
    """refresh 失敗 (トークン失効) は RecoverableError に写像される。"""
    settings = _settings()
    cipher = build_cipher_from_settings(settings)
    cred = _make_credential(cipher, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    session = FakeSession([cred])

    def _refresher(_c: Credentials) -> None:
        raise RuntimeError("invalid_grant")

    oauth = YouTubeOAuth(settings, cipher, refresher=_refresher)
    with pytest.raises(RecoverableError):
        await oauth.get_access_token(session=session)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# fake YouTube service (uploader 用)。
# --------------------------------------------------------------------------- #
class _FakeRequest:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def execute(self) -> dict[str, Any]:
        return self._response


class _FakeVideosResource:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def insert(self, *, part: str, body: dict[str, Any], media_body: Any) -> _FakeRequest:
        self._recorder["insert_part"] = part
        self._recorder["insert_body"] = body
        self._recorder["insert_media"] = media_body
        return _FakeRequest({"id": "yt-video-123"})


class _FakeThumbnailsResource:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def set(self, *, videoId: str, media_body: Any) -> _FakeRequest:
        self._recorder["thumbnail_video_id"] = videoId
        return _FakeRequest({})


class _FakeYouTubeService:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def videos(self) -> _FakeVideosResource:
        return _FakeVideosResource(self._recorder)

    def thumbnails(self) -> _FakeThumbnailsResource:
        return _FakeThumbnailsResource(self._recorder)


class _StubOAuth:
    """``get_access_token`` だけを返すスタブ (uploader テスト用)。"""

    async def get_access_token(self, *, session: Any) -> str:
        return "access-token"


def _make_post(*, with_thumbnail: bool = True) -> Post:
    post = Post(
        id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        position=0,
        genre="lo-fi-hip-hop",
        payload={},
        status="generated",
    )
    post.video_uri = "memory://outputs/video/post/final.mp4"
    post.final_title = "Lo-Fi Beats"
    post.final_description = "AI 生成の音楽です。"
    post.thumbnail_uri = "memory://outputs/thumb/post/thumb.jpg" if with_thumbnail else None
    return post


def _memory_storage() -> StorageAdapter:
    storage = StorageAdapter()
    storage.write_bytes("memory://outputs/video/post/final.mp4", b"FAKE-MP4-BYTES")
    storage.write_bytes("memory://outputs/thumb/post/thumb.jpg", b"FAKE-JPG-BYTES")
    return storage


# --------------------------------------------------------------------------- #
# YouTubeUploader
# --------------------------------------------------------------------------- #
@pytest.mark.fr("FR-006")
async def test_upload_sets_synthetic_media_flag_and_updates_post() -> None:
    """FR-006: body に containsSyntheticMedia=true を付与し、 投稿後 Post を更新する。"""
    settings = _settings()
    recorder: dict[str, Any] = {}
    uploader = YouTubeUploader(
        _StubOAuth(),  # type: ignore[arg-type]
        _memory_storage(),
        settings,
        service_factory=lambda _creds: _FakeYouTubeService(recorder),
    )
    post = _make_post()
    session = FakeSession()

    video_id = await uploader.upload(session=session, post=post)  # type: ignore[arg-type]

    assert video_id == "yt-video-123"
    assert recorder["insert_part"] == "snippet,status"
    assert recorder["insert_body"]["status"]["containsSyntheticMedia"] is True
    assert recorder["thumbnail_video_id"] == "yt-video-123"
    assert post.youtube_video_id == "yt-video-123"
    assert post.posted_at is not None
    assert session.flush_count >= 1


async def test_upload_missing_video_uri_raises_fatal() -> None:
    """必須フィールド (video_uri) 欠落は FatalError。"""
    settings = _settings()
    uploader = YouTubeUploader(
        _StubOAuth(),  # type: ignore[arg-type]
        _memory_storage(),
        settings,
        service_factory=lambda _creds: _FakeYouTubeService({}),
    )
    post = _make_post()
    post.video_uri = None
    session = FakeSession()

    with pytest.raises(FatalError):
        await uploader.upload(session=session, post=post)  # type: ignore[arg-type]


async def test_upload_without_thumbnail_skips_thumbnail_call() -> None:
    """サムネ URI が無ければ thumbnails.set を呼ばない。"""
    settings = _settings()
    recorder: dict[str, Any] = {}
    uploader = YouTubeUploader(
        _StubOAuth(),  # type: ignore[arg-type]
        _memory_storage(),
        settings,
        service_factory=lambda _creds: _FakeYouTubeService(recorder),
    )
    post = _make_post(with_thumbnail=False)
    session = FakeSession()

    await uploader.upload(session=session, post=post)  # type: ignore[arg-type]
    assert "thumbnail_video_id" not in recorder


def test_build_body_includes_publish_at_for_scheduled_post() -> None:
    """scheduled_at があれば private + publishAt の予約投稿になる。"""
    settings = _settings()
    uploader = YouTubeUploader(
        _StubOAuth(),  # type: ignore[arg-type]
        _memory_storage(),
        settings,
        service_factory=lambda _creds: _FakeYouTubeService({}),
    )
    post = _make_post()
    post.scheduled_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    body = uploader._build_body(post)
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == "2026-07-01T09:00:00Z"


def test_extract_video_id_and_rfc3339_helpers() -> None:
    assert _extract_video_id({"id": "abc"}) == "abc"
    assert _extract_video_id({}) is None
    assert _extract_video_id("not-a-dict") is None
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert _to_rfc3339(naive) == "2026-01-02T03:04:05Z"


# --------------------------------------------------------------------------- #
# compliance_gate
# --------------------------------------------------------------------------- #
class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify(self, *, level: Any, message: str, context: Any = None) -> None:
        self.calls.append({"level": level, "message": message, "context": context})

    async def notify_error(
        self, exc: BaseException, *, context: Any = None
    ) -> None:  # pragma: no cover
        self.calls.append({"exc": exc, "context": context})


async def test_compliance_gate_passes_when_flag_present() -> None:
    """正常 body (フラグ付き) なら例外なく通過し、 通知も飛ばない。"""
    post = _make_post()
    session = FakeSession()
    notifier = _RecordingNotifier()

    await compliance_gate(
        session=session,  # type: ignore[arg-type]
        post=post,
        description="AI 生成の音楽です。",
        video_uri=str(post.video_uri),
        notifier=notifier,  # type: ignore[arg-type]
    )
    assert notifier.calls == []


@pytest.mark.fr("FR-007")
async def test_compliance_gate_raises_when_flag_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-007: フラグ欠落 body が来たら ComplianceError + ERROR 通知。"""
    import importlib

    # パッケージ __init__ が関数を再 export しているため、 真のモジュールを取得する。
    gate_mod = importlib.import_module("ymg_backend.infrastructure.youtube.compliance_gate")

    # body を意図的にフラグ無しへ差し替え、 enforcer の違反検出経路を通す。
    def _broken_body(*, description: str, video_uri: str) -> dict[str, Any]:
        return {"snippet": {"description": description}, "status": {}}

    monkeypatch.setattr(gate_mod, "_build_upload_status_body", _broken_body)

    post = _make_post()
    session = FakeSession()
    notifier = _RecordingNotifier()

    with pytest.raises(ComplianceError):
        await gate_mod.compliance_gate(
            session=session,
            post=post,
            description="開示なし",
            video_uri=str(post.video_uri),
            notifier=notifier,
        )
    # 違反通知が 1 件飛ぶ (FR-114: カテゴリ名 prefix [COMPLIANCE])。
    assert len(notifier.calls) == 1
    assert "[COMPLIANCE]" in notifier.calls[0]["message"]


def test_build_upload_status_body_default_has_flag() -> None:
    body = _build_upload_status_body(description="d", video_uri="memory://v")
    assert body["status"]["containsSyntheticMedia"] is True
