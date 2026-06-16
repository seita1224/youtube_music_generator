"""Unit テスト: VideoPrivacyUpdater の wire 形式 + エラー写像 (T111 / US4)。

対象は US4 内部契約 ``ymg_backend.infrastructure.youtube.privacy_client`` :

    YOUTUBE_VIDEOS_URL: Final[str] = "https://www.googleapis.com/youtube/v3/videos"

    class VideoPrivacyUpdater:
        def __init__(self, oauth: YouTubeOAuth, *, http: httpx.AsyncClient | None = None): ...
        async def set_privacy(self, *, session, youtube_video_id, privacy_status) -> None: ...

panic-stop / 手動 ``PUT /youtube/videos/{video_id}/privacy`` の共用 client。
``comments_client`` / ``uploader`` が確立した「取得層を httpx に分離、 Bearer で打つ」
パターンを踏襲する (respx mock 容易)。

検証観点:
1. wire 形式: ``PUT .../v3/videos?part=status`` に
   body ``{"id":..., "status":{"privacyStatus":...}}`` と Bearer ヘッダを載せる。
2. 4xx は RecoverableError、 5xx は TransientError に写像する (ADR-0028)。
3. 注入 http クライアントを尊重する。

OAuth は ``get_access_token`` を持つ duck-typed stub。 実 YouTube / 実 refresh は打たない。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.infrastructure.youtube.privacy_client import (
    YOUTUBE_VIDEOS_URL,
    VideoPrivacyUpdater,
)

pytestmark = pytest.mark.unit

_FAKE_TOKEN = "privacy-token"


class _StubOAuth:
    """``YouTubeOAuth`` の代替 (固定トークンを返す)。

    ``YouTubeOAuth`` は ``__slots__`` で ``get_access_token`` を上書きできないため、
    必要な I/F (``get_access_token``) のみを持つ stub を渡す。
    """

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


class _SpySession:
    """``set_privacy`` が触れない最小 session スパイ (token 取得経由のみ使用)。"""

    async def flush(self) -> None:  # pragma: no cover - 呼ばれない想定
        return None


def _build_updater(*, http: httpx.AsyncClient | None = None) -> VideoPrivacyUpdater:
    return VideoPrivacyUpdater(_StubOAuth(), http=http)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# wire 形式
# ---------------------------------------------------------------------------


@respx.mock
async def test_set_privacy_wire_format() -> None:
    """``PUT .../v3/videos?part=status`` に id / privacyStatus / Bearer を載せる。"""
    route = respx.put(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(
            200, json={"id": "vid_X", "status": {"privacyStatus": "private"}}
        )
    )

    updater = _build_updater()
    await updater.set_privacy(
        session=_SpySession(),  # type: ignore[arg-type]
        youtube_video_id="vid_X",
        privacy_status="private",
    )

    assert route.call_count == 1
    req = route.calls.last.request
    assert req.method == "PUT"
    # part=status を query に載せる
    assert req.url.params.get("part") == "status"
    # Bearer ヘッダ
    assert req.headers.get("Authorization") == f"Bearer {_FAKE_TOKEN}"
    # body は {"id":..., "status":{"privacyStatus":...}}
    payload = json.loads(req.content.decode())
    assert payload["id"] == "vid_X"
    assert payload["status"]["privacyStatus"] == "private"


@respx.mock
async def test_set_privacy_accepts_public_and_unlisted() -> None:
    """settable enum (public / unlisted / private) を body にそのまま載せる。"""
    route = respx.put(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"id": "v", "status": {"privacyStatus": "unlisted"}})
    )
    updater = _build_updater()
    await updater.set_privacy(
        session=_SpySession(),  # type: ignore[arg-type]
        youtube_video_id="vid_U",
        privacy_status="unlisted",
    )
    payload = json.loads(route.calls.last.request.content.decode())
    assert payload["status"]["privacyStatus"] == "unlisted"


# ---------------------------------------------------------------------------
# エラー写像 (ADR-0028)
# ---------------------------------------------------------------------------


@respx.mock
async def test_4xx_maps_to_recoverable() -> None:
    """4xx は RecoverableError に写像する (uploader / comments_client と同方針)。"""
    respx.put(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(403, json={"error": {"message": "forbidden"}})
    )
    updater = _build_updater()
    with pytest.raises(RecoverableError):
        await updater.set_privacy(
            session=_SpySession(),  # type: ignore[arg-type]
            youtube_video_id="vid_403",
            privacy_status="private",
        )


@respx.mock
async def test_5xx_maps_to_transient() -> None:
    """5xx は TransientError に写像する (ADR-0028)。"""
    respx.put(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(503, json={"error": {"message": "unavailable"}})
    )
    updater = _build_updater()
    with pytest.raises(TransientError):
        await updater.set_privacy(
            session=_SpySession(),  # type: ignore[arg-type]
            youtube_video_id="vid_503",
            privacy_status="private",
        )


# ---------------------------------------------------------------------------
# 注入 http クライアント
# ---------------------------------------------------------------------------


@respx.mock
async def test_uses_injected_http_client() -> None:
    """http= で渡した AsyncClient を使う (取得層が httpx 分離されている保証)。"""
    route = respx.put(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"id": "vid_INJ"})
    )
    async with httpx.AsyncClient() as http:
        updater = _build_updater(http=http)
        await updater.set_privacy(
            session=_SpySession(),  # type: ignore[arg-type]
            youtube_video_id="vid_INJ",
            privacy_status="private",
        )
    assert route.called


def test_url_constant_sane() -> None:
    assert YOUTUBE_VIDEOS_URL.startswith("https://")
    assert "videos" in YOUTUBE_VIDEOS_URL
