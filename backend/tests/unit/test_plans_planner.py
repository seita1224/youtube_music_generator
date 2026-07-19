"""PlanGenerator (domain/plans/planner.py) の単体テスト。

契約 (共有契約書 / ADR-0032):

- ``PlanGenerator(provider, prompt_loader=...)`` — :class:`LlmProvider` と
  :class:`PromptLoader` を注入。 ステートレス(session は引数渡し)。
- ``generate_daily_plan(session, target_date, allowed_genres) -> (DailyPlan, LlmUsage)``
  — 集計 + 履歴から計画を生成し、 辞書照合して返す(永続化なし)。
- 辞書外ジャンルを LLM が返したら :class:`QualityError`(ADR-0028 quality)。

外部依存(実 LLM / DB)は使わず、 ``LlmProvider.generate`` を stub で差し替える。
``session=None`` 経路(集計スキップ)で DB 非依存にテストする。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ymg_backend.domain.errors import QualityError
from ymg_backend.domain.plans.planner import (
    MetricSnapshot,
    PlanGenerator,
    _resolve_provider_name,
)
from ymg_backend.domain.plans.schemas import (
    ALLOWED_GENRES_CONTEXT_KEY,
    DailyPlan,
    DailyPost,
    ReferencedMetrics,
)
from ymg_backend.domain.prompts.loader import PromptLoader
from ymg_backend.llm.base import LlmProviderName, LlmResponse, LlmUsage

# asyncio_mode = "auto"(pyproject.toml)のため async テストは自動収集される。
# 同期テストに asyncio マークが波及しないよう、 モジュール全体への ``pytestmark`` は付けない。

# 実 planner prompts(``backend/prompts/planner``)を解決するため repo の prompts root を使う。
_PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"
_ALLOWED_GENRES = ["lo-fi hip-hop", "chillhop", "ambient"]
_TARGET_DATE = date(2026, 6, 16)


def _make_plan(*, genre: str = "lo-fi hip-hop") -> DailyPlan:
    """全制約を満たす最小の DailyPlan を組む(辞書照合は呼び出し側に委ねる)。"""
    return DailyPlan(
        plan_id="0190b3aa-0000-7000-8000-000000000001",
        target_date=_TARGET_DATE,
        posts=[
            DailyPost(
                genre=genre,
                mood="calm",
                visual_direction="soft grain night",
                title_directive="lofi mix tonight",
                description_directive="relax study beats for the evening",
            )
        ],
        rationale="improve retention by leaning lo-fi based on recent metrics",
        referenced_metrics=ReferencedMetrics(
            window_days=7,
            sample_size=10,
            top_metrics_summary="lo-fi retention leads the window clearly",
        ),
    )


class _StubProvider:
    """``LlmProvider.generate`` だけを差し替える stub(構造化出力を即返す)。"""

    def __init__(self, plan: DailyPlan, *, provider: LlmProviderName = "openai") -> None:
        self._plan = plan
        self._provider: LlmProviderName = provider
        self.requests: list[Any] = []

    async def generate(self, req: Any) -> Any:
        self.requests.append(req)
        return LlmResponse(
            parsed=self._plan,
            raw_text=self._plan.model_dump_json(),
            usage=LlmUsage(
                prompt_tokens=200,
                cached_tokens=0,
                completion_tokens=120,
                cost_usd=0.0021,
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


def _planner(provider: _StubProvider) -> PlanGenerator:
    """実 prompts root を指す PromptLoader で PlanGenerator を組む。"""
    return PlanGenerator(provider, prompt_loader=PromptLoader(_PROMPTS_ROOT))  # type: ignore[arg-type]


# ===========================================================================
# generate_daily_plan(session=None 経路、 集計スキップ)
# ===========================================================================
@pytest.mark.fr("FR-030")
async def test_generate_returns_validated_plan_and_usage() -> None:
    """FR-030: session=None でも provider 出力を検証済み DailyPlan + usage として返す。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)

    plan, usage = await planner.generate_daily_plan(
        session=None, target_date=_TARGET_DATE, allowed_genres=_ALLOWED_GENRES
    )

    assert isinstance(plan, DailyPlan)
    assert plan.target_date == _TARGET_DATE
    assert plan.posts[0].genre == "lo-fi hip-hop"
    assert usage.completion_tokens == 120
    assert provider.requests, "planner must call provider.generate"


@pytest.mark.fr("FR-036")
async def test_generate_injects_allowed_genres_into_user_prompt() -> None:
    """FR-036: 許可ジャンルを user prompt に注入する(provider は context なしのため prompt 経由)。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)

    await planner.generate_daily_plan(
        session=None, target_date=_TARGET_DATE, allowed_genres=_ALLOWED_GENRES
    )

    req = provider.requests[0]
    user_msg = next(m for m in req.messages if m.role == "user")
    for genre in _ALLOWED_GENRES:
        assert genre in user_msg.content
    assert req.response_model is DailyPlan
    assert req.context_type == "planner"
    assert req.prompt_version == "planner/system_v1"


@pytest.mark.fr("FR-027")
async def test_generate_uses_system_prompt_message() -> None:
    """FR-027: system メッセージに planner system prompt 本文を載せ、 cacheable=True にする。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)

    await planner.generate_daily_plan(
        session=None, target_date=_TARGET_DATE, allowed_genres=_ALLOWED_GENRES
    )

    req = provider.requests[0]
    system_msg = next(m for m in req.messages if m.role == "system")
    assert "改善計画担当 LLM" in system_msg.content
    assert system_msg.cacheable is True


