"""YouTube ``videos.insert`` アップローダ (ADR-0020 / FR-006/007/100).

``Post`` の最終成果物 (動画 / サムネ / タイトル / 説明) を YouTube へ投稿する。
``status.containsSyntheticMedia=true`` を **必ず** body に設定し (ADR-0020)、 投入前に
:func:`assert_contains_synthetic_media` で防御的に検証してから ``videos.insert`` を実行する。

設計方針:

- google-api-python-client の ``build("youtube","v3",credentials=...)`` でリソースを生成し、
  ``videos().insert(...).execute()`` で resumable upload を行う。 SDK 呼び出しは
  ``ServiceFactory`` 越しに注入でき、 テストでは実 API を打たない mock を渡せる。
- access token は :class:`YouTubeOAuth` から取得し、 ``google.oauth2.credentials.Credentials``
  にラップして SDK に渡す (期限前 refresh は OAuth 側で完結)。
- 動画バイトは :class:`StorageAdapter` 経由で読み出し、 ``MediaIoBaseUpload`` で送る
  (storage が ``file://`` 以外でも動くよう、 ローカルパス前提にしない)。
- ブロッキングな SDK 呼び出しは ``asyncio.to_thread`` でオフロードし async 契約を守る。
- 失敗は ADR-0028 に写像: 一過性 (5xx / quota 一時) は ``TransientError``、 認証/権限
  (401/403) は ``RecoverableError``、 設定不備で続行不能なら ``FatalError``。
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from loguru import logger

from ymg_backend.domain.compliance.validators import assert_contains_synthetic_media
from ymg_backend.domain.errors.errors import FatalError, RecoverableError, TransientError
from ymg_backend.infrastructure.youtube.oauth import (
    GOOGLE_TOKEN_URI,
    YOUTUBE_UPLOAD_SCOPE,
    YouTubeOAuth,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.core.config import Settings
    from ymg_backend.infrastructure.db.models import Post
    from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

# videos.insert に指定する part / 既定メタ (ADR-0020 body 構造)。
_INSERT_PART: Final[str] = "snippet,status"
_MUSIC_CATEGORY_ID: Final[str] = "10"  # YouTube category "Music"
_DEFAULT_PRIVACY: Final[str] = "public"
_VIDEO_MIMETYPE: Final[str] = "video/mp4"
_THUMBNAIL_MIMETYPE: Final[str] = "image/jpeg"
# resumable upload のチャンクサイズ (-1 = 一括, 大容量はストリーム)。
_UPLOAD_CHUNKSIZE: Final[int] = 16 * 1024 * 1024

# リトライ対象とみなす HTTP ステータス (5xx は一過性)。
_TRANSIENT_STATUS_MIN: Final[int] = 500
_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})


class ServiceFactory(Protocol):
    """``credentials`` から YouTube API リソースを生成する処理の最小インターフェース。

    既定実装は ``googleapiclient.discovery.build`` を呼ぶ。 テストでは
    ``videos()`` / ``thumbnails()`` を持つ mock リソースを返す実装を注入できる。
    """

    # 位置引数で受け渡す (引数名は実装側で自由に命名できる)。
    def __call__(self, credentials: Credentials, /) -> Any: ...


def _default_service_factory(credentials: Credentials) -> Any:
    """google-api-python-client で YouTube Data API v3 リソースを生成する。"""
    # 遅延 import: discovery doc 読み込みを伴うため、 実投稿時のみロードする。
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def _map_http_error(exc: HttpError, *, context: dict[str, Any]) -> Exception:
    """``HttpError`` を ADR-0028 のエラー分類へ写像する。"""
    status_code = getattr(exc.resp, "status", None)
    full_context = {**context, "status_code": status_code}
    if isinstance(status_code, int) and status_code in _AUTH_STATUSES:
        return RecoverableError(
            "YouTube API が認証/権限エラーを返しました "
            "(トークン失効・スコープ不足の可能性)。 再認証が必要かもしれません。",
            context=full_context,
            original=exc,
        )
    if isinstance(status_code, int) and status_code >= _TRANSIENT_STATUS_MIN:
        return TransientError(
            f"YouTube API が {status_code} を返しました (一過性)。",
            context=full_context,
            original=exc,
        )
    return RecoverableError(
        f"YouTube API がリクエストを拒否しました ({status_code})。",
        context=full_context,
        original=exc,
    )


class YouTubeUploader:
    """``videos.insert`` による YouTube 投稿 (ADR-0020)。

    Args:
        oauth: access token を供給する :class:`YouTubeOAuth`。
        storage: 動画 / サムネバイトを読み出す :class:`StorageAdapter`。
        settings: ``youtube_client_id`` / ``youtube_client_secret`` 等の設定。
        service_factory: YouTube API リソース生成処理 (テスト注入用)。 省略時は
            google-api-python-client の ``build`` を使う。
    """

    __slots__ = ("_oauth", "_service_factory", "_settings", "_storage")

    def __init__(
        self,
        oauth: YouTubeOAuth,
        storage: StorageAdapter,
        settings: Settings,
        *,
        service_factory: ServiceFactory | None = None,
    ) -> None:
        self._oauth: Final[YouTubeOAuth] = oauth
        self._storage: Final[StorageAdapter] = storage
        self._settings: Final[Settings] = settings
        self._service_factory: Final[ServiceFactory] = (
            service_factory if service_factory is not None else _default_service_factory
        )

    async def upload(self, *, session: AsyncSession, post: Post) -> str:
        """``Post`` を YouTube へ投稿し ``youtube_video_id`` を返す。

        ``Post.video_uri`` / ``thumbnail_uri`` / ``final_title`` / ``final_description`` /
        ``scheduled_at`` を入力に body を組み立て、 ``containsSyntheticMedia=true`` を
        防御検証してから ``videos.insert`` を実行する。 成功時は ``post.youtube_video_id`` /
        ``post.posted_at`` を更新し flush する (commit は呼び出し側)。

        Args:
            session: ``OAuthCredential`` 読み書き / ``Post`` 更新用 ``AsyncSession``。
            post: 投稿対象。 ``video_uri`` / ``final_title`` / ``final_description`` 必須。

        Returns:
            投稿された動画の ``youtube_video_id``。

        Raises:
            FatalError: 必須フィールド (動画 URI / タイトル / 説明) が欠落している場合。
            ComplianceError: ``containsSyntheticMedia=true`` が body に無い場合 (防御検証)。
            RecoverableError: 認証/権限エラー・4xx 拒否の場合。
            TransientError: 5xx (一過性) の場合。
        """
        video_ref = str(post.id)
        self._require_fields(post, video_ref=video_ref)
        access_token = await self._oauth.get_access_token(session=session)
        body = self._build_body(post)
        # ガード層 (compliance_gate) とは別に、 アップローダ自身でもフラグを防御検証する
        # (ADR-0020「単体テストでも明示的に網羅」)。
        assert_contains_synthetic_media(body, video_ref=video_ref)

        video_bytes = self._storage.read_bytes(str(post.video_uri))
        credentials = self._wrap_credentials(access_token)
        service = self._service_factory(credentials)

        video_id = await asyncio.to_thread(
            self._insert_video,
            service=service,
            body=body,
            video_bytes=video_bytes,
            video_ref=video_ref,
        )

        await self._set_thumbnail_if_present(
            service=service, video_id=video_id, post=post, video_ref=video_ref
        )

        post.youtube_video_id = video_id
        post.posted_at = datetime.now(UTC)
        await session.flush()
        logger.info("youtube 投稿成功 (post_id={}, video_id={})", video_ref, video_id)
        return video_id

    @staticmethod
    def _require_fields(post: Post, *, video_ref: str) -> None:
        """投稿に必須なフィールドが揃っているか検証する (欠落は fatal)。"""
        missing = [
            name
            for name, value in (
                ("video_uri", post.video_uri),
                ("final_title", post.final_title),
                ("final_description", post.final_description),
            )
            if not value
        ]
        if missing:
            raise FatalError(
                f"YouTube 投稿に必要なフィールドが欠落しています: {', '.join(missing)}",
                context={"post_id": video_ref, "missing": missing},
            )

    def _build_body(self, post: Post) -> dict[str, Any]:
        """``videos.insert`` の body を構築する (合成メディアフラグ必須, ADR-0020)。"""
        status: dict[str, Any] = {
            "privacyStatus": _DEFAULT_PRIVACY,
            "containsSyntheticMedia": True,  # AI 開示フラグ (必須, ADR-0020)
            "selfDeclaredMadeForKids": False,
        }
        # 予約投稿時刻があれば privacyStatus=private + publishAt で予約投稿にする。
        if post.scheduled_at is not None:
            status["privacyStatus"] = "private"
            status["publishAt"] = _to_rfc3339(post.scheduled_at)
        return {
            "snippet": {
                "title": post.final_title,
                "description": post.final_description,
                "categoryId": _MUSIC_CATEGORY_ID,
            },
            "status": status,
        }

    def _wrap_credentials(self, access_token: str) -> Credentials:
        """平文 access token を SDK 用 ``Credentials`` にラップする。

        OAuth 側で refresh 済みのため token のみで投稿できるが、 SDK が万一 refresh を
        試みても破綻しないよう refresh 情報も併せて渡す。
        """
        return Credentials(  # type: ignore[no-untyped-call]
            token=access_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=self._settings.youtube_client_id,
            client_secret=self._settings.youtube_client_secret.get_secret_value(),
            scopes=[YOUTUBE_UPLOAD_SCOPE],
        )

    def _insert_video(
        self,
        *,
        service: Any,
        body: dict[str, Any],
        video_bytes: bytes,
        video_ref: str,
    ) -> str:
        """``videos().insert(...).execute()`` を実行し video_id を返す (同期)。"""
        media = MediaIoBaseUpload(
            io.BytesIO(video_bytes),
            mimetype=_VIDEO_MIMETYPE,
            chunksize=_UPLOAD_CHUNKSIZE,
            resumable=True,
        )
        try:
            request = service.videos().insert(part=_INSERT_PART, body=body, media_body=media)
            response = request.execute()
        except HttpError as exc:
            raise _map_http_error(
                exc, context={"post_id": video_ref, "op": "videos.insert"}
            ) from exc
        video_id = _extract_video_id(response)
        if not video_id:
            raise RecoverableError(
                "YouTube videos.insert のレスポンスに video id が含まれていません。",
                context={"post_id": video_ref},
            )
        return video_id

    async def _set_thumbnail_if_present(
        self,
        *,
        service: Any,
        video_id: str,
        post: Post,
        video_ref: str,
    ) -> None:
        """サムネ URI があれば ``thumbnails().set`` で設定する (失敗は quality 扱いで握る)。"""
        if not post.thumbnail_uri:
            return
        thumbnail_bytes = self._storage.read_bytes(str(post.thumbnail_uri))
        try:
            await asyncio.to_thread(
                self._set_thumbnail,
                service=service,
                video_id=video_id,
                thumbnail_bytes=thumbnail_bytes,
            )
        except HttpError as exc:
            # サムネ設定失敗は投稿自体を巻き戻さない (動画は公開済み)。 警告ログのみ。
            logger.warning(
                "youtube サムネ設定に失敗しました (post_id={}, video_id={}): {}",
                video_ref,
                video_id,
                exc,
            )

    @staticmethod
    def _set_thumbnail(*, service: Any, video_id: str, thumbnail_bytes: bytes) -> None:
        """``thumbnails().set(...).execute()`` を実行する (同期)。"""
        media = MediaIoBaseUpload(
            io.BytesIO(thumbnail_bytes), mimetype=_THUMBNAIL_MIMETYPE, resumable=False
        )
        service.thumbnails().set(videoId=video_id, media_body=media).execute()


def _to_rfc3339(value: datetime) -> str:
    """datetime を YouTube API 用の RFC3339 (UTC, ``Z``) 文字列へ整形する。"""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _extract_video_id(response: object) -> str | None:
    """``videos.insert`` レスポンスから video id を取り出す。"""
    if isinstance(response, dict):
        video_id = response.get("id")
        if isinstance(video_id, str) and video_id:
            return video_id
    return None
