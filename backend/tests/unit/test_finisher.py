"""LlmFinisher (domain/plans/finisher.py) の単体テスト。

契約 (共有契約書 / llm/base.py FinisherClient):

- ``LlmFinisher(provider, *, prompt_loader=..., prompt_name=..., prompt_version=...)``。
- ``render(req: FinisherRequest) -> FinisherResponse`` — 単一 directive を 1 文字列に展開。
- ``finish(parsed: ParsedTemplate, context) -> dict[str, str]`` — 全 GenerativeSlot を
  1 回の LLM 呼び出しでバッチ展開し ``slot_id`` → 生成文の dict を返す。

外部依存 (実 LLM provider / prompt ファイル) はすべて mock し、 実 HTTP / ファイル I/O は
打たない。 provider は :class:`LlmProvider` を満たす最小 fake、 prompt はメモリ上の prompt
ローダで差し替える。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, cast

import pytest
from pydantic import BaseModel

from ymg_backend.domain.directive.parser import parse_template
from ymg_backend.domain.errors import QualityError
from ymg_backend.domain.plans.finisher import LlmFinisher
from ymg_backend.domain.prompts.loader import PromptLoader
from ymg_backend.llm.base import (
    FinisherRequest,
    LlmProvider,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)

pytestmark = pytest.mark.asyncio

_PROMPT_TEXT: Final[str] = "あなたは仕上げ担当です。 directive を短く展開してください。"


def _usage() -> LlmUsage:
    """テスト用の固定 usage。"""
    return LlmUsage(
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.0001,
        duration_ms=12,
    )


class _FakeProvider(LlmProvider):
    """``LlmProvider`` を満たす最小 fake。

    ``response_model`` を ``provided`` のフィールドから組み立てて返す。 直近の
    ``LlmRequest`` を ``last_request`` に記録し、 呼び出し回数を ``calls`` で数える。
    """

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls = 0
        self.last_request: LlmRequest[BaseModel] | None = None

    async def generate[T: BaseModel](self, req: LlmRequest[T]) -> LlmResponse[T]:
        self.calls += 1
        # LlmRequest は T に対し invariant のため、 検証で参照する非ジェネリック
        # フィールド (messages / context_type 等) のみを見る用途で基底型へ widen する。
        self.last_request = cast("LlmRequest[BaseModel]", req)
        parsed = req.response_model.model_validate(self._payload)
        return LlmResponse(
            parsed=parsed,
            raw_text="{}",
            usage=_usage(),
            provider="openai",
            model="gpt-4.1-mini",
            finish_reason="stop",
        )

    def supported_models(self) -> list[str]:
        return ["gpt-4.1-mini"]

    def supports_caching(self) -> bool:
        return True

    async def health_check(self) -> bool:
        return True


def _prompt_loader(tmp_path: Path) -> PromptLoader:
    """``finisher/title_v1.md`` を持つ一時 prompt ローダを作る。"""
    area = tmp_path / "finisher"
    area.mkdir(parents=True, exist_ok=True)
    (area / "title_v1.md").write_text(_PROMPT_TEXT, encoding="utf-8")
    return PromptLoader(tmp_path)


async def test_render_single_directive(tmp_path: Path) -> None:
    """単一 directive を展開し FinisherResponse を返す。"""
    provider = _FakeProvider({"text": "雨夜のラウンジ"})
    finisher = LlmFinisher(provider, prompt_loader=_prompt_loader(tmp_path))
    req = FinisherRequest(
        instruction="12字以内の日本語サブタイトル",
        context={"genre": "Lo-Fi Hip Hop", "mood": "rainy night"},
        max_chars=12,
    )
    resp = await finisher.render(req)
    assert resp.text == "雨夜のラウンジ"
    assert resp.usage.cost_usd == pytest.approx(0.0001)
    assert provider.calls == 1
    # system prompt が cacheable で先頭に乗る (prompt caching, ADR-0033)。
    assert provider.last_request is not None
    assert provider.last_request.messages[0].role == "system"
    assert provider.last_request.messages[0].cacheable is True
    assert provider.last_request.context_type == "finisher"
    assert provider.last_request.prompt_version == "finisher/title_v1"


async def test_render_rejects_overflow(tmp_path: Path) -> None:
    """文字数上限を超えた展開結果は QualityError で弾く。"""
    provider = _FakeProvider({"text": "これは十二文字をはるかに超える長すぎるサブタイトルです"})
    finisher = LlmFinisher(provider, prompt_loader=_prompt_loader(tmp_path))
    req = FinisherRequest(instruction="12字以内", context={}, max_chars=12)
    with pytest.raises(QualityError):
        await finisher.render(req)


async def test_finish_batch_expands_all_slots(tmp_path: Path) -> None:
    """テンプレ内の全 GenerativeSlot を 1 回の呼び出しで展開する。"""
    parsed = parse_template("{{12字サブタイトル}} / {{絵文字1個}}")
    # 出現順に gen_0 / gen_1 が採番される。
    provider = _FakeProvider({"slots": {"gen_0": " 雨夜のラウンジ ", "gen_1": "🌧"}})
    finisher = LlmFinisher(provider, prompt_loader=_prompt_loader(tmp_path))
    generated = await finisher.finish(parsed, {"genre": "Lo-Fi Hip Hop"})
    # 前後空白は除去される。
    assert generated == {"gen_0": "雨夜のラウンジ", "gen_1": "🌧"}
    assert provider.calls == 1


async def test_finish_no_generative_slots_skips_llm(tmp_path: Path) -> None:
    """生成スロットが無ければ LLM を呼ばず空 dict を返す (コスト節約)。"""
    parsed = parse_template("Lo-Fi Hip Hop {{genre}} 30min")  # 変数のみ
    provider = _FakeProvider({"slots": {}})
    finisher = LlmFinisher(provider, prompt_loader=_prompt_loader(tmp_path))
    generated = await finisher.finish(parsed, {"genre": "Lo-Fi Hip Hop"})
    assert generated == {}
    assert provider.calls == 0


async def test_finish_missing_slot_raises_quality_error(tmp_path: Path) -> None:
    """必要な slot_id が欠落していたら QualityError を送出する。"""
    parsed = parse_template("{{12字サブA}} / {{12字サブB}}")
    provider = _FakeProvider({"slots": {"gen_0": "雨夜のラウンジ"}})  # gen_1 欠落
    finisher = LlmFinisher(provider, prompt_loader=_prompt_loader(tmp_path))
    with pytest.raises(QualityError):
        await finisher.finish(parsed, {})


async def test_finish_renderable_with_render_template(tmp_path: Path) -> None:
    """finish の戻り値が render_template の generated に渡せること。"""
    from ymg_backend.domain.directive.parser import render_template

    parsed = parse_template("{{genre}}: {{12字サブ}}")
    provider = _FakeProvider({"slots": {"gen_0": "雨夜のラウンジ"}})
    finisher = LlmFinisher(provider, prompt_loader=_prompt_loader(tmp_path))
    generated = await finisher.finish(parsed, {"genre": "Lo-Fi Hip Hop"})
    rendered = render_template(parsed, context={"genre": "Lo-Fi Hip Hop"}, generated=generated)
    assert rendered == "Lo-Fi Hip Hop: 雨夜のラウンジ"
