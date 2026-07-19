"""experiment ジャンルの採用 / 削除を半自動判定する recommend (T106 / FR-037)。

``experiment_slot`` で投入した新ジャンルが十分に検証されたか (本数・経過日数) を確認し、
主力ジャンル (``role="main"``) の平均 retention と比較して採用 / 削除 / 継続を推奨する。
判定結果は WeeklyPlan の rationale や ``GET /analytics`` (``api/analytics.py``) で人間に提示し、
最終承認は管理 UI 経由で ``Genre.role`` を遷移させる (FR-038、 本モジュールは判定のみ)。

判定ロジック (FR-037 / spec.md L183):

- 前提: experiment ジャンルが **4 本以上投稿** かつ **14 日以上経過**。
  未達なら ``keep`` (rationale に「本数 N / 経過 D 日でサンプル不足」と明記)。
- 前提を満たす場合、 主力ジャンル平均 retention に対する比 ``r`` で:
    - ``r >= 0.80`` → ``adopt`` (採用推奨)
    - ``r <  0.60`` → ``drop``  (削除推奨)
    - その間        → ``keep``  (継続)
- 主力 retention が 0 / 未取得、 または experiment 側 retention が未取得の場合は比を
  算出できないため ``keep`` (rationale に理由)。

retention は ``analytics_daily.retention_pct`` の動画横断平均を採る (動画ごとに複数日の
行があり得るため日次行単位の単純平均)。 経過日数は当該ジャンルの最古 ``videos.posted_at``
から ``window`` 終端時刻 (= 評価時点) までの日数。 本数は当該ジャンルの動画本数 (``videos``)。

commit は不要 (読み取りのみ)。 session は呼び出し側が管理する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import func, select

from ymg_backend.infrastructure.db.models import AnalyticsDaily, Genre, Video

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# 主力ジャンルの role 値 (シード値 ``main`` に整合、 alembic 001_initial.py)。
PRIMARY_ROLE: Final[str] = "main"
# 実験ジャンルの role 値 (シード値 ``experiment`` に整合)。
EXPERIMENT_ROLE: Final[str] = "experiment"

# FR-037 の判定前提。
MIN_VIDEO_COUNT: Final[int] = 4  # 採用 / 削除を判定するに足る投入本数。
MIN_DAYS_ELAPSED: Final[int] = 14  # 投入から十分な観察期間 (日)。

# 主力 retention に対する比の閾値 (FR-037)。
ADOPT_RATIO_THRESHOLD: Final[float] = 0.80  # これ以上で採用推奨。
DROP_RATIO_THRESHOLD: Final[float] = 0.60  # これ未満で削除推奨。

RecommendAction = Literal["adopt", "drop", "keep"]


@dataclass(frozen=True, slots=True)
class GenreRecommendation:
    """1 experiment ジャンルに対する採用 / 削除 / 継続の推奨 (不変、 契約準拠)。

    Attributes:
        genre: 対象ジャンル名 (``Genre.name``)。
        action: ``"adopt"`` (採用推奨) / ``"drop"`` (削除推奨) / ``"keep"`` (継続)。
        retention_ratio_to_primary: 主力ジャンル平均 retention に対する比。 算出不能なら ``None``。
        sample_size: 当該ジャンルの投入本数 (``videos`` 件数)。
        days_elapsed: 最古投稿からの経過日数 (評価時点まで)。 投稿が無ければ 0。
        rationale: 判定理由 (本数・経過・比を引用、 WeeklyPlan rationale / UI 表示用)。
    """

    genre: str
    action: RecommendAction
    retention_ratio_to_primary: float | None
    sample_size: int
    days_elapsed: int
    rationale: str


@dataclass(frozen=True, slots=True)
class _GenreStats:
    """1 ジャンルの集計 (本数 / 平均 retention / 最古投稿日時)。"""

    genre: str
    video_count: int
    avg_retention_pct: float | None
    earliest_posted_at: datetime | None


def _to_float(value: Decimal | float | None) -> float | None:
    """``Decimal`` / ``float`` / ``None`` を ``float | None`` に正規化する。"""
    if value is None:
        return None
    return float(value)


async def recommend_genres(
    session: AsyncSession,
    *,
    window_days: int = 14,
) -> list[GenreRecommendation]:
    """experiment ジャンルごとに採用 / 削除 / 継続を判定して返す (T106 / FR-037)。

    主力ジャンル (``role="main"``) の平均 retention をベースラインに、 各 experiment
    ジャンルの本数・経過日数・retention 比から ``adopt`` / ``drop`` / ``keep`` を決める。

    Args:
        session: ``genres`` / ``videos`` / ``analytics_daily`` を参照する AsyncSession。
        window_days: retention 集計に含める日数のウィンドウ (``analytics_daily.metric_date``
            が ``[now - window_days, now]`` の行を対象)。 1 以上。

    Returns:
        experiment ジャンルごとの :class:`GenreRecommendation` を genre 名昇順で。
        experiment ジャンルが無ければ空リスト。

    Raises:
        ValueError: ``window_days < 1`` の場合 (境界での入力検証)。
    """
    if window_days < 1:
        raise ValueError("window_days must be >= 1")

    now = datetime.now(UTC)
    window_start_date = (now - timedelta(days=window_days)).date()

    primary_avg = await _primary_avg_retention(session, window_start_date)
    experiments = await _experiment_genre_stats(session, window_start_date)

    recommendations = [_evaluate(stats, primary_avg=primary_avg, now=now) for stats in experiments]
    return sorted(recommendations, key=lambda r: r.genre)


def _evaluate(
    stats: _GenreStats,
    *,
    primary_avg: float | None,
    now: datetime,
) -> GenreRecommendation:
    """1 ジャンルの集計から推奨を決める (純粋ロジック、 DB 非依存でテスト容易)。"""
    days_elapsed = _days_elapsed(stats.earliest_posted_at, now)

    # --- 前提チェック: 本数 >= 4 かつ 経過 >= 14 日 -------------------------------
    if stats.video_count < MIN_VIDEO_COUNT or days_elapsed < MIN_DAYS_ELAPSED:
        return GenreRecommendation(
            genre=stats.genre,
            action="keep",
            retention_ratio_to_primary=None,
            sample_size=stats.video_count,
            days_elapsed=days_elapsed,
            rationale=(
                f"判定前提未達 (本数 {stats.video_count}/{MIN_VIDEO_COUNT}、 "
                f"経過 {days_elapsed}/{MIN_DAYS_ELAPSED} 日)。 継続して観察する。"
            ),
        )

    # --- 比が算出不能なケース (retention 未取得 / 主力 0) ------------------------
    if primary_avg is None or primary_avg <= 0.0 or stats.avg_retention_pct is None:
        return GenreRecommendation(
            genre=stats.genre,
            action="keep",
            retention_ratio_to_primary=None,
            sample_size=stats.video_count,
            days_elapsed=days_elapsed,
            rationale=(
                "主力ジャンルまたは当該ジャンルの retention が未取得のため比を算出できない。"
                " retention 取得後に再判定する。"
            ),
        )

    ratio = round(stats.avg_retention_pct / primary_avg, 4)
    action: RecommendAction
    if ratio >= ADOPT_RATIO_THRESHOLD:
        action = "adopt"
        verdict = "採用推奨"
    elif ratio < DROP_RATIO_THRESHOLD:
        action = "drop"
        verdict = "削除推奨"
    else:
        action = "keep"
        verdict = "継続"

    rationale = (
        f"本数 {stats.video_count}・経過 {days_elapsed} 日。 retention "
        f"{round(stats.avg_retention_pct, 1)}% は主力平均 {round(primary_avg, 1)}% の "
        f"{round(ratio * 100, 1)}% (比 {ratio})。 "
        f"閾値 採用>={ADOPT_RATIO_THRESHOLD:.0%} / 削除<{DROP_RATIO_THRESHOLD:.0%} より {verdict}。"
    )
    return GenreRecommendation(
        genre=stats.genre,
        action=action,
        retention_ratio_to_primary=ratio,
        sample_size=stats.video_count,
        days_elapsed=days_elapsed,
        rationale=rationale,
    )


def _days_elapsed(earliest_posted_at: datetime | None, now: datetime) -> int:
    """最古投稿日時から ``now`` までの経過日数 (負値・None は 0 に丸める)。"""
    if earliest_posted_at is None:
        return 0
    # naive datetime (DB から tz 無しで来た場合) を UTC として扱い比較可能にする。
    posted = (
        earliest_posted_at
        if earliest_posted_at.tzinfo is not None
        else earliest_posted_at.replace(tzinfo=UTC)
    )
    delta = now - posted
    return max(delta.days, 0)


async def _primary_avg_retention(
    session: AsyncSession,
    window_start_date: date,
) -> float | None:
    """主力ジャンル (``role="main"``) の平均 retention をウィンドウ内で集計する。

    ``analytics_daily`` を ``videos`` → ``genres`` (role) で結合し、 主力ジャンルに属する
    日次行の ``retention_pct`` 単純平均を返す。 該当行が無ければ ``None``。
    """
    stmt = (
        select(func.avg(AnalyticsDaily.retention_pct))
        .select_from(AnalyticsDaily)
        .join(Video, Video.youtube_video_id == AnalyticsDaily.youtube_video_id)
        .join(Genre, Genre.name == Video.genre)
        .where(Genre.role == PRIMARY_ROLE)
        .where(AnalyticsDaily.metric_date >= window_start_date)
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    return _to_float(value)


async def _experiment_genre_stats(
    session: AsyncSession,
    window_start_date: date,
) -> list[_GenreStats]:
    """experiment ジャンルごとの本数 / 平均 retention / 最古投稿日時を集計する。

    本数 (``video_count``) と最古投稿 (``earliest_posted_at``) は ``videos`` 全体から
    (retention の有無に依存させない)、 平均 retention はウィンドウ内 ``analytics_daily``
    の動画横断平均を別クエリで採り、 ジャンル単位で突き合わせる。 これにより analytics
    未取得のジャンルでも本数・経過の前提判定 (``keep``) を成立させられる。
    """
    # 1. experiment ジャンルの本数 / 最古投稿 (videos + genres、 全期間)。
    base_stmt = (
        select(
            Genre.name.label("genre"),
            func.count(Video.id).label("video_count"),
            func.min(Video.posted_at).label("earliest_posted_at"),
        )
        .select_from(Genre)
        .outerjoin(Video, Video.genre == Genre.name)
        .where(Genre.role == EXPERIMENT_ROLE)
        .group_by(Genre.name)
    )
    base_rows = (await session.execute(base_stmt)).all()

    # 2. experiment ジャンルのウィンドウ内 平均 retention (analytics_daily 経由)。
    retention_stmt = (
        select(
            Genre.name.label("genre"),
            func.avg(AnalyticsDaily.retention_pct).label("avg_retention_pct"),
        )
        .select_from(AnalyticsDaily)
        .join(Video, Video.youtube_video_id == AnalyticsDaily.youtube_video_id)
        .join(Genre, Genre.name == Video.genre)
        .where(Genre.role == EXPERIMENT_ROLE)
        .where(AnalyticsDaily.metric_date >= window_start_date)
        .group_by(Genre.name)
    )
    retention_by_genre = {
        row.genre: _to_float(row.avg_retention_pct)
        for row in (await session.execute(retention_stmt)).all()
    }

    return [
        _GenreStats(
            genre=row.genre,
            video_count=int(row.video_count),
            avg_retention_pct=retention_by_genre.get(row.genre),
            earliest_posted_at=row.earliest_posted_at,
        )
        for row in base_rows
    ]


__all__ = [
    "ADOPT_RATIO_THRESHOLD",
    "DROP_RATIO_THRESHOLD",
    "EXPERIMENT_ROLE",
    "MIN_DAYS_ELAPSED",
    "MIN_VIDEO_COUNT",
    "PRIMARY_ROLE",
    "GenreRecommendation",
    "RecommendAction",
    "recommend_genres",
]
