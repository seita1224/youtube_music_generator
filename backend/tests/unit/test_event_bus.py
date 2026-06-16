"""``EventBus`` と SSE ジョブ進捗 endpoint の単体テスト (US6 T120/T121)。

TDD 先行 (実装より先・初回 RED 可)。 対象は未作成の 2 モジュール:

- ``ymg_backend.infrastructure.event_bus``: プロセス内シングルトンの asyncio pub/sub。
  ``EventBus.publish`` は同期・ノンブロッキング・例外を漏らさない。 ``subscribe`` は
  ``async with bus.subscribe() as sub: async for ev in sub: ...`` を満たし、 切断時 (finally)
  に必ず購読解除する (queue リーク無し)。
- ``ymg_backend.api.jobs``: ``GET /jobs/stream`` (``text/event-stream``)。 接続時に
  ``JobHistory`` の初期スナップショットを ``data: {json}\n\n`` で流し、 以降 ``event_bus`` の
  差分イベントを流す。 Basic 認証下 (``require_basic_auth`` override)。

実 DB / 実 LLM / GPU / HTTP は一切起動しない。 セッションは in-memory の :class:`_FakeSession`、
認証は override で固定する (``test_dryrun_api.py`` と同方針)。

検証観点 (EventBus):

- publish したイベントが subscribe 側へ届く。
- 複数購読者へ fan-out する (同一イベントが全 subscriber に配信される)。
- 購読解除 (``async with`` 退出) で subscriber 集合から除去されリークしない。
- 購読者ゼロ / queue 満杯でも publish は例外を投げない (pipeline を止めない)。
- bus が ``timestamp`` を補完する (発行側が渡さなくても ISO8601 文字列が入る)。

検証観点 (SSE endpoint):

- ``GET /jobs/stream`` の content-type が ``text/event-stream``。
- 初期スナップショット (JobHistory) が ``data: {json}\n\n`` 形式で流れる。
- 接続後に ``event_bus.publish`` した差分イベントがストリームに現れる。
- 認証が無ければ 401。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.infrastructure.event_bus import EventBus, JobEvent, event_bus

pytestmark = pytest.mark.unit

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)

# grid 行順 (契約 (d): step 7 値固定)。SSE / publish 側が共有する語彙。
_EXPECTED_STEPS = ("cycle", "post", "music", "acoustid", "image", "render", "publish")


def _event(**overrides: Any) -> JobEvent:
    """テスト用 ``JobEvent`` を生成する (必須項目に既定値を与える)。

    ``timestamp`` は bus が補完する設計なので空文字を既定にし、 補完テスト以外では
    補完後の値を検証しない。
    """
    base: dict[str, Any] = {
        "timestamp": "",
        "job_name": "daily_cycle",
        "step": "music",
        "status": "running",
    }
    base.update(overrides)
    return JobEvent(**base)


# ============================================================================
# EventBus: pub/sub の基本契約
# ============================================================================


async def test_publish_delivers_to_subscriber() -> None:
    """publish したイベントが 1 購読者へ届く。"""
    bus = EventBus()
    async with bus.subscribe() as sub:
        bus.publish(_event(step="music", status="running"))
        received = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert received.step == "music"
    assert received.status == "running"
    assert received.job_name == "daily_cycle"


async def test_publish_fans_out_to_multiple_subscribers() -> None:
    """同一 publish が全 subscriber へ fan-out される。"""
    bus = EventBus()
    async with bus.subscribe() as sub_a, bus.subscribe() as sub_b:
        bus.publish(_event(step="render", status="succeeded", genre="lofi"))
        got_a = await asyncio.wait_for(sub_a.__anext__(), timeout=1.0)
        got_b = await asyncio.wait_for(sub_b.__anext__(), timeout=1.0)

    assert got_a.step == "render" == got_b.step
    assert got_a.status == "succeeded" == got_b.status
    assert got_a.genre == "lofi" == got_b.genre


async def test_unsubscribe_removes_subscriber_no_leak() -> None:
    """``async with`` 退出後は subscriber 集合から除去され、 後続 publish を受けない。

    退出後に publish した分を新規購読者が受け取らないこと (= 古い queue が残っていない)
    で、 リーク無しを間接的に検証する。 さらに bus が subscriber 数を観測できる場合は
    0 に戻ることも確認する。
    """
    bus = EventBus()
    async with bus.subscribe() as sub:
        bus.publish(_event(step="cycle", status="running"))
        first = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert first.step == "cycle"

    # 退出後の publish は誰にも届かず例外も出ない。
    bus.publish(_event(step="cycle", status="succeeded"))

    # subscriber 集合が空に戻っていることを観測 (実装が公開していれば)。
    count = getattr(bus, "subscriber_count", None)
    if callable(count):
        assert count() == 0
    elif count is not None:
        assert count == 0

    # 新規購読者は退出済み購読の取りこぼし分を受け取らない (queue は購読ごとに独立)。
    async with bus.subscribe() as fresh:
        bus.publish(_event(step="image", status="running"))
        got = await asyncio.wait_for(fresh.__anext__(), timeout=1.0)
        assert got.step == "image"  # cycle/succeeded は流れてこない。


async def test_publish_with_no_subscribers_does_not_raise() -> None:
    """購読者ゼロでも publish は例外を投げない (pipeline を絶対に止めない)。"""
    bus = EventBus()
    # 例外が出ないこと自体が assertion。
    bus.publish(_event(step="cycle", status="running"))


async def test_publish_when_queue_full_does_not_raise() -> None:
    """queue 満杯でも publish は例外を漏らさない (put_nowait の Full を握り潰す)。

    購読だけして読み出さないまま大量に publish し、 例外が伝播しないことを確認する。
    """
    bus = EventBus()
    async with bus.subscribe():
        # 読み出さずに大量発行。 満杯になっても publish は静かに捨てる。
        for _ in range(10_000):
            bus.publish(_event(step="music", status="running"))


async def test_bus_fills_timestamp_when_missing() -> None:
    """発行側が timestamp を渡さなくても bus が ISO8601 文字列を補完する。"""
    bus = EventBus()
    async with bus.subscribe() as sub:
        bus.publish(_event(timestamp="", step="post", status="running"))
        got = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert got.timestamp  # 非空。
    # ISO8601 としてパースできる (補完値の形式検証)。
    datetime.fromisoformat(got.timestamp)


def test_module_singleton_is_event_bus_instance() -> None:
    """module-level ``event_bus`` が ``EventBus`` のインスタンス (publish/subscribe 双方が import)。"""
    assert isinstance(event_bus, EventBus)


# ============================================================================
# SSE endpoint: GET /jobs/stream
# ============================================================================


@dataclass
class _FakeJobHistory:
    """ORM ``JobHistory`` の最小スタンドイン (初期スナップショット写像用)。"""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    job_name: str = "daily_cycle"
    status: str = "succeeded"
    context_type: str | None = "plan"
    context_id: uuid.UUID | None = field(default_factory=uuid.uuid4)
    error_category: str | None = None
    error_message: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime(2026, 6, 16, 9, 0, tzinfo=UTC))
    finished_at: datetime | None = field(
        default_factory=lambda: datetime(2026, 6, 16, 9, 5, tzinfo=UTC)
    )
    duration_ms: int | None = 300_000


class _ScalarsResult:
    """``execute(...).scalars().all()`` を満たす最小結果。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


