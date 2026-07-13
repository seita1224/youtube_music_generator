"""``/posts`` 業務ルータ (contracts/backend-api.yaml ``/posts`` 系)。

endpoint を提供する (すべて Basic 認証必須。 配線は ``main.py`` の
``_build_protected_router`` 配下で後段が ``include_router`` する)。

- ``GET /posts``: ``plan_id`` / ``status`` / ``genre`` / ``from_date`` / ``to_date`` で投稿一覧を絞り込む。
- ``GET /posts/{post_id}``: 単一投稿を返す (不在は 404)。
- ``POST /posts/{post_id}/retry``: **失敗した** 投稿パイプラインを再実行する (ADR-0035 §3)。
- ``GET /posts/{post_id}/tracks``: AudioTrack メタ一覧 (``audio_uri`` は露出せない)。
- ``GET /posts/{post_id}/tracks/{position}/audio``: WAV を Basic 認証付き StreamingResponse で返す。
- ``GET /posts/{post_id}/tracks/{position}/download``: 同じバイト源を attachment で返す。

設計方針:

- レスポンスは ORM ``Post`` / ``AudioTrack`` を contracts スキーマへ写像した Pydantic で返す。
  ``AudioTrack.audio_uri`` は API レスポンスに含めず、再生/DL は storage ストリーム経由。
- 音声配信は dryrun 動画配信と同方式: ``StorageAdapter.open`` のチャンク読み +
  ``StreamingResponse``。 全読み込み (``read_bytes``) はしない。
- 本ルータは「呼び出し側」なので状態変更後に ``session.commit()`` する
  (service 層は flush まで、 という US1 規約の境界はここで閉じる)。
- 再実行の実体 (daily_cycle オーケストレータの組み立て) は本タスクのスコープ外
  (後段の専任が配線する)。 ここでは :class:`PostRetryLauncher` プロトコルへの委譲点
  だけを定義し、 既定実装は FastAPI の ``BackgroundTasks`` に投入する薄い launcher を
  返す。 launcher は ``app.dependency_overrides[get_retry_launcher]`` で差し替え可能。
- 失敗は 5 区分 (transient/recoverable/fatal/compliance/quality) の既存例外型を尊重し、
  ルータ層では業務的に妥当な HTTP ステータス (404 / 409) のみ ``HTTPException`` で返す。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import IO, Annotated, Any, Final, Literal, Protocol

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.logging import bind_context
from ymg_backend.core.security import BasicAuthUser
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import AudioTrack, Post
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

router: Final = APIRouter(prefix="/posts", tags=["posts"])

# contracts の post_status enum (backend-api.yaml ``/posts`` query / Post.status)。
PostStatus = Literal[
    "pending",
    "generating",
    "music_generated",
    "generated",
    "posting",
    "posted",
    "failed",
]

# contracts の acoustid_status enum (AudioTrack.acoustid_status)。
AcoustidStatus = Literal["not_checked", "clear", "hit", "api_error"]

# retry を受け付ける状態。 ADR-0035 §3 に従い「失敗した投稿の再実行」のみ許可する。
_RETRYABLE_STATUSES: Final[frozenset[str]] = frozenset({"failed"})

# 一覧のデフォルト件数上限 (contracts に limit は無いが、 暴走防止の安全弁)。
_LIST_LIMIT: Final[int] = 100

# from_date/to_date の終端境界計算用 (to_date を含めるため翌日 0:00 を排他上界にする)。
_ONE_DAY: Final[timedelta] = timedelta(days=1)

# WAV ストリームのチャンクサイズ (dryrun 動画配信と同値: 1 MiB)。
_AUDIO_CHUNK_SIZE: Final[int] = 1024 * 1024

_AUDIO_MEDIA_TYPE: Final[str] = "audio/wav"

# TrackPositionPath: AudioTrack.position (0..5)。
TrackPosition = Annotated[int, Path(ge=0, le=5, description="AudioTrack.position (0..5)")]


class PostResponse(BaseModel):
    """contracts ``Post`` スキーマ (backend-api.yaml components.schemas.Post)。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plan_id: uuid.UUID
    position: int
    genre: str
    payload: dict[str, Any]
    status: PostStatus
    final_title: str | None = None
    final_description: str | None = None
    thumbnail_uri: str | None = None
    video_uri: str | None = None
    youtube_video_id: str | None = None
    scheduled_at: datetime | None = None
    posted_at: datetime | None = None
    retention_24h: Decimal | None = None
    views_24h: int | None = None
    error_category: str | None = None
    error_message: str | None = None
    created_at: datetime


