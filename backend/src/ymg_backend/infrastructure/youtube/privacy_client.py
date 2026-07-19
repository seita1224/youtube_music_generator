"""YouTube 動画 privacy 更新クライアント (US4 T111, ADR-0031 / ADR-0028)。

公開済み動画の公開範囲 (``privacyStatus``) を ``videos.update`` で変更する。 panic-stop
(コンプラ緊急停止) と手動 ``PUT /youtube/videos/{video_id}/privacy`` が共用する薄い client。

設計方針 (``comments_client`` / ``uploader`` の確立パターンを踏襲):

- access token は :class:`YouTubeOAuth` から取得し ``Authorization: Bearer`` で **httpx** に
  載せる。 ``videos.insert`` は google-api-python-client (SDK) だが、 privacy 更新は取得層を
  httpx に分離して respx で mock 可能にする (``comments_client`` と同方針)。
- エンドポイント: ``PUT https://www.googleapis.com/youtube/v3/videos?part=status``、
  body ``{"id": video_id, "status": {"privacyStatus": privacy_status}}``。
- HTTP / ネットワーク障害は ADR-0028 のエラー分類へ写像する:
  - 接続失敗・タイムアウト・5xx → :class:`TransientError`
  - 4xx (拒否) → :class:`RecoverableError`
- commit は呼び出し側責務。 本 client は DB を触らず (token 取得経由の session のみ)、
  YouTube 側の privacy 更新だけを担う。 ``Video.privacy_status`` の DB 反映は service 側。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
from loguru import logger

from ymg_backend.domain.errors.errors import RecoverableError, TransientError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth

# YouTube Data API v3 videos エンドポイント (videos.update part=status)。
YOUTUBE_VIDEOS_URL: Final[str] = "https://www.googleapis.com/youtube/v3/videos"

# videos.update で更新する part (status のみ更新する)。
_UPDATE_PART: Final[str] = "status"

# リトライ対象とみなす HTTP ステータス下限 (5xx は一過性)。
_TRANSIENT_STATUS_MIN: Final[int] = 500

_DEFAULT_TIMEOUT_SEC: Final[float] = 30.0


class VideoPrivacyUpdater:
    """``videos.update(part=status)`` で動画の公開範囲を変更する client (ADR-0031)。

    Args:
        oauth: access token を供給する :class:`YouTubeOAuth`。
        http: 注入する ``httpx.AsyncClient`` (テスト用)。 省略時は内部生成し、 その場合は
            所有権を本インスタンスが持つ (``aclose`` でクローズ)。

    本 client は async コンテキストマネージャとして利用でき、 終了時に内部生成した
    client をクローズする (外部注入された client は呼び出し側の所有とみなしクローズしない)。
    """

    __slots__ = ("_http", "_oauth", "_owns_http")

    def __init__(self, oauth: YouTubeOAuth, *, http: httpx.AsyncClient | None = None) -> None:
        self._oauth: Final[YouTubeOAuth] = oauth
        self._owns_http: Final[bool] = http is None
        self._http: Final[httpx.AsyncClient] = (
            http if http is not None else httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SEC)
        )

    async def __aenter__(self) -> VideoPrivacyUpdater:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """内部生成した ``httpx.AsyncClient`` をクローズする (外部注入時は何もしない)。"""
        if self._owns_http:
            await self._http.aclose()

    async def set_privacy(
        self,
        *,
        session: AsyncSession,
        youtube_video_id: str,
        privacy_status: str,
    ) -> None:
        """動画の公開範囲を ``privacy_status`` に変更する (``videos.update part=status``)。

        Args:
            session: access token 取得 (``OAuthCredential`` 読み書き) 用 ``AsyncSession``
                (commit は呼び出し側)。
            youtube_video_id: 対象動画の YouTube 動画 ID。
            privacy_status: 設定する公開範囲 (``public`` / ``unlisted`` / ``private``)。

        Raises:
            RecoverableError: 認証/権限エラー・4xx 拒否の場合 (ADR-0028)。
            TransientError: 接続失敗・タイムアウト・5xx の場合 (ADR-0028)。
        """
        access_token = await self._oauth.get_access_token(session=session)
        headers = {"Authorization": f"Bearer {access_token}"}
        body: dict[str, Any] = {
            "id": youtube_video_id,
            "status": {"privacyStatus": privacy_status},
        }
        context: dict[str, Any] = {
            "op": "videos.update",
            "youtube_video_id": youtube_video_id,
            "privacy_status": privacy_status,
        }
        try:
            response = await self._http.put(
                YOUTUBE_VIDEOS_URL,
                params={"part": _UPDATE_PART},
                headers=headers,
                json=body,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(
                f"YouTube API への videos.update が通信失敗しました: {exc}",
                context=context,
                original=exc,
            ) from exc

        _raise_for_http_status(response, context=context)
        logger.bind(component="youtube.privacy").info(
            "youtube 動画の公開範囲を更新しました (video_id={}, privacy_status={})",
            youtube_video_id,
            privacy_status,
        )


def _raise_for_http_status(response: httpx.Response, *, context: dict[str, Any]) -> None:
    """HTTP ステータスをエラー分類へ写像する (ADR-0028)。

    5xx は :class:`TransientError` (リトライ可)、 それ以外の 4xx は API からの拒否として
    :class:`RecoverableError` に写像する。 2xx は触れない (``comments_client`` と同形)。
    """
    status_code = response.status_code
    if status_code < 400:
        return
    detail = _extract_error_message(response)
    full_context = {**context, "status_code": status_code}
    if status_code >= _TRANSIENT_STATUS_MIN:
        raise TransientError(
            f"YouTube API が {status_code} を返しました: {detail}",
            context=full_context,
        )
    raise RecoverableError(
        f"YouTube API がリクエストを拒否しました ({status_code}): {detail}",
        context=full_context,
    )


def _extract_error_message(response: httpx.Response) -> str:
    """エラーレスポンス本文から message を最善努力で取り出す (失敗は生テキスト先頭)。"""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])
    return response.text[:200]


__all__ = ["YOUTUBE_VIDEOS_URL", "VideoPrivacyUpdater"]
