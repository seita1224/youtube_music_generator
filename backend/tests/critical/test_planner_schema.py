"""Critical path テスト: 改善計画 LLM 出力スキーマ (T073, ADR-0032 / ADR-0004)。

改善計画(daily)の LLM 出力が :class:`DailyPlan` で正しく validation されることを
100% カバーする。LLM は呼ばず、``DailyPlan`` / ``DailyPost`` を直接構築 or
``model_validate(context=...)`` で駆動する。さらに planner を ``LlmProvider`` mock で
駆動する経路(契約 ``Planner.generate_daily_plan``)も検証する。

カバー項目:

1. 正常系: 辞書内ジャンル + 全制約充足で validation 成功
2. 辞書外ジャンル拒否(context 注入による照合, ADR-0032 (4))
3. context 未指定なら辞書照合をスキップ(部分検証/ユニットテスト用)
4. posts 件数制約(0 件 / 3 件で失敗, 1〜2 件で成功, ADR-0004)
5. rationale ``min_length=20`` 制約(下回ると失敗、 境界値で成功)
6. DailyPost の各 directive 最小長制約(mood/visual/title/description)
7. frozen(不変)モデルであること
8. planner mock 駆動: ``LlmProvider`` mock 経由で ``DailyPlan`` を得る経路
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from ymg_backend.domain.plans.schemas import (
    ALLOWED_GENRES_CONTEXT_KEY,
    DailyPlan,
    DailyPost,
    ReferencedMetrics,
)

pytestmark = pytest.mark.critical

# --- テスト固定値 ----------------------------------------------------------
_ALLOWED_GENRES = ["lo-fi-hip-hop", "chillhop", "ambient"]
_TARGET_DATE = date(2026, 6, 16)


def _referenced_metrics() -> ReferencedMetrics:
    """再現性確保用の参照メトリクス(全制約充足の最小値)。"""
    return ReferencedMetrics(
        window_days=7,
        sample_size=10,
        top_metrics_summary="retention up, ctr stable",  # min_length=20 充足
    )


def _post_payload(*, genre: str = "lo-fi-hip-hop", **overrides: Any) -> dict[str, Any]:
    """全制約を満たす DailyPost の dict payload を組む(overrides で個別に崩す)。"""
    payload: dict[str, Any] = {
        "genre": genre,
        "mood": "calm",  # min_length=4 ちょうど
        "visual_direction": "soft grain",  # min_length=10 ちょうど
        "title_directive": "lofi mix",  # min_length=8 ちょうど
        "description_directive": "relax study beats today",  # min_length=20 充足
    }
    payload.update(overrides)
    return payload


def _plan_payload(*, posts: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    """全制約を満たす DailyPlan の dict payload を組む。"""
    payload: dict[str, Any] = {
        "plan_id": "0190b3aa-0000-7000-8000-000000000001",
        "target_date": _TARGET_DATE.isoformat(),
        "posts": posts if posts is not None else [_post_payload()],
        "rationale": "improve retention by lofi",  # min_length=20 充足
        "referenced_metrics": _referenced_metrics().model_dump(),
    }
    payload.update(overrides)
    return payload


def _validate(payload: dict[str, Any]) -> DailyPlan:
    """許可ジャンル context 付きで DailyPlan を検証する(本番経路と同じ)。"""
    return DailyPlan.model_validate(payload, context={ALLOWED_GENRES_CONTEXT_KEY: _ALLOWED_GENRES})


# ===========================================================================
# 1. 正常系
# ===========================================================================
def test_valid_plan_with_single_post_passes() -> None:
    """辞書内ジャンル + 全制約充足で 1 投稿の DailyPlan が成立する。"""
    plan = _validate(_plan_payload())

    assert plan.cycle == "daily"
    assert plan.target_date == _TARGET_DATE
    assert len(plan.posts) == 1
    assert plan.posts[0].genre == "lo-fi-hip-hop"
    assert plan.expected_kpi is None  # 初期は optional(未指定で None)


def test_valid_plan_with_two_posts_passes() -> None:
    """2 投稿(max_length=2 境界)でも成立する(ADR-0004)。"""
    posts = [_post_payload(genre="lo-fi-hip-hop"), _post_payload(genre="chillhop")]
    plan = _validate(_plan_payload(posts=posts))

    assert len(plan.posts) == 2
    assert [p.genre for p in plan.posts] == ["lo-fi-hip-hop", "chillhop"]


def test_valid_plan_accepts_optional_post_fields() -> None:
    """bpm_range / thumbnail_directive / schedule_jst の optional 充足を受理する。"""
    post = _post_payload(
        bpm_range=[70, 90],
        thumbnail_directive="centered title, warm tone",
        schedule_jst="2026-06-16T21:00:00",
    )
    plan = _validate(_plan_payload(posts=[post]))

    daily_post = plan.posts[0]
    assert daily_post.bpm_range == (70, 90)
    assert daily_post.schedule_jst == datetime(2026, 6, 16, 21, 0, 0)
    assert daily_post.thumbnail_directive == "centered title, warm tone"


# ===========================================================================
# 2. 辞書外ジャンル拒否(context 照合, ADR-0032 (4))
# ===========================================================================
def test_genre_not_in_allowed_list_is_rejected() -> None:
    """allowed_genres に無いジャンルは value_error で拒否される。"""
    payload = _plan_payload(posts=[_post_payload(genre="death-metal")])

    with pytest.raises(ValidationError) as exc_info:
        _validate(payload)

    errors = exc_info.value.errors()
    assert any(
        err["loc"] == ("posts", 0, "genre") and err["type"] == "value_error" for err in errors
    )


def test_genre_rejection_targets_correct_post_index() -> None:
    """複数投稿のうち辞書外ジャンルの位置(index)が loc に反映される。"""
    posts = [_post_payload(genre="lo-fi-hip-hop"), _post_payload(genre="not-a-genre")]
    payload = _plan_payload(posts=posts)

    with pytest.raises(ValidationError) as exc_info:
        _validate(payload)

    assert any(err["loc"] == ("posts", 1, "genre") for err in exc_info.value.errors())


# ===========================================================================
# 3. context 未指定なら辞書照合をスキップ(ADR-0032 (4) の末尾)
# ===========================================================================
def test_genre_check_skipped_without_context() -> None:
    """context 無し(辞書未注入)ではジャンル照合をスキップしスキーマのみ流す。"""
    payload = _plan_payload(posts=[_post_payload(genre="anything-goes")])

    plan = DailyPlan.model_validate(payload)  # context なし

    assert plan.posts[0].genre == "anything-goes"


def test_genre_check_skipped_with_empty_allowed_list() -> None:
    """allowed_genres が空集合でも照合をスキップする(空 = 辞書を持たない扱い)。"""
    payload = _plan_payload(posts=[_post_payload(genre="anything-goes")])

    plan = DailyPlan.model_validate(payload, context={ALLOWED_GENRES_CONTEXT_KEY: []})

    assert plan.posts[0].genre == "anything-goes"


# ===========================================================================
# 4. posts 件数制約(0 件 / 3 件で失敗, ADR-0004)
# ===========================================================================
def test_zero_posts_is_rejected() -> None:
    """posts 0 件は too_short(min_length=1)で拒否される。"""
    with pytest.raises(ValidationError) as exc_info:
        _validate(_plan_payload(posts=[]))

    assert any(
        err["loc"] == ("posts",) and err["type"] == "too_short" for err in exc_info.value.errors()
    )


def test_three_posts_is_rejected() -> None:
    """posts 3 件は too_long(max_length=2)で拒否される。"""
    posts = [_post_payload(genre="lo-fi-hip-hop") for _ in range(3)]

    with pytest.raises(ValidationError) as exc_info:
        _validate(_plan_payload(posts=posts))

    assert any(
        err["loc"] == ("posts",) and err["type"] == "too_long" for err in exc_info.value.errors()
    )


# ===========================================================================
# 5. rationale min_length=20
# ===========================================================================
def test_rationale_below_min_length_is_rejected() -> None:
    """rationale が 20 文字未満なら string_too_short で拒否される。"""
    short_rationale = "x" * 19

    with pytest.raises(ValidationError) as exc_info:
        _validate(_plan_payload(rationale=short_rationale))

    assert any(
        err["loc"] == ("rationale",) and err["type"] == "string_too_short"
        for err in exc_info.value.errors()
    )


def test_rationale_at_min_length_boundary_passes() -> None:
    """rationale が 20 文字ちょうど(境界値)なら受理される。"""
    plan = _validate(_plan_payload(rationale="x" * 20))

    assert len(plan.rationale) == 20


# ===========================================================================
# 6. DailyPost の各 directive 最小長(mood/visual/title/description)
# ===========================================================================
@pytest.mark.parametrize(
    ("field", "too_short_value"),
    [
        ("mood", "abc"),  # min_length=4 を 1 文字下回る
        ("visual_direction", "x" * 9),  # min_length=10
        ("title_directive", "x" * 7),  # min_length=8
        ("description_directive", "x" * 19),  # min_length=20
    ],
)
def test_post_directive_below_min_length_is_rejected(field: str, too_short_value: str) -> None:
    """DailyPost の各 directive 最小長を 1 文字下回ると string_too_short になる。"""
    payload = _plan_payload(posts=[_post_payload(**{field: too_short_value})])

    with pytest.raises(ValidationError) as exc_info:
        _validate(payload)

    assert any(
        err["loc"] == ("posts", 0, field) and err["type"] == "string_too_short"
        for err in exc_info.value.errors()
    )


def test_post_directives_at_min_length_boundary_pass() -> None:
    """各 directive が最小長ちょうど(境界値)なら DailyPost が成立する。"""
    post = DailyPost.model_validate(
        _post_payload(
            mood="x" * 4,
            visual_direction="x" * 10,
            title_directive="x" * 8,
            description_directive="x" * 20,
        ),
        context={ALLOWED_GENRES_CONTEXT_KEY: _ALLOWED_GENRES},
    )

    assert len(post.mood) == 4
    assert len(post.visual_direction) == 10
    assert len(post.title_directive) == 8
    assert len(post.description_directive) == 20


# ===========================================================================
# 7. frozen(不変)モデル
# ===========================================================================
def test_daily_plan_is_frozen() -> None:
    """DailyPlan は frozen で、属性の事後代入を拒否する(不変データ志向)。"""
    plan = _validate(_plan_payload())

    with pytest.raises(ValidationError):
        plan.rationale = "mutated rationale value"  # type: ignore[misc]


def test_daily_post_is_frozen() -> None:
    """DailyPost も frozen で、属性の事後代入を拒否する。"""
    plan = _validate(_plan_payload())

    with pytest.raises(ValidationError):
        plan.posts[0].genre = "chillhop"  # type: ignore[misc]


# ===========================================================================
# 8. planner mock 駆動(契約 Planner.generate_daily_plan、 LLM は実呼びしない)
# ===========================================================================
class _StubPlanProvider:
    """``LlmProvider.generate`` だけを差し替える stub(構造化出力を即返す)。

    planner が ``provider.generate(LlmRequest[DailyPlan](...))`` を呼ぶ前提で、
    context に注入された ``allowed_genres`` で検証済みの ``DailyPlan`` を返す。
    """

    def __init__(self, plan: DailyPlan) -> None:
        self._plan = plan
        self.calls: list[Any] = []

    async def generate(self, req: Any) -> Any:
        self.calls.append(req)
        from ymg_backend.llm.base import LlmResponse, LlmUsage

        return LlmResponse(
            parsed=self._plan,
            raw_text=self._plan.model_dump_json(),
            usage=LlmUsage(
                prompt_tokens=120,
                cached_tokens=0,
                completion_tokens=80,
                cost_usd=0.0,
                duration_ms=10,
            ),
            provider="openai",
            model="gpt-4.1",
            finish_reason="stop",
        )

    def supported_models(self) -> list[str]:
        return ["gpt-4.1"]

    def supports_caching(self) -> bool:
        return False

    async def health_check(self) -> bool:
        return True


async def test_planner_returns_validated_daily_plan_from_provider() -> None:
    """planner を LlmProvider mock で駆動し、検証済み DailyPlan を返す経路を確認する。

    planner 未実装(TDD RED)の間は ``importorskip`` で skip し、実装後に有効化される。
    実装後は ``generate_daily_plan`` が provider 出力をそのまま検証済み計画として返す。
    実 LLM は呼ばず、``LlmProvider.generate`` を stub で差し替える。
    """
    planner_module = pytest.importorskip(
        "ymg_backend.domain.pipeline.planner",
        reason="planner 未実装(TDD RED)。Planner 実装後にこのテストが有効化される。",
    )
    from ymg_backend.domain.prompts.loader import PromptLoader

    expected = _validate(_plan_payload())
    provider = _StubPlanProvider(expected)
    planner = planner_module.Planner(provider, PromptLoader())

    plan, usage = await planner.generate_daily_plan(
        session=None,
        target_date=_TARGET_DATE,
        allowed_genres=_ALLOWED_GENRES,
    )

    assert isinstance(plan, DailyPlan)
    assert plan.target_date == _TARGET_DATE
    assert plan.posts[0].genre in _ALLOWED_GENRES
    assert usage.completion_tokens == 80
    assert provider.calls, "planner must call provider.generate exactly once"