class PostListResponse(BaseModel):
    """``GET /posts`` のレスポンス (contracts: ``{ items: Post[] }``)。"""

    items: list[PostResponse]


class AudioTrackResponse(BaseModel):
    """contracts ``AudioTrack`` スキーマ (``audio_uri`` は露出せない)。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    position: int = Field(ge=0, le=5)
    duration_sec: int
    bpm: int | None = None
    music_key: str | None = None
    subtheme: str | None = None
    acoustid_status: AcoustidStatus
    regenerated_count: int = 0
    generated_at: datetime


class AudioTrackListResponse(BaseModel):
    """``GET /posts/{post_id}/tracks`` のレスポンス (contracts: ``{ items: AudioTrack[] }``)。"""

    model_config = ConfigDict(extra="forbid")

    items: list[AudioTrackResponse]


class RetryAccepted(BaseModel):
    """``POST /posts/{post_id}/retry`` の 202 ボディ (contracts は本文未定義のため最小)。"""

    post_id: uuid.UUID
    status: PostStatus
    message: str


class PostRetryLauncher(Protocol):
    """失敗 post の再実行を起動する委譲点 (daily_cycle 単一 post 経路の再起動)。

    実体 (オーケストレータの組み立て + 単一 post 実行) は後段が配線する。 ここでは
    「``post_id`` を受けて再実行を非同期に起動する」最小 I/F のみを固定し、 テスト・
    後段の差し替えを ``app.dependency_overrides[get_retry_launcher]`` で可能にする。
    """

    def __call__(self, *, post_id: uuid.UUID, background: BackgroundTasks) -> None: ...


def _noop_retry_launcher(*, post_id: uuid.UUID, background: BackgroundTasks) -> None:
    """既定 launcher: 再実行の起動点をログに記録するだけの no-op。

    daily_cycle オーケストレータの配線は本タスクのスコープ外のため、 既定では実際の
    パイプライン起動はしない。 post は ``pending`` に戻り監査ログが残るので、 後段の
    scheduler / runner が ``pending`` を拾って再処理できる。 後段はこの依存を差し替えて
    オーケストレータの単一 post 経路を ``background`` に投入する。
    """
    bind_context(step="posts.retry", job_id=str(post_id)).info(
        "post retry enqueued (launcher not wired; post reset to pending)",
        post_id=str(post_id),
    )


def get_retry_launcher() -> PostRetryLauncher:
    """retry launcher を解決する依存性 (後段が override で実体を注入する)。"""
    return _noop_retry_launcher


@lru_cache(maxsize=1)
def _build_default_storage(settings: Settings) -> StorageAdapter:
    """settings から既定の :class:`StorageAdapter` を構築する (プロセス単位 1 個)。"""
    return StorageAdapter(settings.storage_base_uri)


def get_storage_adapter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> StorageAdapter:
    """音声配信に使う :class:`StorageAdapter` を解決する依存性 (override 可)。"""
    return _build_default_storage(settings)


def _normalize_genre(genre: str) -> str:
    """ジャンル絞り込み値を正規化する (前後空白除去)。 空文字は呼び出し側で除外済み。"""
    return genre.strip()


def _day_start_utc(day: date) -> datetime:
    """日付の 0:00 を UTC aware の :class:`datetime` にする。

    ``Post.created_at`` は ``TIMESTAMP(timezone=True)`` なので、 比較境界も timezone
    aware (UTC) で組み、 DB セッションのタイムゾーン設定に依存しないようにする。
    """
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


async def _load_post(session: AsyncSession, post_id: uuid.UUID) -> Post:
    """``post_id`` の ``Post`` を取得する (不在は 404)。"""
    post = await session.get(Post, post_id)
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"post {post_id} not found",
        )
    return post


async def _load_track(
    session: AsyncSession,
    *,
    post_id: uuid.UUID,
    position: int,
) -> AudioTrack:
    """``post_id`` + ``position`` の ``AudioTrack`` を取得する (不在は 404)。"""
    stmt = select(AudioTrack).where(
        AudioTrack.post_id == post_id,
        AudioTrack.position == position,
    )
    track = (await session.execute(stmt)).scalar_one_or_none()
    if track is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"track position {position} for post {post_id} not found",
        )
    return track


def _ensure_audio_uri_contained(storage: StorageAdapter, audio_uri: str) -> None:
    """``audio_uri`` が storage base 配下であることを保証する (逸脱は 404)。

    DB 改ざんや不正 worker 応答で絶対 URI が差し込まれても、 設定外の任意ファイルを
    読ませない。 詳細パスはレスポンスに出さない。
    """
    if not storage.is_under_base(audio_uri):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="audio object is missing or not readable",
        )


async def _stream_file(storage: StorageAdapter, uri: str) -> AsyncIterator[bytes]:
    """ファイルを固定チャンクで読み出す async generator (dryrun 動画配信と同方式)。

    ``StorageAdapter.open`` の同期ハンドルを ``with`` で確実にクローズしつつ、 1MiB ずつ
    yield する。 全読み込み (``read_bytes``) を避けてメモリ常駐を抑える。
    """
    handle: IO[Any]
    with storage.open(uri, "rb") as handle:
        for chunk in _iter_chunks(handle):
            yield chunk


def _iter_chunks(handle: IO[Any]) -> Iterator[bytes]:
    """ファイルハンドルから ``_AUDIO_CHUNK_SIZE`` 単位で読み出す同期イテレータ。"""
    while True:
        chunk = handle.read(_AUDIO_CHUNK_SIZE)
        if not chunk:
            break
        yield chunk


def _audio_streaming_response(
    storage: StorageAdapter,
    *,
    audio_uri: str,
    position: int,
    as_attachment: bool,
) -> StreamingResponse:
    """WAV を StreamingResponse で返す (再生 / ダウンロード共用)。

    オブジェクト不在は呼び出し側で 404 済み。 ``as_attachment=True`` のとき
    ``Content-Disposition: attachment; filename="track-{position}.wav"`` を付与する。
    """
    headers: dict[str, str] = {}
    if as_attachment:
        headers["Content-Disposition"] = f'attachment; filename="track-{position}.wav"'
    return StreamingResponse(
        _stream_file(storage, audio_uri),
        media_type=_AUDIO_MEDIA_TYPE,
        headers=headers,
    )


@router.get("", response_model=PostListResponse, summary="List posts")
async def list_posts(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    plan_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[PostStatus | None, Query(alias="status")] = None,
    genre: Annotated[str | None, Query()] = None,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
) -> PostListResponse:
    """投稿一覧を ``plan_id`` / ``status`` / ``genre`` / ``from_date`` / ``to_date`` で絞り込む。

    ``plan_id`` は Plan 詳細の音声一覧用。 ``from_date`` / ``to_date`` は ``created_at`` の
    日付境界に対する閉区間で判定する (``from_date`` 当日 0:00 以降、 ``to_date`` の翌日
    0:00 未満)。 新しい順で返す。
    """
    del user  # 認証のみ目的 (ユーザー名は本 endpoint では未使用)。
    stmt = select(Post)
    if plan_id is not None:
        stmt = stmt.where(Post.plan_id == plan_id)
    if status_filter is not None:
        stmt = stmt.where(Post.status == status_filter)
    if genre is not None and (normalized := _normalize_genre(genre)):
        stmt = stmt.where(Post.genre == normalized)
    if from_date is not None:
        stmt = stmt.where(Post.created_at >= _day_start_utc(from_date))
    if to_date is not None:
        # 終端は to_date を含めたいので翌日 0:00 未満 (排他上界) で表現する。
        stmt = stmt.where(Post.created_at < _day_start_utc(to_date) + _ONE_DAY)
    stmt = stmt.order_by(Post.created_at.desc()).limit(_LIST_LIMIT)

    rows = (await session.execute(stmt)).scalars().all()
    return PostListResponse(items=[PostResponse.model_validate(row) for row in rows])


@router.get("/{post_id}", response_model=PostResponse, summary="Get a single post")
async def get_post(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    post_id: uuid.UUID,
) -> PostResponse:
    """単一投稿を返す (不在は 404)。"""
    del user
    post = await _load_post(session, post_id)
    return PostResponse.model_validate(post)


@router.post(
    "/{post_id}/retry",
    response_model=RetryAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry a failed post pipeline",
)
async def retry_post(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    background: BackgroundTasks,
    post_id: uuid.UUID,
    launcher: Annotated[PostRetryLauncher, Depends(get_retry_launcher)],
) -> RetryAccepted:
    """失敗した投稿パイプラインを再実行する (ADR-0035 §3, daily_cycle 単一 post 経路)。

    再実行可能なのは ``failed`` 状態の投稿のみ (それ以外は ``409 Conflict``)。 post を
    ``pending`` に戻してエラー情報をクリアし、 監査ログ (``action="post_retry_requested"``)
    を残してから launcher へ再実行を委譲し、 ``202 Accepted`` を返す。 実際の再生成は
    バックグラウンドで進むため、 本 endpoint は結果を待たない。
    """
    log = bind_context(step="posts.retry", job_id=str(post_id))
    post = await _load_post(session, post_id)

    if post.status not in _RETRYABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"post {post_id} is '{post.status}'; only failed posts can be retried",
        )

    previous_category = post.error_category
    previous_message = post.error_message
    post.status = "pending"
    post.error_category = None
    post.error_message = None

    await write_audit_log(
        session,
        action="post_retry_requested",
        actor=user,
        target_type="post",
        target_id=str(post_id),
        payload={
            "actor": user,
            "previous_error_category": previous_category,
            "previous_error_message": previous_message,
        },
    )
    # 本ルータは「呼び出し側」なので状態変更を確定する (service 層は flush まで)。
    await session.commit()

    launcher(post_id=post_id, background=background)
    log.info("post retry accepted", post_id=str(post_id), actor=user)

    return RetryAccepted(
        post_id=post_id,
        status="pending",
        message="retry queued",
    )


@router.get(
    "/{post_id}/tracks",
    response_model=AudioTrackListResponse,
    summary="List audio tracks for a post",
    responses={404: {"description": "Post が存在しない"}},
)
async def list_post_tracks(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    post_id: uuid.UUID,
) -> AudioTrackListResponse:
    """AudioTrack メタを position 昇順で返す (``audio_uri`` は露出せない)。

    Post 不在は 404。 トラック未生成でも空 ``items`` を 200 で返す。
    """
    del user
    await _load_post(session, post_id)
    stmt = (
        select(AudioTrack).where(AudioTrack.post_id == post_id).order_by(AudioTrack.position.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return AudioTrackListResponse(items=[AudioTrackResponse.model_validate(row) for row in rows])


@router.get(
    "/{post_id}/tracks/{position}/audio",
    summary="Stream track WAV for in-browser playback",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {_AUDIO_MEDIA_TYPE: {}},
            "description": "WAV byte stream (chunked).",
        },
        404: {"description": "Post / track / オブジェクトが存在しない"},
    },
)
async def stream_post_track_audio(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageAdapter, Depends(get_storage_adapter)],
    post_id: uuid.UUID,
    position: TrackPosition,
) -> StreamingResponse:
    """StorageAdapter 経由で WAV を StreamingResponse で返す (Basic 認証必須)。

    ``audio_uri`` はレスポンスに含めない。 Post / track / オブジェクト不在は 404。
    """
    del user
    await _load_post(session, post_id)
    track = await _load_track(session, post_id=post_id, position=position)
    _ensure_audio_uri_contained(storage, track.audio_uri)
    if not storage.exists(track.audio_uri):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"audio object for post {post_id} position {position} is missing",
        )
    return _audio_streaming_response(
        storage,
        audio_uri=track.audio_uri,
        position=position,
        as_attachment=False,
    )


@router.get(
    "/{post_id}/tracks/{position}/download",
    summary="Download track WAV (Content-Disposition attachment)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {_AUDIO_MEDIA_TYPE: {}},
            "description": "WAV download with Content-Disposition attachment.",
        },
        404: {"description": "Post / track / オブジェクトが存在しない"},
    },
)
async def download_post_track_audio(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageAdapter, Depends(get_storage_adapter)],
    post_id: uuid.UUID,
    position: TrackPosition,
) -> StreamingResponse:
    """再生用と同じバイト源を ``Content-Disposition: attachment`` で返す。

    Post / track / オブジェクト不在は 404。
    """
    del user
    await _load_post(session, post_id)
    track = await _load_track(session, post_id=post_id, position=position)
    _ensure_audio_uri_contained(storage, track.audio_uri)
    if not storage.exists(track.audio_uri):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"audio object for post {post_id} position {position} is missing",
        )
    return _audio_streaming_response(
        storage,
        audio_uri=track.audio_uri,
        position=position,
        as_attachment=True,
    )


__all__ = [
    "AudioTrackListResponse",
    "AudioTrackResponse",
    "PostListResponse",
    "PostResponse",
    "PostRetryLauncher",
    "RetryAccepted",
    "get_retry_launcher",
    "get_storage_adapter",
    "router",
]
