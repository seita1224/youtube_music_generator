"""Critical path テスト: YouTube アナリティクス取得クライアント (T100 / T101)。

ADR-0021 (Data API + Analytics API 併用) / FR-101 を 100% カバーする。

対象は ``ymg_backend.domain.analytics.client.AnalyticsClient`` (US3 内部契約):

    class AnalyticsClient:
        def __init__(self, oauth: YouTubeOAuth, *, http: httpx.AsyncClient | None = None): ...
        async def fetch_and_upsert(self, *, session, metric_date, video_ids=None) -> int

検証観点 (取得指標の 100% カバー):

1. **retention / views / impressions / ctr / traffic_sources の取得**:
   YouTube Analytics API の ``reports.query`` レスポンス (``columnHeaders`` + ``rows``)
   から ``views`` / ``estimatedMinutesWatched`` / ``averageViewDuration`` /
   ``averageViewPercentage`` (= retention_pct) / ``impressions`` /
   ``cardImpressions``→CTR を解析し、 ``trafficSourceType`` 別の流入を
   ``traffic_sources`` (JSONB) に組み立てて ``analytics_daily`` へ upsert する。
2. **upsert 形 (返り値 + 副作用)**: 返り値は upsert 件数。 副作用として
   ``analytics_daily`` への PG ``insert(...).on_conflict_do_update`` が
   動画件数ぶん実行され、 ``metric_date`` / ``youtube_video_id`` / 各指標が
   values に載る。 commit は呼ばず flush まで。
3. **video_ids=None なら videos テーブルの公開済み動画を対象**にする
   (``session.execute(select(...))`` の結果を使う)。
4. **wire 形式**: Analytics API は ``GET .../v2/reports`` に ids=channel==<id> /
   metrics / dimensions=video / filters=video==... / startDate==endDate==metric_date /
   ``Authorization: Bearer <token>`` を載せる。 Data API ``videos.list`` (impressions
   が Analytics に無い場合の補完など) も Bearer を載せる。
5. **OAuth トークン取得は stub** (実 HTTP / 実 refresh を打たない)。

HTTP は respx で Analytics API + Data API を mock する。 OAuth は ``__slots__`` で
``get_access_token`` を上書きできないため、 実 ``YouTubeOAuth`` を構築せず固定トークンを
返す duck-typed stub (``_StubOAuth``) を渡す。 session は副作用記録スパイ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

# 実装が公開すべき URL 定数 (acoustid の ACOUSTID_LOOKUP_URL と同方針)。
# 実装側 (domain/analytics/client.py) はこの 2 定数を module 直下に export すること。
from ymg_backend.domain.analytics.client import (
    ANALYTICS_REPORTS_URL,
    YOUTUBE_VIDEOS_URL,
    AnalyticsClient,
)
from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth

pytestmark = pytest.mark.critical

_FAKE_TOKEN = "fake-access-token-xyz"
_CHANNEL_ID = "UC_test_channel"


# --- session スパイ -----------------------------------------------------------------


@dataclass
class _ExecutedStatement:
    """記録した execute() 呼び出し。"""

    table_name: str
    kind: str  # "insert" | "select" | ...
    values: dict[str, Any]


@dataclass
class _SelectResult:
    """``session.execute(select(...))`` の戻り。 公開済み動画 id を供給する。"""

    rows: list[Any]

    def scalars(self) -> _SelectResult:
        return self

    def all(self) -> list[Any]:
        return list(self.rows)


class SpySession:
    """``AsyncSession`` の最小スパイ。

    - ``execute(select(...))``: 公開済み動画 id を返す (video_ids=None 経路で使用)。
    - ``execute(insert(...).on_conflict_do_update(...))``: upsert を記録する。
    - ``flush``: 回数を記録 (commit は呼ばれないことの確認用)。
    """

    def __init__(self, video_ids: list[str] | None = None) -> None:
        self.executed: list[_ExecutedStatement] = []
        self.flush_count = 0
        self.commit_count = 0
        self._video_ids = video_ids or []

    async def execute(self, statement: Any) -> Any:
        kind = type(statement).__name__.lower()
        if "select" in kind:
            self.executed.append(_ExecutedStatement("videos", "select", {}))
            return _SelectResult([(vid,) for vid in self._video_ids])
        # insert / update 系: コンパイルして table / values を記録。
        compiled = statement.compile()
        table = getattr(statement, "table", None)
        table_name = table.name if table is not None else "<unknown>"
        self.executed.append(_ExecutedStatement(table_name, kind, dict(compiled.params)))
        return _SelectResult([])

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:  # pragma: no cover - 呼ばれてはいけない
        self.commit_count += 1

    @property
    def analytics_upserts(self) -> list[_ExecutedStatement]:
        return [
            e for e in self.executed if e.table_name == "analytics_daily" and "insert" in e.kind
        ]


# --- OAuth stub ---------------------------------------------------------------------


class _StubOAuth:
    """``YouTubeOAuth`` の代替 (固定トークン + channel_id 参照用 ``_settings``)。

    ``YouTubeOAuth`` は ``__slots__`` で ``get_access_token`` を上書きできないため、
    実 OAuth を構築せず、 ``AnalyticsClient`` が触れる I/F (``get_access_token`` と
    ``_oauth_channel_id`` が読む ``_settings.youtube_channel_id``) のみを持つ stub を渡す。
    実 HTTP / 実 refresh / 実 cipher は一切打たない。
    """

    class _Settings:
        youtube_channel_id = _CHANNEL_ID

    def __init__(self) -> None:
        self._settings = self._Settings()

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


def _build_oauth() -> YouTubeOAuth:
    return _StubOAuth()  # type: ignore[return-value]


# --- Analytics API / Data API レスポンス組み立て ------------------------------------


def _reports_response(*, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``youtubeAnalytics.reports.query`` 形 (columnHeaders + rows)。

    各 row は dict で渡し、 列順 (columnHeaders) に整列して 2 次元配列へ展開する。
    """
    columns = [
        "video",
        "views",
        "estimatedMinutesWatched",
        "averageViewDuration",
        "averageViewPercentage",
    ]
    column_headers = [{"name": c} for c in columns]
    data_rows = [[row.get(c) for c in columns] for row in rows]
    return {
        "kind": "youtubeAnalytics#resultTable",
        "columnHeaders": column_headers,
        "rows": data_rows,
    }


