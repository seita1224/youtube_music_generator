"""音楽専用オーケストレータ / JobProgressRecorder / MusicRunService の単体テスト。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from ymg_backend.domain.errors.errors import RecoverableError, SchedulerHaltError
from ymg_backend.domain.pipeline.music_generation import MusicGenerationOrchestrator
from ymg_backend.domain.pipeline.music_run import (
    MusicGenerationBusyError,
    MusicRunService,
    PlanNotApprovedError,
    PlanNotFoundError,
)
from ymg_backend.domain.plans.schemas import DailyPlan, DailyPost, ReferencedMetrics
from ymg_backend.infrastructure.db.models import (
    MUSIC_GENERATION_JOB_NAME,
    AudioTrack,
    JobHistory,
    JobStepEvent,
    Plan,
    Post,
)
from ymg_backend.infrastructure.event_bus import EventBus
from ymg_backend.infrastructure.job_progress import JobProgressRecorder

pytestmark = pytest.mark.asyncio

_GENRE = "lo-fi hip-hop"
_TARGET = date(2026, 7, 11)


def _daily_post() -> DailyPost:
    return DailyPost(
        genre=_GENRE,
        mood="rainy late-night lounge",
        bpm_range=(70, 90),
        visual_direction="rain on a neon window, warm desk lamp, lo-fi study room",
        title_directive="Lo-Fi Hip Hop {{duration}}min | {{12字以内の日本語サブタイトル}}",
        description_directive="夜のチル作業用 BGM。 {{シーン説明を一文で}} #lofi #chill",
        thumbnail_directive=None,
        schedule_jst=None,
    )


def _daily_plan_payload(*, plan_id: str) -> dict[str, Any]:
    plan = DailyPlan(
        plan_id=plan_id,
        target_date=_TARGET,
        posts=[_daily_post()],
        rationale="retention が高い lo-fi 帯を厚めに当て、 雨夜ムードで滞在時間を伸ばす。",
        referenced_metrics=ReferencedMetrics(
            window_days=14,
            sample_size=20,
            top_metrics_summary="lo-fi 帯の平均視聴維持率が他ジャンルより 8pt 高い傾向。",
        ),
    )
    return plan.model_dump(mode="json")


def _approved_plan(*, status: str = "approved") -> Plan:
    plan_id = uuid.uuid4()
    return Plan(
        id=plan_id,
        cycle="daily",
        target_date=_TARGET,
        payload=_daily_plan_payload(plan_id=str(plan_id)),
        rationale="retention が高い lo-fi 帯を厚めに当て、 雨夜ムードで滞在時間を伸ばす。",
        status=status,
        llm_provider="ollama",
        llm_model="qwen2.5:3b",
        llm_prompt_version="v1",
        approved_at=datetime(2026, 7, 10, tzinfo=UTC),
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
    )


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(
        self,
        *,
        plan: Plan | None = None,
        genres: list[str] | None = None,
        fail_commit_with: Exception | None = None,
        job_by_id: dict[uuid.UUID, JobHistory] | None = None,
    ) -> None:
        self.plan = plan
        self.genres = genres if genres is not None else [_GENRE]
        self.added: list[Any] = []
        self.commit_count = 0
        self._fail_commit_with = fail_commit_with
        self._job_by_id = job_by_id or {}
        self._plans_for_select: list[Plan] = [plan] if plan is not None else []

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, JobHistory):
            self._job_by_id[obj.id] = obj

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        if self._fail_commit_with is not None:
            exc = self._fail_commit_with
            self._fail_commit_with = None
            raise exc
        self.commit_count += 1

    async def rollback(self) -> None:
        return None

    async def get(self, model: type[Any], pk: Any) -> Any:
        if model is Plan and self.plan is not None and self.plan.id == pk:
            return self.plan
        if model is JobHistory:
            return self._job_by_id.get(pk)
        return None

    async def execute(self, stmt: Any) -> _ScalarResult:
        # Genre select or Plan select for cron
        text = str(stmt)
        if "genres" in text.lower() or "Genre" in text:
            return _ScalarResult(self.genres)
        return _ScalarResult(self._plans_for_select)

    def added_of(self, model: type[Any]) -> list[Any]:
        return [o for o in self.added if isinstance(o, model)]


class _StubMusic:
    def __init__(self, *, track_count: int = 6, fail: Exception | None = None) -> None:
        self.track_count = track_count
        self.fail = fail
        self.calls = 0

    async def submit_and_wait(
        self, *, session: Any, post: Post, daily_post: Any, track_count: int = 6
    ) -> list[AudioTrack]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return [
            AudioTrack(
                id=uuid.uuid4(),
                post_id=post.id,
                position=i,
                audio_uri=f"file:///tmp/t{i}.wav",
                duration_sec=180,
                acoustid_status="not_checked",
                regenerated_count=0,
            )
            for i in range(track_count)
        ]


class _RecordingProgress:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


# --- JobProgressRecorder -----------------------------------------------------------


async def test_job_progress_recorder_writes_then_publishes() -> None:
    """短い TX で JobStepEvent を書き、 EventBus へ publish する。"""
    bus = EventBus()
    added: list[Any] = []

    class _Sess:
        def add(self, obj: Any) -> None:
            added.append(obj)

        async def commit(self) -> None:
            return None

        async def __aenter__(self) -> _Sess:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    def _factory() -> _Sess:
        return _Sess()

    recorder = JobProgressRecorder(_factory, bus=bus, job_name=MUSIC_GENERATION_JOB_NAME)  # type: ignore[arg-type]
    run_id = uuid.uuid4()
    received: list[Any] = []

    async with bus.subscribe() as sub:
        await recorder.record(
            run_id=run_id,
            step="music",
            status="running",
            genre=_GENRE,
            context_type="post",
            context_id=uuid.uuid4(),
        )
        event = await asyncio_wait_event(sub)
        received.append(event)

    assert len(added) == 1
    assert isinstance(added[0], JobStepEvent)
    assert added[0].step == "music"
    assert received[0].run_id == str(run_id)
    assert received[0].step == "music"
    assert received[0].status == "running"


async def asyncio_wait_event(sub: Any) -> Any:
    return await sub.__anext__()


# --- MusicGenerationOrchestrator ---------------------------------------------------


async def test_music_orchestrator_marks_plan_and_post_music_generated() -> None:
    plan = _approved_plan()
    session = _FakeSession(plan=plan)
    progress = _RecordingProgress()
    orch = MusicGenerationOrchestrator(music=_StubMusic(), progress=progress)  # type: ignore[arg-type]
    run_id = uuid.uuid4()

    result = await orch.run(session=session, plan_id=plan.id, run_id=run_id)

    assert result.failed_count == 0
    assert result.succeeded_count == 1
    assert plan.status == "music_generated"
    posts = session.added_of(Post)
    assert len(posts) == 1
    assert posts[0].status == "music_generated"
    steps = [e["step"] for e in progress.events]
    assert steps.count("cycle") == 2
    assert steps.count("post") == 2
    assert steps.count("music") == 2
    assert progress.events[0]["status"] == "running"
    assert progress.events[-1]["status"] == "succeeded"


async def test_music_orchestrator_records_failed_steps_on_music_error() -> None:
    plan = _approved_plan()
    session = _FakeSession(plan=plan)
    progress = _RecordingProgress()
    orch = MusicGenerationOrchestrator(
        music=_StubMusic(fail=RecoverableError("gpu down")),
        progress=progress,  # type: ignore[arg-type]
    )
    run_id = uuid.uuid4()

    result = await orch.run(session=session, plan_id=plan.id, run_id=run_id)

    assert result.failed_count == 1
    assert plan.status == "failed"
    posts = session.added_of(Post)
    assert posts[0].status == "failed"
    failed = [e for e in progress.events if e["status"] == "failed"]
    assert {e["step"] for e in failed} >= {"music", "post", "cycle"}


async def test_music_orchestrator_terminalizes_on_scheduler_halt() -> None:
    """SchedulerHaltError でも Plan / cycle / post / music を failed にしてから再送出する。"""
    plan = _approved_plan()
    session = _FakeSession(plan=plan)
    progress = _RecordingProgress()
    orch = MusicGenerationOrchestrator(
        music=_StubMusic(fail=SchedulerHaltError("db unreachable")),
        progress=progress,  # type: ignore[arg-type]
    )
    run_id = uuid.uuid4()

    with pytest.raises(SchedulerHaltError):
        await orch.run(session=session, plan_id=plan.id, run_id=run_id)

    assert plan.status == "failed"
    posts = session.added_of(Post)
    assert posts[0].status == "failed"
    failed = [e for e in progress.events if e["status"] == "failed"]
    assert {e["step"] for e in failed} >= {"music", "post", "cycle"}
    assert not any(
        e["status"] == "running"
        and e["step"] in {"cycle", "post", "music"}
        and not any(
            t["step"] == e["step"] and t["status"] == "failed" for t in progress.events
        )
        for e in progress.events
        if e["status"] == "running"
    )


async def test_music_orchestrator_terminalizes_on_post_create_error() -> None:
    """Post 作成 commit 失敗でも Plan / cycle を failed にしてから再送出する。"""
    from sqlalchemy.exc import OperationalError

    plan = _approved_plan()
    session = _FakeSession(plan=plan)
    fail_on = {"n": 0}

    async def _selective_commit() -> None:
        fail_on["n"] += 1
        if fail_on["n"] == 2:  # 1=executing, 2=create_post
            raise OperationalError("stmt", {}, Exception("db down"))
        session.commit_count += 1

    session.commit = _selective_commit  # type: ignore[method-assign]
    progress = _RecordingProgress()
    orch = MusicGenerationOrchestrator(music=_StubMusic(), progress=progress)  # type: ignore[arg-type]
    run_id = uuid.uuid4()

    with pytest.raises(SchedulerHaltError):
        await orch.run(session=session, plan_id=plan.id, run_id=run_id)

    assert plan.status == "failed"
    cycle_failed = [
        e for e in progress.events if e["step"] == "cycle" and e["status"] == "failed"
    ]
    assert len(cycle_failed) == 1


# --- MusicRunService reservation ---------------------------------------------------


async def test_reserve_run_now_rejects_missing_plan() -> None:
    session = _FakeSession(plan=None)
    service = MusicRunService(
        orchestrator=MagicMock(),
        session_factory=MagicMock(),
    )
    with pytest.raises(PlanNotFoundError):
        await service.reserve_run_now(session, plan_id=uuid.uuid4())  # type: ignore[arg-type]


async def test_reserve_run_now_rejects_non_approved() -> None:
    plan = _approved_plan(status="generated")
    session = _FakeSession(plan=plan)
    service = MusicRunService(orchestrator=MagicMock(), session_factory=MagicMock())
    with pytest.raises(PlanNotApprovedError):
        await service.reserve_run_now(session, plan_id=plan.id)  # type: ignore[arg-type]


async def test_reserve_run_now_commits_running_row() -> None:
    plan = _approved_plan()
    session = _FakeSession(plan=plan)
    service = MusicRunService(orchestrator=MagicMock(), session_factory=MagicMock())

    reservation = await service.reserve_run_now(session, plan_id=plan.id)  # type: ignore[arg-type]

    assert reservation.plan_id == plan.id
    assert reservation.target_date == _TARGET
    assert reservation.trigger == "run_now"
    jobs = session.added_of(JobHistory)
    assert len(jobs) == 1
    assert jobs[0].status == "running"
    assert jobs[0].job_name == MUSIC_GENERATION_JOB_NAME
    assert session.commit_count == 1


async def test_reserve_run_now_busy_on_integrity_error() -> None:
    plan = _approved_plan()
    session = _FakeSession(plan=plan, fail_commit_with=IntegrityError("dup", None, None))
    service = MusicRunService(orchestrator=MagicMock(), session_factory=MagicMock())
    with pytest.raises(MusicGenerationBusyError):
        await service.reserve_run_now(session, plan_id=plan.id)  # type: ignore[arg-type]


async def test_run_cron_skips_when_no_approved_plan() -> None:
    session = _FakeSession(plan=None)
    session._plans_for_select = []

    class _SessCtx:
        async def __aenter__(self) -> _FakeSession:
            return session

        async def __aexit__(self, *_: object) -> None:
            return None

    service = MusicRunService(
        orchestrator=MagicMock(),
        session_factory=lambda: _SessCtx(),  # type: ignore[arg-type,return-value]
    )

    await service.run_cron(target_date=_TARGET)

    jobs = session.added_of(JobHistory)
    assert len(jobs) == 1
    assert jobs[0].status == "skipped"
    assert jobs[0].trigger == "cron"


async def test_run_cron_skips_when_busy() -> None:
    plan = _approved_plan()
    session = _FakeSession(plan=plan, fail_commit_with=IntegrityError("dup", None, None))
    session._plans_for_select = [plan]
    orch = MagicMock()
    orch.run = AsyncMock()

    class _SessCtx:
        async def __aenter__(self) -> _FakeSession:
            return session

        async def __aexit__(self, *_: object) -> None:
            return None

    service = MusicRunService(
        orchestrator=orch,
        session_factory=lambda: _SessCtx(),  # type: ignore[arg-type,return-value]
    )

    await service.run_cron(target_date=_TARGET)

    orch.run.assert_not_called()


async def test_run_cron_closes_reservation_session_before_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """予約用 session を閉じてから execute する (GPU 中にコネクションを握らない)。"""
    plan = _approved_plan()
    session = _FakeSession(plan=plan)
    session._plans_for_select = [plan]
    exited = {"reservation": False}
    execute_called_after_exit = {"ok": False}

    class _SessCtx:
        async def __aenter__(self) -> _FakeSession:
            return session

        async def __aexit__(self, *_: object) -> None:
            exited["reservation"] = True

    service = MusicRunService(
        orchestrator=MagicMock(),
        session_factory=lambda: _SessCtx(),  # type: ignore[arg-type,return-value]
    )

    async def _fake_execute(
        self: MusicRunService, *, run_id: uuid.UUID, plan_id: uuid.UUID
    ) -> None:
        del self, run_id, plan_id
        execute_called_after_exit["ok"] = exited["reservation"]

    monkeypatch.setattr(MusicRunService, "execute", _fake_execute)
    await service.run_cron(target_date=_TARGET)
    assert execute_called_after_exit["ok"] is True


async def test_finalize_sets_terminal_status() -> None:
    run_id = uuid.uuid4()
    started = datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    row = JobHistory(
        id=run_id,
        job_name=MUSIC_GENERATION_JOB_NAME,
        status="running",
        started_at=started,
        trigger="run_now",
        target_date=_TARGET,
    )

    class _Sess:
        async def get(self, model: type[Any], pk: Any) -> Any:
            return row if model is JobHistory and pk == run_id else None

        async def commit(self) -> None:
            return None

        async def __aenter__(self) -> _Sess:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    service = MusicRunService(
        orchestrator=MagicMock(),
        session_factory=lambda: _Sess(),  # type: ignore[arg-type,return-value]
    )
    await service.finalize(run_id=run_id, status="succeeded")
    assert row.status == "succeeded"
    assert row.finished_at is not None
    assert row.duration_ms is not None
