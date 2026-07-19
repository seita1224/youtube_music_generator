"""render_description (domain/render/description.py) の単体テスト。

契約 (共有契約書 §description renderer):

    async def render_description(*, daily_post, genre, finisher, template, shared) -> str

検証対象 (FR-052 / FR-053):

- AI 開示固定文 / チャンネル宣伝が ``_shared/*.txt`` の逐語で挿入される (LLM が書き換えない)。
- チャプターが 6 トラック x ``interval_seconds`` のタイムスタンプ + 英 / 日タイトルで動的生成される。
- ハッシュタグ 3 個が finisher 生成値で埋まる。
- テンプレ ``body`` 中の全プレースホルダが解決され、未解決の ``{{...}}`` が残らない。

外部依存 (LLM) は ``FinisherClient`` のフェイク実装で差し替え、 実 API を打たない。
テンプレは実体の ``templates/description/default.yaml`` を ``TemplateLoader`` で読む。
"""

from __future__ import annotations

import re

import pytest

from ymg_backend.domain.plans.schemas import DailyPost
from ymg_backend.domain.render.description import render_description
from ymg_backend.domain.templates.loader import (
    GenreTemplate,
    TemplateCategory,
    TemplateLoader,
)
from ymg_backend.llm.base import (
    FinisherClient,
    FinisherRequest,
    FinisherResponse,
    LlmUsage,
)

pytestmark = pytest.mark.asyncio

_ZERO_USAGE = LlmUsage(
    prompt_tokens=0,
    cached_tokens=0,
    completion_tokens=0,
    cost_usd=0.0,
    duration_ms=1,
)


class _FakeFinisher(FinisherClient):
    """field 名ごとに決定的なテキストを返す FinisherClient フェイク。

    instruction から生成対象フィールド名を読み取り、 ``track_title`` 系は呼び出し順に
    通し番号を振る。 これにより 6 トラックのタイトルが互いに区別できる。
    """

    def __init__(self) -> None:
        self.calls: list[FinisherRequest] = []
        self._en_idx = 0
        self._ja_idx = 0

    async def render(self, req: FinisherRequest) -> FinisherResponse:
        self.calls.append(req)
        instruction = req.instruction
        if "track_title_en" in instruction:
            self._en_idx += 1
            text = f"EnTitle{self._en_idx}"
        elif "track_title_ja" in instruction:
            self._ja_idx += 1
            text = f"邦題{self._ja_idx}"
        elif "scene_description_en" in instruction:
            text = "A calm rainy night."
        elif "scene_description_ja" in instruction:
            text = "静かな雨の夜。"
        elif "genre_hashtag_1" in instruction:
            text = "lofi"
        elif "genre_hashtag_2" in instruction:
            text = "chill"
        elif "scene_hashtag" in instruction:
            text = "rainynight"
        else:
            text = "fallback"
        return FinisherResponse(text=text, usage=_ZERO_USAGE)


def _load_template() -> GenreTemplate:
    return TemplateLoader().load_genre_template(TemplateCategory.DESCRIPTION, "default")


def _load_shared() -> dict[str, str]:
    loader = TemplateLoader()
    return {
        "ai_disclosure": loader.load_shared_text(TemplateCategory.DESCRIPTION, "ai_disclosure.txt"),
        "channel_promo": loader.load_shared_text(TemplateCategory.DESCRIPTION, "channel_promo.txt"),
    }


def _daily_post() -> DailyPost:
    return DailyPost(
        genre="lo-fi hip-hop",
        mood="rainy calm night",
        visual_direction="a neon-lit window with rain streaks at night",
        title_directive="Lo-Fi {{duration}}min",
        description_directive="Describe a relaxing rainy lo-fi session for studying.",
    )


@pytest.mark.fr("FR-053")
async def test_render_description_includes_static_blocks_verbatim() -> None:
    """FR-053: AI 開示 / チャンネル宣伝は _shared の逐語が含まれる。"""
    shared = _load_shared()
    out = await render_description(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=_FakeFinisher(),
        template=_load_template(),
        shared=shared,
    )
    # 開示文の英 / 日の特徴行が逐語で残っている。
    assert "AI-generated using ACE-Step" in out
    assert "第三者の著作物は含まれません" in out
    assert "Subscribe for daily lo-fi" in out


@pytest.mark.fr("FR-052")
async def test_render_description_builds_six_chapter_lines() -> None:
    """FR-052: チャプターは 6 行 + ヘッダ。 タイムスタンプが 5 分刻みで英 / 日タイトル付き。"""
    out = await render_description(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=_FakeFinisher(),
        template=_load_template(),
        shared=_load_shared(),
    )
    assert "⏱ Chapters" in out
    # 6 トラック x 300 秒 -> 0:00, 5:00, 10:00, 15:00, 20:00, 25:00。
    for expected_ts in ("0:00", "5:00", "10:00", "15:00", "20:00", "25:00"):
        assert expected_ts in out
    # 英 / 日タイトルが行に差し込まれている。
    assert "EnTitle1" in out and "邦題1" in out
    assert "EnTitle6" in out and "邦題6" in out
    # 行フォーマット "{{start}} {{en}} / {{ja}}" が成立している。
    assert "0:00 EnTitle1 / 邦題1" in out


