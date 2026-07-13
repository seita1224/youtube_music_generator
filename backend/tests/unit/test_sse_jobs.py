"""``GET /jobs/stream`` SSE エンドポイント (api.jobs) の単体テスト (US6)。

無限ストリームのため ``TestClient.stream`` は本環境の Starlette TestClient (1.2.x) では
レスポンス全体をバッファして app の完了を待つ実装で、 ストリームが終わらず deadlock する
(``test_event_bus.py`` の streaming 系がこの理由でハングする)。 本テストは TestClient を介さず
**SSE generator (``_event_stream``) を直接駆動**して契約を検証する:

- 初期スナップショット (``JobStepEvent`` 写像) が最初に ``data: {json}`` で出る。
- 購読登録がスナップショット読込・送出より先 — snapshot 読込中に publish した差分も届く。
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
from ymg_backend.infrastructure.db.models import JobHistory, JobStepEvent
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


def _step_event(**overrides: Any) -> JobStepEvent:
    """テスト用 ``JobStepEvent`` 行を生成する。"""
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "step": "cycle",
        "status": "succeeded",
        "genre": None,
        "context_type": None,
        "context_id": None,
        "error_category": None,
        "error_message": None,
        "payload": None,
        "created_at": datetime(2026, 6, 16, 9, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return JobStepEvent(**base)


class _ScalarsResult:
    """``execute(...).scalars().all()`` を満たす最小結果。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """初期スナップショット読み出し専用の in-memory セッション (commit しない)。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.executed = False

    async def execute(self, _statement: Any) -> _ScalarsResult:
        self.executed = True
        return _ScalarsResult(self._rows)


class _ImmediateSessionFactory:
    """``async with factory() as session`` を満たす即時セッション工場。"""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.enter_count = 0

    def __call__(self) -> _ImmediateSessionFactory:
        return self

    async def __aenter__(self) -> _FakeSession:
        self.enter_count += 1
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _GatedSessionFactory:
    """snapshot 読込をゲートし、 購読後〜読込完了前の publish を再現する。"""

    def __init__(
        self,
        session: _FakeSession,
        *,
        load_started: asyncio.Event,
        continue_load: asyncio.Event,
    ) -> None:
        self._session = session
        self._load_started = load_started
        self._continue_load = continue_load
        self.enter_count = 0

    def __call__(self) -> _GatedSessionFactory:
        return self

    async def __aenter__(self) -> _FakeSession:
        self.enter_count += 1
        self._load_started.set()
        await self._continue_load.wait()
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        return None


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

    専用 ``EventBus`` を差し込み、 module-level singleton に依存しない。
    ``run_id`` 一致イベントのみ流す。
    """
    bus = EventBus()
    run_id = uuid.uuid4()
    snap_row = _step_event(
        run_id=run_id,
        step="cycle",
        status="succeeded",
        created_at=datetime(2026, 6, 16, 9, 5, tzinfo=UTC),
    )
    factory = _ImmediateSessionFactory(_FakeSession([snap_row]))
    gen = _event_stream(
        run_id=run_id,
        job_name="music_generation",
        bus=bus,
        session_factory=factory,  # type: ignore[arg-type]
    )

    # 1) 最初のフレーム = 初期スナップショット。
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert first.startswith("data: ")
    assert json.loads(first[len("data: ") : -2])["status"] == "succeeded"
    assert factory.enter_count == 1  # 短い TX で snapshot を読んだ。

    # 購読登録はスナップショット送出より先 (契約 (e))。
    assert bus.subscriber_count() == 1

    # 2) 接続後 publish した差分が次フレームで届く (run_id 一致)。
    bus.publish(
        JobEvent(
            timestamp="",
            job_name="music_generation",
            step="music",
            status="running",
            run_id=str(run_id),
            genre="lofi",
        )
    )
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    payload = json.loads(second[len("data: ") : -2])
    assert payload["step"] == "music"
    assert payload["status"] == "running"
    assert payload["genre"] == "lofi"
    assert payload["run_id"] == str(run_id)

    # 3) 別 run_id のイベントはスキップされる。
    bus.publish(
        JobEvent(
            timestamp="",
            job_name="music_generation",
            step="music",
            status="failed",
            run_id=str(uuid.uuid4()),
        )
    )
    bus.publish(
        JobEvent(
            timestamp="",
            job_name="music_generation",
            step="post",
            status="succeeded",
            run_id=str(run_id),
        )
    )
    third = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert json.loads(third[len("data: ") : -2])["step"] == "post"

    # 4) generator を閉じる (= クライアント切断相当) と購読解除される。
    await gen.aclose()
    assert bus.subscriber_count() == 0  # リーク無し。


