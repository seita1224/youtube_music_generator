"""``/jobs`` 実行履歴・工程イベント・SSE (ADR-0023 / backend-api.yaml)。

- ``GET /jobs/runs`` — ``job_history`` 直近一覧
- ``GET /jobs/runs/{run_id}/events`` — ``job_step_events`` 永続履歴
- ``GET /jobs/stream?run_id=...`` — 選択実行のライブ差分 (初期は DB snapshot)

設計方針:

- **取りこぼし防止**: ``event_bus.subscribe()`` をスナップショット読込・送出より先に行う。
- **短い DB セッション**: 存在確認と snapshot 読込はそれぞれ独立した短い TX。 SSE 接続中は握らない。
- **接続維持**: ``_PING_INTERVAL`` 秒ごとに ``: ping\\n\\n`` を送る。
- **正本は DB**: SSE は差分。 ``run_id`` でフィルタする。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime
from typing import Annotated, Final, Literal, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.security import BasicAuthUser
from ymg_backend.infrastructure.db.models import JobHistory, JobStepEvent
from ymg_backend.infrastructure.db.session import get_session, get_sessionmaker
from ymg_backend.infrastructure.event_bus import (
    ErrorCategoryStr,
    EventBus,
    JobEvent,
    JobStatus,
    event_bus,
)


class _SessionFactory(Protocol):
    """短い snapshot 読込用のセッション工場 (``async with factory() as session``)。"""

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...

router: Final = APIRouter(prefix="/jobs", tags=["jobs"])

# grid 行順 (契約: step 語彙)。 音楽専用は cycle/post/music を使う。
STEPS: Final[tuple[str, ...]] = (
    "cycle",
    "post",
    "music",
    "acoustid",
    "image",
    "render",
    "publish",
)

_LIST_DEFAULT_LIMIT: Final[int] = 20
_LIST_MAX_LIMIT: Final[int] = 100
_PING_INTERVAL_SECONDS: Final[float] = 15.0
_SSE_MEDIA_TYPE: Final[str] = "text/event-stream"
_SSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

JobRunStatus = Literal["running", "succeeded", "failed", "skipped"]
JobTrigger = Literal["cron", "run_now"]


class JobRun(BaseModel):
    """``GET /jobs/runs`` の 1 要素 (backend-api.yaml JobRun)。"""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    job_name: str
    status: JobRunStatus
    trigger: JobTrigger | None = None
    target_date: date | None = None
    plan_id: uuid.UUID | None = None
    error_category: str | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None


class JobRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[JobRun]


class JobStepEventOut(BaseModel):
    """``GET /jobs/runs/{run_id}/events`` の 1 要素。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    step: str
    status: JobStatus
    genre: str | None = None
    context_type: str | None = None
    context_id: uuid.UUID | None = None
    error_category: str | None = None
    error_message: str | None = None
    created_at: datetime


class JobRunEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    items: list[JobStepEventOut]


@router.get("/runs", response_model=JobRunListResponse, summary="List recent job runs")
async def list_job_runs(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=_LIST_MAX_LIMIT)] = _LIST_DEFAULT_LIMIT,
    job_name: Annotated[str | None, Query()] = None,
) -> JobRunListResponse:
    """``job_history`` の直近実行一覧を返す。"""
    del user
    stmt = select(JobHistory).order_by(JobHistory.started_at.desc()).limit(limit)
    if job_name is not None:
        stmt = stmt.where(JobHistory.job_name == job_name)
    rows = (await session.execute(stmt)).scalars().all()
    return JobRunListResponse(items=[_job_history_to_run(row) for row in rows])


