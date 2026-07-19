"""``final_title`` レンダラ(FR-051 / ADR-0017 / ADR-0034)。

ジャンル別タイトルテンプレ(``templates/title/<slug>.yaml`` の ``template`` フィールド)を
directive parser(ADR-0017)で解析し、 2 種のスロットを解決して最終タイトルを合成する:

- 変数参照(``{{duration}}`` / ``{{genre}}`` 等の ASCII 識別子)→ システム値の context で置換。
- 生成ディレクティブ(``{{12字以内の日本語サブタイトル}}`` 等の自由文)→ finisher LLM で生成。

タイトル形式(FR-051 / ADR-0034 (1))::

    "{英語ジャンル} {duration}min | {<=12字の日本語サブタイトル} {絵文字1個まで}"

最終文字列は 60 字以内に収める(FR-051)。 超過時は :class:`QualityError`(``quality``)で
失敗させ、 オーケストレータが当該部分のみスキップ + デフォルト値で続行できるようにする
(ADR-0028 ``quality`` の運用方針)。 directive 由来の不正(未定義変数・生成結果欠落)は
parser が同じ ``quality`` 系の :class:`DirectiveError` を送出する。

レンダリングは finisher LLM への I/O を含むため非同期。 1 テンプレ内の複数生成スロットは
出現順に逐次解決する(現状のタイトルテンプレはスロット 1〜2 個で、 バッチ最適化の必要は薄い)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from loguru import logger

from ymg_backend.domain.directive.parser import (
    GenerativeSlot,
    parse_template,
    render_template,
)
from ymg_backend.domain.errors import QualityError
from ymg_backend.llm.base import FinisherRequest

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ymg_backend.domain.plans.schemas import DailyPost
    from ymg_backend.domain.templates.loader import GenreTemplate
    from ymg_backend.llm.base import FinisherClient

# YouTube タイトルの最大長(FR-051)。 表示幅ではなく文字数で判定する。
MAX_TITLE_CHARS: Final[int] = 60

# 動画尺の既定値(分)。 FR-002 = 5 分 x 6 トラック連結で 30 分尺。
# 契約上 ``render_title`` は duration を引数で受けないため、 テンプレ ``{{duration}}`` は
# この既定値で解決する(長尺バリエーションは PoC 後に拡張、 spec.md:17)。
_DEFAULT_DURATION_MIN: Final[int] = 30

# テンプレ YAML の ``template`` フィールド名(ADR-0034 (1))。
_TEMPLATE_KEY: Final[str] = "template"
# 生成スロット(日本語サブタイトル / 絵文字)の文字数上限(FR-051 「<=12 字の日本語サブタイトル」)。
# 生成ヒントとして finisher に渡し、 合成前に生成テキスト長を本値で**強制検証**する。
_SUBTITLE_MAX_CHARS: Final[int] = 12
# タイトルに含められる絵文字数の上限(FR-051 「絵文字 1 個まで」)。
_MAX_TITLE_EMOJI: Final[int] = 1


def _count_emoji_groups(text: str) -> int:
    """文字列中の絵文字「個数」を数える(FR-051 の絵文字数判定用)。

    各絵文字ベース・コードポイントを 1 個と数えるが、 直前が ZWJ(U+200D)なら前の絵文字に
    結合しているとみなし加算しない。 これにより ``👨‍👩‍👧``(ZWJ 結合)は 1 個、 隣接した別絵文字
    ``🎵🎶`` は 2 個と数える。 異体字セレクタ / 肌色修飾子は継続として無視する。
    music 動画タイトルが使う単純な絵文字(🎵 / 🌙 / 🎧 等)に十分な近似。
    """
    count = 0
    prev_was_zwj = False
    for ch in text:
        cp = ord(ch)
        is_emoji_base = (
            0x1F000 <= cp <= 0x1FAFF  # 絵文字・絵文字的記号の主要ブロック
            or 0x2600 <= cp <= 0x27BF  # Misc Symbols + Dingbats(☀ ✂ ✅ 等)
            or 0x2B00 <= cp <= 0x2BFF  # ⭐ ⬆ 等
            or 0x1F1E6 <= cp <= 0x1F1FF  # 国旗(regional indicator)
        )
        if is_emoji_base:
            if not prev_was_zwj:
                count += 1
            prev_was_zwj = False
        elif cp == 0x200D:  # ZWJ → 次の絵文字は前に結合
            prev_was_zwj = True
        else:  # 異体字セレクタ / 肌色修飾子 / 非絵文字
            prev_was_zwj = False
    return count


async def render_title(
    *,
    daily_post: DailyPost,
    genre: str,
    finisher: FinisherClient,
    template: GenreTemplate,
) -> str:
    """ジャンル別テンプレと finisher LLM から ``Post.final_title`` を生成する(FR-051)。

    ``template.data["template"]`` を :func:`parse_template` で解析し、 変数スロットを
    システム context(``genre`` / ``duration``)で、 生成スロットを ``finisher.render`` で
    埋めて :func:`render_template` で合成する。 合成結果が 60 字を超える場合は
    :class:`QualityError` を送出する。

    Args:
        daily_post: 当該投稿の改善計画指示(``mood`` 等を finisher の context に渡す)。
        genre: ジャンル名(DB 表記 "lo-fi hip-hop" 等)。 テンプレに display 名が無い場合の
            フォールバックとして変数 ``{{genre}}`` の解決にも用いる。
        finisher: 生成スロット展開用の仕上げ LLM クライアント(``FinisherClient``)。
        template: ``TemplateCategory.TITLE`` のジャンル別テンプレ。

    Returns:
        60 字以内に収まる最終タイトル文字列。

    Raises:
        QualityError: テンプレに ``template`` キーが無い / 値が文字列でない場合、
            または合成後のタイトルが ``MAX_TITLE_CHARS`` を超える場合。
        DirectiveError: テンプレ構文不正・未定義変数・生成結果欠落の場合(``quality`` 系)。
    """
    raw_template = _extract_template_text(template)
    parsed = parse_template(raw_template)

    context = _build_context(template=template, genre=genre)
    generated = await _render_generative_slots(
        slots=parsed.generative_slots,
        daily_post=daily_post,
        genre=genre,
        finisher=finisher,
    )

    title = render_template(parsed, context=context, generated=generated)

    if len(title) > MAX_TITLE_CHARS:
        raise QualityError(
            f"rendered title exceeds {MAX_TITLE_CHARS} chars: {len(title)}",
            context={
                "genre": genre,
                "length": len(title),
                "max_chars": MAX_TITLE_CHARS,
                "title": title,
            },
        )

    emoji_count = _count_emoji_groups(title)
    if emoji_count > _MAX_TITLE_EMOJI:
        raise QualityError(
            f"rendered title contains {emoji_count} emoji (max {_MAX_TITLE_EMOJI})",
            context={
                "genre": genre,
                "emoji_count": emoji_count,
                "max_emoji": _MAX_TITLE_EMOJI,
                "title": title,
            },
        )

    logger.bind(genre=genre, step="render_title").info("rendered title ({} chars)", len(title))
    return title


def _extract_template_text(template: GenreTemplate) -> str:
    """テンプレ YAML から ``template`` 文字列を取り出す。

    ``template`` キー欠落 / 非文字列はテンプレ運用ミス(``quality``)として扱い、
    当該ジャンルのみスキップ可能にする(ADR-0028)。
    """
    raw = template.data.get(_TEMPLATE_KEY)
    if not isinstance(raw, str) or not raw.strip():
        raise QualityError(
            f"title template missing string {_TEMPLATE_KEY!r} field",
            context={"genre": template.genre, "category": template.category.value},
        )
    return raw


def _build_context(*, template: GenreTemplate, genre: str) -> Mapping[str, str]:
    """変数スロット(``{{duration}}`` / ``{{genre}}`` 等)解決用の context を組む。

    ``genre`` はテンプレの ``display_name`` を優先し(英語ジャンル表記、 FR-051)、
    無ければ引数の ``genre`` を用いる。 ``duration`` は既定の動画尺(分)。
    """
    display = template.data.get("display_name")
    genre_value = display if isinstance(display, str) and display else genre
    return {
        "genre": genre_value,
        "duration": str(_DEFAULT_DURATION_MIN),
    }


async def _render_generative_slots(
    *,
    slots: tuple[GenerativeSlot, ...],
    daily_post: DailyPost,
    genre: str,
    finisher: FinisherClient,
) -> Mapping[str, str]:
    """生成スロットを ``slot_id`` → 生成テキストの辞書に展開する。

    各スロットの ``instruction`` を finisher LLM に渡し、 ``daily_post`` の意図
    (``mood`` / ``genre``)を context として与える。 戻り値は :func:`render_template`
    の ``generated`` 引数に渡す。
    """
    base_context = {"genre": genre, "mood": daily_post.mood}
    generated: dict[str, str] = {}
    for slot in slots:
        req = FinisherRequest(
            instruction=slot.instruction,
            context=base_context,
            max_chars=_SUBTITLE_MAX_CHARS,
        )
        resp = await finisher.render(req)
        text = resp.text.strip()
        # FR-051: 生成スロット(サブタイトル)は 12 字以内に強制する。 超過は quality 失敗とし、
        # オーケストレータが当該部分のみスキップ + デフォルトで続行できるようにする(ADR-0028)。
        if len(text) > _SUBTITLE_MAX_CHARS:
            raise QualityError(
                f"generated slot exceeds {_SUBTITLE_MAX_CHARS} chars: {len(text)}",
                context={
                    "genre": genre,
                    "slot_id": slot.slot_id,
                    "length": len(text),
                    "max_chars": _SUBTITLE_MAX_CHARS,
                    "text": text,
                },
            )
        generated[slot.slot_id] = text
    return generated
