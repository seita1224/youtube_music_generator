"""起動時 orphan reconciliation の単体テスト。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.sql.dml import Update

from ymg_backend.infrastructure.db.orphan_reconciliation import (
    OrphanReconcileResult,
    reconcile_orphaned_runs,
)

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, rowcount: int = 0, *, ids: list[Any] | None = None) -> None:
        self.rowcount = rowcount
        self._ids = list(ids or [])

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._ids)


class _FakeSession:
    """``execute(update...)`` の呼び出しを記録する最小セッション。"""

    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)
        self.statements: list[Update] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return self._results.pop(0)


async def test_reconcile_updates_jobs_steps_plans_posts_and_returns_counts() -> None:
    """music running / step running / executing / generating を順に UPDATE し件数を返す。"""
    run_a = object()
    run_b = object()
    session = _FakeSession(
        [
            _Result(ids=[run_a, run_b]),  # job_history returning
            _Result(rowcount=4),  # job_step_events
            _Result(rowcount=1),  # plans
            _Result(rowcount=3),  # posts
        ]
    )

    result = await reconcile_orphaned_runs(session)  # type: ignore[arg-type]

    assert result == OrphanReconcileResult(
        job_history=2, job_step_events=4, plans=1, posts=3
    )
    assert result.total == 10
    assert len(session.statements) == 4
    assert all(isinstance(s, Update) for s in session.statements)


async def test_reconcile_skips_step_update_when_no_orphaned_jobs() -> None:
    """job_history 0 件なら step UPDATE を発行しない。"""
    session = _FakeSession(
        [
            _Result(ids=[]),
            _Result(rowcount=0),
            _Result(rowcount=0),
        ]
    )
    result = await reconcile_orphaned_runs(session)  # type: ignore[arg-type]
    assert result.total == 0
    assert len(session.statements) == 3  # jobs + plans + posts (steps なし)


async def test_reconcile_sets_fatal_message_duration_and_scopes_music(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JobHistory に duration_ms / fatal を入れ、 music_generation に限定する。"""
    captured: list[dict[str, Any]] = []

    original_values = Update.values

    def _spy_values(self: Update, *args: Any, **kwargs: Any) -> Update:
        if kwargs:
            captured.append(dict(kwargs))
        elif args and isinstance(args[0], dict):
            captured.append(dict(args[0]))
        return original_values(self, *args, **kwargs)

    monkeypatch.setattr(Update, "values", _spy_values)
    session = _FakeSession(
        [
            _Result(ids=[object()]),
            _Result(rowcount=1),
            _Result(rowcount=1),
            _Result(rowcount=1),
        ]
    )
    await reconcile_orphaned_runs(session)  # type: ignore[arg-type]

    assert len(captured) == 4
    job_vals, step_vals, plan_vals, post_vals = captured
    assert job_vals["status"] == "failed"
    assert job_vals["error_category"] == "fatal"
    assert "Orphaned by process restart" in job_vals["error_message"]
    assert "duration_ms" in job_vals
    assert step_vals["status"] == "failed"
    assert step_vals["error_category"] == "fatal"
    assert plan_vals == {"status": "failed"}
    assert post_vals["status"] == "failed"
    assert post_vals["error_category"] == "fatal"

    # WHERE に music_generation が含まれること (compile 文字列で確認)。
    job_sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": False}))
    assert "music_generation" in job_sql.lower() or "job_name" in job_sql.lower()