def _traffic_response(*, video_id: str, sources: dict[str, int]) -> dict[str, Any]:
    """``dimensions=insightTrafficSourceType`` で流入元別 views を返す形。"""
    column_headers = [{"name": "insightTrafficSourceType"}, {"name": "views"}]
    rows = [[src, views] for src, views in sources.items()]
    return {
        "kind": "youtubeAnalytics#resultTable",
        "columnHeaders": column_headers,
        "rows": rows,
    }


def _videos_response(*, items: list[dict[str, Any]]) -> dict[str, Any]:
    """``youtube/v3/videos`` の最小形 (impressions/CTR を持つ場合の補完用)。"""
    return {"kind": "youtube#videoListResponse", "items": items}


def _route_analytics(*, response: dict[str, Any]) -> respx.Route:
    return respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(200, json=response)
    )


# ---------------------------------------------------------------------------
# 取得 + upsert (本命): 全指標カバー
# ---------------------------------------------------------------------------


@pytest.mark.fr("FR-101")
@respx.mock
async def test_fetch_and_upsert_parses_all_metrics() -> None:
    """FR-101: retention / views / minutes / avg_duration / impressions / ctr / traffic_sources
    をすべて解析し、 analytics_daily へ upsert する。 返り値 = 件数。
    """
    metric_date = date(2026, 6, 15)
    video_id = "vid_AAA"

    # Analytics API は複数回呼ばれ得る (主指標レポート + 流入元レポート)。
    # url クエリで分岐し、 metrics に insightTrafficSourceType を含むか否かで返す。
    def _analytics_side_effect(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        dims = q.get("dimensions", "")
        if "insightTrafficSourceType" in dims:
            return httpx.Response(
                200,
                json=_traffic_response(
                    video_id=video_id, sources={"YT_SEARCH": 80, "RELATED_VIDEO": 20}
                ),
            )
        return httpx.Response(
            200,
            json=_reports_response(
                rows=[
                    {
                        "video": video_id,
                        "views": 1234,
                        "estimatedMinutesWatched": 4567.5,
                        "averageViewDuration": 222,
                        "averageViewPercentage": 48.3,
                    }
                ]
            ),
        )

    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=_analytics_side_effect)
    # Data API videos.list (impressions / CTR 補完)。
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_videos_response(
                items=[
                    {
                        "id": video_id,
                        "statistics": {"viewCount": "1234"},
                    }
                ]
            ),
        )
    )

    session = SpySession()
    client = AnalyticsClient(_build_oauth())

    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        metric_date=metric_date,
        video_ids=[video_id],
    )

    assert count == 1
    upserts = session.analytics_upserts
    assert len(upserts) == 1
    values = upserts[0].values
    # 主キー
    assert values.get("youtube_video_id") == video_id
    assert values.get("metric_date") == metric_date
    # 数値指標
    assert values.get("views") == 1234
    assert Decimal(str(values.get("estimated_minutes_watched"))) == Decimal("4567.5")
    assert values.get("average_view_duration_sec") == 222
    # retention_pct = averageViewPercentage
    assert Decimal(str(values.get("retention_pct"))) == Decimal("48.3")
    # traffic_sources は流入元別 dict
    traffic = values.get("traffic_sources")
    assert isinstance(traffic, dict)
    assert traffic.get("YT_SEARCH") == 80
    assert traffic.get("RELATED_VIDEO") == 20
    # commit は呼ばない、 flush は行う
    assert session.commit_count == 0
    assert session.flush_count >= 1


