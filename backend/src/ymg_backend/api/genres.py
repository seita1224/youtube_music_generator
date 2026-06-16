"""``/genres`` 業務ルータ (T107, contracts/backend-api.yaml ``/genres`` 群)。

ジャンル辞書 (``genres`` テーブル) の rotation 状態を確認・操作する管理 UI 向け
endpoint。 daily_cycle の ``_load_enabled_genres`` (api/plans.py) が ``Genre.enabled=True``
のジャンルを許可辞書として拾うため、 本ルータの promote/disable は次回サイクルの
許可ジャンルを実質的に切り替える操作になる (FR-038)。

エンドポイント:

- ``GET  /genres``               : enabled / role で絞った一覧 (``{items, total}``)。
- ``POST /genres/{name}/promote``: enabled=true + role を採用方向へ 1 段昇格
  (target_role 明示も可)。 audit ``genre_promoted``。
- ``POST /genres/{name}/disable``: enabled=false に無効化。 audit ``genre_disabled``。

設計方針:

- DB セッションは共有契約 (a) のとおり ``Depends(get_session)`` で受ける
  (commit は呼び出し側責務。 状態を変える promote/disable は本ルータで明示 commit する)。
- 本ルータは素の ``APIRouter`` を export し ``require_basic_auth`` を再付与しない
  (共有契約 (b) 準拠)。 配線は後段の ``main._build_protected_router`` が担う。
- ``Genre.role`` は enum ではなく文字列運用 (primary / extended / experimental)。
  promote は ``experimental → extended → primary`` の昇格方向のみ許可し、 これ以上
  昇格できない (primary を更に promote 等) 不正遷移は contract の 409 にマップする。
- 不在ジャンルは contract の 404。 エラーは ``ErrorResponse`` 形 (category/message/detail)
  で返し、 既存 ``api/plans.py`` の ``ErrorBody`` / ``_error_detail`` と同じ封筒に揃える。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.domain.errors import ErrorCategory
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import Genre
from ymg_backend.infrastructure.db.session import get_session

router: Final = APIRouter(prefix="/genres", tags=["genres"])

# 一覧の上限 (DoS / UI 肥大を避ける安全弁)。
_DEFAULT_LIST_LIMIT: Final[int] = 200

# role の昇格順 (experimental → extended → primary)。promote はこの順方向のみ。
_ROLE_PRIMARY: Final[str] = "primary"
_ROLE_EXTENDED: Final[str] = "extended"
_ROLE_EXPERIMENTAL: Final[str] = "experimental"

# 昇格ラダー: index が大きいほど主力寄り。1 段昇格はこの順で次へ進む。
_PROMOTE_LADDER: Final[tuple[str, ...]] = (_ROLE_EXPERIMENTAL, _ROLE_EXTENDED, _ROLE_PRIMARY)

# promote の明示遷移先として許可する role (contract: target_role enum)。
_PROMOTABLE_TARGETS: Final[frozenset[str]] = frozenset({_ROLE_EXTENDED, _ROLE_PRIMARY})


# ---------------------------------------------------------------------------
# レスポンス / リクエストスキーマ (backend-api.yaml schemas.Genre に整合)
# ---------------------------------------------------------------------------
class GenreResponse(BaseModel):
    """``Genre`` の API 表現 (backend-api.yaml ``schemas.Genre``)。"""

    model_config = ConfigDict(from_attributes=True)

    name: str
    display_name: str
    bpm_min: int | None = None
    bpm_max: int | None = None
    description: str | None = None
    role: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class GenreListResponse(BaseModel):
    """``GET /genres`` のレスポンス封筒 (``{items, total}``)。"""

    items: list[GenreResponse]
    total: int


class PromoteRequest(BaseModel):
    """``POST /genres/{name}/promote`` のリクエストボディ (任意)。"""

    target_role: str | None = None
    reason: str | None = None


class DisableRequest(BaseModel):
    """``POST /genres/{name}/disable`` のリクエストボディ (任意)。"""

    reason: str | None = None


class ErrorBody(BaseModel):
    """``ErrorResponse`` 封筒 (backend-api.yaml ``schemas.ErrorResponse``)。"""

    category: str
    message: str
    detail: dict[str, Any] | None = None


def _error_detail(
    category: ErrorCategory,
    message: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``ErrorResponse`` 形の ``HTTPException.detail`` dict を組む。"""
    body = ErrorBody(category=category.value, message=message, detail=detail)
    return body.model_dump()


async def _get_genre_or_404(session: AsyncSession, name: str) -> Genre:
    """``name`` (主キー) で ``Genre`` を取得する (無ければ 404 ``ErrorResponse``)。"""
    genre = await session.get(Genre, name)
    if genre is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_error_detail(
                ErrorCategory.RECOVERABLE,
                "genre not found",
                {"name": name},
            ),
        )
    return genre


def _resolve_promote_target(current_role: str, target_role: str | None) -> str:
    """promote 後の role を決める (1 段昇格 or target_role 明示)。

    昇格は ``experimental → extended → primary`` の順方向のみ許可する。 これ以上
    昇格できない (current が primary、 または明示 target が現在より下位/同位) 場合は
    409 相当の不正遷移として ``HTTPException`` を送出する。
    """
    if target_role is not None:
        # 明示指定: contract の enum (extended/primary) のみ受け付け、 かつ前進方向のみ許可。
        if target_role not in _PROMOTABLE_TARGETS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_error_detail(
                    ErrorCategory.RECOVERABLE,
                    "invalid target_role (allowed: extended, primary)",
                    {"target_role": target_role},
                ),
            )
        current_rank = _role_rank(current_role)
        if _PROMOTE_LADDER.index(target_role) <= current_rank:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_error_detail(
                    ErrorCategory.RECOVERABLE,
                    "target_role must be higher than current role",
                    {"current_role": current_role, "target_role": target_role},
                ),
            )
        return target_role

    # 省略時: 1 段昇格。 既に primary (最上位) なら昇格不可。
    current_rank = _role_rank(current_role)
    if current_rank >= len(_PROMOTE_LADDER) - 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_error_detail(
                ErrorCategory.RECOVERABLE,
                "genre is already at the highest role (primary) and cannot be promoted",
                {"current_role": current_role},
            ),
        )
    return _PROMOTE_LADDER[current_rank + 1]


