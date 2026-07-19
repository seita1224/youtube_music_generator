"""migration 003 (music generation jobs) のスキーマ意味論 integration テスト。

実 PostgreSQL が必要。 接続不可なら skip。 検証:

- ``music_generated`` が plan_status / post_status に存在する
- ``job_history.trigger`` / ``target_date`` 列
- ``uq_job_history_music_generation_running`` による single-flight
- ``job_step_events`` FK / index
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

pytestmark = pytest.mark.integration


def _sync_url() -> str:
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(_sync_url())
    try:
        eng.connect().close()
    except OperationalError as exc:
        pytest.skip(f"postgres へ接続できないため skip: {exc}")
    command.upgrade(Config("alembic.ini"), "head")
    return eng


def test_music_generated_enum_values_exist(engine) -> None:
    with engine.connect() as conn:
        plan_vals = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON e.enumtypid = t.oid "
                    "WHERE t.typname = 'plan_status'"
                )
            )
        }
        post_vals = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT enumlabel FROM pg_enum e "
                    "JOIN pg_type t ON e.enumtypid = t.oid "
                    "WHERE t.typname = 'post_status'"
                )
            )
        }
    assert "music_generated" in plan_vals
    assert "music_generated" in post_vals


def test_job_history_has_trigger_and_target_date(engine) -> None:
    with engine.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'job_history'"
                )
            )
        }
    assert "trigger" in cols
    assert "target_date" in cols


def test_single_flight_unique_rejects_second_running_music_generation(engine) -> None:
    """music_generation + running の 2 行目 INSERT は UNIQUE 違反。"""
    run_a = uuid.uuid4()
    run_b = uuid.uuid4()
    now = datetime.now(tz=UTC)
    insert_sql = text(
        """
        INSERT INTO job_history (
            id, job_name, status, started_at, trigger, target_date
        ) VALUES (
            :id, 'music_generation', 'running', :started_at, :trigger, :target_date
        )
        """
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM job_history WHERE job_name = 'music_generation' AND status = 'running'"
            )
        )
        conn.execute(
            insert_sql,
            {
                "id": run_a,
                "started_at": now,
                "trigger": "cron",
                "target_date": date(2026, 7, 11),
            },
        )

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert_sql,
            {
                "id": run_b,
                "started_at": now,
                "trigger": "run_now",
                "target_date": date(2026, 7, 11),
            },
        )

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM job_history WHERE id = :id"),
            {"id": run_a},
        )


def test_job_step_events_fk_and_indexes(engine) -> None:
    run_id = uuid.uuid4()
    event_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM job_history WHERE id = :id"),
            {"id": run_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO job_history (
                    id, job_name, status, started_at, finished_at, trigger, target_date
                ) VALUES (
                    :id, 'music_generation', 'succeeded', :started_at, :finished_at,
                    'run_now', :target_date
                )
                """
            ),
            {
                "id": run_id,
                "started_at": now,
                "finished_at": now,
                "target_date": date(2026, 7, 11),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO job_step_events (
                    id, run_id, step, status, genre, context_type, created_at
                ) VALUES (
                    :id, :run_id, 'music', 'succeeded', 'lofi', 'post', :created_at
                )
                """
            ),
            {
                "id": event_id,
                "run_id": run_id,
                "created_at": now,
            },
        )
        count = conn.execute(
            text("SELECT COUNT(*) FROM job_step_events WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()
        assert count == 1

        indexes = {
            r[0]
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'job_step_events'")
            )
        }
        assert "idx_job_step_events_run_id" in indexes
        assert "idx_job_step_events_created_at" in indexes
        assert "idx_job_step_events_step" in indexes
        assert "idx_job_step_events_status" in indexes

        # CASCADE: job_history 削除で step events も消える
        conn.execute(text("DELETE FROM job_history WHERE id = :id"), {"id": run_id})
        left = conn.execute(
            text("SELECT COUNT(*) FROM job_step_events WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()
        assert left == 0


def test_job_history_trigger_nullable_and_target_date_index(engine) -> None:
    """004: trigger は NULL 可、 target_date / job_name_started_at index がある。"""
    with engine.connect() as conn:
        nullable = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'job_history' AND column_name = 'trigger'"
            )
        ).scalar_one()
        assert nullable == "YES"

        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'job_step_events'"
                )
            )
        }
        assert "payload" in cols

        indexes = {
            r[0]
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'job_history'")
            )
        }
        assert "idx_job_history_target_date" in indexes
        assert "idx_job_history_job_name_started_at" in indexes
