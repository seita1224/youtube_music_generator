"""daily_cycle の US6 進捗 publish 配線の単体テスト (T120 配線フェーズ)。

検証対象は ``daily_cycle.py`` に追加した best-effort publish 配線:

- ``_publish_cycle``: サイクル (plan) 単位イベント。 ``genre`` を持たず ``context_type="plan"``。
- ``_publish_post``: post 単位イベント。 ``genre=post.genre`` / ``context_type="post"``、
  失敗時は ``error_category`` / ``message`` を付す。

両ヘルパは module-level singleton :data:`event_bus` へ発行する。 本テストは実 ``event_bus``
を購読し、 発行されたイベントの値が内部契約 (d) (step 語彙 / status 3 値 / genre / context)
に一致することを確認する。 publish は同期・例外を漏らさない契約なので、 購読者ゼロでも
配線が落ちないこと (best-effort) も併せて確認する。

実 DB / LLM / GPU / HTTP は起動しない。 ``Post`` は SQLAlchemy declarative モデルだが
ORM 行はセッション無しでもインスタンス化でき (publish ヘルパは属性参照のみ)、 本テストは
それをメモリ上で構築して使う。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ymg_backend.domain.pipeline import daily_cycle
from ymg_backend.infrastructure.db.models import Post
from ymg_backend.infrastructure.event_bus import EventBus, JobEvent, event_bus

pytestmark = pytest.mark.unit

# 内部契約 (d): step 7 値固定 (grid 行順)。 publish 側 / SSE 側で共有する語彙。
_EXPECTED_STEPS = ("cycle", "post", "music", "acoustid", "image", "render", "publish")


def _make_post(*, genre: str = "lofi") -> Post:
    """publish ヘルパが参照する属性 (id / genre) だけ設定した ORM ``Post`` を返す。

    セッションに add しないため DB へ触れない。 ``error_category`` / ``error_message`` は
    既定 ``None`` で、 失敗系テストでヘルパ引数経由で渡す。
    """
    return Post(id=uuid.uuid4(), genre=genre)


# ============================================================================
# _publish_cycle: サイクル (plan) 単位
# ============================================================================


async def test_publish_cycle_running_event_shape() -> None:
    """``_publish_cycle`` は genre 無し・context_type=plan の cycle イベントを発行する。"""
    plan_id = str(uuid.uuid4())
    async with event_bus.subscribe() as sub:
        daily_cycle._publish_cycle(step="cycle", status="running", plan_id=plan_id)
        ev = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert ev.job_name == "daily_cycle"
    assert ev.step == "cycle"
    assert ev.status == "running"
    assert ev.genre is None
    assert ev.context_type == "plan"
    assert ev.context_id == plan_id
    assert ev.error_category is None
    assert ev.message is None
    # bus が timestamp を補完する (発行側は空文字を渡す)。
    assert ev.timestamp != ""


async def test_publish_cycle_succeeded_and_failed_status() -> None:
    """cycle 完了は succeeded / failed の両 status を発行できる (contract enum)。"""
    plan_id = str(uuid.uuid4())
    async with event_bus.subscribe() as sub:
        daily_cycle._publish_cycle(step="cycle", status="succeeded", plan_id=plan_id)
        daily_cycle._publish_cycle(step="cycle", status="failed", plan_id=plan_id)
        first = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        second = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert (first.status, second.status) == ("succeeded", "failed")
    assert first.step == second.step == "cycle"


# ============================================================================
# _publish_post: post 単位
# ============================================================================


async def test_publish_post_running_event_shape() -> None:
    """``_publish_post`` は genre=post.genre・context_type=post の post イベントを発行する。"""
    post = _make_post(genre="ambient")
    async with event_bus.subscribe() as sub:
        daily_cycle._publish_post(step="music", status="running", post=post)
        ev = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert ev.job_name == "daily_cycle"
    assert ev.step == "music"
    assert ev.status == "running"
    assert ev.genre == "ambient"
    assert ev.context_type == "post"
    assert ev.context_id == str(post.id)
    assert ev.error_category is None
    assert ev.message is None


async def test_publish_post_failed_carries_error_fields() -> None:
    """失敗イベントは error_category / message を載せる (failed セルのツールチップ用)。"""
    post = _make_post()
    async with event_bus.subscribe() as sub:
        daily_cycle._publish_post(
            step="acoustid",
            status="failed",
            post=post,
            error_category="compliance",
            message="AcoustID 連続 hit によりジャンルを一時停止しました",
        )
        ev = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

    assert ev.step == "acoustid"
    assert ev.status == "failed"
    assert ev.error_category == "compliance"
    assert ev.message == "AcoustID 連続 hit によりジャンルを一時停止しました"


# ============================================================================
# best-effort / 配線語彙
# ============================================================================


async def test_publish_helpers_never_raise_without_subscribers() -> None:
    """購読者ゼロでも publish ヘルパは例外を投げない (pipeline を止めない best-effort)。"""
    # 購読していない状態で呼んでも例外が出ないこと (publish は黙って捨てる)。
    daily_cycle._publish_cycle(step="cycle", status="running", plan_id=str(uuid.uuid4()))
    daily_cycle._publish_post(step="post", status="running", post=_make_post())


async def test_publish_helpers_emit_only_contract_steps() -> None:
    """配線で使う step 語彙が内部契約 (d) の 7 値に収まる (grid 行キー整合)。"""
    post = _make_post(genre="jazz")
    received: list[JobEvent] = []
    async with event_bus.subscribe() as sub:
        for step in _EXPECTED_STEPS:
            if step == "cycle":
                daily_cycle._publish_cycle(step=step, status="running", plan_id="p")
            else:
                daily_cycle._publish_post(step=step, status="running", post=post)
        for _ in _EXPECTED_STEPS:
            received.append(await asyncio.wait_for(sub.__anext__(), timeout=1.0))

    assert {ev.step for ev in received} == set(_EXPECTED_STEPS)
    assert all(ev.status == "running" for ev in received)


async def test_helpers_publish_to_shared_singleton_bus() -> None:
    """daily_cycle のヘルパが共有 singleton ``event_bus`` へ発行する (ADR-0031 前提)。

    publish 側 (daily_cycle) と subscribe 側 (SSE) が同一プロセスで同一 bus を参照する
    こと: 本テストが ``event_bus`` を購読し、 別 import 経路の ``daily_cycle`` ヘルパが
    発行したイベントを受け取れることで確認する。
    """
    assert isinstance(event_bus, EventBus)
    async with event_bus.subscribe() as sub:
        daily_cycle._publish_cycle(step="cycle", status="running", plan_id="p")
        ev = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert ev.step == "cycle"


async def test_subscription_cleaned_up_after_publish() -> None:
    """``async with`` 退出で購読が解除され subscriber がリークしない。"""
    before = event_bus.subscriber_count()
    async with event_bus.subscribe() as sub:
        daily_cycle._publish_post(step="image", status="running", post=_make_post())
        await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert event_bus.subscriber_count() == before + 1
    assert event_bus.subscriber_count() == before
