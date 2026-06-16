"""US3 拡張ジョブ (週次改善計画 + analytics 取得) の単体テスト (T108)。

外部依存は一切起動しない真の単体テスト:

- APScheduler は ``add_job`` / ``remove_job`` / ``get_job`` を記録する fake scheduler stub
  (実 ``AsyncIOScheduler`` を start しない)。
- weekly_runner / analytics_runner は呼び出しを記録する引数なし stub (``fail`` で例外送出)。
- notifier は ``notify_error`` 呼び出しを記録する stub。

検証対象 (本タスクで scheduler.py に足した分):

- ``add_weekly_plan_job`` / ``add_analytics_job`` が runner 注入時のみジョブを登録すること
  (未注入なら no-op で既存ジョブを壊さない)。
- ``enable`` が週次ジョブを登録し analytics は登録しないこと (ADR-0035: analytics は投稿
  モードに依らず常時走らせるため enable/disable 外)。
- ``disable`` が週次を外す一方、 analytics は残すこと。
- ジョブ本体 (``_run_weekly_plan`` / ``_run_analytics``) が runner を呼び、 例外を握り潰して
  notifier へ通知すること (1 回の失敗で scheduler を落とさない)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.infrastructure.scheduler import (
    ANALYTICS_JOB_ID,
    DAILY_CYCLE_JOB_IDS,
    WEEKLY_PLAN_JOB_ID,
    SchedulerService,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

pytestmark = pytest.mark.asyncio


# --- fake scheduler / runner / notifier --------------------------------------------


class _FakeScheduler:
    """``add_job`` / ``remove_job`` / ``get_job`` / ``start`` を記録する scheduler stub。"""

    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.jobs: dict[str, Any] = {}
        self.start_count = 0

    def add_job(self, func: Any, *, id: str, **_: Any) -> None:  # APScheduler 互換シグネチャ
        self.jobs[id] = func

    def remove_job(self, job_id: str) -> None:
        del self.jobs[job_id]

    def get_job(self, job_id: str) -> Any:
        return self.jobs.get(job_id)

    def start(self) -> None:
        self.running = True
        self.start_count += 1


class _RecordingRunner:
    """呼び出しを記録する CycleRunner stub (日次サイクル本体の差し替え用)。"""

    def __init__(self) -> None:
        self.calls: list[date] = []

    async def __call__(self, *, session: Any, target_date: date) -> None:
        self.calls.append(target_date)


class _ArglessRunner:
    """引数なし runner stub (weekly / analytics 本体)。 ``fail`` 指定で例外を送出する。"""

    def __init__(self, *, fail: Exception | None = None, result: object = 0) -> None:
        self.call_count = 0
        self._fail = fail
        self._result = result

    async def __call__(self) -> object:
        self.call_count += 1
        if self._fail is not None:
            raise self._fail
        return self._result


class _SpyNotifier:
    """``notify_error`` 呼び出しを記録する notifier stub。"""

    def __init__(self) -> None:
        self.errors: list[tuple[BaseException, Mapping[str, Any] | None]] = []

    async def notify_error(
        self, exc: BaseException, *, context: Mapping[str, Any] | None = None
    ) -> None:
        self.errors.append((exc, context))


def _make_service(
    scheduler: Any,
    *,
    weekly: _ArglessRunner | None = None,
    analytics: _ArglessRunner | None = None,
    notifier: _SpyNotifier | None = None,
) -> SchedulerService:
    return SchedulerService(
        scheduler,
        cycle_runner=_RecordingRunner(),
        notifier=notifier,  # type: ignore[arg-type]
        weekly_runner=weekly,
        analytics_runner=analytics,
    )


# --- add_weekly_plan_job / add_analytics_job ---------------------------------------


async def test_add_weekly_plan_job_registers_when_runner_wired() -> None:
    """weekly_runner 注入時は週次ジョブが 1 件登録される。"""
    sched = _FakeScheduler()
    service = _make_service(sched, weekly=_ArglessRunner())

    service.add_weekly_plan_job()

    assert WEEKLY_PLAN_JOB_ID in sched.jobs


async def test_add_weekly_plan_job_noop_without_runner() -> None:
    """weekly_runner 未注入なら週次ジョブは登録されない (no-op)。"""
    sched = _FakeScheduler()
    service = _make_service(sched, weekly=None)

    service.add_weekly_plan_job()

    assert WEEKLY_PLAN_JOB_ID not in sched.jobs


async def test_add_weekly_plan_job_is_idempotent() -> None:
    """週次ジョブを二重登録しても 1 件のまま (replace_existing 相当)。"""
    sched = _FakeScheduler()
    service = _make_service(sched, weekly=_ArglessRunner())

    service.add_weekly_plan_job()
    service.add_weekly_plan_job()

    assert list(sched.jobs).count(WEEKLY_PLAN_JOB_ID) == 1


async def test_add_analytics_job_registers_when_runner_wired() -> None:
    """analytics_runner 注入時は analytics ジョブが 1 件登録される。"""
    sched = _FakeScheduler()
    service = _make_service(sched, analytics=_ArglessRunner())

    service.add_analytics_job()

    assert ANALYTICS_JOB_ID in sched.jobs


async def test_add_analytics_job_noop_without_runner() -> None:
    """analytics_runner 未注入なら analytics ジョブは登録されない (no-op)。"""
    sched = _FakeScheduler()
    service = _make_service(sched, analytics=None)

    service.add_analytics_job()

    assert ANALYTICS_JOB_ID not in sched.jobs


# --- enable / disable のライフサイクル (ADR-0035: analytics は enable/disable 外) --------


async def test_enable_registers_weekly_but_not_analytics() -> None:
    """enable は週次を登録するが analytics は登録しない (投稿モードに依らず別管理)。"""
    sched = _FakeScheduler(running=False)
    service = _make_service(sched, weekly=_ArglessRunner(), analytics=_ArglessRunner())

    service.enable()

    assert set(DAILY_CYCLE_JOB_IDS) <= set(sched.jobs)
    assert WEEKLY_PLAN_JOB_ID in sched.jobs
    assert ANALYTICS_JOB_ID not in sched.jobs


async def test_disable_removes_weekly_but_keeps_analytics() -> None:
    """disable は週次を外すが analytics は残す (ADR-0035)。"""
    sched = _FakeScheduler(running=True)
    service = _make_service(sched, weekly=_ArglessRunner(), analytics=_ArglessRunner())
    service.enable()
    service.add_analytics_job()  # analytics は enable 外なので明示登録
    assert ANALYTICS_JOB_ID in sched.jobs

    service.disable()

    assert WEEKLY_PLAN_JOB_ID not in sched.jobs
    assert set(DAILY_CYCLE_JOB_IDS).isdisjoint(sched.jobs)
    assert ANALYTICS_JOB_ID in sched.jobs  # 観測ジョブは止めない


# --- ジョブ本体: runner 呼び出し + 例外握り潰し + 通知 ----------------------------------


async def test_run_weekly_plan_invokes_runner() -> None:
    """cron 発火で weekly_runner が 1 回呼ばれる。"""
    runner = _ArglessRunner()
    service = _make_service(_FakeScheduler(), weekly=runner)

    await service._run_weekly_plan()

    assert runner.call_count == 1


async def test_run_analytics_invokes_runner() -> None:
    """cron 発火で analytics_runner が 1 回呼ばれる。"""
    runner = _ArglessRunner(result=3)
    service = _make_service(_FakeScheduler(), analytics=runner)

    await service._run_analytics()

    assert runner.call_count == 1


async def test_run_weekly_plan_swallows_error_and_notifies() -> None:
    """週次ジョブ内例外は握り潰し notifier へ通知する (scheduler を落とさない)。"""
    runner = _ArglessRunner(fail=RecoverableError("weekly boom"))
    notifier = _SpyNotifier()
    service = _make_service(_FakeScheduler(), weekly=runner, notifier=notifier)

    await service._run_weekly_plan()  # 例外が外に漏れないこと

    assert len(notifier.errors) == 1
    assert isinstance(notifier.errors[0][0], RecoverableError)
    assert notifier.errors[0][1] is not None
    assert notifier.errors[0][1]["slot"] == WEEKLY_PLAN_JOB_ID


async def test_run_analytics_swallows_error_and_notifies() -> None:
    """analytics ジョブ内例外は握り潰し notifier へ通知する。"""
    runner = _ArglessRunner(fail=TransientError("analytics 5xx"))
    notifier = _SpyNotifier()
    service = _make_service(_FakeScheduler(), analytics=runner, notifier=notifier)

    await service._run_analytics()

    assert len(notifier.errors) == 1
    assert isinstance(notifier.errors[0][0], TransientError)
    assert notifier.errors[0][1] is not None
    assert notifier.errors[0][1]["slot"] == ANALYTICS_JOB_ID


async def test_run_weekly_plan_error_without_notifier_is_silent() -> None:
    """notifier 未設定でも例外は握り潰されジョブは静かに終わる。"""
    runner = _ArglessRunner(fail=RecoverableError("boom"))
    service = _make_service(_FakeScheduler(), weekly=runner, notifier=None)

    await service._run_weekly_plan()  # 例外も通知失敗も外に漏れない

    assert runner.call_count == 1
