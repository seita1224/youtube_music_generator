"""scheduler の単体テスト (infrastructure/scheduler.py + api/scheduler.py, US1 / T087-T088)。

外部依存は一切起動しない真の単体テスト:

- APScheduler は実 ``AsyncIOScheduler`` を **start せず** に add/remove だけ検証する
  (start すると asyncio イベントループ上でジョブが走るため、 enable/disable は fake
  scheduler stub で start/shutdown 呼び出しを観測する)。
- DB は ``execute`` / ``flush`` / ``commit`` を記録する fake セッション (Postgres 不要)。
  Core の ``select`` は app_state フラグ dict から応答、 ``insert`` は対象テーブル名で
  app_state upsert と audit_log を振り分けて記録する。
- ``_run_slot`` の ``get_sessionmaker`` は monkeypatch でダミーセッションメーカに差し替える。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import Insert, Select

from ymg_backend.api import scheduler as api_scheduler
from ymg_backend.domain.errors.errors import (
    FatalError,
    RecoverableError,
    SchedulerHaltError,
)
from ymg_backend.infrastructure import scheduler as infra_scheduler
from ymg_backend.infrastructure.scheduler import DAILY_CYCLE_JOB_IDS, SchedulerService

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.asyncio

_TARGET_DATE = date(2026, 6, 16)


# --- fake DB セッション ------------------------------------------------------------


class _ScalarResult:
    """``execute(select).scalar_one_or_none()`` を満たす最小ラッパ。"""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """app_state フラグの select / upsert と audit_log insert を記録する fake セッション。

    - ``execute(Select)`` は ``flags`` 辞書から該当 key の値を返す (不在は ``None``)。
    - ``execute(Insert into app_state)`` は ``flags`` を更新する (upsert)。
    - ``execute(Insert into audit_log)`` は ``audit_rows`` に values を蓄積する。
    """

    def __init__(self, *, flags: dict[str, bool] | None = None) -> None:
        self.flags: dict[str, bool] = dict(flags or {})
        self.audit_rows: list[Mapping[str, Any]] = []
        self.commit_count = 0
        self.flush_count = 0

    async def execute(self, stmt: Any) -> _ScalarResult:
        if isinstance(stmt, Select):
            key = self._select_key(stmt)
            return _ScalarResult(self.flags.get(key))
        if isinstance(stmt, Insert):
            self._apply_insert(stmt)
            return _ScalarResult(None)
        raise AssertionError(f"想定外の statement: {stmt!r}")

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:  # 互換のため (本テストでは未使用)
        pass

    @staticmethod
    def _select_key(stmt: Select[Any]) -> str:
        """``where(app_state.key == <KEY>)`` の右辺リテラルを compile 済み params から取り出す。"""
        params = stmt.compile().params
        for value in params.values():
            if isinstance(value, str):
                return value
        raise AssertionError("select の where から key を解決できません")

    def _apply_insert(self, stmt: Insert) -> None:
        table_name = stmt.table.name
        params = stmt.compile().params
        if table_name == "app_state":
            self.flags[str(params["key"])] = bool(params["value"])
        elif table_name == "audit_log":
            self.audit_rows.append(dict(params))
        else:
            raise AssertionError(f"想定外の insert 先テーブル: {table_name}")


# --- fake scheduler / runner -------------------------------------------------------


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
    """呼び出しを記録する CycleRunner stub。 ``fail`` 指定で例外を送出する。"""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[date] = []
        self._fail = fail

    async def __call__(self, *, target_date: date) -> None:
        self.calls.append(target_date)
        if self._fail is not None:
            raise self._fail


class _SpyNotifier:
    """``notify_error`` 呼び出しを記録する notifier stub。"""

    def __init__(self) -> None:
        self.errors: list[tuple[BaseException, Mapping[str, Any] | None]] = []

    async def notify_error(
        self, exc: BaseException, *, context: Mapping[str, Any] | None = None
    ) -> None:
        self.errors.append((exc, context))


# --- SchedulerService: ジョブ登録 / 除去 --------------------------------------------


def _make_service(
    scheduler: Any, *, runner: _RecordingRunner | None = None, notifier: _SpyNotifier | None = None
) -> SchedulerService:
    return SchedulerService(
        scheduler,
        cycle_runner=runner or _RecordingRunner(),
        notifier=notifier,  # type: ignore[arg-type]
    )


@pytest.mark.fr("FR-070")
async def test_add_daily_jobs_registers_both_slots() -> None:
    """FR-070: ``add_daily_jobs`` で朝 / 夕の 2 slot がジョブ登録される。"""
    sched = _FakeScheduler()
    service = _make_service(sched)

    service.add_daily_jobs()

    assert set(sched.jobs) == set(DAILY_CYCLE_JOB_IDS)
    assert service.registered_job_ids() == DAILY_CYCLE_JOB_IDS


async def test_add_daily_jobs_is_idempotent() -> None:
    """同じジョブを二重登録しても slot 数は 2 のまま (冪等)。"""
    sched = _FakeScheduler()
    service = _make_service(sched)

    service.add_daily_jobs()
    service.add_daily_jobs()

    assert len(sched.jobs) == len(DAILY_CYCLE_JOB_IDS)


async def test_remove_daily_jobs_clears_all() -> None:
    """``remove_daily_jobs`` で全ジョブが除去される (未登録 ID は無視)。"""
    sched = _FakeScheduler()
    service = _make_service(sched)
    service.add_daily_jobs()

    service.remove_daily_jobs()

    assert sched.jobs == {}
    assert service.registered_job_ids() == ()


async def test_enable_starts_scheduler_and_adds_jobs() -> None:
    """``enable`` は未 start の scheduler を start し日次ジョブを登録する。"""
    sched = _FakeScheduler(running=False)
    service = _make_service(sched)

    service.enable()

    assert sched.start_count == 1
    # enable は日次サイクルに加え dryrun リテンション ジョブも登録するため subset 検証。
    assert set(DAILY_CYCLE_JOB_IDS) <= set(sched.jobs)


async def test_enable_does_not_restart_running_scheduler() -> None:
    """既に running の scheduler では start を呼ばずジョブだけ登録する。"""
    sched = _FakeScheduler(running=True)
    service = _make_service(sched)

    service.enable()

    assert sched.start_count == 0
    # enable は日次サイクルに加え dryrun リテンション ジョブも登録するため subset 検証。
    assert set(DAILY_CYCLE_JOB_IDS) <= set(sched.jobs)


async def test_disable_removes_jobs_but_keeps_scheduler() -> None:
    """``disable`` はジョブを外すが scheduler instance は止めない (running 維持)。"""
    sched = _FakeScheduler(running=True)
    service = _make_service(sched)
    service.add_daily_jobs()

    service.disable()

    assert sched.jobs == {}
    assert sched.running is True


# --- SchedulerService._run_slot: ジョブ本体 -----------------------------------------


def _patch_sessionmaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_sessionmaker`` を、 async with でダミーセッションを返すメーカに差し替える。"""

    class _DummySession:
        async def __aenter__(self) -> _DummySession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    def _maker() -> Any:
        return _DummySession()

    monkeypatch.setattr(infra_scheduler, "get_sessionmaker", lambda: _maker)


