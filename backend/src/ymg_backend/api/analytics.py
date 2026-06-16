"""``/analytics`` 業務ルータ (US3 T110, contracts/backend-api.yaml ``/analytics``)。

frontend の分析画面 (チャート + 推奨テーブル) が直接消費する 1 endpoint を提供する。
``/analytics/summary`` が LLM 入力寄りの生集計を返すのに対し、 本 endpoint は
``by_genre`` (ジャンル別 retention/views) と ``recommendations`` (experiment ジャンルの
採用/削除/継続判定) を合成した :class:`AnalyticsOverview` を返す。

設計方針:

- 集計は :func:`~ymg_backend.domain.analytics.summary.build_analytics_summary` (T103) に、
  推奨は :func:`~ymg_backend.domain.genres.recommend.recommend_genres` (T106) に委譲する。
  本ルータは 2 つのドメイン結果を contract スキーマへ写像する「合成層」に徹し、 集計 SQL を
  ここに重複させない (関心の分離)。
- DB セッションは共有契約 (a) のとおり ``Depends(get_session)`` で受ける。 読み取り専用 GET
  のため ``commit`` は行わない (``get_session`` が例外時 rollback を担う)。
- 認証は共有契約 (c) のとおり親の ``_build_protected_router`` が一括付与するため、 本ルータは
  素の ``APIRouter`` を ``router`` で公開し ``require_basic_auth`` を再付与しない。 配線
  (``main.py`` の ``include_router``) は後段の責務。
- ``window_days`` は安全弁として下限/上限を設ける (DoS / 過大ウィンドウによる集計肥大を避ける)。
  契約既定は 14 日。
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.domain.analytics.summary import build_analytics_summary
from ymg_backend.domain.genres.recommend import recommend_genres
from ymg_backend.infrastructure.db.session import get_session

router: Final = APIRouter(prefix="/analytics", tags=["analytics"])

# 集計ウィンドウ日数の境界 (契約既定 14。 下限 1 / 上限 180 は ReferencedMetrics と整合)。
_DEFAULT_WINDOW_DAYS: Final[int] = 14
_MIN_WINDOW_DAYS: Final[int] = 1
_MAX_WINDOW_DAYS: Final[int] = 180

# recommend(T106) の action enum (contracts GenreRecommendation.action)。
RecommendAction = Literal["adopt", "drop", "keep"]


# ---------------------------------------------------------------------------
# レスポンススキーマ (backend-api.yaml schemas.AnalyticsOverview に整合)
# ---------------------------------------------------------------------------
class GenreAnalyticsResponse(BaseModel):
    """ジャンル別集計 1 件 (backend-api.yaml ``schemas.GenreAnalytics``)。"""

    genre: str
    role: str
    video_count: int
    avg_retention_pct: float | None = None
    total_views: int


class GenreRecommendationResponse(BaseModel):
    """ジャンル採用/削除推奨 1 件 (backend-api.yaml ``schemas.GenreRecommendation``)。"""

    genre: str
    action: RecommendAction
    retention_ratio_to_primary: float | None = None
    sample_size: int
    days_elapsed: int
    rationale: str


class AnalyticsOverviewResponse(BaseModel):
    """``GET /analytics`` のレスポンス封筒 (backend-api.yaml ``schemas.AnalyticsOverview``)。"""

    window_days: int
    sample_size: int
    by_genre: list[GenreAnalyticsResponse]
    recommendations: list[GenreRecommendationResponse]


# ---------------------------------------------------------------------------
# GET /analytics
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=AnalyticsOverviewResponse,
    summary="Genre retention/views overview + adopt/drop recommendations",
)
async def get_analytics(
    session: Annotated[AsyncSession, Depends(get_session)],
    window_days: Annotated[
        int, Query(ge=_MIN_WINDOW_DAYS, le=_MAX_WINDOW_DAYS)
    ] = _DEFAULT_WINDOW_DAYS,
) -> AnalyticsOverviewResponse:
    """ジャンル別の retention/views 集計と採用/削除推奨を合成して返す。

    ``by_genre`` は :func:`build_analytics_summary` (T103) のジャンル別生集計を、
    ``recommendations`` は :func:`recommend_genres` (T106) の experiment ジャンル判定を
    それぞれ contract スキーマへ写像する。 同一 ``window_days`` を両者へ渡し、 集計窓を揃える。
    読み取り専用のため commit はしない。
    """
    summary = await build_analytics_summary(session, window_days=window_days)
    recommendations = await recommend_genres(session, window_days=window_days)

    return AnalyticsOverviewResponse(
        window_days=summary.window_days,
        sample_size=summary.sample_size,
        by_genre=[
            GenreAnalyticsResponse(
                genre=g.genre,
                role=g.role,
                video_count=g.video_count,
                avg_retention_pct=(
                    float(g.avg_retention_pct) if g.avg_retention_pct is not None else None
                ),
                total_views=g.total_views,
            )
            for g in summary.by_genre
        ],
        recommendations=[
            GenreRecommendationResponse(
                genre=r.genre,
                action=r.action,
                retention_ratio_to_primary=(
                    float(r.retention_ratio_to_primary)
                    if r.retention_ratio_to_primary is not None
                    else None
                ),
                sample_size=r.sample_size,
                days_elapsed=r.days_elapsed,
                rationale=r.rationale,
            )
            for r in recommendations
        ],
    )


__all__ = [
    "AnalyticsOverviewResponse",
    "GenreAnalyticsResponse",
    "GenreRecommendationResponse",
    "router",
]
