"""``GET /jobs/runs`` / events API の単体テスト。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from ymg_backend.api import jobs as jobs_api
from ymg_backend.infrastructure.db.models import JobHistory, JobStepEvent

pytestmark = pytest.mark.asyncio


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    def __init__(
        self,
        *,
        runs: list[JobHistory] | None = None,
        events: list[JobStepEvent] | None = None,
        run: JobHistory | None = None,
    ) -> None:
        self._runs = runs or []
        self._events = events or []
        self._run = run

    async def execute(self, _stmt: Any) -> _ScalarsResult:
        # list runs vs list events — 簡易に両方の候補を返す経路を分ける
        if self._events and self._run is not None:
            return _ScalarsResult(self._events)
        return _ScalarsResult(self._runs)

    async def get(self, model: type[Any], pk: Any) -> Any:
        if model is JobHistory:
            if self._run is not None and self._run.id == pk:
                return self._run
            for row in self._runs:
                if row.id == pk:
                    return row
        return None


def _run(**overrides: Any) -> JobHistory:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "job_name": "music_generation",
        "status": "succeeded",
        "context_type": "plan",
        "context_id": uuid.uuid4(),
        "started_at": datetime(2026, 7, 11, 1, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 7, 11, 1, 5, tzinfo=UTC),
        "duration_ms": 300_000,
        "trigger": "run_now",
        "target_date": date(2026, 7, 11),
    }
    base.update(overrides)
    return JobHistory(**base)


async def test_list_job_runs_maps_items() -> None:
    row = _run()
    session = _FakeSession(runs=[row])
    result = await jobs_api.list_job_runs(
        user="seita",
        session=session,  # type: ignore[arg-type]
        limit=20,
        job_name=None,
    )
    assert len(result.items) == 1
    assert result.items[0].run_id == row.id
    assert result.items[0].plan_id == row.context_id
    assert result.items[0].trigger == "run_now"


async def test_list_job_run_events_404_when_missing() -> None:
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc_info:
        await jobs_api.list_job_run_events(
            run_id=uuid.uuid4(),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 404


async def test_list_job_run_events_returns_items() -> None:
    run = _run()
    event = JobStepEvent(
        id=uuid.uuid4(),
        run_id=run.id,
        step="music",
        status="succeeded",
        genre="lofi",
        created_at=datetime(2026, 7, 11, 1, 1, tzinfo=UTC),
    )
    session = _FakeSession(run=run, events=[event])
    result = await jobs_api.list_job_run_events(
        run_id=run.id,
        user="seita",
        session=session,  # type: ignore[arg-type]
    )
    assert result.run_id == run.id
    assert len(result.items) == 1
    assert result.items[0].step == "music"
