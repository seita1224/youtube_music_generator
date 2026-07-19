"""``/scheduler/panic-stop`` コンプライアンス緊急停止ルータ (US4 T113, ADR-0031)。

``POST /scheduler/panic-stop`` を 1 本提供する (Basic 認証配下。 配線は ``main.py`` の
``_build_protected_router`` 配下で後段が ``include_router(router)`` する。 本モジュールは
素の ``APIRouter`` を module 変数 ``router`` で公開するだけで auth を個別付与しない)。

緊急停止の業務ロジックは :class:`~ymg_backend.domain.panic_stop.service.PanicStopService`
に委譲する。 本ルータは「呼び出し側」なので service の ``flush`` 後に ``session.commit()``
する (service 層は flush まで、 という境界をここで閉じる)。

設計方針 (``api/dryrun.py`` / ``api/scheduler.py`` を踏襲):

- ``scheduler_service`` は ``main.py`` の lifespan が ``app.state.scheduler_service`` に
  格納する前提のため ``Depends`` で解決できない。 ハンドラが ``request.app.state`` から
  取り出し service に渡す (未配線でも 500 で落とさず、 フラグ永続化 / audit は行う)。
- service / privacy_updater は ``Depends`` で解決し、 composition root が
  ``app.dependency_overrides`` で実体 (実 OAuth + privacy client) を注入する。 既定実装は
  ``get_settings()`` から構築する (``api/dryrun.py`` の ``get_dryrun_service`` と同方針)。
- service の業務例外を HTTP に写像する: :class:`RecoverableError` → 502 (YouTube が拒否)、
  :class:`TransientError` → 503 (一過性)、 ``ValueError`` (window_hours 境界) は
  ``Field(ge=1)`` で 422 として前段が弾く。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import BasicAuthUser, build_cipher_from_settings
from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.domain.panic_stop.service import (
    DEFAULT_WINDOW_HOURS,
    PanicStopResult,
    PanicStopService,
)
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.infrastructure.scheduler import SchedulerService
from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth
from ymg_backend.infrastructure.youtube.privacy_client import VideoPrivacyUpdater

router: Final = APIRouter(tags=["scheduler"])

# contracts の youtube_privacy_status enum (Video.privacy_status, backend-api.yaml L849)。
PrivacyStatus = Literal["public", "unlisted", "private", "deleted"]


class PanicStopRequest(BaseModel):
    """``POST /scheduler/panic-stop`` のリクエストボディ (contracts L340-356)。

    ``set_private`` は **bool ではなく ``youtube_video_id`` の文字列配列**。 ``None`` / 空なら
    候補列挙のみで private 化は行わない。
    """

    model_config = ConfigDict(extra="forbid")

    window_hours: int = Field(default=DEFAULT_WINDOW_HOURS, ge=1)
    set_private: list[str] | None = None


class VideoOut(BaseModel):
    """contracts ``Video`` スキーマの応答 DTO (backend-api.yaml L849)。"""

    model_config = ConfigDict(from_attributes=True)

    youtube_video_id: str
    title: str
    posted_at: datetime
    privacy_status: PrivacyStatus
    contains_synthetic_media: bool
    genre: str | None = None
    duration_sec: int | None = None
    thumbnail_uri: str | None = None


class PanicStopResponse(BaseModel):
    """``POST /scheduler/panic-stop`` のレスポンス (contracts ``PanicStopResponse``)。"""

    scheduler_enabled: bool
    recent_videos: list[VideoOut]
    updated_count: int


# ---------------------------------------------------------------------------------
# 依存性 (composition root が override で実体を注入する。 既定は settings から構築)。
# ---------------------------------------------------------------------------------
def get_panic_stop_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> PanicStopService:
    """:class:`PanicStopService` を解決する依存性 (override 可)。

    既定では ``settings`` から OAuth + :class:`VideoPrivacyUpdater` を組み立てる。 テスト /
    後段は ``app.dependency_overrides[get_panic_stop_service]`` で実体や stub を注入できる。
    OAuth / updater の構築自体は I/O を伴わないため、 ここで都度組んでも実呼び出し時まで
    副作用は無い (``api/dryrun.py`` の ``get_dryrun_service`` と同方針)。
    """
    cipher = build_cipher_from_settings(settings)
    oauth = YouTubeOAuth(settings, cipher)
    updater = VideoPrivacyUpdater(oauth)
    return PanicStopService(privacy_updater=updater)


# ---------------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------------
@router.post(
    "/scheduler/panic-stop",
    response_model=PanicStopResponse,
    operation_id="panicStop",
    summary="Compliance emergency stop (disable scheduler + set recent videos private)",
)
async def panic_stop(
    body: PanicStopRequest,
    request: Request,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[PanicStopService, Depends(get_panic_stop_service)],
) -> PanicStopResponse:
    """投稿を緊急停止し、 直近の動画を private 化する (ADR-0031)。

    scheduler を停止 + ``scheduler_enabled=false`` を永続化し、 直近 ``window_hours`` 時間の
    動画を列挙、 ``set_private`` に指定された ``youtube_video_id`` を YouTube + DB で private 化
    する。 ``audit_log`` に ``panic_stop`` を記録する。 service の ``flush`` 後に本ルータが
    commit する。

    Args:
        body: ``{"window_hours": int>=1, "set_private": [youtube_video_id, ...] | null}``。
        request: ``app.state.scheduler_service`` 参照用。
        user: Basic 認証済みユーザー名 (audit actor)。
        session: DB セッション (本ハンドラが commit する)。
        service: 緊急停止サービス (Depends で解決, override 可)。

    Returns:
        :class:`PanicStopResponse` (停止後フラグ / 候補動画 / private 化件数)。

    Raises:
        HTTPException: YouTube 側 privacy 更新が 4xx 拒否 → 502、 5xx / 通信失敗 → 503。
    """
    scheduler_service: SchedulerService | None = getattr(
        request.app.state, "scheduler_service", None
    )
    result = await _run_panic_stop(
        service,
        session=session,
        scheduler_service=scheduler_service,
        window_hours=body.window_hours,
        set_private=body.set_private,
        actor=user,
    )
    await session.commit()
    return PanicStopResponse(
        scheduler_enabled=result.scheduler_enabled,
        recent_videos=[VideoOut.model_validate(v) for v in result.recent_videos],
        updated_count=result.updated_count,
    )


async def _run_panic_stop(
    service: PanicStopService,
    *,
    session: AsyncSession,
    scheduler_service: SchedulerService | None,
    window_hours: int,
    set_private: list[str] | None,
    actor: str,
) -> PanicStopResult:
    """service.panic_stop を呼び、 業務例外を HTTP に写像する (``api/dryrun.py`` と同方針)。"""
    try:
        return await service.panic_stop(
            session=session,
            scheduler_service=scheduler_service,
            window_hours=window_hours,
            set_private=set_private,
            actor=actor,
        )
    except RecoverableError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except TransientError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


__all__ = [
    "PanicStopRequest",
    "PanicStopResponse",
    "VideoOut",
    "get_panic_stop_service",
    "router",
]
