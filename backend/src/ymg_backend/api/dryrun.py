"""``/dryrun`` レビュー業務ルータ (US2, contracts/backend-api.yaml ``/dryrun`` 系)。

4 endpoint を提供する (すべて Basic 認証必須。 配線は ``main.py`` の
``_build_protected_router`` 配下で後段が ``include_router(router)`` する。 本モジュールは
素の ``APIRouter`` を module 変数 ``router`` で公開するだけで auth を個別付与しない)。

- ``GET /dryrun/outputs``: ``state`` で絞り込んだ dryrun 成果物一覧 (新しい順)。
- ``POST /dryrun/outputs/{id}/approve``: ``pending`` を承認し YouTube 投稿 → ``posted``。
- ``POST /dryrun/outputs/{id}/reject``: ``pending`` を却下 (理由 min4) + 動画削除。
- ``GET /dryrun/outputs/{id}/video``: プレビュー動画を ``StorageAdapter.open`` で
  チャンク読みし ``StreamingResponse`` (``video/mp4``) で返す。 ブラウザの ``<video>`` が
  同一オリジン proxied パスで参照し、 Basic 認証はブラウザのセッションに従う。

設計方針:

- 状態変更ロジックは :class:`~ymg_backend.domain.dryrun.service.DryrunService` に委譲する。
  本ルータは「呼び出し側」なので service の ``flush`` 後に ``session.commit()`` する
  (service 層は flush まで、 という境界をここで閉じる)。
- service / storage は ``Depends`` で解決し、 composition root が
  ``app.dependency_overrides`` で実体 (実 uploader / 実 storage) を注入する (``posts.py`` の
  ``get_retry_launcher`` と同方針)。 既定実装は ``get_settings()`` から構築する。
- service の業務例外を HTTP に写像する: :class:`DryrunNotFoundError` → 404、
  :class:`DryrunStateConflictError` → 409、 ``reason`` の min4 違反 (``ValueError``) は
  FastAPI のリクエストボディ検証 (``Field(min_length=4)``) で 422 として弾く。
- 動画配信は全読み込み (``read_bytes``) を避け、 固定チャンク (1MiB) の generator で
  ストリームする。 404 (不在 / オブジェクト消失) と 409 (削除済み state) を返す。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from functools import lru_cache
from typing import IO, Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import BasicAuthUser, build_cipher_from_settings
from ymg_backend.domain.dryrun.service import (
    DryrunNotFoundError,
    DryrunService,
    DryrunStateConflictError,
)
from ymg_backend.infrastructure.db.models import DryrunOutput
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter
from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth
from ymg_backend.infrastructure.youtube.uploader import YouTubeUploader

router: Final = APIRouter(prefix="/dryrun", tags=["dryrun"])

# contracts の dryrun_state enum (backend-api.yaml DryrunOutput.state / state query)。
DryrunState = Literal["pending", "approved", "rejected", "auto_expired", "posted"]

# 動画ストリームのチャンクサイズ (1 MiB。 全読み込みを避けつつ往復回数も抑える)。
_VIDEO_CHUNK_SIZE: Final[int] = 1024 * 1024

# 配信不可な state (動画は削除済みなので 409。 pending/approved/posted は配信可)。
_NO_VIDEO_STATES: Final[frozenset[str]] = frozenset({"rejected", "auto_expired"})

# 一覧のデフォルト件数上限 (契約に limit は無いが暴走防止の安全弁。 ``posts.py`` と同値)。
_LIST_LIMIT: Final[int] = 100

_VIDEO_MEDIA_TYPE: Final[str] = "video/mp4"


class DryrunOutputResponse(BaseModel):
    """contracts ``DryrunOutput`` スキーマ (backend-api.yaml components.schemas)。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    state: DryrunState
    video_uri: str
    reject_reason: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None


class DryrunListResponse(BaseModel):
    """``GET /dryrun/outputs`` のレスポンス (contracts: ``{ items: DryrunOutput[] }``)。"""

    items: list[DryrunOutputResponse]