# ===========================================================================
# 辞書照合(ADR-0032 (4))
# ===========================================================================
async def test_generate_rejects_out_of_dictionary_genre() -> None:
    """LLM が辞書外ジャンルを返したら QualityError で弾く(再検証)。"""
    provider = _StubProvider(_make_plan(genre="death-metal"))
    planner = _planner(provider)

    with pytest.raises(QualityError) as exc_info:
        await planner.generate_daily_plan(
            session=None, target_date=_TARGET_DATE, allowed_genres=_ALLOWED_GENRES
        )

    assert "death-metal" in exc_info.value.context["genres"]


async def test_generate_skips_genre_check_with_empty_allowed_list() -> None:
    """allowed_genres が空なら辞書照合をスキップする(辞書を持たない経路)。"""
    provider = _StubProvider(_make_plan(genre="anything-goes"))
    planner = _planner(provider)

    plan, _ = await planner.generate_daily_plan(
        session=None, target_date=_TARGET_DATE, allowed_genres=[]
    )

    assert plan.posts[0].genre == "anything-goes"


# ===========================================================================
# 集計スナップショット(session=None フォールバック)
# ===========================================================================
async def test_aggregate_without_session_returns_conservative_summary() -> None:
    """session=None の集計は空サンプルで保守的サマリ(>=20 文字)を返す。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)

    snapshot = await planner._aggregate_metrics(None, _TARGET_DATE)

    assert isinstance(snapshot, MetricSnapshot)
    assert snapshot.total_samples == 0
    assert snapshot.genre_metrics == ()
    # window は [target_date - 7, target_date - 1]
    assert snapshot.window_end == date(2026, 6, 15)
    assert snapshot.window_start == date(2026, 6, 9)
    assert len(snapshot.summary_text) >= 20


def test_metric_snapshot_to_json_is_serializable() -> None:
    """to_metrics_json は plan_metric_snapshot.metrics 用に JSON 化可能な dict を返す。"""
    snapshot = MetricSnapshot(
        window_start=date(2026, 6, 9),
        window_end=date(2026, 6, 15),
        window_days=7,
        total_samples=0,
        genre_metrics=(),
        summary_text="analytics 未取得のため保守的に主力へ寄せる方針とする。",
    )

    payload = snapshot.to_metrics_json()

    assert payload["window_start"] == "2026-06-09"
    assert payload["window_end"] == "2026-06-15"
    assert payload["total_samples"] == 0
    assert payload["genres"] == []
    import json

    json.dumps(payload)  # raises if not serializable


# ===========================================================================
# provider 名解決
# ===========================================================================
def test_resolve_provider_name_maps_known_classes() -> None:
    """既知 provider クラス名は対応する ENUM 値に解決される。"""
    provider = _StubProvider(_make_plan())
    # stub のクラス名は対応表に無いため安全側 'openai' フォールバック
    assert _resolve_provider_name(provider) == "openai"  # type: ignore[arg-type]


# ===========================================================================
# 構築時バリデーション
# ===========================================================================
def test_construct_rejects_invalid_window_days() -> None:
    """metric_window_days < 1 は ValueError で拒否する。"""
    provider = _StubProvider(_make_plan())
    with pytest.raises(ValueError, match="metric_window_days"):
        PlanGenerator(provider, metric_window_days=0)  # type: ignore[arg-type]


def test_construct_rejects_negative_history_limit() -> None:
    """history_limit < 0 は ValueError で拒否する。"""
    provider = _StubProvider(_make_plan())
    with pytest.raises(ValueError, match="history_limit"):
        PlanGenerator(provider, history_limit=-1)  # type: ignore[arg-type]


# ===========================================================================
# DailyPlan を生成する経路で辞書照合に通る正常系(再検証が破壊的でないこと)
# ===========================================================================
@pytest.mark.parametrize("genre", _ALLOWED_GENRES)
async def test_generate_accepts_each_allowed_genre(genre: str) -> None:
    """許可ジャンルそれぞれで再検証を通過し、 genre が保たれる。"""
    provider = _StubProvider(_make_plan(genre=genre))
    planner = _planner(provider)

    plan, _ = await planner.generate_daily_plan(
        session=None, target_date=_TARGET_DATE, allowed_genres=_ALLOWED_GENRES
    )

    assert plan.posts[0].genre == genre
    # 再検証は元の制約を壊さない(辞書照合 context が effective であること)
    revalidated = DailyPlan.model_validate(
        plan.model_dump(), context={ALLOWED_GENRES_CONTEXT_KEY: _ALLOWED_GENRES}
    )
    assert revalidated.posts[0].genre == genre
