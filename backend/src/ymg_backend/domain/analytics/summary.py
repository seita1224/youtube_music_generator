"""analytics 集計サマリ生成 (ADR-0021 / ADR-0032 方針継承)。

企画 LLM 入力と frontend ``/analytics`` 概要の双方が消費する、
``analytics_daily`` + ``videos`` + ``genres`` の **読み取り専用** 集計を提供する。

本モジュールはジャンル別の役割(``role``)込みの「現状サマリ」と、 retention 上位の代表動画
(``top_videos``)を返す。 企画 LLM は ``AnalyticsSummaryData`` を実績スナップショット
(``metrics_snapshots``)の材料や user prompt の集計節へ、 ``/analytics`` API は
ジャンル別カードへ転用する。

集計ウィンドウは「直近 ``window_days`` 日」で、 アンカーは JST の本日。 区間は
``[anchor - window_days + 1, anchor]`` の **両端含む**(``analytics_daily.metric_date`` の DATE と
比較)。 daily planner が ``[target_date - N, target_date - 1]`` と当日除外なのと異なり、 こちらは
取得済み最新日(本日分まで)を含む現状把握用のため当日も含める。

集計仕様:

- ``by_genre``: ``genres`` を起点に左結合し、 ウィンドウ内 ``analytics_daily`` をジャンル別に集約。
  enabled / disabled を問わず ``analytics_daily`` の実績があるジャンルを対象とする(実験 →
  drop 判断にも disabled ジャンルの過去実績が要るため)。 ``avg_retention_pct`` は
  ``retention_pct``(NULL 可)の平均で、 全件 NULL なら ``None``。 ``video_count`` はウィンドウ内に
  指標行を持つ **ユニーク動画数**(日次行の単純件数ではない)。 ``total_views`` は ``views`` 総和。
- ``top_videos``: ウィンドウ内の動画別集計(retention 平均 / views 総和)を retention 降順
  (NULL は末尾)で上位 :data:`_TOP_VIDEOS_LIMIT` 件。
- ``sample_size``: ウィンドウ内に指標行を持つユニーク動画数の総計。

エラー方針: 読み取り専用のため副作用なし。 ``session`` は呼び出し側のトランザクションを共有し、
commit / rollback は呼び出し側責務。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, cast, func, select

from ymg_backend.infrastructure.db.models import AnalyticsDaily, Genre, Video

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# planner / scheduler と同じ JST 基準(infrastructure/scheduler.py の JST と一致させる)。
_JST: Final[ZoneInfo] = ZoneInfo("Asia/Tokyo")

# top_videos に載せる代表動画の上限(プロンプト / カードの肥大化を防ぐ安全弁)。
_TOP_VIDEOS_LIMIT: Final[int] = 10

# retention / views の表示丸め桁数。
_RETENTION_DIGITS: Final[int] = 1


@dataclass(frozen=True, slots=True)
class GenreSummary:
    """1 ジャンル分の集計サマリ(集計ウィンドウ内)。

    Attributes:
        genre: ジャンル名(``Genre.name`` / ``Video.genre``)。
        role: ジャンルの役割(``primary`` / ``extended`` / ``experimental`` 等の文字列)。
        video_count: ウィンドウ内に指標行を持つユニーク動画数。
        avg_retention_pct: ``retention_pct`` の平均(全件 NULL なら ``None``)。
        total_views: ``views`` 総和。
    """

    genre: str
    role: str
    video_count: int
    avg_retention_pct: float | None
    total_views: int


@dataclass(frozen=True, slots=True)
class TopVideo:
    """retention 上位の代表動画 1 件(集計ウィンドウ内)。

    Attributes:
        youtube_video_id: YouTube 動画 ID。
        genre: 動画のジャンル(``Video.genre``)。
        title: 動画タイトル。
        avg_retention_pct: ウィンドウ内 ``retention_pct`` 平均(全件 NULL なら ``None``)。
        total_views: ウィンドウ内 ``views`` 総和。
        posted_at: 動画の公開日時(``Video.posted_at``)。
    """

    youtube_video_id: str
    genre: str
    title: str
    avg_retention_pct: float | None
    total_views: int
    posted_at: datetime


@dataclass(frozen=True, slots=True)
class AnalyticsSummaryData:
    """analytics 集計サマリの不変ビュー(planner 入力 / ``/analytics`` 概要の共通材料)。

    Attributes:
        window_days: 集計ウィンドウ日数。
        window_start: 集計区間の開始日(両端含む)。
        window_end: 集計区間の終了日(両端含む、 通常は本日 JST)。
        sample_size: ウィンドウ内に指標行を持つユニーク動画数の総計。
        by_genre: ジャンル別サマリ(retention 降順、 NULL 末尾)。
        top_videos: retention 上位の代表動画。
    """

    window_days: int
    window_start: date
    window_end: date
    sample_size: int
    by_genre: tuple[GenreSummary, ...]
    top_videos: tuple[TopVideo, ...]


def _to_float(value: Decimal | float | None) -> float | None:
    """``Decimal`` / ``float`` / ``None`` を ``float | None`` に正規化する。"""
    if value is None:
        return None
    return float(value)


def _round_or_none(value: float | None, *, digits: int = _RETENTION_DIGITS) -> float | None:
    """``None`` を保ちつつ丸める(表示桁数を揃える)。"""
    return None if value is None else round(value, digits)


def _resolve_window(window_days: int) -> tuple[date, date]:
    """``window_days`` から両端含むの集計区間 ``(window_start, window_end)`` を返す。

    アンカーは本日 JST。 区間は ``[anchor - window_days + 1, anchor]``。

    Raises:
        ValueError: ``window_days < 1`` の場合。
    """
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    window_end = datetime.now(tz=_JST).date()
    window_start = window_end - timedelta(days=window_days - 1)
    return window_start, window_end


async def build_analytics_summary(
    session: AsyncSession,
    *,
    window_days: int = 14,
) -> AnalyticsSummaryData:
    """直近 ``window_days`` 日の ``analytics_daily`` をジャンル別 / 動画別に集計して返す。

    読み取り専用(副作用なし、 commit は呼び出し側)。 集計区間は本日 JST を終端とする
    両端含むの ``window_days`` 日間。 ジャンルは ``genres`` を起点に ``videos`` →
    ``analytics_daily`` を左結合し、 ウィンドウ内に実績のあるジャンルのみを ``by_genre`` に含める。

    Args:
        session: 集計に使う AsyncSession(commit は呼び出し側)。
        window_days: 集計ウィンドウ日数(1 以上)。

    Returns:
        ジャンル別サマリと retention 上位動画を束ねた :class:`AnalyticsSummaryData`。

    Raises:
        ValueError: ``window_days < 1`` の場合。
    """
    window_start, window_end = _resolve_window(window_days)

    by_genre = await _aggregate_by_genre(session, window_start, window_end)
    top_videos = await _aggregate_top_videos(session, window_start, window_end)
    sample_size = sum(g.video_count for g in by_genre)

    return AnalyticsSummaryData(
        window_days=window_days,
        window_start=window_start,
        window_end=window_end,
        sample_size=sample_size,
        by_genre=by_genre,
        top_videos=top_videos,
    )


async def _aggregate_by_genre(
    session: AsyncSession,
    window_start: date,
    window_end: date,
) -> tuple[GenreSummary, ...]:
    """ジャンル別に ``video_count`` / ``avg_retention_pct`` / ``total_views`` を集計する。

    ``genres`` を起点に ``videos`` / ``analytics_daily`` を左結合し、 ウィンドウ内に指標行を持つ
    ジャンル(``video_count > 0``)のみを retention 降順(NULL 末尾)で返す。 ``role`` は
    ``genres`` 行から取り、 役割込みのジャンル状況を 1 行で表す。
    """
    avg_retention = func.avg(AnalyticsDaily.retention_pct)
    stmt = (
        select(
            Genre.name.label("genre"),
            Genre.role.label("role"),
            func.count(func.distinct(AnalyticsDaily.youtube_video_id)).label("video_count"),
            avg_retention.label("avg_retention_pct"),
            func.coalesce(func.sum(cast(AnalyticsDaily.views, Integer)), 0).label("total_views"),
        )
        .select_from(Genre)
        .join(Video, Video.genre == Genre.name)
        .join(
            AnalyticsDaily,
            (AnalyticsDaily.youtube_video_id == Video.youtube_video_id)
            & (AnalyticsDaily.metric_date >= window_start)
            & (AnalyticsDaily.metric_date <= window_end),
        )
        .group_by(Genre.name, Genre.role)
        .having(func.count(func.distinct(AnalyticsDaily.youtube_video_id)) > 0)
        .order_by(avg_retention.desc().nullslast(), Genre.name.asc())
    )
    rows = (await session.execute(stmt)).all()
    return tuple(
        GenreSummary(
            genre=row.genre,
            role=row.role,
            video_count=int(row.video_count),
            avg_retention_pct=_round_or_none(_to_float(row.avg_retention_pct)),
            total_views=int(row.total_views),
        )
        for row in rows
    )


async def _aggregate_top_videos(
    session: AsyncSession,
    window_start: date,
    window_end: date,
) -> tuple[TopVideo, ...]:
    """ウィンドウ内の動画別集計を retention 降順(NULL 末尾)で上位 N 件返す。"""
    avg_retention = func.avg(AnalyticsDaily.retention_pct)
    stmt = (
        select(
            Video.youtube_video_id.label("youtube_video_id"),
            Video.genre.label("genre"),
            Video.title.label("title"),
            Video.posted_at.label("posted_at"),
            avg_retention.label("avg_retention_pct"),
            func.coalesce(func.sum(cast(AnalyticsDaily.views, Integer)), 0).label("total_views"),
        )
        .select_from(Video)
        .join(
            AnalyticsDaily,
            (AnalyticsDaily.youtube_video_id == Video.youtube_video_id)
            & (AnalyticsDaily.metric_date >= window_start)
            & (AnalyticsDaily.metric_date <= window_end),
        )
        .group_by(
            Video.youtube_video_id,
            Video.genre,
            Video.title,
            Video.posted_at,
        )
        .order_by(avg_retention.desc().nullslast(), func.sum(AnalyticsDaily.views).desc())
        .limit(_TOP_VIDEOS_LIMIT)
    )
    rows = (await session.execute(stmt)).all()
    return tuple(
        TopVideo(
            youtube_video_id=row.youtube_video_id,
            genre=row.genre,
            title=row.title,
            avg_retention_pct=_round_or_none(_to_float(row.avg_retention_pct)),
            total_views=int(row.total_views),
            posted_at=row.posted_at,
        )
        for row in rows
    )


__all__ = [
    "AnalyticsSummaryData",
    "GenreSummary",
    "TopVideo",
    "build_analytics_summary",
]