async def test_run_slot_invokes_cycle_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """cron 発火で当日分の cycle_runner が呼ばれる。"""
    del monkeypatch  # session は CycleRunner 側の責務 (本テストでは不要)。
    runner = _RecordingRunner()
    service = _make_service(_FakeScheduler(), runner=runner)

    await service._run_slot(slot="morning")

    assert len(runner.calls) == 1


@pytest.mark.fr("FR-113")
async def test_run_slot_swallows_error_and_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-113: 非 halt のジョブ内例外は ADR-0028 分類 + Slack 通知し、 scheduler は止めない。

    recoverable 等の通常例外は握り潰してサイクルを中断するのみ(scheduler 継続)。 scheduler の
    自動停止はインフラ級 fatal(:class:`SchedulerHaltError`)に限る(下の halt テスト参照)。
    """
    del monkeypatch
    runner = _RecordingRunner(fail=RecoverableError("boom"))
    notifier = _SpyNotifier()
    sched = _FakeScheduler()
    service = _make_service(sched, runner=runner, notifier=notifier)
    service.add_daily_jobs()

    await service._run_slot(slot="evening")  # 例外が外に漏れないこと

    assert len(notifier.errors) == 1
    assert isinstance(notifier.errors[0][0], RecoverableError)
    assert service.registered_job_ids() == DAILY_CYCLE_JOB_IDS  # scheduler は継続


@pytest.mark.fr("FR-113")
async def test_run_slot_disables_scheduler_on_halt_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-113: SchedulerHaltError(DB 不達等インフラ級 fatal)で scheduler を自動停止する。"""
    del monkeypatch
    runner = _RecordingRunner(fail=SchedulerHaltError("db unreachable (scheduler 停止)"))
    notifier = _SpyNotifier()
    sched = _FakeScheduler()
    service = _make_service(sched, runner=runner, notifier=notifier)
    service.add_daily_jobs()
    assert service.registered_job_ids() == DAILY_CYCLE_JOB_IDS  # 停止前

    await service._run_slot(slot="morning")  # 例外は外に漏れない

    assert service.registered_job_ids() == ()  # disable() で投稿ジョブ除去
    assert len(notifier.errors) == 1
    assert isinstance(notifier.errors[0][0], SchedulerHaltError)


