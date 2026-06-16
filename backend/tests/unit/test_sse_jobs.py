"""``GET /jobs/stream`` SSE エンドポイント (api.jobs) の単体テスト (US6)。

無限ストリームのため ``TestClient.stream`` は本環境の Starlette TestClient (1.2.x) では
レスポンス全体をバッファして app の完了を待つ実装で、 ストリームが終わらず deadlock する
(``test_event_bus.py`` の streaming 系がこの理由でハングする)。 本テストは TestClient を介さず
**SSE generator (``_event_stream``) を直接駆動**して契約を検証する:

- 初期スナップショット (``JobHistory`` 写像) が最初に ``data: {json}`` で出る。
- 購読登録がスナップショット送出より先 (契約 (e)) — 最初のフレーム後に
  ``event_bus.subscriber_count()`` が 1。
- 接続後 ``event_bus.publish`` した差分 (``step=music`` / ``genre`` 付き) が後続フレームに出る。
- generator 終了 (``aclose`` = クライアント切断相当) で購読解除されリーク無し (count→0)。
- ``JobHistory`` → ``JobEvent`` 写像 (status 同語彙・step は job_name 流用・context_id str 化)。

認証は ``api.jobs.stream_jobs`` が ``BasicAuthUser`` を引数に持つこと、 ``STEPS`` が契約 (d) の
7 値であることで担保する。 実 DB / 実 LLM / GPU / HTTP は一切起動しない。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from ymg_backend.api.jobs import (
    STEPS,
    _event_stream,
    _format_event,
    _job_history_to_event,
    _load_snapshot,
    router,
    stream_jobs,
)
from ymg_backend.infrastructure.db.models import JobHistory
from ymg_backend.infrastructure.event_bus import EventBus, JobEvent

pytestmark = pytest.mark.unit

_EXPECTED_STEPS = ("cycle", "post", "music", "acoustid", "image", "render", "publish")


def _job_history(**overrides: Any) -> JobHistory:
    """テスト用 ``JobHistory`` 行を生成する (必須列に既定値)。"""
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "job_name": "daily_cycle",
        "status": "succeeded",
        "context_type": "plan",
        "context_id": uuid.uuid4(),
        "error_category": None,
        "error_message": None,
        "started_at": datetime(2026, 6, 16, 9, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 6, 16, 9, 5, tzinfo=UTC),
        "duration_ms": 300_000,
    }
    base.update(overrides)
    return JobHistory(**base)


class _ScalarsResult:
    """``execute(...).scalars().all()`` を満たす最小結果。"""

    def __init__(self, rows: list[JobHistory]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[JobHistory]:
        return list(self._rows)


class _FakeSession:
    """初期スナップショット読み出し専用の in-memory セッション (commit しない)。"""

    def __init__(self, rows: list[JobHistory]) -> None:
        self._rows = rows
        self.executed = False

    async def execute(self, _statement: Any) -> _ScalarsResult:
        self.executed = True
        return _ScalarsResult(self._rows)


# ============================================================================
# JobHistory → JobEvent 写像 (初期スナップショット)
# ============================================================================
def test_job_history_to_event_maps_fields() -> None:
    """``JobHistory`` 行が ``JobEvent`` へ正しく写像される (契約 (e))。"""
    row = _job_history(job_name="daily_cycle", status="succeeded", context_type="plan")
    event = _job_history_to_event(row)

    assert event.status == "succeeded"  # status は同語彙で変換不要。
    assert event.step == "daily_cycle"  # step 列が無いため job_name を流用。
    assert event.job_name == "daily_cycle"
    assert event.genre is None  # JobHistory は genre を持たない。
    assert event.context_type == "plan"
    assert event.context_id == str(row.context_id)  # UUID → str。
    assert row.finished_at is not None
    assert event.timestamp == row.finished_at.isoformat()  # finished_at 優先。


def test_job_history_to_event_uses_started_at_when_unfinished() -> None:
    """``finished_at`` が無い (進行中) 行は ``started_at`` を timestamp に使う。"""
    row = _job_history(status="running", finished_at=None)
    event = _job_history_to_event(row)

    assert event.status == "running"
    assert event.timestamp == row.started_at.isoformat()


def test_job_history_to_event_carries_error_fields() -> None:
    """失敗行は ``error_category`` / ``message`` (error_message) を引き継ぐ。"""
    row = _job_history(status="failed", error_category="fatal", error_message="boom")
    event = _job_history_to_event(row)

    assert event.status == "failed"
    assert event.error_category == "fatal"
    assert event.message == "boom"


async def test_load_snapshot_reads_and_maps_rows() -> None:
    """``_load_snapshot`` が session から行を読み ``JobEvent`` 列へ写像する (commit しない)。"""
    rows = [
        _job_history(status="succeeded"),
        _job_history(status="running", finished_at=None),
    ]
    session = _FakeSession(rows)

    events = await _load_snapshot(session)  # type: ignore[arg-type]

    assert session.executed is True
    assert {e.status for e in events} == {"succeeded", "running"}
    assert all(isinstance(e, JobEvent) for e in events)


# ============================================================================
# _format_event: SSE フレーム整形
# ============================================================================
def test_format_event_is_sse_data_frame() -> None:
    """``JobEvent`` が ``data: {json}\\n\\n`` 形式 (genre 含む) に整形される。"""
    event = JobEvent(
        timestamp="2026-06-16T09:00:00+09:00",
        job_name="daily_cycle",
        step="music",
        status="running",
        genre="lofi",
        context_type="post",
        context_id="0c1f0000-0000-0000-0000-000000000000",
    )
    frame = _format_event(event)

    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame[len("data: ") : -2])
    assert payload["step"] == "music"
    assert payload["status"] == "running"
    assert payload["genre"] == "lofi"  # contract 拡張フィールド。
    assert payload["context_type"] == "post"


# ============================================================================
# _event_stream: 購読 → スナップショット → 差分 → 購読解除
# ============================================================================
async def test_event_stream_emits_snapshot_then_live_diff_and_unsubscribes() -> None:
    """購読登録 → スナップショット送出 → 差分配信 → ``aclose`` で購読解除 (リーク無し)。

    専用 ``EventBus`` を ``monkeypatch`` で差し込み、 module-level singleton に依存しない。
    """
    bus = EventBus()
    snapshot = [
        JobEvent(
            timestamp="2026-06-16T09:05:00+09:00",
            job_name="daily_cycle",
            step="daily_cycle",
            status="succeeded",
        )
    ]
    gen = _event_stream(snapshot, bus=bus)

    # 1) 最初のフレーム = 初期スナップショット。
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert first.startswith("data: ")
    assert json.loads(first[len("data: ") : -2])["status"] == "succeeded"

    # 購読登録はスナップショット送出より先 (契約 (e))。
    assert bus.subscriber_count() == 1

    # 2) 接続後 publish した差分が次フレームで届く。
    bus.publish(
        JobEvent(
            timestamp="",
            job_name="daily_cycle",
            step="music",
            status="running",
            genre="lofi",
        )
    )
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    payload = json.loads(second[len("data: ") : -2])
    assert payload["step"] == "music"
    assert payload["status"] == "running"
    assert payload["genre"] == "lofi"

    # 3) generator を閉じる (= クライアント切断相当) と購読解除される。
    await gen.aclose()
    assert bus.subscriber_count() == 0  # リーク無し。


# ============================================================================
# router / 認証 / STEPS 語彙
# ============================================================================
def test_router_exposes_jobs_stream_route() -> None:
    """``router`` が ``GET /jobs/stream`` を公開する。"""
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/jobs/stream" in paths


def test_stream_endpoint_requires_basic_auth_dependency() -> None:
    """``stream_jobs`` が ``BasicAuthUser`` 引数を持つ (単体マウントでも 401 を強制)。"""
    annotations = stream_jobs.__annotations__
    assert "user" in annotations  # BasicAuthUser (Annotated[str, Depends(require_basic_auth)])。


def test_steps_vocabulary_is_seven_fixed_values() -> None:
    """``STEPS`` が契約 (d) の 7 値・順序であること。"""
    assert tuple(STEPS) == _EXPECTED_STEPS
