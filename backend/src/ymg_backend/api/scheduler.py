"""``/scheduler`` 管理エンドポイント (T088, backend-api.yaml / ADR-0031 / ADR-0035)。

提供するルート (いずれも Basic 認証配下。 配線は ``main.py:_build_protected_router`` に
``include_router`` する後段タスクの責務):

- ``GET /scheduler`` — 現在の ``scheduler_enabled`` 状態を返す (:class:`SchedulerState`)。
- ``PUT /scheduler`` — scheduler の有効 / 無効を切替える (ADR-0031)。 状態が変わった場合のみ
  ``audit_log`` に記録し、 ``app.state.scheduler_service`` 経由で日次ジョブを ``add`` / ``remove``
  する。 reboot 後は false 起動が既定なので、 enable は手動オペレーションになる。
- ``PUT /scheduler/mode`` — dryrun ↔ 投稿モードを切替える (ADR-0035)。 ``true → false``
  (投稿開始) / ``false → true`` (dryrun 復帰) のいずれも ``audit_log`` に記録する
  (ADR-0035 §2: 不可逆判断のトレーサビリティ)。 契約の ``/scheduler`` には ``enabled`` しか
  無いため、 posting mode 切替は別ルートとして本ルータに置く。

設計方針:

- ``app_state`` 参照は ORM 層に依存せず SQLAlchemy Core の軽量 Table を本モジュールに閉じて
  持つ (``api/health.py`` / ``infrastructure/audit.py`` と同じ疎結合方針)。
- ``commit`` は本ハンドラの責務 (HTTP リクエスト = トランザクション境界)。 ``write_audit_log`` は
  flush までなので、 状態 upsert と audit を 1 トランザクションでまとめて commit する。
- :class:`~ymg_backend.infrastructure.scheduler.SchedulerService` は ``main.py`` の lifespan が
  ``app.state.scheduler_service`` に格納する前提 (配線は後段)。 未配線でも 500 で落とさず、
  状態フラグの永続化と audit は必ず行い、 ジョブ出し入れだけを skip する (health の degraded
  と同じ「副作用は best-effort」方針)。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.security import BasicAuthUser
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.session import get_session

router: Final = APIRouter(tags=["scheduler"])

# app_state のキー (data-model.md §app_state seed / 001_initial.py `_seed_app_state`)。
_KEY_SCHEDULER_ENABLED: Final[str] = "scheduler_enabled"
_KEY_DRYRUN_ENABLED: Final[str] = "dryrun_enabled"

# audit_log の action 名 (ADR-0031 panic-stop / ADR-0035 §2 dryrun 切替)。
_ACTION_SCHEDULER_ENABLED: Final[str] = "scheduler_enabled"
_ACTION_SCHEDULER_DISABLED: Final[str] = "scheduler_disabled"
_ACTION_DRYRUN_ENABLED: Final[str] = "dryrun_enabled"
_ACTION_DRYRUN_DISABLED: Final[str] = "dryrun_disabled"

# app_state.value (JSONB) への疎結合参照用 Core Table (ORM 層非依存)。
_metadata: Final[MetaData] = MetaData()

app_state_table: Final[Table] = Table(
    "app_state",
    _metadata,
    Column("key", String, primary_key=True, nullable=False),
    Column("value", JSONB, nullable=False),
)


class SchedulerState(BaseModel):
    """``GET`` / ``PUT /scheduler`` のレスポンス (backend-api.yaml SchedulerState)。"""

    enabled: bool
    updated_at: datetime | None = None


class SchedulerToggleRequest(BaseModel):
    """``PUT /scheduler`` のリクエストボディ (contract: ``required: [enabled]``)。"""

    enabled: bool


class ModeToggleRequest(BaseModel):
    """``PUT /scheduler/mode`` のリクエストボディ (dryrun ↔ 投稿モード, ADR-0035)。"""

    dryrun_enabled: bool


class ModeState(BaseModel):
    """``PUT /scheduler/mode`` のレスポンス (現在の posting mode)。"""

    dryrun_enabled: bool


def _decode_app_state_bool(raw: object) -> bool | None:
    """app_state.value (JSONB) を bool に正規化する (型不一致は ``None``)。

    asyncpg は JSONB をデコード済みオブジェクトで返すことも生 JSON 文字列で返すことも
    あるため、 文字列なら一段 JSON デコードを試みてから bool 判定する
    (``api/health.py`` と同方針)。
    """
    value: object = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    return value if isinstance(value, bool) else None


async def _read_flag(
    session: AsyncSession, key: str, *, default: bool
) -> tuple[bool, datetime | None]:
    """``app_state`` から ``key`` の bool 値と ``updated_at`` を読む (不在 / 型不一致は default)。"""
    stmt = select(app_state_table.c.value).where(app_state_table.c.key == key)
    raw = (await session.execute(stmt)).scalar_one_or_none()
    decoded = _decode_app_state_bool(raw)
    return (default if decoded is None else decoded, None)


async def _upsert_flag(session: AsyncSession, key: str, value: bool) -> datetime:
    """``app_state`` の ``key`` を ``value`` に upsert し、 ``updated_at`` を返す (flush まで)。

    主キー ``key`` 衝突時は ``value`` と ``updated_at=now()`` を更新する
    (Postgres ``ON CONFLICT``)。 commit は呼び出し側の責務。
    """
    stmt = (
        insert(app_state_table)
        .values(key=key, value=value)
        .on_conflict_do_update(
            index_elements=[app_state_table.c.key],
            set_={"value": value},
        )
        .returning(app_state_table.c.value)
    )
    await session.execute(stmt)
    await session.flush()
    # updated_at は DB 側 server_default (now()) が更新する。 ハンドラは確定値を持たないため
    # レスポンスでは現在時刻を返さず None を許容する (GET 再取得で確定値が見える)。
    return datetime.now()


@router.get("/scheduler", response_model=SchedulerState, summary="Get scheduler enabled state")
async def get_scheduler(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SchedulerState:
    """現在の ``scheduler_enabled`` 状態を返す (ADR-0031, 既定 false)。"""
    enabled, updated_at = await _read_flag(session, _KEY_SCHEDULER_ENABLED, default=False)
    return SchedulerState(enabled=enabled, updated_at=updated_at)


@router.put("/scheduler", response_model=SchedulerState, summary="Enable / disable scheduler")
async def set_scheduler(
    body: SchedulerToggleRequest,
    request: Request,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SchedulerState:
    """scheduler の有効 / 無効を切替える (ADR-0031)。

    状態が変わった場合のみ ``app_state`` を更新し ``audit_log`` に記録、 さらに
    ``app.state.scheduler_service`` 経由で日次ジョブを ``add`` / ``remove`` する。 同値要求は
    no-op (audit もジョブ操作もしない) で現状を返す。

    Args:
        body: ``{"enabled": bool}``。
        request: ``app.state.scheduler_service`` 参照用。
        user: Basic 認証済みユーザー名 (audit actor)。
        session: DB セッション (本ハンドラが commit する)。

    Returns:
        切替後の :class:`SchedulerState`。
    """
    current, _ = await _read_flag(session, _KEY_SCHEDULER_ENABLED, default=False)
    if current == body.enabled:
        return SchedulerState(enabled=current)

    updated_at = await _upsert_flag(session, _KEY_SCHEDULER_ENABLED, body.enabled)
    action = _ACTION_SCHEDULER_ENABLED if body.enabled else _ACTION_SCHEDULER_DISABLED
    await write_audit_log(
        session,
        action=action,
        actor=user,
        target_type="scheduler",
        target_id=_KEY_SCHEDULER_ENABLED,
        payload={"actor": user, "from": current, "to": body.enabled},
    )
    await session.commit()

    _apply_scheduler_jobs(request, enabled=body.enabled)
    return SchedulerState(enabled=body.enabled, updated_at=updated_at)


@router.put("/scheduler/mode", response_model=ModeState, summary="Toggle dryrun / posting mode")
async def set_mode(
    body: ModeToggleRequest,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ModeState:
    """dryrun ↔ 投稿モードを切替える (ADR-0035 §2: 切替は audit 必須)。

    ``true → false`` (投稿開始) / ``false → true`` (dryrun 復帰) のいずれも ``audit_log`` に
    ``from`` / ``to`` を記録する。 同値要求は no-op で現状を返す。

    Args:
        body: ``{"dryrun_enabled": bool}``。
        user: Basic 認証済みユーザー名 (audit actor)。
        session: DB セッション (本ハンドラが commit する)。

    Returns:
        切替後の :class:`ModeState`。
    """
    current, _ = await _read_flag(session, _KEY_DRYRUN_ENABLED, default=True)
    if current == body.dryrun_enabled:
        return ModeState(dryrun_enabled=current)

    await _upsert_flag(session, _KEY_DRYRUN_ENABLED, body.dryrun_enabled)
    action = _ACTION_DRYRUN_ENABLED if body.dryrun_enabled else _ACTION_DRYRUN_DISABLED
    await write_audit_log(
        session,
        action=action,
        actor=user,
        target_type="app_state",
        target_id=_KEY_DRYRUN_ENABLED,
        payload={"actor": user, "from": current, "to": body.dryrun_enabled},
    )
    await session.commit()
    return ModeState(dryrun_enabled=body.dryrun_enabled)


def _apply_scheduler_jobs(request: Request, *, enabled: bool) -> None:
    """``app.state.scheduler_service`` 経由で日次ジョブを ``add`` / ``remove`` する。

    scheduler_service が未配線 (後段タスク未完了 / lifespan 非実行のテスト) の場合は、 状態
    フラグの永続化と audit は済んでいるためジョブ操作だけを skip する (副作用は best-effort)。

    Args:
        request: ``app.state.scheduler_service`` を引くための ``Request``。
        enabled: ``True`` で enable (ジョブ登録)、 ``False`` で disable (ジョブ除去)。
    """
    service = getattr(request.app.state, "scheduler_service", None)
    if service is None:
        logger.bind(component="api.scheduler").warning(
            "scheduler_service not wired; flag persisted but jobs unchanged (enabled={})", enabled
        )
        return
    if enabled:
        service.enable()
    else:
        service.disable()