@respx.mock
async def test_fetch_and_upsert_captures_impressions_and_ctr() -> None:
    """impressions / ctr_pct を取得して upsert values に載せる。"""
    metric_date = date(2026, 6, 15)
    video_id = "vid_CTR"

    def _analytics_side_effect(request: httpx.Request) -> httpx.Response:
        dims = request.url.params.get("dimensions", "")
        metrics = request.url.params.get("metrics", "")
        if "insightTrafficSourceType" in dims:
            return httpx.Response(
                200, json=_traffic_response(video_id=video_id, sources={"YT_SEARCH": 10})
            )
        # impressions / impressionsCtr を含む主レポート。
        columns = ["video", "views", "impressions", "impressionsCtr"]
        if "impressions" in metrics:
            return httpx.Response(
                200,
                json={
                    "columnHeaders": [{"name": c} for c in columns],
                    "rows": [[video_id, 500, 9000, 5.5]],
                },
            )
        return httpx.Response(
            200,
            json=_reports_response(
                rows=[
                    {
                        "video": video_id,
                        "views": 500,
                        "estimatedMinutesWatched": 100.0,
                        "averageViewDuration": 120,
                        "averageViewPercentage": 40.0,
                    }
                ]
            ),
        )

    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=_analytics_side_effect)
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=_videos_response(items=[{"id": video_id}]))
    )

    session = SpySession()
    client = AnalyticsClient(_build_oauth())
    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        metric_date=metric_date,
        video_ids=[video_id],
    )

    assert count == 1
    values = session.analytics_upserts[0].values
    assert values.get("impressions") == 9000
    assert Decimal(str(values.get("ctr_pct"))) == Decimal("5.5")


# ---------------------------------------------------------------------------
# video_ids=None -> videos テーブルの公開済み動画を対象
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_and_upsert_resolves_video_ids_from_db_when_none() -> None:
    """video_ids=None なら videos テーブルから対象動画を取得する。"""
    metric_date = date(2026, 6, 15)
    db_videos = ["vid_1", "vid_2"]

    def _analytics_side_effect(request: httpx.Request) -> httpx.Response:
        dims = request.url.params.get("dimensions", "")
        if "insightTrafficSourceType" in dims:
            return httpx.Response(
                200, json=_traffic_response(video_id="x", sources={"YT_SEARCH": 1})
            )
        # 両動画を 1 レポートで返す。
        return httpx.Response(
            200,
            json=_reports_response(
                rows=[
                    {
                        "video": vid,
                        "views": 10 * (i + 1),
                        "estimatedMinutesWatched": 1.0,
                        "averageViewDuration": 30,
                        "averageViewPercentage": 25.0,
                    }
                    for i, vid in enumerate(db_videos)
                ]
            ),
        )

    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=_analytics_side_effect)
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(
            200, json=_videos_response(items=[{"id": v} for v in db_videos])
        )
    )

    session = SpySession(video_ids=db_videos)
    client = AnalyticsClient(_build_oauth())
    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        metric_date=metric_date,
        video_ids=None,
    )

    # videos テーブルを引いている
    assert any(e.kind == "select" and e.table_name == "videos" for e in session.executed)
    # 2 動画ぶん upsert
    assert count == 2
    assert len(session.analytics_upserts) == 2
    upserted_ids = {e.values.get("youtube_video_id") for e in session.analytics_upserts}
    assert upserted_ids == set(db_videos)


@respx.mock
async def test_fetch_and_upsert_no_target_videos_returns_zero() -> None:
    """対象動画が 0 件なら API を叩かず 0 を返す (空 DB 経路)。"""
    route_analytics = _route_analytics(response=_reports_response(rows=[]))
    session = SpySession(video_ids=[])
    client = AnalyticsClient(_build_oauth())

    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        metric_date=date(2026, 6, 15),
        video_ids=None,
    )

    assert count == 0
    assert session.analytics_upserts == []
    assert not route_analytics.called


# ---------------------------------------------------------------------------
# wire 形式: Analytics API / Data API のリクエスト検証
# ---------------------------------------------------------------------------