@pytest.mark.fr("FR-113")
async def test_run_slot_keeps_scheduler_on_non_halt_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-113: halt でない通常 FatalError では scheduler を止めない(特定 fatal のみ停止)。"""
    del monkeypatch
    runner = _RecordingRunner(fail=FatalError("non-halting fatal"))
    notifier = _SpyNotifier()
    sched = _FakeScheduler()
    service = _make_service(sched, runner=runner, notifier=notifier)
    service.add_daily_jobs()

    await service._run_slot(slot="morning")

    assert service.registered_job_ids() == DAILY_CYCLE_JOB_IDS  # ジョブは残る(停止しない)
    assert len(notifier.errors) == 1


# --- API: GET /scheduler -----------------------------------------------------------


async def test_get_scheduler_defaults_false_when_unset() -> None:
    """app_state 未設定なら ``enabled=false`` (ADR-0031 既定)。"""
    session = _FakeSession()

    state = await api_scheduler.get_scheduler(session=session)  # type: ignore[arg-type]

    assert state.enabled is False


@pytest.mark.fr("FR-071")
async def test_get_scheduler_reads_persisted_true() -> None:
    """FR-071: app_state に ``scheduler_enabled=true`` があればそれを返す。"""
    session = _FakeSession(flags={"scheduler_enabled": True})

    state = await api_scheduler.get_scheduler(session=session)  # type: ignore[arg-type]

    assert state.enabled is True


# --- API: PUT /scheduler -----------------------------------------------------------


class _FakeAppState:
    def __init__(self, service: Any) -> None:
        self.scheduler_service = service


class _FakeApp:
    def __init__(self, service: Any) -> None:
        self.state = _FakeAppState(service)


class _FakeRequest:
    def __init__(self, service: Any) -> None:
        self.app = _FakeApp(service)


class _SpyService:
    def __init__(self) -> None:
        self.enabled_calls = 0
        self.disabled_calls = 0

    def enable(self) -> None:
        self.enabled_calls += 1

    def disable(self) -> None:
        self.disabled_calls += 1


@pytest.mark.fr("FR-073")
async def test_put_scheduler_enable_audits_and_adds_jobs() -> None:
    """FR-073: false→true 切替で app_state 更新 + audit + service.enable() を行う。"""
    session = _FakeSession(flags={"scheduler_enabled": False})
    spy = _SpyService()
    request = _FakeRequest(spy)

    state = await api_scheduler.set_scheduler(
        body=api_scheduler.SchedulerToggleRequest(enabled=True),
        request=request,  # type: ignore[arg-type]
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.enabled is True
    assert session.flags["scheduler_enabled"] is True
    assert session.commit_count == 1
    assert spy.enabled_calls == 1
    assert spy.disabled_calls == 0
    # audit_log に scheduler_enabled が記録される。
    assert session.audit_rows[0]["action"] == "scheduler_enabled"
    assert session.audit_rows[0]["actor"] == "seita"


async def test_put_scheduler_disable_audits_and_removes_jobs() -> None:
    """true→false 切替で audit + service.disable() を行う。"""
    session = _FakeSession(flags={"scheduler_enabled": True})
    spy = _SpyService()
    request = _FakeRequest(spy)

    state = await api_scheduler.set_scheduler(
        body=api_scheduler.SchedulerToggleRequest(enabled=False),
        request=request,  # type: ignore[arg-type]
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.enabled is False
    assert session.flags["scheduler_enabled"] is False
    assert spy.disabled_calls == 1
    assert session.audit_rows[0]["action"] == "scheduler_disabled"


async def test_put_scheduler_same_value_is_noop() -> None:
    """同値要求は audit もジョブ操作もしない no-op。"""
    session = _FakeSession(flags={"scheduler_enabled": True})
    spy = _SpyService()
    request = _FakeRequest(spy)

    state = await api_scheduler.set_scheduler(
        body=api_scheduler.SchedulerToggleRequest(enabled=True),
        request=request,  # type: ignore[arg-type]
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.enabled is True
    assert session.audit_rows == []
    assert session.commit_count == 0
    assert spy.enabled_calls == 0


async def test_put_scheduler_without_wired_service_persists_flag() -> None:
    """scheduler_service 未配線でもフラグ永続化と audit は行う (ジョブ操作のみ skip)。"""
    session = _FakeSession(flags={"scheduler_enabled": False})
    request = _FakeRequest(None)  # service 未配線

    state = await api_scheduler.set_scheduler(
        body=api_scheduler.SchedulerToggleRequest(enabled=True),
        request=request,  # type: ignore[arg-type]
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.enabled is True
    assert session.flags["scheduler_enabled"] is True
    assert session.audit_rows[0]["action"] == "scheduler_enabled"


# --- API: PUT /scheduler/mode (ADR-0035 dryrun 切替) --------------------------------


@pytest.mark.fr("FR-060")
async def test_put_mode_disable_dryrun_audits_dryrun_disabled() -> None:
    """FR-060: dryrun true→false (投稿開始) は audit action=dryrun_disabled で記録する (ADR-0035)。"""
    session = _FakeSession(flags={"dryrun_enabled": True})

    state = await api_scheduler.set_mode(
        body=api_scheduler.ModeToggleRequest(dryrun_enabled=False),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.dryrun_enabled is False
    assert session.flags["dryrun_enabled"] is False
    assert session.commit_count == 1
    row = session.audit_rows[0]
    assert row["action"] == "dryrun_disabled"
    assert row["payload"] == {"actor": "seita", "from": True, "to": False}


async def test_put_mode_enable_dryrun_audits_dryrun_enabled() -> None:
    """dryrun false→true (dryrun 復帰) は audit action=dryrun_enabled で記録する。"""
    session = _FakeSession(flags={"dryrun_enabled": False})

    state = await api_scheduler.set_mode(
        body=api_scheduler.ModeToggleRequest(dryrun_enabled=True),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.dryrun_enabled is True
    assert session.audit_rows[0]["action"] == "dryrun_enabled"


async def test_put_mode_default_dryrun_true_same_value_noop() -> None:
    """未設定 (既定 dryrun=true) に対し true 要求は no-op (audit なし)。"""
    session = _FakeSession()  # dryrun_enabled 未設定 → 既定 True

    state = await api_scheduler.set_mode(
        body=api_scheduler.ModeToggleRequest(dryrun_enabled=True),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.dryrun_enabled is True
    assert session.audit_rows == []
    assert session.commit_count == 0


# --- API: POST /scheduler/run-now --------------------------------------------------


class _FakeAppStateWithRunner:
    def __init__(self, *, music_run_service: Any = None, scheduler_service: Any = None) -> None:
        self.music_run_service = music_run_service
        self.scheduler_service = scheduler_service
        self.cycle_runner = None


class _FakeAppWithRunner:
    def __init__(self, *, music_run_service: Any = None, scheduler_service: Any = None) -> None:
        self.state = _FakeAppStateWithRunner(
            music_run_service=music_run_service, scheduler_service=scheduler_service
        )


class _FakeRequestWithRunner:
    def __init__(self, *, music_run_service: Any = None, scheduler_service: Any = None) -> None:
        self.app = _FakeAppWithRunner(
            music_run_service=music_run_service, scheduler_service=scheduler_service
        )


class _StubMusicRunService:
    def __init__(
        self,
        *,
        reservation: Any = None,
        fail: Exception | None = None,
    ) -> None:
        self.reservation = reservation
        self.fail = fail
        self.execute_calls: list[tuple[Any, Any]] = []
        self.finalize_calls: list[dict[str, Any]] = []

    async def reserve_run_now(self, session: Any, *, plan_id: Any) -> Any:
        del session
        if self.fail is not None:
            raise self.fail
        assert self.reservation is not None
        assert self.reservation.plan_id == plan_id
        return self.reservation

    async def execute(self, *, run_id: Any, plan_id: Any) -> None:
        self.execute_calls.append((run_id, plan_id))

    async def finalize(
        self,
        *,
        run_id: Any,
        status: str,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.finalize_calls.append(
            {
                "run_id": run_id,
                "status": status,
                "error_category": error_category,
                "error_message": error_message,
            }
        )


async def test_run_now_returns_503_when_music_run_service_unwired() -> None:
    """music_run_service 未配線なら 503。"""
    from fastapi import HTTPException

    request = _FakeRequestWithRunner(music_run_service=None)
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc_info:
        await api_scheduler.run_scheduler_now(
            request=request,  # type: ignore[arg-type]
            body=api_scheduler.RunNowRequest(plan_id=uuid.uuid4()),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 503


async def test_run_now_accepts_and_returns_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配線済みなら 202 相当の accepted + run_id / plan_id / target_date を返す。"""
    from unittest.mock import MagicMock

    from ymg_backend.domain.pipeline.music_run import MusicRunReservation

    plan_id = uuid.uuid4()
    run_id = uuid.uuid4()
    reservation = MusicRunReservation(
        run_id=run_id,
        plan_id=plan_id,
        target_date=date(2026, 7, 11),
        trigger="run_now",
    )
    service = _StubMusicRunService(reservation=reservation)
    request = _FakeRequestWithRunner(music_run_service=service)
    session = _FakeSession()
    scheduled: list[Any] = []

    def _capture_create_task(coro: Any, *, name: str | None = None) -> Any:
        del name
        scheduled.append(coro)
        coro.close()
        return MagicMock()

    monkeypatch.setattr(api_scheduler.asyncio, "create_task", _capture_create_task)

    result = await api_scheduler.run_scheduler_now(
        request=request,  # type: ignore[arg-type]
        body=api_scheduler.RunNowRequest(plan_id=plan_id),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert result.accepted is True
    assert result.run_id == run_id
    assert result.plan_id == plan_id
    assert result.target_date == date(2026, 7, 11)
    assert len(scheduled) == 1
    assert len(session.audit_rows) == 1
    audit = session.audit_rows[0]
    assert audit["action"] == "scheduler_run_now"
    assert audit["actor"] == "seita"
    assert str(plan_id) in str(audit.get("target_id", ""))
    assert str(run_id) in str(audit.get("payload", {}))


async def test_run_now_returns_409_when_busy() -> None:
    """別の music_generation が running なら 409。"""
    from fastapi import HTTPException

    from ymg_backend.domain.pipeline.music_run import MusicGenerationBusyError

    service = _StubMusicRunService(fail=MusicGenerationBusyError())
    request = _FakeRequestWithRunner(music_run_service=service)
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc_info:
        await api_scheduler.run_scheduler_now(
            request=request,  # type: ignore[arg-type]
            body=api_scheduler.RunNowRequest(plan_id=uuid.uuid4()),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert "別の音楽生成が実行中" in str(exc_info.value.detail)


async def test_run_now_returns_409_when_not_approved() -> None:
    """未承認 Plan は 409。"""
    from fastapi import HTTPException

    from ymg_backend.domain.pipeline.music_run import PlanNotApprovedError

    plan_id = uuid.uuid4()
    service = _StubMusicRunService(
        fail=PlanNotApprovedError(plan_id=plan_id, status="generated")
    )
    request = _FakeRequestWithRunner(music_run_service=service)
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc_info:
        await api_scheduler.run_scheduler_now(
            request=request,  # type: ignore[arg-type]
            body=api_scheduler.RunNowRequest(plan_id=plan_id),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409


async def test_run_now_returns_404_when_plan_missing() -> None:
    """Plan 不在は 404。"""
    from fastapi import HTTPException

    from ymg_backend.domain.pipeline.music_run import PlanNotFoundError

    service = _StubMusicRunService(fail=PlanNotFoundError("missing"))
    request = _FakeRequestWithRunner(music_run_service=service)
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc_info:
        await api_scheduler.run_scheduler_now(
            request=request,  # type: ignore[arg-type]
            body=api_scheduler.RunNowRequest(plan_id=uuid.uuid4()),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 404


async def test_execute_run_now_calls_music_run_service() -> None:
    """``_execute_run_now`` が MusicRunService.execute を呼ぶ。"""
    service = _StubMusicRunService()
    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    await api_scheduler._execute_run_now(service, run_id=run_id, plan_id=plan_id)  # type: ignore[arg-type]

    assert service.execute_calls == [(run_id, plan_id)]


async def test_run_now_finalizes_reservation_when_audit_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """予約 commit 後に audit commit が失敗したら finalize(failed) で single-flight を解放する。

    ``reserve_run_now`` は ``job_history(running)`` を既に commit 済みのため、
    audit 失敗で HTTP エラーに落ちると running 行が残り全 run が 409 になる。
    """
    from ymg_backend.domain.pipeline.music_run import MusicRunReservation

    plan_id = uuid.uuid4()
    run_id = uuid.uuid4()
    reservation = MusicRunReservation(
        run_id=run_id,
        plan_id=plan_id,
        target_date=date(2026, 7, 11),
        trigger="run_now",
    )
    service = _StubMusicRunService(reservation=reservation)
    request = _FakeRequestWithRunner(music_run_service=service)
    session = _FakeSession()
    scheduled: list[Any] = []

    def _capture_create_task(coro: Any, *, name: str | None = None) -> Any:
        del name
        scheduled.append(coro)
        coro.close()
        return object()

    async def _failing_commit() -> None:
        raise RuntimeError("audit commit failed")

    monkeypatch.setattr(api_scheduler.asyncio, "create_task", _capture_create_task)
    monkeypatch.setattr(session, "commit", _failing_commit)

    with pytest.raises(RuntimeError, match="audit commit failed"):
        await api_scheduler.run_scheduler_now(
            request=request,  # type: ignore[arg-type]
            body=api_scheduler.RunNowRequest(plan_id=plan_id),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert scheduled == []  # execute は起動しない。
    assert len(service.finalize_calls) == 1
    fin = service.finalize_calls[0]
    assert fin["run_id"] == run_id
    assert fin["status"] == "failed"
    assert fin["error_category"] == "fatal"
    assert "audit_log write failed" in str(fin["error_message"])


async def test_run_now_finalizes_reservation_when_audit_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``write_audit_log`` 自体が失敗しても finalize(failed) で枠を解放する。"""
    from ymg_backend.domain.pipeline.music_run import MusicRunReservation

    plan_id = uuid.uuid4()
    run_id = uuid.uuid4()
    reservation = MusicRunReservation(
        run_id=run_id,
        plan_id=plan_id,
        target_date=date(2026, 7, 11),
        trigger="run_now",
    )
    service = _StubMusicRunService(reservation=reservation)
    request = _FakeRequestWithRunner(music_run_service=service)
    session = _FakeSession()

    async def _boom_audit(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(api_scheduler, "write_audit_log", _boom_audit)

    with pytest.raises(RuntimeError, match="audit insert failed"):
        await api_scheduler.run_scheduler_now(
            request=request,  # type: ignore[arg-type]
            body=api_scheduler.RunNowRequest(plan_id=plan_id),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert len(service.finalize_calls) == 1
    assert service.finalize_calls[0]["run_id"] == run_id
    assert service.finalize_calls[0]["status"] == "failed"
