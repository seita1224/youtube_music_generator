"""``/posts`` 業務ルータ (T0xx, contracts/backend-api.yaml ``/posts`` 系)。

3 endpoint を提供する (すべて Basic 認証必須。 配線は ``main.py`` の
``_build_protected_router`` 配下で後段が ``include_router`` する)。

- ``GET /posts``: ``status`` / ``genre`` / ``from_date`` / ``to_date`` で投稿一覧を絞り込む。
- ``GET /posts/{post_id}``: 単一投稿を返す (不在は 404)。
- ``POST /posts/{post_id}/retry``: **失敗した** 投稿パイプラインを再実行する (ADR-0035 §3)。
  daily_cycle の単一 post 経路を再起動するため、 post を ``pending`` に戻し、 監査ログを残し、
  再実行を :class:`PostRetryLauncher` に委譲して即座に ``202 Accepted`` を返す。

設計方針:

- レスポンスは ORM ``Post`` を contracts の ``Post`` スキーマへ写像した
  :class:`PostResponse` (Pydantic) で返す。 ``payload`` (JSONB) はそのまま透過する。
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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Protocol

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.logging import bind_context
from ymg_backend.core.security import BasicAuthUser
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import Post
from ymg_backend.infrastructure.db.session import get_session

router: Final = APIRouter(prefix="/posts", tags=["posts"])

# contracts の post_status enum (backend-api.yaml ``/posts`` query / Post.status)。
PostStatus = Literal["pending", "generating", "generated", "posting", "posted", "failed"]

# retry を受け付ける状態。 ADR-0035 §3 に従い「失敗した投稿の再実行」のみ許可する。
_RETRYABLE_STATUSES: Final[frozenset[str]] = frozenset({"failed"})

# 一覧のデフォルト件数上限 (contracts に limit は無いが、 暴走防止の安全弁)。
_LIST_LIMIT: Final[int] = 100

# from_date/to_date の終端境界計算用 (to_date を含めるため翌日 0:00 を排他上界にする)。
_ONE_DAY: Final[timedelta] = timedelta(days=1)


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


@router.get("", response_model=PostListResponse, summary="List posts")
async def list_posts(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[PostStatus | None, Query(alias="status")] = None,
    genre: Annotated[str | None, Query()] = None,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
) -> PostListResponse:
    """投稿一覧を ``status`` / ``genre`` / ``from_date`` / ``to_date`` で絞り込む。

    ``from_date`` / ``to_date`` は ``created_at`` の日付境界に対する閉区間で判定する
    (``from_date`` 当日 0:00 以降、 ``to_date`` の翌日 0:00 未満)。 新しい順で返す。
    """
    del user  # 認証のみ目的 (ユーザー名は本 endpoint では未使用)。
    stmt = select(Post)
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


__all__ = [
    "PostListResponse",
    "PostResponse",
    "PostRetryLauncher",
    "RetryAccepted",
    "get_retry_launcher",
    "router",
]
