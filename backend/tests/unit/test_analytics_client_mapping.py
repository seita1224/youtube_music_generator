"""Unit テスト: AnalyticsClient の解析・upsert・ADR-0028 エラー写像 (T101)。

本ファイルは担当モジュール (``domain/analytics/client.py``) の unit カバーを担う。
外部依存は全て mock:

- HTTP は respx mock (Analytics API + Data API)。
- OAuth は ``get_access_token`` を持つ最小スタブ (``YouTubeOAuth`` は ``__slots__`` で
  メソッドを monkeypatch できないため、 同インターフェースのスタブを注入する)。
- session は upsert / select を記録するスパイ。

検証観点:
1. 単一動画の全指標解析 + upsert (件数・values)。
2. 流入元レポートの ``traffic_sources`` 組み立て。
3. impressions / ctr_pct の取得。
4. video_ids=None → DB 公開済み動画解決。
5. 空入力の short-circuit (API を叩かない)。
6. ADR-0028 写像: 401/403→Recoverable, 5xx→Transient, 接続失敗→Transient。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
import respx

from ymg_backend.domain.analytics.client import (
    ANALYTICS_REPORTS_URL,
    YOUTUBE_VIDEOS_URL,
    AnalyticsClient,
)
from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth

pytestmark = pytest.mark.unit

_TOKEN = "unit-token"
_CHANNEL_ID = "UC_unit"
_METRIC_DATE = date(2026, 6, 15)


# --- OAuth スタブ (slots を持たないので get_access_token を実メソッドで提供) ----------


class _StubSettings:
    def __init__(self, channel_id: str | None) -> None:
        self.youtube_channel_id = channel_id


class FakeOAuth:
    """``AnalyticsClient`` が必要とする OAuth の最小インターフェース。"""

    def __init__(self, channel_id: str | None = _CHANNEL_ID) -> None:
        self._settings = _StubSettings(channel_id)

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _TOKEN


def _client(oauth: FakeOAuth, *, http: httpx.AsyncClient | None = None) -> AnalyticsClient:
    """型シームを 1 箇所に集約 (FakeOAuth は YouTubeOAuth と構造的に互換)。"""
    return AnalyticsClient(cast(YouTubeOAuth, oauth), http=http)


# --- session スパイ -----------------------------------------------------------------


@dataclass
class _Recorded:
    table_name: str
    kind: str
    values: dict[str, Any]


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class SpySession:
    def __init__(self, video_ids: list[str] | None = None) -> None:
        self.recorded: list[_Recorded] = []
        self.flush_count = 0
        self.commit_count = 0
        self._video_ids = video_ids or []

    async def execute(self, statement: Any) -> Any:
        kind = type(statement).__name__.lower()
        if "select" in kind:
            self.recorded.append(_Recorded("videos", "select", {}))
            return _Result(list(self._video_ids))
        compiled = statement.compile()
        table = getattr(statement, "table", None)
        self.recorded.append(
            _Recorded(table.name if table is not None else "?", kind, dict(compiled.params))
        )
        return _Result([])

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:  # pragma: no cover - 呼ばれてはいけない
        self.commit_count += 1

    @property
    def upserts(self) -> list[_Recorded]:
        return [
            r for r in self.recorded if r.table_name == "analytics_daily" and "insert" in r.kind
        ]


def _session(video_ids: list[str] | None = None) -> Any:
    """型シーム (SpySession は AsyncSession と構造的に互換)。"""
    return SpySession(video_ids)


# --- レスポンス組み立て -------------------------------------------------------------


def _report(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columnHeaders": [{"name": c} for c in columns], "rows": rows}


def _primary(video_id: str) -> dict[str, Any]:
    return _report(
        [
            "video",
            "views",
            "estimatedMinutesWatched",
            "averageViewDuration",
            "averageViewPercentage",
        ],
        [[video_id, 1234, 4567.5, 222, 48.3]],
    )


def _impressions(video_id: str) -> dict[str, Any]:
    return _report(
        ["video", "views", "impressions", "impressionsCtr"], [[video_id, 1234, 9000, 5.5]]
    )


def _traffic(sources: dict[str, int]) -> dict[str, Any]:
    return _report(["insightTrafficSourceType", "views"], [[s, v] for s, v in sources.items()])


def _wire_analytics(video_id: str) -> None:
    def _side_effect(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        dims = params.get("dimensions", "")
        metrics = params.get("metrics", "")
        if "insightTrafficSourceType" in dims:
            return httpx.Response(200, json=_traffic({"YT_SEARCH": 80, "RELATED_VIDEO": 20}))
        if "impressions" in metrics:
            return httpx.Response(200, json=_impressions(video_id))
        return httpx.Response(200, json=_primary(video_id))

    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=_side_effect)
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [{"id": video_id}]})
    )


# --- テスト本体 ---------------------------------------------------------------------


@respx.mock
async def test_parses_all_metrics_and_upserts() -> None:
    video_id = "uv1"
    _wire_analytics(video_id)
    session = _session()
    client = _client(FakeOAuth())

    count = await client.fetch_and_upsert(
        session=session, metric_date=_METRIC_DATE, video_ids=[video_id]
    )

    assert count == 1
    assert len(session.upserts) == 1
    values = session.upserts[0].values
    assert values["youtube_video_id"] == video_id
    assert values["metric_date"] == _METRIC_DATE
    assert values["views"] == 1234
    assert Decimal(str(values["estimated_minutes_watched"])) == Decimal("4567.5")
    assert values["average_view_duration_sec"] == 222
    assert Decimal(str(values["retention_pct"])) == Decimal("48.3")
    assert values["impressions"] == 9000
    assert Decimal(str(values["ctr_pct"])) == Decimal("5.5")
    assert values["traffic_sources"] == {"YT_SEARCH": 80, "RELATED_VIDEO": 20}
    assert session.flush_count >= 1
    assert session.commit_count == 0


@respx.mock
async def test_wire_format_carries_bearer_and_channel() -> None:
    video_id = "uv_wire"
    route = respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        side_effect=lambda req: httpx.Response(
            200,
            json=_traffic({"YT_SEARCH": 1})
            if "insightTrafficSourceType" in req.url.params.get("dimensions", "")
            else _primary(video_id),
        )
    )
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [{"id": video_id}]})
    )

    session = _session()
    await _client(FakeOAuth()).fetch_and_upsert(
        session=session, metric_date=_METRIC_DATE, video_ids=[video_id]
    )

    primary = next(
        c
        for c in route.calls
        if "insightTrafficSourceType" not in c.request.url.params.get("dimensions", "")
    )
    params = primary.request.url.params
    assert primary.request.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert params.get("ids") == f"channel=={_CHANNEL_ID}"
    assert params.get("dimensions") == "video"
    assert params.get("startDate") == _METRIC_DATE.isoformat()
    assert params.get("endDate") == _METRIC_DATE.isoformat()
    assert video_id in params.get("filters", "")


@respx.mock
async def test_resolves_video_ids_from_db_when_none() -> None:
    db_videos = ["vid_1", "vid_2"]

    def _side_effect(request: httpx.Request) -> httpx.Response:
        dims = request.url.params.get("dimensions", "")
        if "insightTrafficSourceType" in dims:
            return httpx.Response(200, json=_traffic({"YT_SEARCH": 1}))
        return httpx.Response(
            200,
            json=_report(
                [
                    "video",
                    "views",
                    "estimatedMinutesWatched",
                    "averageViewDuration",
                    "averageViewPercentage",
                ],
                [[v, 10, 1.0, 30, 25.0] for v in db_videos],
            ),
        )

    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=_side_effect)
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [{"id": v} for v in db_videos]})
    )

    session = _session(db_videos)
    count = await _client(FakeOAuth()).fetch_and_upsert(
        session=session, metric_date=_METRIC_DATE, video_ids=None
    )

    assert any(r.kind == "select" and r.table_name == "videos" for r in session.recorded)
    assert count == 2
    assert {u.values["youtube_video_id"] for u in session.upserts} == set(db_videos)


@respx.mock
async def test_empty_targets_short_circuits() -> None:
    route = respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(200, json=_report(["video", "views"], []))
    )
    session = _session([])
    count = await _client(FakeOAuth()).fetch_and_upsert(
        session=session, metric_date=_METRIC_DATE, video_ids=None
    )
    assert count == 0
    assert session.upserts == []
    assert not route.called


@respx.mock
async def test_auth_error_maps_to_recoverable() -> None:
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    session = _session()
    with pytest.raises(RecoverableError):
        await _client(FakeOAuth()).fetch_and_upsert(
            session=session, metric_date=_METRIC_DATE, video_ids=["v"]
        )


@respx.mock
async def test_server_error_maps_to_transient() -> None:
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(503, json={"error": "unavailable"})
    )
    session = _session()
    with pytest.raises(TransientError):
        await _client(FakeOAuth()).fetch_and_upsert(
            session=session, metric_date=_METRIC_DATE, video_ids=["v"]
        )


@respx.mock
async def test_connection_failure_maps_to_transient() -> None:
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=httpx.ConnectError("boom"))
    session = _session()
    with pytest.raises(TransientError):
        await _client(FakeOAuth()).fetch_and_upsert(
            session=session, metric_date=_METRIC_DATE, video_ids=["v"]
        )


@respx.mock
async def test_missing_channel_id_raises_recoverable() -> None:
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(200, json=_report(["video", "views"], []))
    )
    session = _session()
    with pytest.raises(RecoverableError):
        await _client(FakeOAuth(channel_id=None)).fetch_and_upsert(
            session=session, metric_date=_METRIC_DATE, video_ids=["v"]
        )