class RejectRequest(BaseModel):
    """``POST /dryrun/outputs/{id}/reject`` のリクエストボディ (理由 min4 必須)。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=4)


# ---------------------------------------------------------------------------------
# 依存性 (composition root が override で実体を注入する。 既定は settings から構築)。
# ---------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _build_default_storage(settings: Settings) -> StorageAdapter:
    """settings から既定の :class:`StorageAdapter` を構築する (プロセス単位 1 個)。"""
    return StorageAdapter(settings.storage_base_uri)


def get_storage_adapter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> StorageAdapter:
    """動画配信に使う :class:`StorageAdapter` を解決する依存性 (override 可)。"""
    return _build_default_storage(settings)


def get_dryrun_service(
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[StorageAdapter, Depends(get_storage_adapter)],
) -> DryrunService:
    """:class:`DryrunService` を解決する依存性 (override 可)。

    既定では ``settings`` から uploader (OAuth + storage) を組み立てる。 テスト / 後段は
    ``app.dependency_overrides[get_dryrun_service]`` で実体や stub を注入できる。 OAuth /
    uploader の構築自体は I/O を伴わないため、 ここで都度組んでも実投稿時まで副作用は無い。
    """
    cipher = build_cipher_from_settings(settings)
    oauth = YouTubeOAuth(settings, cipher)
    uploader = YouTubeUploader(oauth, storage, settings)
    return DryrunService(uploader=uploader, storage=storage)


# ---------------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------------
@router.get("/outputs", response_model=DryrunListResponse, summary="List dryrun outputs")
async def list_dryrun_outputs(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    state: Annotated[DryrunState | None, Query()] = None,
) -> DryrunListResponse:
    """dryrun 成果物の一覧を ``state`` で絞り込んで返す (新しい順)。"""
    del user  # 認証のみ目的。
    stmt = select(DryrunOutput)
    if state is not None:
        stmt = stmt.where(DryrunOutput.state == state)
    stmt = stmt.order_by(DryrunOutput.created_at.desc()).limit(_LIST_LIMIT)

    rows = (await session.execute(stmt)).scalars().all()
    return DryrunListResponse(items=[DryrunOutputResponse.model_validate(row) for row in rows])


@router.post(
    "/outputs/{output_id}/approve",
    response_model=DryrunOutputResponse,
    summary="Approve dryrun → upload to YouTube",
)
async def approve_dryrun(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[DryrunService, Depends(get_dryrun_service)],
    output_id: uuid.UUID,
) -> DryrunOutputResponse:
    """``pending`` の成果物を承認し YouTube へ投稿、 ``posted`` まで進める。

    不在は 404、 ``pending`` 以外は 409。 投稿 (uploader) はブロッキングだが service が
    ``asyncio.to_thread`` でオフロードする。 service の ``flush`` 後に本ルータが commit する。
    """
    del user
    output = await _approve(service, session, output_id)
    await session.commit()
    return DryrunOutputResponse.model_validate(output)


@router.post(
    "/outputs/{output_id}/reject",
    response_model=DryrunOutputResponse,
    summary="Reject dryrun → delete + feed reason to next planner",
)
async def reject_dryrun(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[DryrunService, Depends(get_dryrun_service)],
    output_id: uuid.UUID,
    body: RejectRequest,
) -> DryrunOutputResponse:
    """``pending`` の成果物を却下し、 理由 (min4) を保存 + プレビュー動画を削除する。

    ``reason`` の min4 は ``RejectRequest`` で 422 として弾かれる。 不在は 404、
    ``pending`` 以外は 409。 service の ``flush`` 後に本ルータが commit する。
    """
    del user
    output = await _reject(service, session, output_id, body.reason)
    await session.commit()
    return DryrunOutputResponse.model_validate(output)


@router.get(
    "/outputs/{output_id}/video",
    summary="Stream dryrun preview video (chunked)",
    response_class=StreamingResponse,
    responses={
        200: {"content": {_VIDEO_MEDIA_TYPE: {}}, "description": "Video byte stream (chunked)."},
        404: {"description": "dryrun output not found or video object missing."},
        409: {"description": "video deleted for this state (rejected / auto_expired)."},
    },
)
async def get_dryrun_video(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageAdapter, Depends(get_storage_adapter)],
    output_id: uuid.UUID,
) -> StreamingResponse:
    """プレビュー動画をチャンク読みでストリームする (``StorageAdapter.open`` 経由)。

    404: 成果物が存在しない、 または ``storage.exists`` が False (オブジェクト消失)。
    409: ``state`` が ``rejected`` / ``auto_expired`` (動画削除済みで配信不可)。
    """
    del user
    output = await session.get(DryrunOutput, output_id)
    if output is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"dryrun output {output_id} not found",
        )
    if output.state in _NO_VIDEO_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"dryrun output {output_id} is '{output.state}'; video is no longer available",
        )
    if not storage.exists(output.video_uri):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"video object for dryrun output {output_id} is missing",
        )

    stream = _stream_video(storage, output.video_uri)
    return StreamingResponse(stream, media_type=_VIDEO_MEDIA_TYPE)


# ---------------------------------------------------------------------------------
# 内部ヘルパ (例外写像 / ストリーミング)
# ---------------------------------------------------------------------------------
async def _approve(
    service: DryrunService,
    session: AsyncSession,
    output_id: uuid.UUID,
) -> DryrunOutput:
    """service.approve を呼び、 業務例外を HTTP に写像する。"""
    try:
        return await service.approve(session=session, output_id=output_id)
    except DryrunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DryrunStateConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


async def _reject(
    service: DryrunService,
    session: AsyncSession,
    output_id: uuid.UUID,
    reason: str,
) -> DryrunOutput:
    """service.reject を呼び、 業務例外を HTTP に写像する。"""
    try:
        return await service.reject(session=session, output_id=output_id, reason=reason)
    except DryrunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DryrunStateConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:  # service 側の min4 再検証 (通常は 422 で前段が弾く)。
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


async def _stream_video(storage: StorageAdapter, video_uri: str) -> AsyncIterator[bytes]:
    """動画ファイルを固定チャンクで読み出す async generator。

    ``StorageAdapter.open`` の同期ハンドルを ``with`` で確実にクローズしつつ、 1MiB ずつ
    yield する。 全読み込み (``read_bytes``) を避けてメモリ常駐を抑える。
    """
    handle: IO[Any]
    with storage.open(video_uri, "rb") as handle:
        for chunk in _iter_chunks(handle):
            yield chunk


def _iter_chunks(handle: IO[Any]) -> Iterator[bytes]:
    """ファイルハンドルから ``_VIDEO_CHUNK_SIZE`` 単位で読み出す同期イテレータ。"""
    while True:
        chunk = handle.read(_VIDEO_CHUNK_SIZE)
        if not chunk:
            break
        yield chunk


__all__ = [
    "DryrunListResponse",
    "DryrunOutputResponse",
    "RejectRequest",
    "get_dryrun_service",
    "get_storage_adapter",
    "router",
]
