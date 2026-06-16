"""週次サイクル (改善計画 + ジャンルローテーション) の統合テスト (T099)。

`WeeklyPlanGenerator` で 1 本の `WeeklyPlan` を生成し、 承認後の
`apply_weekly_rotation` が `genres` テーブルへ反映するところまでを実 Postgres で
1 本通す。 加えて experiment_slot の採否判定 (`recommend_genres`、 FR-037/038) を
検証する。

検証対象 (US3 内部契約):

- ``WeeklyPlanGenerator.generate_weekly_plan`` / ``create_weekly_plan``
  (``domain/plans/weekly_planner.py``、 ``PlanGenerator`` の流れを踏襲)。
- ``apply_weekly_rotation`` (``domain/genres/rotation.py``)。
- ``recommend_genres`` (``domain/genres/recommend.py``、 FR-037/038 の閾値判定)。

外部依存の扱い (起動禁止):

- LLM (planner): 構造化出力 (固定 ``WeeklyPlan``) を返す stub provider を注入する。
  provider は ``response_model.model_validate`` を context なしで呼ぶため、 weekly_planner
  側が ``WeeklyPlan.model_validate(context={ALLOWED_GENRES_CONTEXT_KEY: allowed})`` で
  再検証する契約。 ここではその allowed 集合と整合する固定プランを返させる。
- prompts: weekly 用 system prompt が無い前提なので、 ``planner/system`` を流用する
  (実 ``backend/prompts`` を解決する PromptLoader を注入)。

DB: ``analytics_daily`` / ``videos`` / ``plans`` / ``genres`` を Postgres 固有型
(JSONB/UUID/ENUM/ARRAY) で永続化するため SQLite では動かせない。 実 Postgres へ
接続できない環境では skip する (test_daily_cycle_pipeline.py の DB fixture/_require_db
を踏襲)。

実装モジュール (weekly_planner / rotation / recommend) が未実装の TDD RED 段階では
import 不能のため、 モジュール解決失敗時も skip する (実装が入った時点で緑になる契約)。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ymg_backend.domain.plans.schemas import (
    ALLOWED_GENRES_CONTEXT_KEY,
    ExperimentSlot,
    ReferencedMetrics,
    WeeklyPlan,
)
from ymg_backend.domain.prompts.loader import PromptLoader
from ymg_backend.infrastructure.db.models import (
    AnalyticsDaily,
    Base,
    Genre,
    Plan,
    PlanMetricSnapshot,
    Video,
)
from ymg_backend.llm.base import LlmProviderName, LlmRequest, LlmResponse, LlmUsage

# 未実装モジュール群 (TDD RED 段階)。 import 失敗時は本ファイル全体を collection 時点で
# skip する。 公開シンボルは module オブジェクト経由で参照し、 トップレベル import を
# 増やさない (importorskip ゲートより前に解決させないため)。
weekly_planner = pytest.importorskip("ymg_backend.domain.plans.weekly_planner")
rotation = pytest.importorskip("ymg_backend.domain.genres.rotation")
recommend = pytest.importorskip("ymg_backend.domain.genres.recommend")

pytestmark = pytest.mark.integration

# 実 planner prompts (``backend/prompts/planner``) を解決するため repo の prompts root を使う。
from pathlib import Path  # noqa: E402  (importorskip ゲート後の意図的な遅延 import)

_PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"

# 週開始は月曜 JST (2026-06-15 は月曜)。
_TARGET_WEEK_START = date(2026, 6, 15)
# analytics 集計ウィンドウ: target_week_start の前週 (月曜の前日まで)。
_WINDOW_END = _TARGET_WEEK_START - timedelta(days=1)

# ジャンル辞書 (role は alembic 001_initial.py のシード値に整合: main/extension/experiment)。
_PRIMARY = "lo-fi hip-hop"  # 主力。 retention 基準。
_EXTENDED = "chillhop"  # 拡張。 WeeklyPlan で avoid に回す。
_EXPERIMENT_RUNNING = "synthwave"  # 評価中の experiment (>=4本 + >=14日経過)。
_EXPERIMENT_NEW = "future funk"  # WeeklyPlan が新規に投入する experiment (genres 行が無い)。


# --- DB 接続ヘルパ (test_daily_cycle_pipeline.py 踏襲) -----------------------------


def _async_url() -> str:
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


def _sync_url() -> str:
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def _require_db() -> None:
    """実 Postgres へ接続できなければ skip する (同期ドライバで軽く疎通確認)。"""
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url())
    try:
        engine.connect().close()
    except OperationalError as exc:
        pytest.skip(f"postgres へ接続できないため skip: {exc}")
    finally:
        engine.dispose()


# --- LLM stub provider ------------------------------------------------------------


def _zero_usage() -> LlmUsage:
    return LlmUsage(
        prompt_tokens=200,
        cached_tokens=0,
        completion_tokens=120,
        cost_usd=0.0031,
        duration_ms=12,
    )


class _StubWeeklyProvider:
    """固定 ``WeeklyPlan`` を返す stub provider (実 LLM を呼ばない)。

    provider 実装は ``response_model.model_validate`` を **context なし**で呼ぶ契約
    (openai/anthropic provider)。 ここでも辞書照合のない素の WeeklyPlan を parsed として
    返し、 weekly_planner 側の ``model_validate(context=...)`` 再検証を本物の経路で通す。
    生成された ``LlmRequest`` は ``requests`` に控え、 weekly 指示や response_model の
    妥当性をテストから観測できるようにする。
    """

    def __init__(self, plan: WeeklyPlan, *, provider: LlmProviderName = "openai") -> None:
        self._plan = plan
        self._provider: LlmProviderName = provider
        self.requests: list[LlmRequest[Any]] = []

    async def generate(self, req: LlmRequest[Any]) -> LlmResponse[Any]:
        self.requests.append(req)
        return LlmResponse(
            parsed=self._plan,
            raw_text=self._plan.model_dump_json(),
            usage=_zero_usage(),
            provider=self._provider,
            model="gpt-4.1",
            finish_reason="stop",
        )

    def supported_models(self) -> list[str]:
        return ["gpt-4.1"]

    def supports_caching(self) -> bool:
        return False

    async def health_check(self) -> bool:
        return True


def _weekly_plan() -> WeeklyPlan:
    """全制約を満たす固定 WeeklyPlan。

    - ``genre_distribution`` 合計 1.0 (主力 0.6 + 拡張 0.25 + 実験 0.15)。
    - ``avoid_genres`` に ``chillhop`` (rotation で enabled=False になる対象)。
    - ``experiment_slots`` に新規 ``future funk`` (rotation で新規行 + role="experiment" になる対象)。
    - ``referenced_metrics.window_days`` は集計ウィンドウ (7) と一致させる。
    """
    return WeeklyPlan(
        plan_id=str(uuid.uuid4()),
        target_week_start=_TARGET_WEEK_START,
        genre_distribution={_PRIMARY: 0.6, _EXPERIMENT_RUNNING: 0.25, _EXPERIMENT_NEW: 0.15},
        avoid_genres=[_EXTENDED],
        experiment_slots=[
            ExperimentSlot(
                genre=_EXPERIMENT_NEW,
                rationale="future funk はチル隣接で新規流入を狙える実験枠と判断した。",
                success_criteria="主力 retention の 80% 以上を 14 日で達成すること。",
                slot_count=2,
            )
        ],
        rationale=(
            "前週の集計では lo-fi hip-hop の retention が突出しているため主力を厚く維持し、"
            " chillhop は伸び悩みのため一旦見送り、 future funk を実験枠で投入して分散を図る。"
        ),
        referenced_metrics=ReferencedMetrics(
            window_days=7,
            sample_size=14,
            top_metrics_summary="lo-fi hip-hop の平均 retention が他ジャンルより明確に高い傾向。",
        ),
    )


# --- fixtures ---------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """実 Postgres の AsyncSession を払い出す。 スキーマは metadata から作成する。

    決定的な期待値計算のため、 払い出し前に全データ表を ``TRUNCATE ... RESTART IDENTITY
    CASCADE`` で空にして既知状態から始める。 本テストは commit するため、 前回 run や
    他統合テスト (``test_daily_cycle_pipeline`` 等) が残した行 (genres / videos / posts /
    plans …) との PK / FK 衝突を避ける。 CASCADE で FK 依存順を自動解決する。
    """
    _require_db()
    engine = create_async_engine(_async_url())
    table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    try:
        async with sessionmaker() as session:
            yield session
    finally:
        await engine.dispose()


def _genre(name: str, *, role: str, enabled: bool = True) -> Genre:
    return Genre(
        name=name,
        display_name=name.title(),
        bpm_min=70,
        bpm_max=110,
        description=f"{name} の統合テスト用シード行。",
        role=role,
        enabled=enabled,
    )


def _video(genre: str, *, posted_at: datetime, suffix: str) -> Video:
    """`videos` の 1 行 (NOT NULL 列を最小限で埋める)。"""
    return Video(
        id=uuid.uuid4(),
        youtube_video_id=f"vid-{genre.replace(' ', '_')}-{suffix}",
        post_id=None,
        genre=genre,
        title=f"{genre} mix {suffix}",
        description=f"{genre} chill set {suffix}",
        duration_sec=3600,
        privacy_status="public",
        contains_synthetic_media=True,
        posted_at=posted_at,
        thumbnail_uri=f"file:///tmp/{genre}-{suffix}.png",
    )


def _analytics_row(
    video_id: str, metric_date: date, *, retention: float, views: int
) -> AnalyticsDaily:
    return AnalyticsDaily(
        youtube_video_id=video_id,
        metric_date=metric_date,
        views=views,
        estimated_minutes_watched=Decimal("1000.00"),
        average_view_duration_sec=180,
        retention_pct=Decimal(str(retention)),
        impressions=10000,
        ctr_pct=Decimal("5.00"),
        traffic_sources=None,
    )


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> dict[str, Any]:
    """1 週間分の analytics + experiment 評価対象を投入する。

    返り値で、 期待値計算に使うジャンル別の平均 retention を控える。

    seed 設計 (週開始の前週ウィンドウ ``[_WINDOW_END-6, _WINDOW_END]`` に分布):

    - 主力 ``lo-fi hip-hop`` (main): retention 60% を 1 動画 x 7 日。
    - 拡張 ``chillhop`` (extension): retention 50% を 1 動画 x 7 日 (avoid 対象)。
    - 実験 ``synthwave`` (experiment): retention 51% (=主力比 0.85) を 4 動画、
      全動画 14 日以上前に投稿 → FR-037 の前提 (>=4本 + >=14日経過) を満たし「adopt 推奨」。

    ``db_session`` フィクスチャが直前に全データ表を ``TRUNCATE ... CASCADE`` するため、
    本フィクスチャは常に空表へ投入する (前回 run の commit 済み残骸との衝突を防ぐ)。
    """
    db_session.add_all(
        [
            _genre(_PRIMARY, role="main"),
            _genre(_EXTENDED, role="extension"),
            _genre(_EXPERIMENT_RUNNING, role="experiment"),
        ]
    )
    await db_session.flush()

    now = datetime.now(UTC)
    old_posted = now - timedelta(days=20)  # 14 日以上前 (FR-037 の経過条件を満たす)

    # 主力 / 拡張: 1 動画ずつ、 ウィンドウ内 7 日分の analytics。
    primary_video = _video(_PRIMARY, posted_at=old_posted, suffix="p1")
    extended_video = _video(_EXTENDED, posted_at=old_posted, suffix="e1")
    db_session.add_all([primary_video, extended_video])

    # 実験 synthwave: 4 動画 (FR-037 の本数条件 >=4 を満たす)、 全て 14 日以上前に投稿。
    experiment_videos = [
        _video(_EXPERIMENT_RUNNING, posted_at=old_posted, suffix=f"x{i}") for i in range(4)
    ]
    db_session.add_all(experiment_videos)
    await db_session.flush()

    for offset in range(7):
        d = _WINDOW_END - timedelta(days=offset)
        db_session.add(
            _analytics_row(primary_video.youtube_video_id, d, retention=60.0, views=5000)
        )
        db_session.add(
            _analytics_row(extended_video.youtube_video_id, d, retention=50.0, views=3000)
        )
        for ev in experiment_videos:
            db_session.add(_analytics_row(ev.youtube_video_id, d, retention=51.0, views=2000))
    await db_session.commit()

    return {
        "primary_retention": 60.0,
        "experiment_retention": 51.0,
        # 51 / 60 = 0.85 >= 0.80 → adopt 推奨
        "expected_ratio": 51.0 / 60.0,
        "allowed_genres": [_PRIMARY, _EXTENDED, _EXPERIMENT_RUNNING, _EXPERIMENT_NEW],
    }


def _generator(provider: _StubWeeklyProvider) -> Any:
    """実 prompts root を指す PromptLoader で WeeklyPlanGenerator を組む (window_days=7)。"""
    return weekly_planner.WeeklyPlanGenerator(
        provider,
        prompt_loader=PromptLoader(_PROMPTS_ROOT),
        metric_window_days=7,
    )


# ===========================================================================
# テスト本体
# ===========================================================================


@pytest.mark.asyncio
async def test_weekly_cycle_generates_plan_and_applies_rotation(
    db_session: AsyncSession,
    seeded: dict[str, Any],
) -> None:
    """週次計画生成 → 永続化 → rotation 反映 を 1 本通す。

    1. WeeklyPlanGenerator.create_weekly_plan で 1 件の Plan(cycle="weekly") を永続化。
    2. payload が WeeklyPlan として検証でき、 genre_distribution 合計 1.0 /
       referenced_metrics.window_days=7 / experiment_slot が反映されていること。
    3. apply_weekly_rotation で genres テーブルが avoid/experiment どおりに更新されること
       (= 翌週 daily への反映)。
    """
    allowed = list(seeded["allowed_genres"])
    provider = _StubWeeklyProvider(_weekly_plan())
    generator = _generator(provider)

    # --- 1. 生成 + 永続化 ---
    plan_row = await generator.create_weekly_plan(
        session=db_session,
        target_week_start=_TARGET_WEEK_START,
        allowed_genres=allowed,
    )
    await db_session.commit()

    # provider は WeeklyPlan を response_model として要求されている。
    assert provider.requests, "provider.generate が呼ばれていない"
    assert provider.requests[0].response_model is WeeklyPlan

    # --- 2. Plan 行 / payload 検証 ---
    assert isinstance(plan_row, Plan)
    assert plan_row.cycle == "weekly"
    assert plan_row.target_week_start == _TARGET_WEEK_START
    assert plan_row.target_date is None
    assert plan_row.status == "generated"

    # payload を WeeklyPlan として再構築できる (辞書照合付き再検証も通る)。
    rebuilt = WeeklyPlan.model_validate(
        plan_row.payload, context={ALLOWED_GENRES_CONTEXT_KEY: allowed}
    )
    assert rebuilt.cycle == "weekly"
    assert rebuilt.target_week_start == _TARGET_WEEK_START
    assert abs(sum(rebuilt.genre_distribution.values()) - 1.0) <= 0.01
    assert rebuilt.referenced_metrics.window_days == 7
    assert rebuilt.avoid_genres == [_EXTENDED]
    assert [s.genre for s in rebuilt.experiment_slots] == [_EXPERIMENT_NEW]

    # PlanMetricSnapshot が前週ウィンドウで残っている (再現性確保、 planner と同方針)。
    snapshot = await db_session.get(PlanMetricSnapshot, plan_row.id)
    assert snapshot is not None
    assert snapshot.metric_window_end == _WINDOW_END
    assert snapshot.metric_window_start == _TARGET_WEEK_START - timedelta(days=7)

    # --- 3. rotation 反映 ---
    result = await rotation.apply_weekly_rotation(db_session, plan_row)
    await db_session.commit()

    assert _EXTENDED in result.disabled  # avoid_genres → enabled=False
    assert _EXPERIMENT_NEW in result.enabled  # experiment_slot → enabled=True (新規)

    # avoid_genres は enabled=False。
    extended = await db_session.get(Genre, _EXTENDED)
    assert extended is not None
    assert extended.enabled is False

    # experiment_slots[].genre は新規行で enabled=True + role="experiment" (rotation.EXPERIMENT_ROLE)。
    new_genre = await db_session.get(Genre, _EXPERIMENT_NEW)
    assert new_genre is not None
    assert new_genre.enabled is True
    assert new_genre.role == rotation.EXPERIMENT_ROLE

    # genre_distribution に載るジャンルは enabled=True を保証。
    for g in rebuilt.genre_distribution:
        row = await db_session.get(Genre, g)
        assert row is not None
        assert row.enabled is True, f"distribution genre {g!r} must be enabled"

    # daily 側が拾う enabled 集合に反映されている (chillhop は外れ、 新規 experiment が入る)。
    enabled_now = set(
        (await db_session.execute(select(Genre.name).where(Genre.enabled.is_(True))))
        .scalars()
        .all()
    )
    assert _EXTENDED not in enabled_now
    assert {_PRIMARY, _EXPERIMENT_RUNNING, _EXPERIMENT_NEW} <= enabled_now


@pytest.mark.asyncio
async def test_recommend_genres_adopts_when_ratio_meets_threshold(
    db_session: AsyncSession,
    seeded: dict[str, Any],
) -> None:
    """FR-037: 実験ジャンルが >=4本 + >=14日経過 かつ retention 比 >=0.80 → adopt 推奨。

    seed では synthwave (role="experiment") を 4 動画 (全て 20 日前投稿)・retention 51% で投入し、
    主力 lo-fi hip-hop は retention 60%。 比 = 0.85 >= 0.80 のため adopt を期待する。
    """
    recs = await recommend.recommend_genres(db_session, window_days=14)

    by_genre = {r.genre: r for r in recs}
    assert _EXPERIMENT_RUNNING in by_genre, "評価中 experiment が推奨結果に含まれていない"
    rec = by_genre[_EXPERIMENT_RUNNING]

    assert rec.action == "adopt"
    assert rec.sample_size >= 4  # FR-037 本数条件
    assert rec.days_elapsed >= 14  # FR-037 経過条件
    assert rec.retention_ratio_to_primary is not None
    # 51 / 60 = 0.85。 集計誤差を吸収して概ね一致を確認。
    assert abs(rec.retention_ratio_to_primary - seeded["expected_ratio"]) <= 0.05
    assert rec.rationale  # 判断根拠が明記されている (FR-037 「rationale に明記」)。
