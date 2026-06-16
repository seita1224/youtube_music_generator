"""``/jobs/stream`` ジョブ進捗 SSE ルータ (US6, contracts/backend-api.yaml ``/jobs/stream`` L497, ADR-0023)。

``GET /jobs/stream`` は ``text/event-stream`` のロングポーリング接続。 接続時に
``JobHistory`` の直近実行履歴 (初期スナップショット) を ``data: {json}\n\n`` で 1 回流し、
以降は :data:`~ymg_backend.infrastructure.event_bus.event_bus` (プロセス内 pub/sub
シングルトン) の差分イベントをそのまま流し続ける。

設計方針:

- **取りこぼし防止 (契約 (e))**: ``event_bus.subscribe()`` の購読登録を **スナップショット
  読み出しより先** に行う。 こうすると「スナップショット送出中に発生した差分」は購読 queue に
  積まれ、 スナップショット送出後の ``async for`` で確実に流れる (購読開始 → スナップショット
  → 差分)。
- **接続維持**: 無イベントが続いても接続を切らせないため、 ``_PING_INTERVAL`` 秒ごとに
  ``: ping\n\n`` (SSE コメント行) を送る。 ``asyncio.wait_for`` のタイムアウトで実装する。
- **後始末 (リーク無し)**: 購読解除は ``async with event_bus.subscribe()`` の ``__aexit__`` が
  担う。 クライアント切断時は generator に ``GeneratorExit`` / ``CancelledError`` が伝播し、
  ``async with`` を抜けて購読が解除される (queue 破棄)。
- **認証**: endpoint 引数に ``user: BasicAuthUser`` を置き、 単体マウント時も Basic 認証を
  強制する。 ``main.py`` の親ルータ (``dependencies=[require_basic_auth]``) 配下に置いても
  二重適用は冪等。 配線は後段が ``_build_protected_router`` へ ``include_router`` する。
- **初期スナップショット読み出し専用**なので ``session`` は ``commit`` しない (``get_session`` の
  契約: コミットは呼び出し側責務、 ここは読み出しのみ)。

contract ``JobEvent`` (L1061) との差分: ``genre`` を JSON に追加する (grid の列キー用)。
yaml は変更しない (既存で必須項目は満たす。 ``genre`` は実装拡張)。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncGenerator
from typing import Annotated, Final, cast

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.security import BasicAuthUser
from ymg_backend.infrastructure.db.models import JobHistory
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.infrastructure.event_bus import (
    ErrorCategoryStr,
    EventBus,
    JobEvent,
    JobStatus,
    event_bus,
)

router: Final = APIRouter(prefix="/jobs", tags=["jobs"])

# grid 行順 (契約 (d): step 7 値固定)。 配線実装者 (publish 側) と語彙を共有する。
STEPS: Final[tuple[str, ...]] = (
    "cycle",
    "post",
    "music",
    "acoustid",
    "image",
    "render",
    "publish",
)

# 初期スナップショットの件数上限 (契約 (e): 直近 50 程度の安全弁。 ``dryrun.py`` の
# ``_LIST_LIMIT=100`` に倣いつつ進捗グリッド用にやや絞る)。
_SNAPSHOT_LIMIT: Final[int] = 50

# 無イベント時に接続維持のため ping を送る間隔 (秒)。
_PING_INTERVAL_SECONDS: Final[float] = 15.0

_SSE_MEDIA_TYPE: Final[str] = "text/event-stream"
# proxy / ブラウザのバッファリングを抑え、 イベントを即時 flush させるためのヘッダ。
_SSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get(
    "/stream",
    summary="SSE for live job progress (ADR-0023)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {_SSE_MEDIA_TYPE: {}},
            "description": "JSON-Lines events of JobEvent (text/event-stream).",
        },
    },
)
async def stream_jobs(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StreamingResponse:
    """ジョブ進捗を SSE (``text/event-stream``) で配信する。

    初期スナップショット (``JobHistory`` 直近) を流したのち、 ``event_bus`` の差分イベントを
    流し続ける。 keep-alive 用に無イベント時は ``: ping`` を周期送出する。
    """
    del user  # 認証のみ目的 (依存性で 401 を強制済み)。
    snapshot = await _load_snapshot(session)
    return StreamingResponse(
        _event_stream(snapshot),
        media_type=_SSE_MEDIA_TYPE,
        headers=_SSE_HEADERS,
    )


async def _load_snapshot(session: AsyncSession) -> list[JobEvent]:
    """``JobHistory`` の直近実行履歴を ``JobEvent`` 列へ写像する (新しい順)。

    ``job_history.status`` は ``running|succeeded|failed`` で ``JobEvent.status`` と同語彙
    なので変換不要。 ``step`` は列が無いため ``job_name`` を流用し、 ``genre`` は持たないので
    ``None``。 ``timestamp`` は ``finished_at`` 優先、 無ければ ``started_at`` を ISO8601 化する。
    """
    stmt = select(JobHistory).order_by(JobHistory.started_at.desc()).limit(_SNAPSHOT_LIMIT)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    # 古い順に流す (グリッドは最新値で上書きするので、 同一 (genre, step) は新しい方が勝つ)。
    return [_job_history_to_event(row) for row in reversed(list(rows))]


def _job_history_to_event(row: JobHistory) -> JobEvent:
    """``JobHistory`` 1 行を ``JobEvent`` へ写像する (初期スナップショット用)。"""
    moment = row.finished_at or row.started_at
    # DB 値は str (制約上は running|succeeded|failed / エラー 5 区分) だが型上は str。
    # JobEvent の Literal へは境界で cast する (SSE は値検証で止めない方針)。
    return JobEvent(
        timestamp=moment.isoformat(),
        job_name=row.job_name,
        step=row.job_name,  # JobHistory に step 列が無いため job_name を流用 (契約 (e))。
        status=cast(JobStatus, row.status),
        genre=None,
        context_type=row.context_type,
        context_id=str(row.context_id) if row.context_id is not None else None,
        error_category=cast("ErrorCategoryStr | None", row.error_category),
        message=row.error_message,
    )


async def _event_stream(
    snapshot: list[JobEvent], *, bus: EventBus = event_bus
) -> AsyncGenerator[str]:
    """SSE フレームを yield する async generator。

    購読登録 (``bus.subscribe()``) を **スナップショット送出より先** に行い、 送出中に
    発生した差分の取りこぼしを防ぐ (契約 (e))。 その後、 差分イベントを ``async for`` で流し、
    無イベントが ``_PING_INTERVAL_SECONDS`` 続いたら ping コメント行を送る。 クライアント切断は
    ``GeneratorExit`` / ``CancelledError`` として伝播し、 ``async with`` 退出で購読解除される。

    ``bus`` は既定で module-level singleton (:data:`event_bus`)。 引数化はテストが専用 bus を
    注入し singleton 汚染を避けるため (本番配線は既定のまま)。
    """
    async with bus.subscribe() as subscription:
        # 1) 初期スナップショット (購読開始後に送るので、 この間の差分は queue に積まれる)。
        for event in snapshot:
            yield _format_event(event)
        # 2) 差分イベント + keep-alive ping。
        while True:
            try:
                event = await asyncio.wait_for(
                    subscription.__anext__(), timeout=_PING_INTERVAL_SECONDS
                )
            except TimeoutError:
                yield ": ping\n\n"  # SSE コメント行 (data 行ではないのでクライアントは無視)。
                continue
            except StopAsyncIteration:
                break
            yield _format_event(event)


def _format_event(event: JobEvent) -> str:
    """``JobEvent`` を SSE の ``data:`` フレーム (``data: {json}\\n\\n``) に整形する。

    contract ``JobEvent`` と同形の JSON に ``genre`` を加えたもの。 ``None`` フィールドも
    ``null`` として残し、 frontend が固定キーで読めるようにする。
    """
    payload = dataclasses.asdict(event)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = ["STEPS", "router", "stream_jobs"]
