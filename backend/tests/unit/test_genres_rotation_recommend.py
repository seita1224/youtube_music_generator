"""genres rotation (T105) / recommend (T106) の単体テスト (US3, FR-037 / FR-038)。

外部依存ゼロの真の単体テスト:

- DB は ``execute`` を文 (Select / Insert) で分岐して事前定義の結果を返し、 ``add`` /
  ``flush`` を記録する fake セッション (Postgres 不要)。
- LLM / YouTube / storage は一切起動しない。
- ``recommend._evaluate`` は純粋ロジックとして DB なしで全分岐を網羅する。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import Insert, Select

from ymg_backend.domain.genres import recommend as rec
from ymg_backend.domain.genres.recommend import GenreRecommendation, recommend_genres
from ymg_backend.domain.genres.rotation import (
    EXPERIMENT_ROLE,
    RotationResult,
    apply_weekly_rotation,
)
from ymg_backend.infrastructure.db.models import Genre

if TYPE_CHECKING:
    from collections.abc import Mapping

# asyncio_mode = "auto" (pyproject) のため async テストは無印で実行される。 同期の
# ``_evaluate`` テストに asyncio マークが付かないよう、 module 全体への mark は付けない。

_NOW = datetime(2026, 6, 16, 0, 0, tzinfo=UTC)


# =====================================================================================
# recommend._evaluate — 純粋ロジック (DB 非依存) で全分岐を網羅
# =====================================================================================


def _stats(
    *,
    genre: str = "future garage",
    video_count: int,
    avg_retention_pct: float | None,
    earliest_posted_at: datetime | None,
) -> rec._GenreStats:
    return rec._GenreStats(
        genre=genre,
        video_count=video_count,
        avg_retention_pct=avg_retention_pct,
        earliest_posted_at=earliest_posted_at,
    )


def test_evaluate_keep_when_below_min_videos() -> None:
    """本数 4 未満は前提未達で keep (比は算出しない)。"""
    stats = _stats(
        video_count=3,
        avg_retention_pct=99.0,
        earliest_posted_at=_NOW - timedelta(days=30),
    )
    out = rec._evaluate(stats, primary_avg=50.0, now=_NOW)
    assert out.action == "keep"
    assert out.retention_ratio_to_primary is None
    assert out.sample_size == 3
    assert "前提未達" in out.rationale


def test_evaluate_keep_when_below_min_days() -> None:
    """経過 14 日未満は前提未達で keep。"""
    stats = _stats(
        video_count=10,
        avg_retention_pct=99.0,
        earliest_posted_at=_NOW - timedelta(days=10),
    )
    out = rec._evaluate(stats, primary_avg=50.0, now=_NOW)
    assert out.action == "keep"
    assert out.days_elapsed == 10
    assert out.retention_ratio_to_primary is None


def test_evaluate_adopt_at_or_above_080() -> None:
    """前提を満たし retention 比 >=0.80 で adopt。"""
    stats = _stats(
        video_count=4,
        avg_retention_pct=40.0,  # 40 / 50 = 0.80
        earliest_posted_at=_NOW - timedelta(days=14),
    )
    out = rec._evaluate(stats, primary_avg=50.0, now=_NOW)
    assert out.action == "adopt"
    assert out.retention_ratio_to_primary == 0.8
    assert "採用推奨" in out.rationale


def test_evaluate_drop_below_060() -> None:
    """retention 比 <0.60 で drop。"""
    stats = _stats(
        video_count=6,
        avg_retention_pct=29.0,  # 29 / 50 = 0.58
        earliest_posted_at=_NOW - timedelta(days=20),
    )
    out = rec._evaluate(stats, primary_avg=50.0, now=_NOW)
    assert out.action == "drop"
    assert out.retention_ratio_to_primary == 0.58
    assert "削除推奨" in out.rationale


def test_evaluate_keep_in_between() -> None:
    """0.60 <= 比 < 0.80 は継続 (keep)。"""
    stats = _stats(
        video_count=5,
        avg_retention_pct=35.0,  # 35 / 50 = 0.70
        earliest_posted_at=_NOW - timedelta(days=21),
    )
    out = rec._evaluate(stats, primary_avg=50.0, now=_NOW)
    assert out.action == "keep"
    assert out.retention_ratio_to_primary == 0.7
    assert "継続" in out.rationale


def test_evaluate_keep_when_primary_retention_unavailable() -> None:
    """前提は満たすが主力 retention が None/0 なら比を出せず keep。"""
    stats = _stats(
        video_count=8,
        avg_retention_pct=40.0,
        earliest_posted_at=_NOW - timedelta(days=30),
    )
    out = rec._evaluate(stats, primary_avg=None, now=_NOW)
    assert out.action == "keep"
    assert out.retention_ratio_to_primary is None
    assert "算出できない" in out.rationale


def test_evaluate_days_elapsed_with_naive_datetime() -> None:
    """tz 無し posted_at も UTC とみなして経過日数を算出する。"""
    naive = datetime(2026, 6, 1, 0, 0)
    stats = _stats(
        video_count=4,
        avg_retention_pct=45.0,
        earliest_posted_at=naive,
    )
    out = rec._evaluate(stats, primary_avg=50.0, now=_NOW)
    assert out.days_elapsed == 15


# =====================================================================================
# recommend_genres — fake session で集計→判定の結線を検証
# =====================================================================================


@dataclass
class _Row:
    """``execute(...).all()`` / ``scalar_one_or_none()`` 用の汎用行 (属性アクセス)。"""

    genre: str | None = None
    video_count: int | None = None
    earliest_posted_at: datetime | None = None
    avg_retention_pct: float | None = None


class _Result:
    def __init__(self, *, rows: list[_Row] | None = None, scalar: Any = None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def all(self) -> list[_Row]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._scalar


class _RecommendSession:
    """recommend_genres 用 fake。 Select 文を順序で判定し結果を返す。

    呼び出し順: (1) 主力平均 retention (scalar) → (2) experiment 本数/最古 (rows) →
    (3) experiment retention (rows)。 ``recommend_genres`` の実装順に一致させる。
    """

    def __init__(
        self,
        *,
        primary_avg: float | None,
        base_rows: list[_Row],
        retention_rows: list[_Row],
    ) -> None:
        self._primary_avg = primary_avg
        self._base_rows = base_rows
        self._retention_rows = retention_rows
        self._select_calls = 0

    async def execute(self, stmt: Any) -> _Result:
        assert isinstance(stmt, Select)
        self._select_calls += 1
        if self._select_calls == 1:
            return _Result(scalar=self._primary_avg)
        if self._select_calls == 2:
            return _Result(rows=self._base_rows)
        return _Result(rows=self._retention_rows)


async def test_recommend_genres_joins_stats_and_sorts() -> None:
    """主力平均 + experiment 集計を結線し、 genre 名昇順で推奨を返す。"""
    session = _RecommendSession(
        primary_avg=50.0,
        base_rows=[
            _Row(
                genre="future garage", video_count=4, earliest_posted_at=_NOW - timedelta(days=20)
            ),
            _Row(genre="drumstep", video_count=2, earliest_posted_at=_NOW - timedelta(days=5)),
        ],
        retention_rows=[
            _Row(genre="future garage", avg_retention_pct=45.0),  # 0.90 → adopt
            # drumstep は retention 無し
        ],
    )

    out = await recommend_genres(session, window_days=14)  # type: ignore[arg-type]

    assert [r.genre for r in out] == ["drumstep", "future garage"]
    drumstep = out[0]
    future = out[1]
    assert drumstep.action == "keep"  # 前提未達 (本数 2 / 経過 5 日)
    assert future.action == "adopt"
    assert future.retention_ratio_to_primary == 0.9
    assert all(isinstance(r, GenreRecommendation) for r in out)


async def test_recommend_genres_rejects_bad_window() -> None:
    session = _RecommendSession(primary_avg=None, base_rows=[], retention_rows=[])
    with pytest.raises(ValueError, match="window_days"):
        await recommend_genres(session, window_days=0)  # type: ignore[arg-type]


async def test_recommend_genres_empty_when_no_experiments() -> None:
    session = _RecommendSession(primary_avg=50.0, base_rows=[], retention_rows=[])
    out = await recommend_genres(session)  # type: ignore[arg-type]
    assert out == []


# =====================================================================================
# rotation.apply_weekly_rotation — fake session で genres 反映を検証
# =====================================================================================


def _weekly_payload(
    *,
    genre_distribution: dict[str, float],
    avoid_genres: list[str],
    experiment_genres: list[str],
) -> dict[str, Any]:
    """``Plan.payload`` 相当 (WeeklyPlan の model_dump(mode="json")) を作る。"""
    return {
        "cycle": "weekly",
        "plan_id": str(uuid.uuid4()),
        "target_week_start": "2026-06-15",  # 月曜
        "genre_distribution": genre_distribution,
        "avoid_genres": avoid_genres,
        "experiment_slots": [
            {
                "genre": g,
                "rationale": "実験投入の理由を 20 文字以上で記述する。",
                "success_criteria": "retention 比 80% 以上を 4 本で達成。",
                "slot_count": 1,
            }
            for g in experiment_genres
        ],
        "rationale": (
            "週次の改善計画。 主力ジャンルへ配分しつつ実験枠を 1 つ投入する方針を"
            " 50 文字以上で説明する根拠テキスト。"
        ),
        "referenced_metrics": {
            "window_days": 14,
            "sample_size": 12,
            "top_metrics_summary": "直近 2 週間の retention 上位は主力ジャンル。",
        },
    }


@dataclass
class _FakePlan:
    """``apply_weekly_rotation`` が読む Plan 代用 (cycle / payload / id のみ)。"""

    payload: dict[str, Any]
    cycle: str = "weekly"
    id: uuid.UUID = uuid.uuid4()  # noqa: RUF009 — テスト用固定 ID で十分


class _RotationSession:
    """select で既存 Genre を返し、 add した行と audit insert を記録する fake。"""

    def __init__(self, existing: list[Genre]) -> None:
        self._existing = existing
        self.added: list[Genre] = []
        self.audit_rows: list[Mapping[str, Any]] = []
        self.flush_count = 0

    async def execute(self, stmt: Any) -> _GenreScalarsResult | _Result:
        if isinstance(stmt, Select):
            return _GenreScalarsResult(self._existing)
        if isinstance(stmt, Insert):
            self.audit_rows.append(dict(stmt.compile().params))
            return _Result()
        raise AssertionError(f"unexpected statement: {stmt!r}")

    def add(self, obj: Any) -> None:
        if isinstance(obj, Genre):
            self.added.append(obj)
            self._existing.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1


class _GenreScalarsResult:
    def __init__(self, rows: list[Genre]) -> None:
        self._rows = rows

    def scalars(self) -> _GenreScalarsResult:
        return self

    def all(self) -> list[Genre]:
        return list(self._rows)


def _genre(name: str, *, role: str, enabled: bool) -> Genre:
    return Genre(
        name=name,
        display_name=name,
        description="seed",
        role=role,
        enabled=enabled,
    )


async def test_rotation_disables_avoid_and_enables_distribution() -> None:
    """avoid → enabled=False、 配分ジャンル → enabled=True を保証し audit を残す。"""
    existing = [
        _genre("lo-fi hip-hop", role="main", enabled=True),
        _genre("synthwave", role="extension", enabled=False),  # 配分で再有効化
        _genre("ambient", role="main", enabled=True),  # avoid で無効化
    ]
    plan = _FakePlan(
        payload=_weekly_payload(
            genre_distribution={"lo-fi hip-hop": 0.6, "synthwave": 0.4},
            avoid_genres=["ambient"],
            experiment_genres=[],
        )
    )
    session = _RotationSession(existing)

    result = await apply_weekly_rotation(session, plan)  # type: ignore[arg-type]

    assert isinstance(result, RotationResult)
    assert result.disabled == ("ambient",)
    assert result.enabled == ("synthwave",)
    by_name = {g.name: g for g in existing}
    assert by_name["ambient"].enabled is False
    assert by_name["synthwave"].enabled is True
    assert len(session.audit_rows) == 1
    assert session.audit_rows[0]["action"] == "weekly_rotation_applied"
    assert session.audit_rows[0]["target_id"] == str(plan.id)


async def test_rotation_creates_experiment_genre_when_missing() -> None:
    """experiment_slot のジャンルが辞書に無ければ新規作成し experiment role + enabled。"""
    existing = [_genre("lo-fi hip-hop", role="main", enabled=True)]
    plan = _FakePlan(
        payload=_weekly_payload(
            genre_distribution={"lo-fi hip-hop": 1.0},
            avoid_genres=[],
            experiment_genres=["future garage"],
        )
    )
    session = _RotationSession(existing)

    result = await apply_weekly_rotation(session, plan)  # type: ignore[arg-type]

    assert len(session.added) == 1
    created = session.added[0]
    assert created.name == "future garage"
    assert created.role == EXPERIMENT_ROLE
    assert created.enabled is True
    assert "future garage" in result.enabled
    assert ("future garage", EXPERIMENT_ROLE) in result.role_changes


async def test_rotation_enable_wins_over_avoid_conflict() -> None:
    """avoid と配分/実験が同一ジャンルを指す矛盾は enable 優先で解消する。"""
    existing = [
        _genre("lo-fi hip-hop", role="main", enabled=True),
        _genre("synthwave", role="extension", enabled=False),
    ]
    plan = _FakePlan(
        payload=_weekly_payload(
            genre_distribution={"lo-fi hip-hop": 0.5, "synthwave": 0.5},
            avoid_genres=["synthwave"],  # 配分にも載る → enable 優先
            experiment_genres=[],
        )
    )
    session = _RotationSession(existing)

    result = await apply_weekly_rotation(session, plan)  # type: ignore[arg-type]

    assert result.disabled == ()
    assert "synthwave" in result.enabled
    assert {g.name: g.enabled for g in existing}["synthwave"] is True


async def test_rotation_rejects_non_weekly_plan() -> None:
    plan = _FakePlan(payload={}, cycle="daily")
    session = _RotationSession([])
    with pytest.raises(ValueError, match="weekly"):
        await apply_weekly_rotation(session, plan)  # type: ignore[arg-type]


async def test_rotation_no_changes_skips_audit() -> None:
    """差分が無ければ audit を残さない (既に enabled の配分のみ)。"""
    existing = [_genre("lo-fi hip-hop", role="main", enabled=True)]
    plan = _FakePlan(
        payload=_weekly_payload(
            genre_distribution={"lo-fi hip-hop": 1.0},
            avoid_genres=[],
            experiment_genres=[],
        )
    )
    session = _RotationSession(existing)

    result = await apply_weekly_rotation(session, plan)  # type: ignore[arg-type]

    assert result == RotationResult(disabled=(), enabled=(), role_changes=())
    assert session.audit_rows == []
