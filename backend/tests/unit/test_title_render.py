"""``render_title`` (domain/render/title.py) の単体テスト (T082)。

契約 (共有契約書):

- ``render_title(*, daily_post, genre, finisher, template) -> str`` — ジャンル YAML テンプレ +
  directive parser + finisher LLM で ``final_title`` を生成し、 60 字制約 (FR-051) を検証する。
- 60 字超過は ``QualityError`` (``quality`` カテゴリ)。

外部依存 (finisher LLM) は :class:`_FakeFinisher` で mock し、 実 LLM は呼ばない。
テンプレは :class:`GenreTemplate` をテスト内で直接組み立てる (YAML I/O 非依存)。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest

from ymg_backend.domain.errors import ErrorCategory, QualityError
from ymg_backend.domain.plans.schemas import DailyPost
from ymg_backend.domain.render.title import (
    _SUBTITLE_MAX_CHARS,
    MAX_TITLE_CHARS,
    render_title,
)
from ymg_backend.domain.templates.loader import GenreTemplate, TemplateCategory
from ymg_backend.llm.base import FinisherClient, FinisherRequest, FinisherResponse, LlmUsage

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.asyncio

_ZERO_USAGE = LlmUsage(
    prompt_tokens=0,
    cached_tokens=0,
    completion_tokens=0,
    cost_usd=0.0,
    duration_ms=0,
)


class _FakeFinisher(FinisherClient):
    """``FinisherClient`` の最小スタブ。 instruction → 固定応答の辞書で答える。

    呼び出された ``FinisherRequest`` を記録し、 context / instruction の受け渡しを検証できる。
    """

    def __init__(self, responses: Mapping[str, str]) -> None:
        self._responses = dict(responses)
        self.calls: list[FinisherRequest] = []

    async def render(self, req: FinisherRequest) -> FinisherResponse:
        self.calls.append(req)
        text = self._responses.get(req.instruction, "x")
        return FinisherResponse(text=text, usage=_ZERO_USAGE)


def _title_template(template_text: str, *, genre: str = "lo-fi hip-hop") -> GenreTemplate:
    """``template`` 文字列を持つタイトル用 :class:`GenreTemplate` を組み立てる。"""
    data: dict[str, Any] = {
        "genre": genre,
        "display_name": "Lo-Fi Hip Hop",
        "template": template_text,
    }
    return GenreTemplate(
        category=TemplateCategory.TITLE,
        genre=genre,
        data=MappingProxyType(data),
    )


def _daily_post(genre: str = "lo-fi hip-hop") -> DailyPost:
    return DailyPost(
        genre=genre,
        mood="rainy lounge",
        visual_direction="dim neon city rain at night",
        title_directive="Lo-Fi Hip Hop {{duration}}min | {{サブタイトル}}",
        description_directive="A relaxing lo-fi mix for focus and rest at night.",
    )


@pytest.mark.fr("FR-051")
async def test_render_title_substitutes_variables_and_generates_slots() -> None:
    """FR-051: 変数 ({{duration}}/{{genre}}) は context 置換、 自由文は finisher で生成される。"""
    finisher = _FakeFinisher({"12字以内の日本語サブタイトル": "雨夜のラウンジ"})
    template = _title_template("Lo-Fi Hip Hop {{duration}}min | {{12字以内の日本語サブタイトル}}")

    title = await render_title(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=finisher,
        template=template,
    )

    assert title == "Lo-Fi Hip Hop 30min | 雨夜のラウンジ"
    assert len(finisher.calls) == 1
    assert finisher.calls[0].instruction == "12字以内の日本語サブタイトル"


async def test_render_title_passes_mood_and_genre_context_to_finisher() -> None:
    """生成スロットへ daily_post.mood / genre が context で渡る。"""
    finisher = _FakeFinisher({"サブタイトル": "夜想曲"})
    template = _title_template("Lo-Fi Hip Hop {{duration}}min | {{サブタイトル}}")

    await render_title(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=finisher,
        template=template,
    )

    ctx = finisher.calls[0].context
    assert ctx["genre"] == "lo-fi hip-hop"
    assert ctx["mood"] == "rainy lounge"


async def test_render_title_uses_display_name_for_genre_variable() -> None:
    """{{genre}} 変数はテンプレの display_name で解決される。"""
    finisher = _FakeFinisher({})
    template = _title_template("{{genre}} {{duration}}min")

    title = await render_title(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=finisher,
        template=template,
    )

    assert title == "Lo-Fi Hip Hop 30min"


async def test_render_title_strips_generated_text() -> None:
    """finisher 応答の前後空白は除去される (テンプレ合成での余計な空白防止)。"""
    finisher = _FakeFinisher({"サブタイトル": "  夜の海  "})
    template = _title_template("Lo-Fi {{duration}}min | {{サブタイトル}}")

    title = await render_title(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=finisher,
        template=template,
    )

    assert title == "Lo-Fi 30min | 夜の海"


@pytest.mark.fr("FR-051")
async def test_render_title_raises_quality_error_when_over_limit() -> None:
    """FR-051: 60 字超過は QualityError (quality カテゴリ) を送出する。

    サブタイトルは 12 字以内に収め(=サブタイトル制約は満たす)、 リテラル部で 60 字を超過させて
    タイトル全体長の検証が発火することを確認する。
    """
    finisher = _FakeFinisher({"サブタイトル": "あ" * _SUBTITLE_MAX_CHARS})
    template = _title_template("x" * 55 + " {{サブタイトル}}")  # 55 + 1 + 12 = 68 字

    with pytest.raises(QualityError) as exc_info:
        await render_title(
            daily_post=_daily_post(),
            genre="lo-fi hip-hop",
            finisher=finisher,
            template=template,
        )

    assert exc_info.value.category is ErrorCategory.QUALITY
    assert exc_info.value.context["max_chars"] == MAX_TITLE_CHARS


async def test_render_title_accepts_exactly_max_chars() -> None:
    """ちょうど 60 字は許容される (境界値、 off-by-one 防止)。"""
    # 47(リテラル) + 1(空白) + 12(サブタイトル) = 60 字。
    template = _title_template("x" * 47 + " {{サブタイトル}}")
    finisher = _FakeFinisher({"サブタイトル": "あ" * _SUBTITLE_MAX_CHARS})

    title = await render_title(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=finisher,
        template=template,
    )

    assert len(title) == MAX_TITLE_CHARS


@pytest.mark.fr("FR-051")
async def test_render_title_rejects_subtitle_over_12_chars() -> None:
    """FR-051: 日本語サブタイトルが 12 字を超えると QualityError(slot 制約)。"""
    finisher = _FakeFinisher({"12字以内の日本語サブタイトル": "あ" * 13})
    template = _title_template("Lo-Fi {{duration}}min | {{12字以内の日本語サブタイトル}}")

    with pytest.raises(QualityError) as exc_info:
        await render_title(
            daily_post=_daily_post(),
            genre="lo-fi hip-hop",
            finisher=finisher,
            template=template,
        )

    assert exc_info.value.category is ErrorCategory.QUALITY
    assert exc_info.value.context["max_chars"] == _SUBTITLE_MAX_CHARS


@pytest.mark.fr("FR-051")
async def test_render_title_accepts_one_emoji() -> None:
    """FR-051: 絵文字 1 個までは許容される。"""
    finisher = _FakeFinisher({"サブタイトル": "夜の街", "絵文字": "🎵"})
    template = _title_template("Lo-Fi {{duration}}min | {{サブタイトル}} {{絵文字}}")

    title = await render_title(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=finisher,
        template=template,
    )

    assert "🎵" in title


@pytest.mark.fr("FR-051")
async def test_render_title_rejects_more_than_one_emoji() -> None:
    """FR-051: 絵文字が 2 個以上だと QualityError を送出する。"""
    # 各スロットは 12 字以内(slot 制約は満たす)だが、 最終タイトルの絵文字数が 2 になる。
    finisher = _FakeFinisher({"サブタイトル": "夜", "絵文字": "🎵🎶"})
    template = _title_template("Lo-Fi {{duration}}min | {{サブタイトル}} {{絵文字}}")

    with pytest.raises(QualityError) as exc_info:
        await render_title(
            daily_post=_daily_post(),
            genre="lo-fi hip-hop",
            finisher=finisher,
            template=template,
        )

    assert exc_info.value.category is ErrorCategory.QUALITY
    assert exc_info.value.context["emoji_count"] == 2


async def test_render_title_missing_template_field_raises_quality_error() -> None:
    """template キー欠落は QualityError として早期失敗する。"""
    template = GenreTemplate(
        category=TemplateCategory.TITLE,
        genre="lo-fi hip-hop",
        data=MappingProxyType({"genre": "lo-fi hip-hop"}),
    )
    finisher = _FakeFinisher({})

    with pytest.raises(QualityError):
        await render_title(
            daily_post=_daily_post(),
            genre="lo-fi hip-hop",
            finisher=finisher,
            template=template,
        )