@dataclass
class _FakeSession:
    """初期スナップショット読み出し専用の in-memory セッション (commit しない)。"""

    rows: list[_FakeJobHistory] = field(default_factory=list)

    async def execute(self, _statement: Any) -> _ScalarsResult:
        # endpoint は started_at desc / limit を付けるが、 fake は与えられた順で返す。
        return _ScalarsResult(list(self.rows))


def _settings() -> Settings:
    return Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))


def test_stream_requires_auth() -> None:
    """認証無しの ``GET /jobs/stream`` は 401。"""
    from ymg_backend.api.jobs import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: _FakeSession()
    app.dependency_overrides[get_settings] = _settings
    client = TestClient(app)

    resp = client.get("/jobs/stream")
    assert resp.status_code == 401


# NOTE: ``GET /jobs/stream`` の content-type / 初期スナップショット / 接続後差分の検証は
# ``tests/unit/test_sse_jobs.py`` が ``_event_stream`` を直接駆動して担保する。 本環境の
# Starlette TestClient (1.2.x) は streaming レスポンスを app 完了までバッファする実装
# (testclient.py: ``portal.call(self.app, ...)`` が app コルーチン完了を待つ) のため、 無限
# SSE generator を ``client.stream(...).__enter__()`` で開くと deadlock する。 TestClient 経由の
# streaming アサーションはここでは行わない (認証 401 のみ TestClient で検証)。


def test_step_vocabulary_is_seven_fixed_values() -> None:
    """grid 行順の step 語彙が契約の 7 値であることを (定数があれば) 検証する。

    実装が ``api.jobs.STEPS`` を公開していれば、 契約 (d) の 7 値・順序と一致することを確認する。
    未公開なら本テストはスキップ相当 (定数共有は配線実装者の責務だが、 あれば固定する)。
    """
    import ymg_backend.api.jobs as jobs_mod

    steps = getattr(jobs_mod, "STEPS", None)
    if steps is None:
        pytest.skip("api.jobs.STEPS is optional; defined by wiring implementer")
    assert tuple(steps) == _EXPECTED_STEPS
