"""プロセス内 asyncio pub/sub (``EventBus``) — US6 オーケストレーション可視化 (T121)。

発行側 (``daily_cycle`` 等の pipeline) と購読側 (``GET /jobs/stream`` の SSE ハンドラ) が
**同一プロセス・同一インスタンス**を共有するためのインメモリ pub/sub。 FastAPI app と
scheduler は同プロセスで動く (ADR-0031) ため、 module-level singleton :data:`event_bus`
を双方が import すれば配線できる。 マルチプロセスでは共有されない (単一 backend 前提・許容)。

設計上の不変条件:

- :meth:`EventBus.publish` は **同期・ノンブロッキング・例外を漏らさない**。 全購読者の
  :class:`asyncio.Queue` へ ``put_nowait`` で fan-out し、 満杯 (:class:`asyncio.QueueFull`)
  でも購読者ゼロでも黙って捨てる。 発行側 (pipeline) を絶対に止めないための設計
  (進捗イベントの欠落は許容、 本処理の停止は不可)。
- :meth:`EventBus.subscribe` は ``async with bus.subscribe() as sub: async for ev in sub``
  を満たす。 購読ごとに独立した :class:`asyncio.Queue` を割り当て、 ``async with`` 退出時
  (``finally`` 相当) に購読者集合から **必ず**除去する (queue リーク防止)。
- :attr:`JobEvent.timestamp` は発行側が空でも :meth:`publish` が ISO8601(JST) を補完する。
  pipeline 側の差分を最小化するため、 timestamp 生成は bus に集約する。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal
from zoneinfo import ZoneInfo

# JobEvent.status の enum。 contract ``JobEvent.status`` (backend-api.yaml) と一致。
JobStatus = Literal["running", "succeeded", "failed"]

# エラー 5 区分。 contract ``JobEvent.error_category`` / data-model.md `error_category` と一致。
ErrorCategoryStr = Literal["transient", "recoverable", "fatal", "compliance", "quality"]

# 進捗イベントの timestamp は JST (+09:00) で補完する (ADR-0011: 時刻は JST 固定)。
_JST: Final[ZoneInfo] = ZoneInfo("Asia/Tokyo")

# 購読ごとの queue 上限。 SSE 購読者が読み出さない / 遅い場合でも publish を詰まらせない
# ための安全弁。 満杯時は publish が黙って捨てる (進捗欠落 < pipeline 停止)。
_QUEUE_MAXSIZE: Final[int] = 1000


@dataclass(frozen=True)
class JobEvent:
    """ジョブ進捗の 1 イベント (不変)。 contract ``JobEvent`` schema + ``genre`` 拡張。

    grid (ジャンル列 x step 行) のセルを更新するための最小情報を持つ。 発行側は本 dataclass を
    そのまま :meth:`EventBus.publish` へ渡す。 ``timestamp`` は空文字でも publish が補完する。
    """

    timestamp: str
    """ISO8601 (JST, ``+09:00``)。 空文字で渡すと :meth:`EventBus.publish` が補完する。"""

    job_name: str
    """ジョブ名 (例 ``"daily_cycle"``)。 ``JobHistory.job_name`` と同語彙。"""

    step: str
    """進捗軸。 grid 行キー (``cycle`` / ``post`` / ``music`` / ``acoustid`` / ``image`` /
    ``render`` / ``publish``)。"""

    status: JobStatus
    """``running`` | ``succeeded`` | ``failed`` の 3 値のみ (contract enum 準拠)。"""

    genre: str | None = None
    """grid 列キー。 post 単位 step のみ非 ``None`` (``cycle`` step は ``None``)。"""

    context_type: str | None = None
    """``"plan"`` / ``"post"`` 等。 ``JobHistory.context_type`` と同語彙。"""

    context_id: str | None = None
    """対象の UUID 文字列。 ``JobHistory.context_id`` と同語彙 (str 化)。"""

    error_category: ErrorCategoryStr | None = None
    """失敗時のエラー 5 区分。 ``status == "failed"`` のとき設定される。"""

    message: str | None = None
    """補足メッセージ (失敗理由等)。 ``JobHistory.error_message`` と同語彙。"""


class Subscription:
    """1 購読者ぶんの非同期イテレータ兼コンテキストマネージャ。

    ``async with bus.subscribe() as sub`` で取得し、 ``async for ev in sub`` で :class:`JobEvent`
    を逐次受け取る。 ``async with`` 退出時に :meth:`EventBus` の購読者集合から自身を除去する
    (queue リーク防止)。 購読者ごとに独立した :class:`asyncio.Queue` を持つので、 ある購読者の
    取りこぼし / 解除が他の購読者へ波及しない。
    """

    def __init__(self, bus: EventBus, queue: asyncio.Queue[JobEvent]) -> None:
        self._bus = bus
        self._queue = queue

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        # 退出時は必ず購読解除する。 例外有無に関わらず queue を集合から除去しリークを防ぐ。
        self._bus._remove_subscriber(self._queue)

    def __aiter__(self) -> AsyncIterator[JobEvent]:
        return self

    async def __anext__(self) -> JobEvent:
        # queue へ put された次のイベントまで待機する。 解除後の取り出しは想定しない
        # (``async with`` のスコープ内でのみ反復される契約)。
        return await self._queue.get()


class EventBus:
    """プロセス内 asyncio pub/sub。 publish を全購読者へ fan-out する。

    購読者は :class:`asyncio.Queue` の集合 (:attr:`_subscribers`) として保持する。 ``set`` で
    管理し、 :meth:`subscribe` で追加・:class:`Subscription` 退出で削除する。 :meth:`publish`
    は同期メソッドで、 イベントループが無い文脈 (同期 pipeline) からも安全に呼べる。
    """

    def __init__(self) -> None:
        # 購読者ごとの queue 集合。 反復中の変更に備え publish ではスナップショットを取る。
        self._subscribers: set[asyncio.Queue[JobEvent]] = set()

    def publish(self, event: JobEvent) -> None:
        """``event`` を全購読者へ fan-out する (同期・ノンブロッキング・例外を漏らさない)。

        ``timestamp`` が空なら ISO8601(JST) を補完する。 各購読者の queue へ ``put_nowait``
        し、 満杯 (:class:`asyncio.QueueFull`) の購読者はスキップする。 購読者ゼロでも何もしない。
        いかなる場合も例外を送出しない — pipeline 側はこれを最小差分で呼ぶだけで、 進捗発行の
        失敗が本処理を止めてはならないため。
        """
        enriched = event if event.timestamp else replace(event, timestamp=_now_iso())
        # 反復中に __aexit__ が集合を変更しても安全なよう、 スナップショットへ配信する。
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(enriched)
            except asyncio.QueueFull:
                # 遅い / 読まない購読者の queue が満杯。 進捗欠落は許容し pipeline を止めない。
                continue

    def subscribe(self) -> Subscription:
        """新規購読を開始する。 ``async with bus.subscribe() as sub: async for ev in sub``。

        購読ごとに独立した :class:`asyncio.Queue` を割り当て、 購読者集合へ登録する。 戻り値の
        :class:`Subscription` が ``async with`` 退出時に自身を集合から除去する (リーク防止)。
        登録は本メソッドの戻り (= ``__aenter__`` より前) で完了するので、 SSE ハンドラが
        「購読登録 → 初期スナップショット送出」の順を守れば差分の取りこぼしが起きない。
        """
        queue: asyncio.Queue[JobEvent] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        return Subscription(self, queue)

    def subscriber_count(self) -> int:
        """現在の購読者数。 リーク検証 (解除後に 0 へ戻る) 用の観測点。"""
        return len(self._subscribers)

    def _remove_subscriber(self, queue: asyncio.Queue[JobEvent]) -> None:
        """購読者集合から ``queue`` を除去する (:class:`Subscription` 退出時に呼ばれる)。

        二重解除でも安全 (:meth:`set.discard` は不在でも例外を投げない)。
        """
        self._subscribers.discard(queue)


def _now_iso() -> str:
    """現在時刻を ISO8601(JST, ``+09:00``) 文字列で返す (publish の timestamp 補完用)。"""
    return datetime.now(tz=_JST).isoformat()


# module-level singleton。 発行側 (pipeline) / 購読側 (SSE) が双方 import して共有する。
event_bus: Final[EventBus] = EventBus()