@respx.mock
async def test_analytics_request_wire_format() -> None:
    """Analytics API は channel==<id> / metrics / dimensions=video / 期間 / Bearer を送る。"""
    metric_date = date(2026, 6, 15)
    video_id = "vid_WIRE"

    def _analytics_side_effect(request: httpx.Request) -> httpx.Response:
        dims = request.url.params.get("dimensions", "")
        if "insightTrafficSourceType" in dims:
            return httpx.Response(
                200, json=_traffic_response(video_id=video_id, sources={"YT_SEARCH": 1})
            )
        return httpx.Response(
            200,
            json=_reports_response(
                rows=[
                    {
                        "video": video_id,
                        "views": 1,
                        "estimatedMinutesWatched": 1.0,
                        "averageViewDuration": 1,
                        "averageViewPercentage": 1.0,
                    }
                ]
            ),
        )

    route = respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        side_effect=_analytics_side_effect
    )
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=_videos_response(items=[{"id": video_id}]))
    )

    session = SpySession()
    client = AnalyticsClient(_build_oauth())
    await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        metric_date=metric_date,
        video_ids=[video_id],
    )

    assert route.called
    # 主指標レポートのリクエストを 1 つ取り出して検証する。
    primary = next(
        c
        for c in route.calls
        if "insightTrafficSourceType" not in c.request.url.params.get("dimensions", "")
    )
    req = primary.request
    params = req.url.params
    # 認証ヘッダ
    assert req.headers.get("Authorization") == f"Bearer {_FAKE_TOKEN}"
    # ids = channel==<channel_id>
    assert params.get("ids") == f"channel=={_CHANNEL_ID}"
    # dimensions=video
    assert params.get("dimensions") == "video"
    # 期間は metric_date 単日 (D-1 集計を呼び出し側が渡す)
    assert params.get("startDate") == metric_date.isoformat()
    assert params.get("endDate") == metric_date.isoformat()
    # metrics に主要指標を含む
    metrics = params.get("metrics", "")
    assert "views" in metrics
    assert "averageViewPercentage" in metrics
    # filters は対象動画に絞る
    assert video_id in params.get("filters", "")


@respx.mock
async def test_data_api_request_carries_bearer() -> None:
    """Data API videos.list も Authorization: Bearer を載せる。"""
    metric_date = date(2026, 6, 15)
    video_id = "vid_DATA"

    def _analytics_side_effect(request: httpx.Request) -> httpx.Response:
        dims = request.url.params.get("dimensions", "")
        if "insightTrafficSourceType" in dims:
            return httpx.Response(
                200, json=_traffic_response(video_id=video_id, sources={"YT_SEARCH": 1})
            )
        return httpx.Response(
            200,
            json=_reports_response(
                rows=[
                    {
                        "video": video_id,
                        "views": 7,
                        "estimatedMinutesWatched": 2.0,
                        "averageViewDuration": 5,
                        "averageViewPercentage": 12.0,
                    }
                ]
            ),
        )

    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=_analytics_side_effect)
    data_route = respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=_videos_response(items=[{"id": video_id}]))
    )

    session = SpySession()
    client = AnalyticsClient(_build_oauth())
    await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        metric_date=metric_date,
        video_ids=[video_id],
    )

    assert data_route.called
    assert data_route.calls.last.request.headers.get("Authorization") == f"Bearer {_FAKE_TOKEN}"


# ---------------------------------------------------------------------------
# 注入 http クライアントの尊重 (respx と同居できる任意 transport)
# ---------------------------------------------------------------------------


@respx.mock
async def test_uses_injected_http_client() -> None:
    """http= で渡した AsyncClient を使う (取得層が httpx 分離されている保証)。"""
    metric_date = date(2026, 6, 15)
    video_id = "vid_INJ"

    def _analytics_side_effect(request: httpx.Request) -> httpx.Response:
        dims = request.url.params.get("dimensions", "")
        if "insightTrafficSourceType" in dims:
            return httpx.Response(
                200, json=_traffic_response(video_id=video_id, sources={"YT_SEARCH": 1})
            )
        return httpx.Response(
            200,
            json=_reports_response(
                rows=[
                    {
                        "video": video_id,
                        "views": 3,
                        "estimatedMinutesWatched": 1.0,
                        "averageViewDuration": 2,
                        "averageViewPercentage": 10.0,
                    }
                ]
            ),
        )

    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=_analytics_side_effect)
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json=_videos_response(items=[{"id": video_id}]))
    )

    async with httpx.AsyncClient() as http:
        session = SpySession()
        client = AnalyticsClient(_build_oauth(), http=http)
        count = await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=metric_date,
            video_ids=[video_id],
        )

    assert count == 1


# ---------------------------------------------------------------------------
# 定数 sanity
# ---------------------------------------------------------------------------


def test_url_constants_sane() -> None:
    assert ANALYTICS_REPORTS_URL.startswith("https://")
    assert "reports" in ANALYTICS_REPORTS_URL
    assert YOUTUBE_VIDEOS_URL.startswith("https://")
    assert "videos" in YOUTUBE_VIDEOS_URL
