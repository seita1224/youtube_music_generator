"""音楽生成の実行予約・確定・cron/run-now 共有サービス (ADR-0011 / ADR-0006)。

``job_history(status=running)`` を commit して single-flight 枠を確保し、 その ``id`` を
API の ``run_id`` とする。 定時と即時は同一制約を共有する。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, Literal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ymg_backend.domain.errors.errors import resolve_category
from ymg_backend.infrastructure.db.models import Plan
from ymg_backend.infrastructure.db.models.job_history import MUSIC_GENERATION_JOB_NAME, JobHistory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ymg_backend.domain.pipeline.music_generation import MusicGenerationOrchestrator

Trigger = Literal["cron", "run_now"]

__all__ = [
    "MusicGenerationBusyError",
    "MusicRunReservation",
    "MusicRunService",
    "PlanNotApprovedError",
    "PlanNotFoundError",
]


class PlanNotFoundError(Exception):
    """``plan_id`` に対応する Plan が存在しない。"""


class PlanNotApprovedError(Exception):
    """Plan が ``approved`` 以外のため起動できない。"""

    def __init__(self, *, plan_id: uuid.UUID, status: str) -> None:
        self.plan_id = plan_id
        self.status = status
        super().__init__(f"Plan {plan_id} is not approved (status={status})")


class MusicGenerationBusyError(Exception):
    """別の ``music_generation`` が ``running`` (single-flight)。"""

    def __init__(self) -> None:
        super().__init__("別の音楽生成が実行中")


@dataclass(frozen=True)
class MusicRunReservation:
    """予約済み実行 (``run_id`` = ``job_history.id``)。"""

    run_id: uuid.UUID
    plan_id: uuid.UUID
    target_date: date
    trigger: Trigger


class MusicRunService:
    """承認 Plan の予約・実行・終端をまとめる共有サービス。

    ``POST /scheduler/run-now`` と APScheduler cron の双方が本クラスを使う。
    """

    __slots__ = ("_orchestrator", "_session_factory")

    def __init__(
        self,
        *,
        orchestrator: MusicGenerationOrchestrator,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._orchestrator: Final[MusicGenerationOrchestrator] = orchestrator
        self._session_factory: Final[async_sessionmaker[AsyncSession]] = session_factory

    async def reserve_run_now(
        self, session: AsyncSession, *, plan_id: uuid.UUID
    ) -> MusicRunReservation:
        """即時実行を予約する。 未承認 / 重複は例外。

        Raises:
            PlanNotFoundError: Plan 不在。
            PlanNotApprovedError: ``status != approved``。
            MusicGenerationBusyError: 別 run が running。
        """
        plan = await session.get(Plan, plan_id)
        if plan is None:
            raise PlanNotFoundError(f"Plan {plan_id} not found")
        if plan.status != "approved":
            raise PlanNotApprovedError(plan_id=plan_id, status=plan.status)
        if plan.cycle != "daily" or plan.target_date is None:
            raise PlanNotApprovedError(plan_id=plan_id, status=plan.status)

        return await self._insert_running(
            session,
            plan_id=plan.id,
            target_date=plan.target_date,
            trigger="run_now",
        )

    async def reserve_cron(
        self, session: AsyncSession, *, target_date: date
    ) -> MusicRunReservation | None:
        """定時実行を短い TX で予約する。

        承認 Plan が無ければ ``skipped`` 履歴を残して ``None``。
        別 run が running なら起動せず ``None``。

        Returns:
            予約できたときだけ :class:`MusicRunReservation`。
        """
        log = logger.bind(component="music_run.cron", target_date=target_date.isoformat())
        plan = await self._find_approved_daily_plan(session, target_date=target_date)
        if plan is None:
            await self._insert_skipped(session, target_date=target_date)
            log.info("no approved plan; recorded skipped")
            return None

        try:
            reservation = await self._insert_running(
                session,
                plan_id=plan.id,
                target_date=target_date,
                trigger="cron",
            )
        except MusicGenerationBusyError:
            log.info("music_generation already running; cron skipped")
            return None

        return reservation

    async def run_cron(self, *, target_date: date) -> None:
        """定時実行: 予約用セッションを閉じてから GPU 実行する。

        予約 (短い TX) と実行 (長時間) で ``AsyncSession`` を分離し、
        コネクションを GPU 生成の全期間握り続けない。
        """
        async with self._session_factory() as session:
            reservation = await self.reserve_cron(session, target_date=target_date)
        if reservation is not None:
            await self.execute(run_id=reservation.run_id, plan_id=reservation.plan_id)

    async def execute(self, *, run_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        """予約済み run をバックグラウンド相当で実行し、 ``job_history`` を終端する。"""
        log = logger.bind(
            component="music_run.execute",
            run_id=str(run_id),
            plan_id=str(plan_id),
        )
        try:
            async with self._session_factory() as session:
                result = await self._orchestrator.run(
                    session=session, plan_id=plan_id, run_id=run_id
                )
            status: Literal["succeeded", "failed"] = (
                "succeeded" if result.failed_count == 0 else "failed"
            )
            await self.finalize(
                run_id=run_id,
                status=status,
                error_message=(
                    f"{result.failed_count} post(s) failed"
                    if result.failed_count > 0
                    else None
                ),
                error_category="recoverable" if result.failed_count > 0 else None,
            )
            log.info("music run finished", status=status)
        except Exception as exc:
            category = resolve_category(exc)
            log.error("music run failed: {}", exc)
            await self.finalize(
                run_id=run_id,
                status="failed",
                error_category=category.value,
                error_message=str(exc),
            )
            raise

    async def finalize(
        self,
        *,
        run_id: uuid.UUID,
        status: Literal["succeeded", "failed", "skipped"],
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """``job_history`` を短い独立 TX で終端する。"""
        finished = datetime.now(tz=UTC)
        async with self._session_factory() as session:
            row = await session.get(JobHistory, run_id)
            if row is None:
                logger.bind(component="music_run").warning(
                    "finalize skipped; run not found", run_id=str(run_id)
                )
                return
            if row.status != "running" and status != "skipped":
                # 既に終端済み (二重 finalize) は無視。
                return
            row.status = status
            row.finished_at = finished
            row.duration_ms = max(
                0, int((finished - row.started_at).total_seconds() * 1000)
            )
            row.error_category = error_category
            row.error_message = error_message
            await session.commit()

    async def _insert_running(
        self,
        session: AsyncSession,
        *,
        plan_id: uuid.UUID,
        target_date: date,
        trigger: Trigger,
    ) -> MusicRunReservation:
        run_id = uuid.uuid4()
        started = datetime.now(tz=UTC)
        session.add(
            JobHistory(
                id=run_id,
                job_name=MUSIC_GENERATION_JOB_NAME,
                status="running",
                context_type="plan",
                context_id=plan_id,
                started_at=started,
                trigger=trigger,
                target_date=target_date,
            )
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise MusicGenerationBusyError() from exc
        return MusicRunReservation(
            run_id=run_id,
            plan_id=plan_id,
            target_date=target_date,
            trigger=trigger,
        )

    async def _insert_skipped(self, session: AsyncSession, *, target_date: date) -> None:
        now = datetime.now(tz=UTC)
        session.add(
            JobHistory(
                id=uuid.uuid4(),
                job_name=MUSIC_GENERATION_JOB_NAME,
                status="skipped",
                started_at=now,
                finished_at=now,
                duration_ms=0,
                trigger="cron",
                target_date=target_date,
                error_message="no approved daily plan for target_date",
            )
        )
        await session.commit()

    @staticmethod
    async def _find_approved_daily_plan(
        session: AsyncSession, *, target_date: date
    ) -> Plan | None:
        stmt = (
            select(Plan)
            .where(
                Plan.cycle == "daily",
                Plan.target_date == target_date,
                Plan.status == "approved",
            )
            .order_by(Plan.approved_at.desc().nullslast(), Plan.created_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()
