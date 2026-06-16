"""PlanGenerator の否認理由フィードバック経路の単体テスト(US2 契約 (d))。

確認する契約:

- ``REJECTED_REASONS_CONTEXT_KEY`` 定数が ``schemas`` にある(planner 側で prompt 注入に使う key)。
- ``generate_daily_plan(..., rejected_reasons=[...])`` で渡した否認理由が user prompt の
  「直近の却下理由」節に展開される。
- ``rejected_reasons`` 未指定 + ``session`` ありなら直近 ``DryrunOutput.reject_reason`` を
  自動集約して注入する(``state == "rejected"`` のみ、 新しい順)。
- 後方互換: 否認理由が無い(``session=None`` かつ未指定)場合は節を出さず、 従来どおり生成できる。

外部依存(実 LLM / 実 DB)は使わず、 ``LlmProvider.generate`` を stub に、 否認理由集約 query は
最小の fake session で差し替える。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ymg_backend.domain.plans.planner import PlanGenerator
from ymg_backend.domain.plans.schemas import (
    REJECTED_REASONS_CONTEXT_KEY,
    DailyPlan,
    DailyPost,
    ReferencedMetrics,
)
from ymg_backend.domain.prompts.loader import PromptLoader
from ymg_backend.llm.base import LlmProviderName, LlmResponse, LlmUsage

# asyncio_mode = "auto"(pyproject.toml)のため async テストは自動収集される。

_PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"
_ALLOWED_GENRES = ["lo-fi hip-hop", "chillhop", "ambient"]
_TARGET_DATE = date(2026, 6, 16)
_REJECT_SECTION_HEADER = "## 直近の却下理由(避ける。 同様の方向性は繰り返さない)"


def test_rejected_reasons_context_key_value() -> None:
    """schemas に prompt 注入用の key 定数が公開されている。"""
    assert REJECTED_REASONS_CONTEXT_KEY == "rejected_reasons"


def _make_plan(*, genre: str = "lo-fi hip-hop") -> DailyPlan:
    """全制約を満たす最小の DailyPlan(辞書照合は呼び出し側に委ねる)。"""
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


class _RejectRow:
    """``select(DryrunOutput.reject_reason)`` の 1 行(``row.reject_reason`` を満たす)。"""

    def __init__(self, reject_reason: str | None) -> None:
        self.reject_reason = reject_reason


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _RejectReasonSession:
    """否認理由集約 query のみを返す最小 fake session。

    planner は ``session=None`` 経路で集計 / 履歴をスキップするが、 ``_resolve_rejected_reasons``
    は ``session`` があると ``execute`` を呼ぶ。 ここでは集計 / 履歴を空に、 否認理由のみ rows を
    返す(``str(statement)`` で ``dryrun_outputs`` を含むかどうかで判別)。
    """

    def __init__(self, reject_rows: list[Any]) -> None:
        self._reject_rows = reject_rows
        self.statements: list[str] = []

    async def execute(self, statement: Any) -> _Result:
        sql = str(statement).lower()
        self.statements.append(sql)
        if "dryrun_outputs" in sql:
            return _Result(self._reject_rows)
        return _Result([])


def _planner(provider: _StubProvider) -> PlanGenerator:
    return PlanGenerator(provider, prompt_loader=PromptLoader(_PROMPTS_ROOT))  # type: ignore[arg-type]


def _user_prompt(provider: _StubProvider) -> str:
    req = provider.requests[0]
    user_msg = next(m for m in req.messages if m.role == "user")
    return str(user_msg.content)


async def test_explicit_rejected_reasons_injected_into_prompt() -> None:
    """明示の rejected_reasons が user prompt の却下理由節に展開される。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)
    reasons = ["音質が低い (ホワイトノイズ)", "サムネが地味すぎる"]

    await planner.generate_daily_plan(
        session=None,
        target_date=_TARGET_DATE,
        allowed_genres=_ALLOWED_GENRES,
        rejected_reasons=reasons,
    )

    prompt = _user_prompt(provider)
    assert _REJECT_SECTION_HEADER in prompt
    for reason in reasons:
        assert reason in prompt


async def test_no_rejected_reasons_omits_section() -> None:
    """否認理由が無ければ節を出さず、 従来どおり生成できる(後方互換)。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)

    plan, _ = await planner.generate_daily_plan(
        session=None, target_date=_TARGET_DATE, allowed_genres=_ALLOWED_GENRES
    )

    assert isinstance(plan, DailyPlan)
    assert _REJECT_SECTION_HEADER not in _user_prompt(provider)


async def test_empty_and_blank_reasons_are_filtered() -> None:
    """空文字 / 空白のみの否認理由は除去され、 全滅なら節を出さない。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)

    await planner.generate_daily_plan(
        session=None,
        target_date=_TARGET_DATE,
        allowed_genres=_ALLOWED_GENRES,
        rejected_reasons=["", "   "],
    )

    assert _REJECT_SECTION_HEADER not in _user_prompt(provider)


async def test_rejected_reasons_aggregated_from_db_when_unset() -> None:
    """rejected_reasons 未指定 + session ありなら DB の reject_reason を集約して注入する。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)
    session = _RejectReasonSession(
        [
            _RejectRow("BPM が速すぎて作業用に向かない"),
            _RejectRow("  タイトルが釣りっぽい  "),
            _RejectRow(None),  # 万一の None 行は除去される
        ]
    )

    await planner.generate_daily_plan(
        session=session,  # type: ignore[arg-type]
        target_date=_TARGET_DATE,
        allowed_genres=_ALLOWED_GENRES,
    )

    prompt = _user_prompt(provider)
    assert _REJECT_SECTION_HEADER in prompt
    assert "BPM が速すぎて作業用に向かない" in prompt
    assert "タイトルが釣りっぽい" in prompt  # strip 済みで展開
    # 否認理由集約 query が dryrun_outputs を参照していること
    assert any("dryrun_outputs" in sql for sql in session.statements)


async def test_explicit_reasons_take_precedence_over_db() -> None:
    """明示 rejected_reasons があれば DB 集約より優先する(session には触れない)。"""
    provider = _StubProvider(_make_plan())
    planner = _planner(provider)
    session = _RejectReasonSession([_RejectRow("DB 由来の理由 (使われないはず)")])

    await planner.generate_daily_plan(
        session=session,  # type: ignore[arg-type]
        target_date=_TARGET_DATE,
        allowed_genres=_ALLOWED_GENRES,
        rejected_reasons=["明示で渡した理由が優先される"],
    )

    prompt = _user_prompt(provider)
    assert "明示で渡した理由が優先される" in prompt
    assert "DB 由来の理由" not in prompt
    # 明示指定時は否認理由集約 query を発行しない
    assert not any("dryrun_outputs" in sql for sql in session.statements)
