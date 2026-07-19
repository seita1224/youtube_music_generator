"""WeeklyPlanGenerator (domain/plans/weekly_planner.py) の単体テスト (T104)。

契約 (US3 内部契約 / ADR-0032):

- ``WeeklyPlanGenerator(provider, *, prompt_loader=..., metric_window_days=14, ...)``
  — :class:`LlmProvider` と :class:`PromptLoader` を注入。 ステートレス (session は引数渡し)。
- ``generate_weekly_plan(session, target_week_start, allowed_genres) -> (WeeklyPlan, LlmUsage)``
  — 集計 + 履歴から週次計画を生成し、 ``WeeklyPlan.model_validate(context=...)`` で辞書照合
  して返す (永続化なし)。
- 辞書外ジャンル (genre_distribution / avoid_genres / experiment_slots) を LLM が返したら
  :class:`QualityError` (ADR-0028 quality)。

外部依存 (実 LLM / DB) は使わず、 ``LlmProvider.generate`` を stub で差し替える。
``session=None`` 経路 (集計スキップ) で DB 非依存にテストする。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ymg_backend.domain.errors import QualityError
from ymg_backend.domain.plans.schemas import (
    ALLOWED_GENRES_CONTEXT_KEY,
    ExperimentSlot,
    ReferencedMetrics,
    WeeklyPlan,
)
from ymg_backend.domain.prompts.loader import PromptLoader
from ymg_backend.llm.base import LlmProviderName, LlmRequest, LlmResponse, LlmUsage

# 未実装モジュール (TDD RED 段階) は import 不能のため module を skip ゲートで解決する。
weekly_planner = pytest.importorskip("ymg_backend.domain.plans.weekly_planner")

# asyncio_mode = "auto" のため async テストは自動収集される。 同期テストへの波及を避けるため
# モジュール全体への pytestmark は付けない。

_PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"
_ALLOWED_GENRES = ["lo-fi hip-hop", "chillhop", "ambient", "synthwave"]
_TARGET_WEEK_START = date(2026, 6, 15)  # 月曜 JST


def _make_weekly_plan(
    *,
    distribution: dict[str, float] | None = None,
    avoid: list[str] | None = None,
    experiment_genre: str = "synthwave",
) -> WeeklyPlan:
    """全制約を満たす最小の WeeklyPlan を組む (辞書照合は呼び出し側に委ねる)。"""
    return WeeklyPlan(
        plan_id="0190b3aa-0000-7000-8000-000000000010",
        target_week_start=_TARGET_WEEK_START,
        genre_distribution=distribution or {"lo-fi hip-hop": 0.7, "chillhop": 0.3},
        avoid_genres=avoid if avoid is not None else ["ambient"],
        experiment_slots=[
            ExperimentSlot(
                genre=experiment_genre,
                rationale="新規流入の拡大を狙い隣接ジャンルを試す実験枠とする。",
                success_criteria="主力 retention の 80% 以上を確認する。",
                slot_count=2,
            )
        ],
        rationale=(
            "前週集計で lo-fi hip-hop の retention が突出しているため主力を厚く維持しつつ、"
            " ambient は伸び悩みのため見送り、 synthwave を実験枠で投入して分散を図る方針。"
        ),
        referenced_metrics=ReferencedMetrics(
            window_days=14,
            sample_size=20,
            top_metrics_summary="lo-fi hip-hop の平均 retention が他ジャンルより明確に高い傾向。",
        ),
    )


class _StubProvider:
    """``LlmProvider.generate`` だけを差し替える stub (構造化出力を即返す)。"""

    def __init__(self, plan: WeeklyPlan, *, provider: LlmProviderName = "openai") -> None:
        self._plan = plan
        self._provider: LlmProviderName = provider
        self.requests: list[LlmRequest[Any]] = []

    async def generate(self, req: LlmRequest[Any]) -> LlmResponse[Any]:
        self.requests.append(req)
        return LlmResponse(
            parsed=self._plan,
            raw_text=self._plan.model_dump_json(),
            usage=LlmUsage(
                prompt_tokens=200,
                cached_tokens=0,
                completion_tokens=120,
                cost_usd=0.0031,
                duration_ms=15,
            ),
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


def _generator(provider: _StubProvider, *, window_days: int = 14) -> Any:
    """実 prompts root を指す PromptLoader で WeeklyPlanGenerator を組む。"""
    return weekly_planner.WeeklyPlanGenerator(
        provider,
        prompt_loader=PromptLoader(_PROMPTS_ROOT),
        metric_window_days=window_days,
    )


# ===========================================================================
# generate_weekly_plan (session=None 経路、 集計スキップ)
# ===========================================================================


@pytest.mark.fr("FR-031", "FR-034")
async def test_generate_returns_validated_plan_and_usage() -> None:
    """FR-031, FR-034: session=None でも provider 出力を検証済み WeeklyPlan + usage として返す。"""
    provider = _StubProvider(_make_weekly_plan())
    generator = _generator(provider)

    plan, usage = await generator.generate_weekly_plan(
        session=None,
        target_week_start=_TARGET_WEEK_START,
        allowed_genres=_ALLOWED_GENRES,
    )

    assert isinstance(plan, WeeklyPlan)
    assert plan.cycle == "weekly"
    assert plan.target_week_start == _TARGET_WEEK_START
    assert abs(sum(plan.genre_distribution.values()) - 1.0) <= 0.01
    assert usage.cost_usd == pytest.approx(0.0031)

    # provider は WeeklyPlan を response_model として要求されている (構造化出力契約)。
    assert provider.requests, "provider.generate が呼ばれていない"
    assert provider.requests[0].response_model is WeeklyPlan
    # planner と同じ context_type で usage 計上される。
    assert provider.requests[0].context_type == "planner"


async def test_generate_rejects_genre_outside_dictionary() -> None:
    """genre_distribution が辞書外ジャンルを含むと QualityError (ADR-0028 quality)。"""
    bad_plan = _make_weekly_plan(distribution={"lo-fi hip-hop": 0.6, "vaporwave": 0.4})
    provider = _StubProvider(bad_plan)
    generator = _generator(provider)

    with pytest.raises(QualityError):
        await generator.generate_weekly_plan(
            session=None,
            target_week_start=_TARGET_WEEK_START,
            allowed_genres=_ALLOWED_GENRES,  # "vaporwave" は含まれない
        )


async def test_generate_rejects_experiment_genre_outside_dictionary() -> None:
    """experiment_slots[].genre の辞書照合も再検証される (avoid/experiment も allowed と照合)。"""
    bad_plan = _make_weekly_plan(experiment_genre="phonk")  # 辞書外
    provider = _StubProvider(bad_plan)
    generator = _generator(provider)

    with pytest.raises(QualityError):
        await generator.generate_weekly_plan(
            session=None,
            target_week_start=_TARGET_WEEK_START,
            allowed_genres=_ALLOWED_GENRES,  # "phonk" は含まれない
        )


async def test_generate_with_empty_allowed_skips_dictionary_check() -> None:
    """allowed_genres が空なら辞書照合をスキップする (辞書を持たない経路を許容)。"""
    provider = _StubProvider(_make_weekly_plan(distribution={"anything": 1.0}))
    generator = _generator(provider)

    plan, _ = await generator.generate_weekly_plan(
        session=None,
        target_week_start=_TARGET_WEEK_START,
        allowed_genres=[],
    )
    assert plan.genre_distribution == {"anything": 1.0}


async def test_referenced_metrics_window_matches_generator_window() -> None:
    """生成された WeeklyPlan は WeeklyPlan として再構築でき、 辞書照合付き検証も通る。"""
    plan = _make_weekly_plan()
    provider = _StubProvider(plan)
    generator = _generator(provider, window_days=14)

    result, _ = await generator.generate_weekly_plan(
        session=None,
        target_week_start=_TARGET_WEEK_START,
        allowed_genres=_ALLOWED_GENRES,
    )

    # 辞書照合付きで再検証しても落ちない (avoid/experiment/distribution が allowed 内)。
    rebuilt = WeeklyPlan.model_validate(
        result.model_dump(), context={ALLOWED_GENRES_CONTEXT_KEY: _ALLOWED_GENRES}
    )
    assert rebuilt.referenced_metrics.window_days == 14


# ===========================================================================
# genre_distribution 合計制約 (FR-034 失敗系: 1.0±0.01 を外れると拒否)
# ===========================================================================


@pytest.mark.fr("FR-034")
@pytest.mark.parametrize(
    "distribution",
    [
        {"lo-fi hip-hop": 0.7, "chillhop": 0.2},  # 合計 0.9 (下振れ)
        {"lo-fi hip-hop": 0.7, "chillhop": 0.4},  # 合計 1.1 (上振れ)
    ],
)
def test_weekly_plan_rejects_distribution_sum_outside_tolerance(
    distribution: dict[str, float],
) -> None:
    """FR-034: genre_distribution 合計が 1.0±0.01 を外れると value_error で拒否する。

    ``WeeklyPlan`` スキーマを直接 ``model_validate`` し、 ``genre_distribution`` の
    sum 制約 (``abs(total - 1.0) > 0.01``) が負経路で発火することを確認する。 辞書照合は
    context 未指定 (allowed 空扱い) でスキップし、 合計制約のみを孤立させる。
    """
    payload = _make_weekly_plan(distribution={"lo-fi hip-hop": 0.7, "chillhop": 0.3}).model_dump()
    payload["genre_distribution"] = distribution

    with pytest.raises(ValidationError) as exc_info:
        WeeklyPlan.model_validate(payload)  # context なし → 辞書照合スキップ

    errors = exc_info.value.errors()
    assert any(
        err["loc"] == ("genre_distribution",)
        and err["type"] == "value_error"
        and "sum to 1.0" in err["msg"]
        for err in errors
    )


@pytest.mark.fr("FR-034")
def test_weekly_plan_accepts_distribution_within_tolerance() -> None:
    """FR-034: 合計が 1.0±0.01 の許容内 (例 0.995) は受理される(正経路の対照)。"""
    payload = _make_weekly_plan(distribution={"lo-fi hip-hop": 0.7, "chillhop": 0.3}).model_dump()
    payload["genre_distribution"] = {"lo-fi hip-hop": 0.695, "chillhop": 0.3}  # 合計 0.995

    plan = WeeklyPlan.model_validate(payload)
    assert abs(sum(plan.genre_distribution.values()) - 1.0) <= 0.01
