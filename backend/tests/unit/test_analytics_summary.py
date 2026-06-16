"""Unit テスト: build_analytics_summary の集計サマリ生成 (T103, 外部依存 mock)。

読み取り専用の集計のため、 ``AsyncSession`` は ``execute`` の戻り値を順に差し替えるスパイで
代替する(実 DB / 実 LLM は使わない)。 本モジュールは 2 回の ``execute``(by_genre →
top_videos)を行うため、 ``FakeSession`` は呼び出し順に応じた行集合を返す。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from ymg_backend.domain.analytics.summary import (
    AnalyticsSummaryData,
    GenreSummary,
    TopVideo,
    build_analytics_summary,
)

pytestmark = pytest.mark.unit


class _Result:
    """``session.execute(...)`` 戻り値の最小スタブ(``.all()`` のみ)。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """``execute`` を呼び出し順に応じた行集合へ差し替えるスパイ。

    本実装の呼び出し順は by_genre(集約)→ top_videos の 2 回。 各回に渡す行集合を
    コンストラクタで受け取り、 ``executed`` に発行 SQL 種別を記録する。
    """

    def __init__(self, *result_rows: list[Any]) -> None:
        self._results = list(result_rows)
        self._index = 0
        self.executed: list[str] = []

    async def execute(self, statement: Any) -> Any:
        self.executed.append(type(statement).__name__.lower())
        rows = self._results[self._index] if self._index < len(self._results) else []
        self._index += 1
        return _Result(rows)


def _genre_row(
    *, genre: str, role: str, video_count: int, avg_retention: Decimal | None, total_views: int
) -> SimpleNamespace:
    return SimpleNamespace(
        genre=genre,
        role=role,
        video_count=video_count,
        avg_retention_pct=avg_retention,
        total_views=total_views,
    )


def _video_row(
    *,
    youtube_video_id: str,
    genre: str,
    title: str,
    posted_at: datetime,
    avg_retention: Decimal | None,
    total_views: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        youtube_video_id=youtube_video_id,
        genre=genre,
        title=title,
        posted_at=posted_at,
        avg_retention_pct=avg_retention,
        total_views=total_views,
    )


# ===========================================================================
# 正常系: ジャンル別 + 動画別の集計を組み立てる
# ===========================================================================
async def test_build_summary_aggregates_genres_and_top_videos() -> None:
    """by_genre / top_videos / sample_size を集計し、 Decimal を丸めた float へ正規化する。"""
    posted = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    genre_rows = [
        _genre_row(
            genre="lo-fi hip-hop",
            role="primary",
            video_count=3,
            avg_retention=Decimal("48.755"),
            total_views=1200,
        ),
        _genre_row(
            genre="ambient",
            role="experimental",
            video_count=2,
            avg_retention=None,
            total_views=300,
        ),
    ]
    video_rows = [
        _video_row(
            youtube_video_id="vid-A",
            genre="lo-fi hip-hop",
            title="night study mix",
            posted_at=posted,
            avg_retention=Decimal("55.5"),
            total_views=800,
        ),
    ]
    session = FakeSession(genre_rows, video_rows)

    result = await build_analytics_summary(session, window_days=14)  # type: ignore[arg-type]

    assert isinstance(result, AnalyticsSummaryData)
    assert result.window_days == 14
    # sample_size はジャンル別 video_count の総和(3 + 2)
    assert result.sample_size == 5
    # 両端含む 14 日窓: window_end - window_start == 13 日
    assert (result.window_end - result.window_start) == timedelta(days=13)

    assert result.by_genre == (
        GenreSummary(
            genre="lo-fi hip-hop",
            role="primary",
            video_count=3,
            avg_retention_pct=48.8,  # 48.755 -> 1 桁丸め
            total_views=1200,
        ),
        GenreSummary(
            genre="ambient",
            role="experimental",
            video_count=2,
            avg_retention_pct=None,  # 全件 NULL は None
            total_views=300,
        ),
    )
    assert result.top_videos == (
        TopVideo(
            youtube_video_id="vid-A",
            genre="lo-fi hip-hop",
            title="night study mix",
            avg_retention_pct=55.5,
            total_views=800,
            posted_at=posted,
        ),
    )
    # by_genre → top_videos の順で 2 回 execute する
    assert len(session.executed) == 2


# ===========================================================================
# 空集計: 実績ゼロでも安全に空サマリを返す
# ===========================================================================
async def test_build_summary_empty_when_no_analytics() -> None:
    """ウィンドウ内に実績が無ければ by_genre / top_videos は空、 sample_size は 0。"""
    session = FakeSession([], [])

    result = await build_analytics_summary(session, window_days=7)  # type: ignore[arg-type]

    assert result.window_days == 7
    assert result.sample_size == 0
    assert result.by_genre == ()
    assert result.top_videos == ()
    assert (result.window_end - result.window_start) == timedelta(days=6)


# ===========================================================================
# 既定ウィンドウ
# ===========================================================================
async def test_build_summary_default_window_is_14_days() -> None:
    """window_days 省略時は 14 日窓を使う。"""
    session = FakeSession([], [])

    result = await build_analytics_summary(session)  # type: ignore[arg-type]

    assert result.window_days == 14
    assert (result.window_end - result.window_start) == timedelta(days=13)
    # window_end は本日 JST(タイムゾーン跨ぎの誤差を避け範囲で確認)
    today_utc = datetime.now(tz=UTC).date()
    assert result.window_end in {today_utc, today_utc + timedelta(days=1)}


# ===========================================================================
# 入力検証
# ===========================================================================
@pytest.mark.parametrize("bad", [0, -1, -14])
async def test_build_summary_rejects_non_positive_window(bad: int) -> None:
    """window_days < 1 は ValueError で弾く(集計前に検証)。"""
    session = FakeSession([], [])

    with pytest.raises(ValueError, match="window_days"):
        await build_analytics_summary(session, window_days=bad)  # type: ignore[arg-type]


# ===========================================================================
# 型: 公開データクラスは frozen(不変)
# ===========================================================================
def test_summary_dataclasses_are_frozen() -> None:
    """GenreSummary / TopVideo は不変(代入で FrozenInstanceError)。"""
    from dataclasses import FrozenInstanceError

    g = GenreSummary(
        genre="lo-fi hip-hop",
        role="primary",
        video_count=1,
        avg_retention_pct=50.0,
        total_views=100,
    )
    with pytest.raises(FrozenInstanceError):
        g.video_count = 2  # type: ignore[misc]
    v = TopVideo(
        youtube_video_id="vid-A",
        genre="lo-fi hip-hop",
        title="mix",
        avg_retention_pct=None,
        total_views=0,
        posted_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        v.total_views = 1  # type: ignore[misc]