@router.get(
    "/runs/{run_id}/events",
    response_model=JobRunEventsResponse,
    summary="Persisted step events for a run",
)
async def list_job_run_events(
    run_id: uuid.UUID,
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JobRunEventsResponse:
    """``job_step_events`` の永続工程履歴を返す。"""
    del user
    run = await session.get(JobHistory, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_id not found")
    stmt = (
        select(JobStepEvent)
        .where(JobStepEvent.run_id == run_id)
        .order_by(JobStepEvent.created_at.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return JobRunEventsResponse(
        run_id=run_id,
        items=[_step_event_to_out(row) for row in rows],
    )


@router.get(
    "/stream",
    summary="SSE for live job progress of a selected run (ADR-0023)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {_SSE_MEDIA_TYPE: {}},
            "description": "JSON-Lines events of JobEvent (text/event-stream).",
        },
        404: {"description": "run_id が存在しない"},
    },
)
async def stream_jobs(
    user: BasicAuthUser,
    run_id: Annotated[uuid.UUID, Query(description="job_history.id")],
) -> StreamingResponse:
    """選択した ``run_id`` のライブ差分を SSE で配信する。

    初期表示は ``job_step_events`` の DB snapshot、 以後 EventBus の差分
    (``run_id`` 一致のみ)。 存在確認は短い TX で行い、 snapshot は購読登録後に
    別の短い TX で読む (取りこぼし防止 + 接続中のセッション握り防止)。
    """
    del user
    job_name = await _resolve_run_job_name(run_id)
    return StreamingResponse(
        _event_stream(run_id=run_id, job_name=job_name),
        media_type=_SSE_MEDIA_TYPE,
        headers=_SSE_HEADERS,
    )


async def _resolve_run_job_name(
    run_id: uuid.UUID,
    *,
    session_factory: _SessionFactory | None = None,
) -> str:
    """``run_id`` の存在確認と ``job_name`` 取得 (短い TX)。 不在は 404。"""
    factory: _SessionFactory = session_factory or get_sessionmaker()
    async with factory() as session:
        run = await session.get(JobHistory, run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="run_id not found"
            )
        return run.job_name


def _job_history_to_run(row: JobHistory) -> JobRun:
    plan_id = row.context_id if row.context_type == "plan" else None
    return JobRun(
        run_id=row.id,
        job_name=row.job_name,
        status=cast(JobRunStatus, row.status),
        trigger=cast(JobTrigger | None, row.trigger) if row.trigger else None,
        target_date=row.target_date,
        plan_id=plan_id,
        error_category=row.error_category,
        error_message=row.error_message,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
    )


def _step_event_to_out(row: JobStepEvent) -> JobStepEventOut:
    return JobStepEventOut(
        id=row.id,
        run_id=row.run_id,
        step=row.step,
        status=cast(JobStatus, row.status),
        genre=row.genre,
        context_type=row.context_type,
        context_id=row.context_id,
        error_category=row.error_category,
        error_message=row.error_message,
        created_at=row.created_at,
    )


async def _load_step_snapshot(
    session: AsyncSession, *, run_id: uuid.UUID, job_name: str
) -> list[JobEvent]:
    """``job_step_events`` を古い順の ``JobEvent`` 列へ写像する。"""
    stmt = (
        select(JobStepEvent)
        .where(JobStepEvent.run_id == run_id)
        .order_by(JobStepEvent.created_at.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_step_event_to_job_event(row, job_name=job_name) for row in rows]


def _step_event_to_job_event(row: JobStepEvent, *, job_name: str) -> JobEvent:
    return JobEvent(
        timestamp=row.created_at.isoformat(),
        job_name=job_name,
        step=row.step,
        status=cast(JobStatus, row.status),
        run_id=str(row.run_id),
        genre=row.genre,
        context_type=row.context_type,
        context_id=str(row.context_id) if row.context_id is not None else None,
        error_category=cast("ErrorCategoryStr | None", row.error_category),
        message=row.error_message,
    )


def _job_history_to_event(row: JobHistory) -> JobEvent:
    """後方互換: ``JobHistory`` 1 行を ``JobEvent`` へ写像する (単体テスト用)。"""
    moment = row.finished_at or row.started_at
    return JobEvent(
        timestamp=moment.isoformat(),
        job_name=row.job_name,
        step=row.job_name,
        status=cast(JobStatus, row.status),
        run_id=str(row.id),
        genre=None,
        context_type=row.context_type,
        context_id=str(row.context_id) if row.context_id is not None else None,
        error_category=cast("ErrorCategoryStr | None", row.error_category),
        message=row.error_message,
    )


async def _load_snapshot(session: AsyncSession) -> list[JobEvent]:
    """後方互換ヘルパ (旧テスト用)。 本番 SSE は ``_load_step_snapshot`` を使う。"""
    stmt = select(JobHistory).order_by(JobHistory.started_at.desc()).limit(50)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [_job_history_to_event(row) for row in reversed(list(rows))]


async def _event_stream(
    *,
    run_id: uuid.UUID,
    job_name: str,
    bus: EventBus = event_bus,
    session_factory: _SessionFactory | None = None,
) -> AsyncGenerator[str]:
    """SSE フレームを yield する。

    順序は ``subscribe → snapshot 読込 (短い TX) → snapshot 送出 → ライブ差分``。
    ``run_id`` 一致イベントのみ流す。 snapshot 読込前に publish された差分は queue に
    残り、 送出後のライブ側で届く (DB 未反映でも取りこぼさない)。
    """
    run_id_str = str(run_id)
    factory: _SessionFactory = session_factory or get_sessionmaker()
    async with bus.subscribe() as subscription:
        async with factory() as snap_session:
            snapshot = await _load_step_snapshot(
                snap_session, run_id=run_id, job_name=job_name
            )
        for event in snapshot:
            yield _format_event(event)
        while True:
            try:
                event = await asyncio.wait_for(
                    subscription.__anext__(), timeout=_PING_INTERVAL_SECONDS
                )
            except TimeoutError:
                yield ": ping\n\n"
                continue
            except StopAsyncIteration:
                break
            if event.run_id != run_id_str:
                continue
            yield _format_event(event)


def _format_event(event: JobEvent) -> str:
    """``JobEvent`` を SSE の ``data:`` フレームに整形する。"""
    payload = dataclasses.asdict(event)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = [
    "STEPS",
    "list_job_run_events",
    "list_job_runs",
    "router",
    "stream_jobs",
]
