"""``EventBus`` (プロセス内 asyncio pub/sub) の単体テスト (US6 T121)。

担当ファイル ``ymg_backend.infrastructure.event_bus`` のみを対象とする (``api.jobs`` の SSE
endpoint は別タスク・別ファイル ``test_event_bus.py`` でカバー)。 実 DB / LLM / GPU / HTTP は
一切起動しない。 ``asyncio_mode = "auto"`` (pyproject) のため ``async def test_*`` をそのまま使う。

検証観点:

- publish したイベントが 1 購読者へ届く / 複数購読者へ fan-out される。
- ``async with`` 退出で購読解除され ``subscriber_count`` が 0 に戻る (queue リーク無し)。
- 購読者ゼロ / queue 満杯でも publish は例外を投げない (pipeline を止めない)。
- bus が空 ``timestamp`` を ISO8601(JST, ``+09:00``) で補完する。
- ``JobEvent`` は不変 (``frozen``) で publish が元イベントを破壊しない。
- module-level ``event_bus`` が共有 singleton。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from ymg_backend.infrastructure.event_bus import (
    EventBus,
    JobEvent,
    Subscription,
    event_bus,
)

pytestmark = pytest.mark.unit


def _event(**overrides: Any) -> JobEvent:
    """テスト用 ``JobEvent`` を生成する (必須項目に既定値を与える)。"""
    base: dict[str, Any] = {
        "timestamp": "",
        "job_name": "daily_cycle",
        "step": "music",
        "status": "running",
    }
    base.update(overrides)
    return JobEvent(**base)


async def test_publish_delivers_to_subscriber() -> None:
    """publish したイベントが 1 購読者へ届く。"""
    bus = EventBus()
    async with bus.subscribe() as sub:
        bus.publish(_event(step="music", status="running", genre="lofi"))
        received = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert received.step == "music"
    assert received.status == "running"
    assert received.genre == "lofi"
    assert received.job_name == "daily_cycle"


async def test_publish_fans_out_to_multiple_subscribers() -> None:
    """同一 publish が全 subscriber へ fan-out され、 各 queue は独立。"""
    bus = EventBus()
    async with bus.subscribe() as sub_a, bus.subscribe() as sub_b:
        assert bus.subscriber_count() == 2
        bus.publish(_event(step="render", status="succeeded", genre="jazz"))
        got_a = await asyncio.wait_for(sub_a.__anext__(), timeout=1.0)
        got_b = await asyncio.wait_for(sub_b.__anext__(), timeout=1.0)

    assert got_a.step == "render" == got_b.step
    assert got_a.status == "succeeded" == got_b.status
    assert got_a.genre == "jazz" == got_b.genre


async def test_unsubscribe_removes_subscriber_no_leak() -> None:
    """``async with`` 退出後は購読者集合から除去され、 count が 0 へ戻る (リーク無し)。"""
    bus = EventBus()
    async with bus.subscribe() as sub:
        assert bus.subscriber_count() == 1
        bus.publish(_event(step="cycle", status="running"))
        first = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert first.step == "cycle"

    # 退出後: 購読者集合は空に戻り、 後続 publish は誰にも届かず例外も出ない。
    assert bus.subscriber_count() == 0
    bus.publish(_event(step="cycle", status="succeeded"))

    # 新規購読者は退出済み購読の取りこぼし分を受け取らない (queue は購読ごとに独立)。
    async with bus.subscribe() as fresh:
        bus.publish(_event(step="image", status="running"))
        got = await asyncio.wait_for(fresh.__anext__(), timeout=1.0)
        assert got.step == "image"  # cycle/succeeded は流れてこない。


async def test_publish_with_no_subscribers_does_not_raise() -> None:
    """購読者ゼロでも publish は例外を投げない (pipeline を絶対に止めない)。"""
    bus = EventBus()
    bus.publish(_event(step="cycle", status="running"))  # 例外が出ないこと自体が assertion。
    assert bus.subscriber_count() == 0


async def test_publish_when_queue_full_does_not_raise() -> None:
    """queue 満杯でも publish は例外を漏らさない (``QueueFull`` を握り潰す)。"""
    bus = EventBus()
    async with bus.subscribe():
        # 読み出さずに上限超まで発行。 満杯になっても publish は静かに捨てる。
        for _ in range(10_000):
            bus.publish(_event(step="music", status="running"))


async def test_bus_fills_timestamp_when_missing() -> None:
    """発行側が timestamp を渡さなくても bus が ISO8601(JST) を補完する。"""
    bus = EventBus()
    async with bus.subscribe() as sub:
        bus.publish(_event(timestamp="", step="post", status="running"))
        got = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert got.timestamp  # 非空。
    parsed = datetime.fromisoformat(got.timestamp)  # ISO8601 としてパース可能。
    assert parsed.utcoffset() is not None  # tz-aware (+09:00)。


async def test_publish_preserves_nonempty_timestamp() -> None:
    """発行側が timestamp を渡した場合は補完で上書きしない。"""
    bus = EventBus()
    given = "2026-06-16T09:00:00+09:00"
    async with bus.subscribe() as sub:
        bus.publish(_event(timestamp=given, step="cycle", status="running"))
        got = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert got.timestamp == given


async def test_publish_does_not_mutate_source_event() -> None:
    """``JobEvent`` は frozen で、 timestamp 補完は元イベントを破壊しない (新インスタンス)。"""
    bus = EventBus()
    source = _event(timestamp="", step="acoustid", status="running")
    async with bus.subscribe() as sub:
        bus.publish(source)
        got = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert source.timestamp == ""  # 元は不変。
    assert got.timestamp  # 配信側は補完済み。
    assert got is not source


def test_subscribe_returns_subscription() -> None:
    """``subscribe()`` は ``Subscription`` (async ctx manager 兼 async iterator) を返す。"""
    bus = EventBus()
    sub = bus.subscribe()
    assert isinstance(sub, Subscription)
    assert hasattr(sub, "__aenter__")
    assert hasattr(sub, "__aexit__")
    assert hasattr(sub, "__anext__")
    # 反復しないまま破棄するので、 ここで明示的に購読解除しておく。
    bus._remove_subscriber(sub._queue)
    assert bus.subscriber_count() == 0


def test_module_singleton_is_event_bus_instance() -> None:
    """module-level ``event_bus`` が ``EventBus`` のインスタンス (publish/subscribe 双方が import)。"""
    assert isinstance(event_bus, EventBus)