def _role_rank(role: str) -> int:
    """role の昇格ラダー上の順位を返す (未知の role は最下位 = experimental 相当)。

    辞書外の role 文字列 (運用上想定外) も昇格を妨げないよう、 最下位 (0) として扱い、
    1 段昇格で ``extended`` へ前進できるようにする。
    """
    try:
        return _PROMOTE_LADDER.index(role)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# GET /genres
# ---------------------------------------------------------------------------
@router.get("", response_model=GenreListResponse, summary="List genres (filter by enabled / role)")
async def list_genres(
    session: Annotated[AsyncSession, Depends(get_session)],
    enabled: Annotated[bool | None, Query()] = None,
    role: Annotated[str | None, Query()] = None,
) -> GenreListResponse:
    """``enabled`` / ``role`` で絞った ``Genre`` 一覧を name 昇順で返す。

    ``total`` はフィルタ適用後の総件数。 未指定のフィルタは全件対象。
    """
    conditions: list[ColumnElement[bool]] = []
    if enabled is not None:
        conditions.append(Genre.enabled.is_(enabled))
    if role is not None and (normalized := role.strip()):
        conditions.append(Genre.role == normalized)

    total_stmt = select(func.count()).select_from(Genre)
    list_stmt = select(Genre).order_by(Genre.name).limit(_DEFAULT_LIST_LIMIT)
    for cond in conditions:
        total_stmt = total_stmt.where(cond)
        list_stmt = list_stmt.where(cond)

    total = (await session.execute(total_stmt)).scalar_one()
    rows = (await session.execute(list_stmt)).scalars().all()
    return GenreListResponse(
        items=[GenreResponse.model_validate(row) for row in rows],
        total=int(total),
    )


# ---------------------------------------------------------------------------
# POST /genres/{name}/promote
# ---------------------------------------------------------------------------
@router.post(
    "/{name}/promote",
    response_model=GenreResponse,
    summary="Promote a genre toward primary (enabled=true + role step up)",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
    },
)
async def promote_genre(
    session: Annotated[AsyncSession, Depends(get_session)],
    name: str,
    body: Annotated[PromoteRequest | None, Body()] = None,
) -> GenreResponse:
    """ジャンルを採用方向へ昇格する (``enabled=true`` + role 1 段昇格 or 明示)。

    ``experimental → extended → primary`` の順方向のみ許可する (不正遷移は 409)。
    ``enabled`` は昇格と同時に必ず true へ戻す (主力寄りジャンルは daily_cycle が拾える)。
    audit ``genre_promoted`` を残し、 commit は本 endpoint の責務。
    """
    payload = body or PromoteRequest()
    genre = await _get_genre_or_404(session, name)

    previous_role = genre.role
    previous_enabled = genre.enabled
    next_role = _resolve_promote_target(previous_role, payload.target_role)

    genre.role = next_role
    genre.enabled = True
    await session.flush()
    await write_audit_log(
        session,
        action="genre_promoted",
        target_type="genre",
        target_id=name,
        payload={
            "previous_role": previous_role,
            "new_role": next_role,
            "previous_enabled": previous_enabled,
            "reason": payload.reason,
        },
    )
    await session.commit()
    await session.refresh(genre)
    logger.info(
        "genres.promote_genre promoted genre",
        name=name,
        previous_role=previous_role,
        new_role=next_role,
    )
    return GenreResponse.model_validate(genre)


# ---------------------------------------------------------------------------
# POST /genres/{name}/disable
# ---------------------------------------------------------------------------
@router.post(
    "/{name}/disable",
    response_model=GenreResponse,
    summary="Disable a genre (enabled=false)",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
)
async def disable_genre(
    session: Annotated[AsyncSession, Depends(get_session)],
    name: str,
    body: Annotated[DisableRequest | None, Body()] = None,
) -> GenreResponse:
    """ジャンルを無効化する (``enabled=false``)。

    無効化後は daily_cycle の ``_load_enabled_genres`` が次回以降このジャンルを拾わない。
    role は変更しない (昇格履歴を保つ。 再有効化時に元の role から再開できる)。
    audit ``genre_disabled`` を残し、 commit は本 endpoint の責務。 冪等 (既に無効でも 200)。
    """
    payload = body or DisableRequest()
    genre = await _get_genre_or_404(session, name)

    previous_enabled = genre.enabled
    genre.enabled = False
    await session.flush()
    await write_audit_log(
        session,
        action="genre_disabled",
        target_type="genre",
        target_id=name,
        payload={
            "previous_enabled": previous_enabled,
            "role": genre.role,
            "reason": payload.reason,
        },
    )
    await session.commit()
    await session.refresh(genre)
    logger.info("genres.disable_genre disabled genre", name=name, role=genre.role)
    return GenreResponse.model_validate(genre)


__all__ = [
    "DisableRequest",
    "ErrorBody",
    "GenreListResponse",
    "GenreResponse",
    "PromoteRequest",
    "router",
]