async def test_event_stream_does_not_lose_events_published_during_snapshot_load() -> None:
    """subscribe 後・snapshot 読込完了前に publish された差分は取りこぼさない。

    旧実装は snapshot を subscribe 前に固定していたため、 ``T_snap``〜``T_sub`` の
    イベントが snapshot にも live にも載らなかった。 本テストはそのレースを再現する。
    """
    bus = EventBus()
    run_id = uuid.uuid4()
    snap_row = _step_event(run_id=run_id, step="cycle", status="succeeded")
    load_started = asyncio.Event()
    continue_load = asyncio.Event()
    factory = _GatedSessionFactory(
        _FakeSession([snap_row]),
        load_started=load_started,
        continue_load=continue_load,
    )
    gen = _event_stream(
        run_id=run_id,
        job_name="music_generation",
        bus=bus,
        session_factory=factory,  # type: ignore[arg-type]
    )

    async def _drive() -> list[str]:
        frames: list[str] = []
        frames.append(await gen.__anext__())  # snapshot
        frames.append(await gen.__anext__())  # live during-load event
        await gen.aclose()
        return frames

    task = asyncio.create_task(_drive())
    await asyncio.wait_for(load_started.wait(), timeout=1.0)
    assert bus.subscriber_count() == 1  # snapshot 読込中でも既に購読済み。

    bus.publish(
        JobEvent(
            timestamp="2026-06-16T09:00:01+09:00",
            job_name="music_generation",
            step="music",
            status="running",
            run_id=str(run_id),
            genre="lofi",
        )
    )
    continue_load.set()

    frames = await asyncio.wait_for(task, timeout=2.0)
    assert bus.subscriber_count() == 0

    snap_payload = json.loads(frames[0][len("data: ") : -2])
    live_payload = json.loads(frames[1][len("data: ") : -2])
    assert snap_payload["step"] == "cycle"
    assert snap_payload["status"] == "succeeded"
    assert live_payload["step"] == "music"
    assert live_payload["status"] == "running"
    assert live_payload["genre"] == "lofi"
    assert live_payload["run_id"] == str(run_id)


# ============================================================================
# router / 認証 / STEPS 語彙
# ============================================================================
def test_router_exposes_jobs_stream_and_runs_routes() -> None:
    """``router`` が runs / events / stream を公開する。"""
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/jobs/stream" in paths
    assert "/jobs/runs" in paths
    assert "/jobs/runs/{run_id}/events" in paths


def test_stream_endpoint_requires_basic_auth_dependency() -> None:
    """``stream_jobs`` が ``BasicAuthUser`` 引数を持つ (単体マウントでも 401 を強制)。"""
    annotations = stream_jobs.__annotations__
    assert "user" in annotations  # BasicAuthUser (Annotated[str, Depends(require_basic_auth)])。
    assert "run_id" in annotations
    assert "session" not in annotations  # 短い TX は内部で開く (接続中に握らない)。


def test_steps_vocabulary_is_seven_fixed_values() -> None:
    """``STEPS`` が契約 (d) の 7 値・順序であること。"""
    assert tuple(STEPS) == _EXPECTED_STEPS
