"""音楽生成向け ORM / ENUM / JobHistory・JobStepEvent の単体テスト。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from ymg_backend.infrastructure.db.models import (
    MUSIC_GENERATION_JOB_NAME,
    JobHistory,
    JobStepEvent,
)
from ymg_backend.infrastructure.db.models.base import _ENUM_VALUES

pytestmark = pytest.mark.unit


def test_plan_and_post_status_include_music_generated() -> None:
    """003 追加の ``music_generated`` が ENUM 末尾に含まれる。"""
    assert _ENUM_VALUES["plan_status"][-1] == "music_generated"
    assert "music_generated" in _ENUM_VALUES["plan_status"]
    assert _ENUM_VALUES["post_status"][-1] == "music_generated"
    assert "music_generated" in _ENUM_VALUES["post_status"]


def test_job_history_defaults_trigger_and_accepts_run_metadata() -> None:
    """``trigger`` / ``target_date`` / run_id(=id) を保持できる。"""
    run_id = uuid.uuid4()
    row = JobHistory(
        id=run_id,
        job_name=MUSIC_GENERATION_JOB_NAME,
        status="running",
        started_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
        target_date=date(2026, 7, 11),
        trigger="run_now",
    )
    assert row.id == run_id
    assert row.trigger == "run_now"
    assert row.target_date == date(2026, 7, 11)
    assert row.job_name == MUSIC_GENERATION_JOB_NAME


def test_job_history_accepts_null_trigger_for_non_music() -> None:
    """非 music_generation は trigger=NULL を許容する (ADR-0036)。"""
    row = JobHistory(
        id=uuid.uuid4(),
        job_name="daily_cycle",
        status="succeeded",
        started_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
        trigger=None,
    )
    assert row.trigger is None
    assert row.target_date is None


def test_job_step_event_links_run_and_step_fields() -> None:
    """工程イベントが run_id / step / status / genre / context / error / payload を持つ。"""
    run_id = uuid.uuid4()
    post_id = uuid.uuid4()
    event = JobStepEvent(
        id=uuid.uuid4(),
        run_id=run_id,
        step="music",
        status="failed",
        genre="lofi",
        context_type="post",
        context_id=post_id,
        error_category="fatal",
        error_message="gpu timeout",
        payload={"attempt": 1},
    )
    assert event.run_id == run_id
    assert event.step == "music"
    assert event.status == "failed"
    assert event.genre == "lofi"
    assert event.context_id == post_id
    assert event.error_category == "fatal"
    assert event.payload == {"attempt": 1}