@pytest.mark.fr("FR-052")
async def test_render_description_has_three_hashtags() -> None:
    """FR-052: ハッシュタグ 3 個 (ジャンル 2 + シーン 1) が末尾に並ぶ。"""
    out = await render_description(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=_FakeFinisher(),
        template=_load_template(),
        shared=_load_shared(),
    )
    assert "#lofi" in out
    assert "#chill" in out
    assert "#rainynight" in out
    # ちょうど 3 個。
    assert len(re.findall(r"#\w+", out)) == 3


@pytest.mark.fr("FR-050")
async def test_render_description_leaves_no_unresolved_placeholders() -> None:
    """FR-050: 実 default.yaml を TemplateLoader で読み、全 {{...}} が解決される。"""
    out = await render_description(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=_FakeFinisher(),
        template=_load_template(),
        shared=_load_shared(),
    )
    assert "{{" not in out
    assert "}}" not in out


async def test_render_description_missing_shared_block_raises_quality_error() -> None:
    """必須の固定ブロックが shared に無い場合は QualityError。"""
    from ymg_backend.domain.errors import QualityError

    with pytest.raises(QualityError):
        await render_description(
            daily_post=_daily_post(),
            genre="lo-fi hip-hop",
            finisher=_FakeFinisher(),
            template=_load_template(),
            shared={"channel_promo": "x"},  # ai_disclosure 欠落
        )


class _SentinelFinisher(FinisherClient):
    """全 instruction に対し固定の番兵文字列だけを返す FinisherClient。

    AI 開示文を一切生成しないため、出力に開示文が含まれれば「LLM 生成ではなく shared
    逐語注入で入った」ことの証拠になる (FR-053)。 ハッシュタグ等は番兵で埋まり開示文とは
    重ならない。
    """

    SENTINEL = "LLM_NEVER_WRITES_DISCLOSURE"

    def __init__(self) -> None:
        self.calls: list[FinisherRequest] = []

    async def render(self, req: FinisherRequest) -> FinisherResponse:
        self.calls.append(req)
        return FinisherResponse(text=self.SENTINEL, usage=_ZERO_USAGE)


@pytest.mark.fr("FR-053")
async def test_ai_disclosure_is_shared_verbatim_not_llm_generated() -> None:
    """FR-053: AI 開示固定文は shared 逐語で挿入され、仕上げ LLM 生成に依存しない。

    finisher が開示文言を一切生成しない (番兵だけ返す) 場合でも、開示文の英 / 日の特徴行が
    出力に逐語で含まれることを示す。 これは開示文が ``shared`` マッピング (TemplateLoader が
    読んだ ``_shared/ai_disclosure.txt``) 経由で注入され、LLM 出力経路を通らないため。
    """
    shared = _load_shared()
    finisher = _SentinelFinisher()
    out = await render_description(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=finisher,
        template=_load_template(),
        shared=shared,
    )

    # finisher は確かに呼ばれているが、開示文は一切生成していない (番兵のみ)。
    assert finisher.calls, "finisher should still be invoked for chapters/hashtags"
    assert all(c is not None for c in finisher.calls)

    # それでも shared の開示文が英 / 日とも逐語で出力に含まれる。
    disclosure = shared["ai_disclosure"]
    assert "AI-generated using ACE-Step" in disclosure  # 前提: 番兵には含まれない
    assert "AI-generated using ACE-Step" in out
    assert "第三者の著作物は含まれません" in out
    # 開示文そのものは LLM 番兵の影響を受けず、原文ブロックがそのまま入る。
    assert disclosure.strip() in out


async def test_render_description_uses_description_directive_in_finisher() -> None:
    """finisher の instruction に description_directive が含まれる (生成の文脈付与)。"""
    finisher = _FakeFinisher()
    await render_description(
        daily_post=_daily_post(),
        genre="lo-fi hip-hop",
        finisher=finisher,
        template=_load_template(),
        shared=_load_shared(),
    )
    assert finisher.calls, "finisher should be called at least once"
    assert all("Describe a relaxing rainy lo-fi session" in c.instruction for c in finisher.calls)
    # context に genre / mood が注入されている。
    assert finisher.calls[0].context.get("genre") == "lo-fi hip-hop"
    assert finisher.calls[0].context.get("mood") == "rainy calm night"
