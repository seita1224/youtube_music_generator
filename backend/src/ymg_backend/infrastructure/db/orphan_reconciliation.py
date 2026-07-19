"""起動時の孤立実行状態の整合 (migration-models / 音楽生成進捗基盤)。

プロセス再起動後に残る中間状態を失敗確定する:

- ``job_history`` (``job_name='music_generation'`` かつ ``status='running'``)
  → ``failed`` + ``duration_ms``
- 上記 run に紐づく ``job_step_events.status='running'`` → ``failed``
- ``plans.status = 'executing'`` → ``failed``
- ``posts.status = 'generating'`` → ``failed``

いずれも ``error_category=fatal`` と固定メッセージを付与する。
``main.py`` lifespan が DB 接続検証の直後に呼ぶ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from loguru import logger
from sqlalchemy import text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.infrastructure.db.models import JobHistory, JobStepEvent, Plan, Post
from ymg_backend.infrastructure.db.models.job_history import MUSIC_GENERATION_JOB_NAME

_ORPHAN_MESSAGE: Final[str] = "Orphaned by process restart; marked failed at startup reconciliation"
_ERROR_CATEGORY: Final[str] = "fatal"


def _rowcount(result: object) -> int:
    """``CursorResult.rowcount`` を安全に取り出す (stub 上は Result に無い)。"""
    if isinstance(result, CursorResult):
        return int(result.rowcount or 0)
    return int(getattr(result, "rowcount", 0) or 0)


@dataclass(frozen=True, slots=True)
class OrphanReconcileResult:
    """整合で更新した行数。"""

    job_history: int
    job_step_events: int
    plans: int
    posts: int

    @property
    def total(self) -> int:
        return self.job_history + self.job_step_events + self.plans + self.posts


async def reconcile_orphaned_runs(session: AsyncSession) -> OrphanReconcileResult:
    """孤立した music_generation running / executing / generating を failed に確定する。

    Args:
        session: 呼び出し側が commit する ``AsyncSession``。

    Returns:
        更新件数。 0 件なら no-op。
    """
    now = datetime.now(tz=UTC)

    jobs_result = await session.execute(
        update(JobHistory)
        .where(
            JobHistory.status == "running",
            JobHistory.job_name == MUSIC_GENERATION_JOB_NAME,
        )
        .values(
            status="failed",
            error_category=_ERROR_CATEGORY,
            error_message=_ORPHAN_MESSAGE,
            finished_at=now,
            # started_at からの経過を DB 側で計算 (アプリ時刻との微小差は許容)。
            duration_ms=text(
                "GREATEST(0, (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - started_at)) * 1000)::integer)"
            ),
        )
        .returning(JobHistory.id)
    )
    orphaned_run_ids = list(jobs_result.scalars().all())

    steps_updated = 0
    if orphaned_run_ids:
        steps_result = await session.execute(
            update(JobStepEvent)
            .where(
                JobStepEvent.status == "running",
                JobStepEvent.run_id.in_(orphaned_run_ids),
            )
            .values(
                status="failed",
                error_category=_ERROR_CATEGORY,
                error_message=_ORPHAN_MESSAGE,
            )
        )
        steps_updated = _rowcount(steps_result)

    plans_result = await session.execute(
        update(Plan)
        .where(Plan.status == "executing")
        .values(
            status="failed",
            # Plan に error_* 列は無い。 status のみ失敗確定する。
        )
    )
    posts_result = await session.execute(
        update(Post)
        .where(Post.status == "generating")
        .values(
            status="failed",
            error_category=_ERROR_CATEGORY,
            error_message=_ORPHAN_MESSAGE,
            updated_at=now,
        )
    )

    result = OrphanReconcileResult(
        job_history=len(orphaned_run_ids),
        job_step_events=steps_updated,
        plans=_rowcount(plans_result),
        posts=_rowcount(posts_result),
    )
    if result.total:
        logger.bind(component="orphan_reconciliation").warning(
            "reconciled orphaned in-flight rows",
            job_history=result.job_history,
            job_step_events=result.job_step_events,
            plans=result.plans,
            posts=result.posts,
        )
    else:
        logger.bind(component="orphan_reconciliation").info("no orphaned in-flight rows")
    return result
