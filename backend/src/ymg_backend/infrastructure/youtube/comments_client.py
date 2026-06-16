"""YouTube コメント取得クライアント (ADR-0021 / FR-101, US3 T102)。

公開済み動画に付いたコメントを YouTube Data API ``commentThreads.list`` から取得し、
``comments`` テーブルへ ``youtube_comment_id`` (unique) で upsert する。重複コメントは
PG ``insert(...).on_conflict_do_update`` で冪等に取り込む (再実行しても二重登録しない)。

設計方針 (analytics の :mod:`ymg_backend.domain.analytics.client` と同方針):

- access token は :class:`YouTubeOAuth` から取得し、``Authorization: Bearer`` で **httpx** に
  載せる。google-api-python-client は使わず取得層を httpx に分離する (respx mock 可能)。
- HTTP / ネットワーク障害は ADR-0028 のエラー分類へ写像する:
  - 接続失敗・タイムアウト・5xx → :class:`TransientError` (exponential backoff で最大 3 試行)
  - 4xx (拒否) → :class:`RecoverableError` (該当ジョブのみスキップ)
- commit は呼び出し側 (オーケストレータ / scheduler) の責務。本クライアントは ``flush`` まで。
- ``sentiment`` / ``topic_tags`` はこの段では未設定 (後段の分析タスクが埋める)。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.infrastructure.db.models import Comment, Video

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth

# YouTube Data API v3 commentThreads エンドポイント (top-level comment 取得)。
# 実装側はこの定数を module 直下に export する (analytics client の URL 定数と同方針)。
YOUTUBE_COMMENT_THREADS_URL: Final[str] = "https://www.googleapis.com/youtube/v3/commentThreads"

# commentThreads.list の 1 リクエスト最大件数 (YouTube API 上限)。
_MAX_RESULTS_PER_PAGE: Final[int] = 100

# 取得する part (top-level comment の snippet が欲しい)。
_COMMENT_PART: Final[str] = "snippet"

# --- HTTP / リトライ設定 (ADR-0028) ------------------------------------------------
# TransientError は最大 3 回・exponential backoff (1s→2s→4s) でリトライ。
_MAX_ATTEMPTS: Final[int] = 3
_BACKOFF_BASE_SEC: Final[float] = 1.0
_DEFAULT_TIMEOUT_SEC: Final[float] = 30.0
_RETRYABLE_STATUS_MIN: Final[int] = 500


class CommentsClient:
    """``commentThreads.list`` 経由の YouTube コメント取得 + ``comments`` upsert (ADR-0021)。

    Args:
        oauth: access token を供給する :class:`YouTubeOAuth`。
        http: 注入する ``httpx.AsyncClient`` (テスト用)。省略時は内部生成し、その場合は
            所有権を本インスタンスが持つ (``aclose`` でクローズ)。

    本クライアントは async コンテキストマネージャとして利用でき、終了時に内部生成した
    client をクローズする (外部注入された client は呼び出し側の所有とみなしクローズしない)。
    """

    __slots__ = ("_http", "_oauth", "_owns_http")

    def __init__(self, oauth: YouTubeOAuth, *, http: httpx.AsyncClient | None = None) -> None:
        self._oauth: Final[YouTubeOAuth] = oauth
        self._owns_http: Final[bool] = http is None
        self._http: Final[httpx.AsyncClient] = (
            http if http is not None else httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SEC)
        )

    async def __aenter__(self) -> CommentsClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """内部生成した ``httpx.AsyncClient`` をクローズする (外部注入時は何もしない)。"""
        if self._owns_http:
            await self._http.aclose()

    async def fetch_and_upsert(
        self,
        *,
        session: AsyncSession,
        video_ids: list[str] | None = None,
        max_per_video: int = 100,
    ) -> int:
        """対象動画のコメントを取得し ``comments`` テーブルへ upsert する。

        Args:
            session: ``videos`` 読み出し / ``comments`` upsert 用 ``AsyncSession``
                (commit は呼び出し側)。
            video_ids: 対象動画 ID。``None`` の場合は ``videos`` テーブルの公開済み動画を対象。
            max_per_video: 1 動画あたり取得する top-level コメントの最大件数 (>=1)。

        Returns:
            upsert したコメント件数。

        Raises:
            ValueError: ``max_per_video`` が 1 未満の場合 (境界での入力検証)。
            RecoverableError: 認証/権限エラー・4xx 拒否の場合 (ADR-0028)。
            TransientError: 接続失敗・タイムアウト・5xx の場合 (リトライ後, ADR-0028)。
        """
        if max_per_video < 1:
            raise ValueError("max_per_video must be >= 1")

        targets = await self._resolve_target_video_ids(session, video_ids)
        if not targets:
            return 0

        access_token = await self._oauth.get_access_token(session=session)
        headers = {"Authorization": f"Bearer {access_token}"}

        upserted = 0
        for video_id in targets:
            comments = await self._fetch_comments_for_video(
                video_id=video_id, headers=headers, max_per_video=max_per_video
            )
            for comment in comments:
                await self._upsert_comment(session=session, comment=comment)
                upserted += 1

        if upserted:
            await session.flush()
        logger.info(
            "youtube コメント取得 upsert 完了 (videos={}, comments={})",
            len(targets),
            upserted,
        )
        return upserted

    async def _resolve_target_video_ids(
        self, session: AsyncSession, video_ids: list[str] | None
    ) -> list[str]:
        """対象動画 ID を解決する (``None`` なら公開済み動画を ``videos`` から取得)。"""
        if video_ids is not None:
            return list(video_ids)
        stmt = select(Video.youtube_video_id).where(Video.privacy_status == "public")
        rows = (await session.execute(stmt)).scalars().all()
        return [str(row) for row in rows]

    async def _fetch_comments_for_video(
        self,
        *,
        video_id: str,
        headers: Mapping[str, str],
        max_per_video: int,
    ) -> list[_ParsedComment]:
        """1 動画分の top-level コメントを ``commentThreads.list`` でページングして取得する。"""
        collected: list[_ParsedComment] = []
        page_token: str | None = None
        while len(collected) < max_per_video:
            remaining = max_per_video - len(collected)
            params: dict[str, str] = {
                "part": _COMMENT_PART,
                "videoId": video_id,
                "maxResults": str(min(remaining, _MAX_RESULTS_PER_PAGE)),
                "textFormat": "plainText",
                "order": "time",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = await self._get_json(
                YOUTUBE_COMMENT_THREADS_URL,
                params=params,
                headers=headers,
                context={"op": "commentThreads.list", "video_id": video_id},
            )
            collected.extend(_parse_comment_threads(payload, video_id=video_id))
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token
        return collected[:max_per_video]

    async def _upsert_comment(self, *, session: AsyncSession, comment: _ParsedComment) -> None:
        """``comments`` を ``youtube_comment_id`` 衝突時 upsert する (flush は呼び出し側)。"""
        stmt = (
            insert(Comment)
            .values(
                id=uuid.uuid4(),
                youtube_video_id=comment.youtube_video_id,
                youtube_comment_id=comment.youtube_comment_id,
                author=comment.author,
                text=comment.text,
                like_count=comment.like_count,
                published_at=comment.published_at,
            )
            .on_conflict_do_update(
                index_elements=[Comment.youtube_comment_id],
                set_={
                    "author": comment.author,
                    "text": comment.text,
                    "like_count": comment.like_count,
                    "published_at": comment.published_at,
                },
            )
        )
        await session.execute(stmt)

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """GET を exponential backoff 付きでリトライ実行し JSON を返す (ADR-0028)。

        ``TransientError`` (接続/タイムアウト/5xx) のみリトライし、最大 ``_MAX_ATTEMPTS`` 試行。
        ``RecoverableError`` (4xx / 契約違反) はリトライせず即時送出する。
        """
        last_error = TransientError(
            f"YouTube API への GET {url} が試行されませんでした",
            context=dict(context),
        )
        for attempt in range(_MAX_ATTEMPTS):
            attempt_context = {**context, "attempt": attempt + 1}
            try:
                response = await self._http.get(url, params=dict(params), headers=dict(headers))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = TransientError(
                    f"YouTube API への GET {url} が通信失敗しました: {exc}",
                    context=attempt_context,
                    original=exc,
                )
            else:
                try:
                    _raise_for_http_status(response, context=attempt_context)
                except TransientError as exc:
                    last_error = exc
                else:
                    return _parse_json_body(response, context=attempt_context)

            if attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_BASE_SEC * (2**attempt))

        raise last_error


class _ParsedComment:
    """``commentThreads.list`` から取り出した top-level コメント 1 件 (内部 DTO)。"""

    __slots__ = (
        "author",
        "like_count",
        "published_at",
        "text",
        "youtube_comment_id",
        "youtube_video_id",
    )

    def __init__(
        self,
        *,
        youtube_video_id: str,
        youtube_comment_id: str,
        author: str | None,
        text: str,
        like_count: int,
        published_at: datetime,
    ) -> None:
        self.youtube_video_id: Final[str] = youtube_video_id
        self.youtube_comment_id: Final[str] = youtube_comment_id
        self.author: Final[str | None] = author
        self.text: Final[str] = text
        self.like_count: Final[int] = like_count
        self.published_at: Final[datetime] = published_at


def _parse_comment_threads(payload: Mapping[str, Any], *, video_id: str) -> list[_ParsedComment]:
    """``commentThreads.list`` レスポンスから top-level コメントを解析する。

    解釈できない / 必須フィールド (id / published_at) を欠く item は黙ってスキップする
    (1 件の不正で取得全体を落とさない)。
    """
    items = payload.get("items")
    if not isinstance(items, Sequence):
        return []
    parsed: list[_ParsedComment] = []
    for item in items:
        comment = _parse_single_thread(item, video_id=video_id)
        if comment is not None:
            parsed.append(comment)
    return parsed


def _parse_single_thread(item: Any, *, video_id: str) -> _ParsedComment | None:
    """1 thread item から top-level comment の snippet を取り出す (不正は ``None``)。"""
    if not isinstance(item, Mapping):
        return None
    top_level = item.get("snippet", {})
    top_comment = top_level.get("topLevelComment") if isinstance(top_level, Mapping) else None
    if not isinstance(top_comment, Mapping):
        return None
    comment_id = top_comment.get("id")
    snippet = top_comment.get("snippet")
    if not isinstance(comment_id, str) or not comment_id or not isinstance(snippet, Mapping):
        return None
    published_at = _parse_rfc3339(snippet.get("publishedAt"))
    if published_at is None:
        return None
    return _ParsedComment(
        youtube_video_id=video_id,
        youtube_comment_id=comment_id,
        author=_as_optional_str(snippet.get("authorDisplayName")),
        text=str(snippet.get("textDisplay") or snippet.get("textOriginal") or ""),
        like_count=_as_int(snippet.get("likeCount")),
        published_at=published_at,
    )


def _parse_rfc3339(value: Any) -> datetime | None:
    """RFC3339 (``...Z``) 文字列を aware UTC ``datetime`` に変換する (不正は ``None``)。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_optional_str(value: Any) -> str | None:
    """文字列なら返し、それ以外は ``None`` (空文字も ``None`` 扱い)。"""
    if isinstance(value, str) and value:
        return value
    return None


