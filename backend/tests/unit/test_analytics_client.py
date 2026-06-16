"""Unit テスト: AnalyticsClient の取得 + upsert (T101, 外部依存 mock)。

critical (``tests/critical/test_youtube_analytics_client.py``) が全指標カバーを担う。
本 unit テストは軽量に、 単一動画の取得→upsert と空入力ガードのみを確認する。

HTTP は respx mock、 OAuth は ``get_access_token`` を monkeypatch、 session は
副作用記録スパイ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from ymg_backend.domain.analytics.client import (
    ANALYTICS_REPORTS_URL,
    YOUTUBE_VIDEOS_URL,
    AnalyticsClient,
)
from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth

pytestmark = pytest.mark.unit

_FAKE_TOKEN = "unit-token"
_CHANNEL_ID = "UC_unit"


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


class FakeSession:
    """upsert / select を記録する ``AsyncSession`` スパイ。"""

    def __init__(self, video_ids: list[str] | None = None) -> None:
        self.recorded: list[_Recorded] = []
        self.flush_count = 0
        self._video_ids = video_ids or []

    async def execute(self, statement: Any) -> Any:
        kind = type(statement).__name__.lower()
        if "select" in kind:
            self.recorded.append(_Recorded("videos", "select", {}))
            return _Result([(v,) for v in self._video_ids])
        compiled = statement.compile()
        table = getattr(statement, "table", None)
        self.recorded.append(
            _Recorded(table.name if table is not None else "?", kind, dict(compiled.params))
        )
        return _Result([])

    async def flush(self) -> None:
        self.flush_count += 1

    @property
    def upserts(self) -> list[_Recorded]:
        return [r for r in self.recorded if r.table_name == "analytics_daily"]


class _StubOAuth:
    """``YouTubeOAuth`` の代替 (固定トークン + channel_id 参照用 ``_settings``)。

    ``YouTubeOAuth`` は ``__slots__`` で ``get_access_token`` を上書きできないため、
    実 OAuth を構築せず、 ``AnalyticsClient`` が触れる I/F (``get_access_token`` と
    ``_oauth_channel_id`` が読む ``_settings.youtube_channel_id``) のみを持つ stub を渡す。
    """

    class _Settings:
        youtube_channel_id = _CHANNEL_ID

    def __init__(self) -> None:
        self._settings = self._Settings()

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


def _make_oauth() -> YouTubeOAuth:
    return _StubOAuth()  # type: ignore[return-value]


def _reports(rows: list[list[Any]], columns: list[str]) -> dict[str, Any]:
    return {"columnHeaders": [{"name": c} for c in columns], "rows": rows}


@respx.mock
async def test_single_video_upsert() -> None:
    """1 動画分の指標を解析し analytics_daily へ upsert、 件数 1 を返す。"""
    metric_date = date(2026, 6, 15)
    video_id = "uv1"

    def _side_effect(request: httpx.Request) -> httpx.Response:
        if "insightTrafficSourceType" in request.url.params.get("dimensions", ""):
            return httpx.Response(
                200,
                json=_reports([["YT_SEARCH", 42]], ["insightTrafficSourceType", "views"]),
            )
        return httpx.Response(
            200,
            json=_reports(
                [[video_id, 100, 50.0, 30, 33.3]],
                [
                    "video",
                    "views",
                    "estimatedMinutesWatched",
                    "averageViewDuration",
                    "averageViewPercentage",
                ],
            ),
        )

    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(side_effect=_side_effect)
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [{"id": video_id}]})
    )

    session = FakeSession()
    client = AnalyticsClient(_make_oauth())
    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        metric_date=metric_date,
        video_ids=[video_id],
    )

    assert count == 1
    assert len(session.upserts) == 1
    values = session.upserts[0].values
    assert values.get("youtube_video_id") == video_id
    assert values.get("views") == 100
    assert Decimal(str(values.get("retention_pct"))) == Decimal("33.3")
    assert values.get("traffic_sources", {}).get("YT_SEARCH") == 42
    assert session.flush_count >= 1


@respx.mock
async def test_empty_targets_short_circuits() -> None:
    """対象 0 件なら API を叩かず 0 を返す。"""
    route = respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(200, json=_reports([], ["video", "views"]))
    )
    session = FakeSession(video_ids=[])
    client = AnalyticsClient(_make_oauth())

    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        metric_date=date(2026, 6, 15),
        video_ids=None,
    )

    assert count == 0
    assert session.upserts == []
    assert not route.called
