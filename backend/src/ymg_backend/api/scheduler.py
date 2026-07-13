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
- ``POST /scheduler/run-now`` — 承認済み Daily Plan の音楽生成を即時 1 回起動する
  (ADR-0006 / ADR-0011)。 ``job_history(running)`` 予約後に 202 + ``run_id`` を返す。
  予約 commit 後に audit が失敗した場合は予約を ``finalize(failed)`` して single-flight
  を解放してから例外を再送出する。

設計方針:

- ``app_state`` 参照は ORM 層に依存せず SQLAlchemy Core の軽量 Table を本モジュールに閉じて
  持つ (``api/health.py`` / ``infrastructure/audit.py`` と同じ疎結合方針)。
- ``commit`` は本ハンドラの責務 (HTTP リクエスト = トランザクション境界)。 ``write_audit_log`` は
  flush までなので、 状態 upsert と audit を 1 トランザクションでまとめて commit する。
- :class:`~ymg_backend.infrastructure.scheduler.SchedulerService` は ``main.py`` の lifespan が
  ``app.state.scheduler_service`` / ``app.state.music_run_service`` に格納する。 未配線でも
  ``PUT /scheduler`` は 500 で落とさず、 状態フラグの永続化と audit は必ず行い、 ジョブ
  出し入れだけを skip する (health の degraded と同じ「副作用は best-effort」方針)。
  ``POST /scheduler/run-now`` は未配線時 503。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import date, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.security import BasicAuthUser
from ymg_backend.domain.pipeline.music_run import (
    MusicGenerationBusyError,
    MusicRunService,
    PlanNotApprovedError,
    PlanNotFoundError,
)
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.session import get_session

# run-now のバックグラウンド Task 参照を保持し GC で消えないようにする (RUF006)。
_run_now_tasks: Final[set[asyncio.Task[None]]] = set()

router: Final = APIRouter(tags=["scheduler"])

# app_state のキー (data-model.md §app_state seed / 001_initial.py `_seed_app_state`)。
_KEY_SCHEDULER_ENABLED: Final[str] = "scheduler_enabled"
_KEY_DRYRUN_ENABLED: Final[str] = "dryrun_enabled"

# audit_log の action 名 (ADR-0031 panic-stop / ADR-0035 §2 dryrun 切替)。
_ACTION_SCHEDULER_ENABLED: Final[str] = "scheduler_enabled"
_ACTION_SCHEDULER_DISABLED: Final[str] = "scheduler_disabled"
_ACTION_DRYRUN_ENABLED: Final[str] = "dryrun_enabled"
_ACTION_DRYRUN_DISABLED: Final[str] = "dryrun_disabled"
_ACTION_SCHEDULER_RUN_NOW: Final[str] = "scheduler_run_now"

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


class RunNowRequest(BaseModel):
    """``POST /scheduler/run-now`` のリクエストボディ (``plan_id`` 必須)。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: uuid.UUID = Field(description="status=approved の Daily Plan id")


class RunNowResponse(BaseModel):
    """``POST /scheduler/run-now`` の 202 レスポンス。"""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    run_id: uuid.UUID
    plan_id: uuid.UUID
    target_date: date


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
    """dryrun ↔ 投稿モードを切替える (ADR-0035 §2: 切替は audit 必須)。"""
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


@router.post(
    "/scheduler/run-now",
    response_model=RunNowResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start music generation for an approved Daily Plan",
)
async def run_scheduler_now(
    request: Request,
    body: RunNowRequest,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunNowResponse:
    """承認済み Daily Plan の音楽生成を即時 1 回起動する (ADR-0006 / ADR-0011)。

    実行予約時に ``job_history(status=running)`` を commit し、 その id を ``run_id`` として
    返す。 長時間処理はバックグラウンドで実行し HTTP は 202。
    起動操作は ``audit_log`` に actor / run_id / plan_id を残す。

    Raises:
        HTTPException: 404 (Plan 不在) / 409 (未承認 or 別 run running) / 503 (未配線)。
    """
    music_run_service = getattr(request.app.state, "music_run_service", None)
    if music_run_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="music_run_service not wired; cannot run music generation",
        )

    try:
        reservation = await music_run_service.reserve_run_now(session, plan_id=body.plan_id)
    except PlanNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PlanNotApprovedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Plan is not approved (status={exc.status})",
        ) from exc
    except MusicGenerationBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    # reserve_run_now は job_history(running) を既に commit 済み。 audit 失敗時も
    # single-flight 枠を解放しないと、 再起動の orphan 整合まで全 run が 409 になる。
    try:
        await write_audit_log(
            session,
            action=_ACTION_SCHEDULER_RUN_NOW,
            actor=user,
            target_type="plan",
            target_id=str(reservation.plan_id),
            payload={
                "actor": user,
                "run_id": str(reservation.run_id),
                "plan_id": str(reservation.plan_id),
                "target_date": reservation.target_date.isoformat(),
            },
        )
        await session.commit()
    except Exception as audit_exc:
        logger.bind(component="api.scheduler").error(
            "run-now audit failed after reservation; finalizing run as failed",
            run_id=str(reservation.run_id),
            plan_id=str(reservation.plan_id),
            error=str(audit_exc),
        )
        with contextlib.suppress(Exception):  # 解放を優先し、 rollback 失敗は握る
            await session.rollback()
        await music_run_service.finalize(
            run_id=reservation.run_id,
            status="failed",
            error_category="fatal",
            error_message=f"audit_log write failed after reservation: {audit_exc}",
        )
        raise

    task = asyncio.create_task(
        _execute_run_now(music_run_service, run_id=reservation.run_id, plan_id=reservation.plan_id),
        name=f"scheduler-run-now:{reservation.run_id}",
    )
    _run_now_tasks.add(task)
    task.add_done_callback(_run_now_tasks.discard)
    logger.bind(component="api.scheduler").info(
        "run-now accepted",
        run_id=str(reservation.run_id),
        plan_id=str(reservation.plan_id),
        target_date=reservation.target_date.isoformat(),
        actor=user,
    )
    return RunNowResponse(
        accepted=True,
        run_id=reservation.run_id,
        plan_id=reservation.plan_id,
        target_date=reservation.target_date,
    )


async def _execute_run_now(
    music_run_service: MusicRunService,
    *,
    run_id: uuid.UUID,
    plan_id: uuid.UUID,
) -> None:
    """予約済み run を実行する (例外はログして握り潰す)。"""
    log = logger.bind(
        component="api.scheduler.run_now",
        run_id=str(run_id),
        plan_id=str(plan_id),
    )
    try:
        await music_run_service.execute(run_id=run_id, plan_id=plan_id)
        log.info("run-now completed")
    except Exception as exc:  # バックグラウンド失敗でイベントループを落とさない
        log.error("run-now failed: {}", exc)


def _apply_scheduler_jobs(request: Request, *, enabled: bool) -> None:
    """``app.state.scheduler_service`` 経由で日次ジョブを ``add`` / ``remove`` する。"""
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