def _as_int(value: Any) -> int:
    """整数として解釈可能なら ``int`` を返し、不正は 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _raise_for_http_status(response: httpx.Response, *, context: Mapping[str, Any]) -> None:
    """HTTP ステータスをエラー分類へ写像する (ADR-0028)。

    5xx は :class:`TransientError` (リトライ可)、それ以外の 4xx は API からの拒否として
    :class:`RecoverableError` に写像する。2xx は触れない。
    """
    status_code = response.status_code
    if status_code < 400:
        return
    detail = _extract_error_message(response)
    full_context = {**context, "status_code": status_code}
    if status_code >= _RETRYABLE_STATUS_MIN:
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
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return str(error["message"])
    return response.text[:200]


def _parse_json_body(response: httpx.Response, *, context: Mapping[str, Any]) -> dict[str, Any]:
    """成功レスポンス本文を JSON dict として解釈する (非 dict / 不正は contract 違反)。"""
    try:
        payload = response.json()
    except ValueError as exc:
        raise RecoverableError(
            f"YouTube API のレスポンスが JSON ではありません: {exc}",
            context=dict(context),
            original=exc,
        ) from exc
    if not isinstance(payload, dict):
        raise RecoverableError(
            "YouTube API のレスポンスが JSON オブジェクトではありません。",
            context=dict(context),
        )
    return payload
