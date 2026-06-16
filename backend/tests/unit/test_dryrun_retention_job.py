"""dryrun リテンション ジョブの単体テスト (domain/dryrun/retention_job.py, US2)。

外部依存は一切起動しない真の単体テスト:

- DB は ``execute`` (select→事前投入行 / insert→audit 記録) と ``flush`` / ``commit`` を
  記録する fake セッション (Postgres 不要)。 sessionmaker は async with でこの fake を返す。
- StorageAdapter は ``delete`` 呼び出しを記録する fake (fsspec 不使用)。
- Slack notifier は ``notify_error`` を記録する spy。
- ``get_settings`` を呼ばないよう ``settings`` / ``storage`` / ``sessionmaker`` を全て注入する。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import Insert, Select

from ymg_backend.domain.dryrun.retention_job import (
    RETENTION_DAYS,
    run_dryrun_retention_job,
)
from ymg_backend.domain.errors.errors import RecoverableError

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 6, 16, 0, 30, tzinfo=UTC)


# --- fake DryrunOutput 行 ----------------------------------------------------------


class _FakeOutput:
    """``state`` / ``auto_expired_at`` を書き換え可能な DryrunOutput 代用。"""

    def __init__(self, *, video_uri: str, created_at: datetime) -> None:
        self.id = uuid.uuid4()
        self.state = "pending"
        self.video_uri = video_uri
        self.created_at = created_at
        self.auto_expired_at: datetime | None = None


# --- fake DB セッション ------------------------------------------------------------


class _ScalarsResult:
    def __init__(self, rows: list[_FakeOutput]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[_FakeOutput]:
        return self._rows


class _FakeSession:
    """select で事前投入行を返し、 audit insert / commit / flush を記録する fake。"""

    def __init__(self, rows: list[_FakeOutput]) -> None:
        self._rows = rows
        self.audit_rows: list[Mapping[str, Any]] = []
        self.commit_count = 0
        self.flush_count = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, stmt: Any) -> _ScalarsResult:
        if isinstance(stmt, Select):
            return _ScalarsResult(self._rows)
        if isinstance(stmt, Insert):
            params = stmt.compile().params
            self.audit_rows.append(dict(params))
            return _ScalarsResult([])
        raise AssertionError(f"想定外の statement: {stmt!r}")

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        pass


class _FakeStorage:
    """``delete`` 呼び出しを記録する StorageAdapter 代用。 ``fail`` で例外を送出。"""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.deleted: list[str] = []
        self._fail = fail

    def delete(self, uri: str, *, missing_ok: bool = False) -> None:
        if self._fail is not None:
            raise self._fail
        self.deleted.append(uri)


class _SpyNotifier:
    def __init__(self) -> None:
        self.errors: list[BaseException] = []

    async def notify_error(
        self, exc: BaseException, *, context: Mapping[str, Any] | None = None
    ) -> None:
        self.errors.append(exc)


def _maker_for(session: _FakeSession) -> Any:
    def _maker() -> _FakeSession:
        return session

    return _maker


# --- happy path --------------------------------------------------------------------


async def test_expires_pending_older_than_retention_days() -> None:
    """7 日超過の pending を auto_expired にし、 動画削除 + audit + commit する。"""
    stale = _FakeOutput(
        video_uri="file:///out/a.mp4",
        created_at=_NOW - timedelta(days=RETENTION_DAYS, seconds=1),
    )
    session = _FakeSession([stale])
    storage = _FakeStorage()

    expired = await run_dryrun_retention_job(
        now=_NOW,
        sessionmaker=_maker_for(session),
        settings=object(),  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
    )

    assert expired == 1
    assert stale.state == "auto_expired"
    assert stale.auto_expired_at == _NOW
    assert storage.deleted == ["file:///out/a.mp4"]
    assert session.commit_count == 1
    assert session.audit_rows[0]["action"] == "dryrun_auto_expired"
    assert session.audit_rows[0]["target_type"] == "dryrun_output"
    assert session.audit_rows[0]["target_id"] == str(stale.id)


async def test_no_eligible_rows_commits_zero() -> None:
    """対象 0 件でも commit され (空トランザクション)、 削除も audit も発生しない。"""
    session = _FakeSession([])
    storage = _FakeStorage()

    expired = await run_dryrun_retention_job(
        now=_NOW,
        sessionmaker=_maker_for(session),
        settings=object(),  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
    )

    assert expired == 0
    assert storage.deleted == []
    assert session.audit_rows == []
    assert session.commit_count == 1


# --- error swallowing --------------------------------------------------------------


async def test_swallows_error_and_notifies() -> None:
    """ジョブ内例外は握り潰し notifier へ通知する (scheduler を落とさない)。"""
    stale = _FakeOutput(
        video_uri="file:///out/b.mp4",
        created_at=_NOW - timedelta(days=RETENTION_DAYS + 1),
    )
    session = _FakeSession([stale])
    storage = _FakeStorage(fail=RecoverableError("disk gone"))
    notifier = _SpyNotifier()

    expired = await run_dryrun_retention_job(
        now=_NOW,
        sessionmaker=_maker_for(session),
        settings=object(),  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
    )

    assert expired == 0  # 失敗時は 0 を返す (例外は外へ漏れない)
    assert session.commit_count == 0
    assert len(notifier.errors) == 1
    assert isinstance(notifier.errors[0], RecoverableError)
