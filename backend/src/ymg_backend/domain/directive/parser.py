"""ディレクティブパーサ(自動判別方式、ADR-0017)。

要件 6.4 / 6.5 のテンプレ機能では ``{{...}}`` に 2 種類のディレクティブが混在する:

1. **変数参照**: 中身が単一識別子(``\\w+``)→ context dict の値で置換。
2. **生成ディレクティブ**: それ以外(空白・日本語の文章)→ finisher LLM 行きのスロット。

ADR-0017 の「実装(概略)」に従い ``{{...}}`` を唯一の構文とし、中身を ``IDENT_RE.fullmatch``
で自動判別する。ネストは不可、リテラルの波括弧は ``\\{\\{`` / ``\\}\\}`` でエスケープする。

パース(:func:`parse_template`)とレンダリング(:func:`render_template`)を分離する。
パースは network 不要で純粋な構文解析、レンダリングは変数を context で解決し、
生成ディレクティブの結果(LLM が別途展開した dict)を差し込む。これにより 1 テンプレ内の
複数生成ディレクティブを 1 回の LLM 呼び出しでバッチ展開する設計(ADR-0017)を後段に委ねられる。

不正なテンプレ(空 directive・ネスト・未閉じ・未定義変数・生成結果欠落)は
:class:`DirectiveError`(``quality`` カテゴリ)で早期に失敗させる(ADR-0017 / ADR-0028)。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ymg_backend.domain.errors import QualityError

# ``{{`` / ``}}`` をリテラルとして扱うエスケープ表記(ADR-0017)。
_ESCAPED_OPEN: Final[str] = r"\{\{"
_ESCAPED_CLOSE: Final[str] = r"\}\}"
# プレースホルダ。エスケープを一旦退避してから波括弧を走査するため、
# 入力に現れ得ない制御文字列を用いる。
_PH_OPEN: Final[str] = "\x00YMG_OPEN\x00"
_PH_CLOSE: Final[str] = "\x00YMG_CLOSE\x00"

# 中身が単一識別子か(変数参照か)を判定する(ADR-0017 IDENT_RE = \w+)。
# ``re.ASCII`` で ``\w`` を ``[A-Za-z0-9_]`` に限定する。これがないと ``\w`` が Unicode で
# 日本語(空白なしの自由文)までマッチし、生成ディレクティブが変数誤判定される。
# 変数名は通常の ASCII 識別子規則と整合的(ADR-0017「悪い影響」)。
_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"\w+", re.ASCII)
# 生成ディレクティブの slot_id プレフィックス。
_GEN_ID_PREFIX: Final[str] = "gen_"


class DirectiveError(QualityError):
    """テンプレ定義・レンダリングの不正(ADR-0017 / ADR-0028 ``quality``)。

    空 directive・ネスト・未閉じ・未定義変数・生成結果欠落などの運用ミスを早期に検出する。
    付随情報(``variable`` / ``slot_id`` / ``template`` など)は基底の ``context`` に格納する。
    """


@dataclass(frozen=True, slots=True)
class VariableSlot:
    """変数参照ディレクティブ(``{{genre}}`` 等)。

    ``name`` は ``\\w+`` 制約を満たす識別子で、レンダリング時に context dict から解決する。
    """

    name: str


@dataclass(frozen=True, slots=True)
class GenerativeSlot:
    """生成ディレクティブ(``{{この曲の雰囲気を1文で}}`` 等)。

    ``slot_id`` は出現順に採番した安定 ID(``gen_0`` 等)で、finisher LLM の出力 JSON の
    キーと対応する。``instruction`` は LLM に渡す自由文の指示(前後空白は除去済み)。
    """

    slot_id: str
    instruction: str


# テンプレを構成するセグメント: リテラル文字列 / 変数参照 / 生成スロット。
_Segment = str | VariableSlot | GenerativeSlot


@dataclass(frozen=True, slots=True)
class ParsedTemplate:
    """パース済みテンプレの不変表現。

    ``segments`` はリテラルとスロットを出現順に並べた列。``variable_names`` /
    ``generative_slots`` は重複排除・出現順を保った参照ビューで、呼び出し側が
    必要な context キー集合・LLM へ渡すスロット集合を取得するために使う。
    """

    segments: tuple[_Segment, ...]
    variable_names: tuple[str, ...]
    generative_slots: tuple[GenerativeSlot, ...]


def _classify(body: str) -> Literal["var", "gen"]:
    """ディレクティブ本体を変数参照 / 生成ディレクティブに自動判別する(ADR-0017)。"""
    return "var" if _IDENT_RE.fullmatch(body.strip()) else "gen"


def _split_directives(template: str) -> list[tuple[Literal["lit", "dir"], str]]:
    """エスケープを退避したテンプレを ``{{...}}`` 境界で分割する。

    Returns:
        ``("lit", text)``(リテラル)/ ``("dir", body)``(ディレクティブ本体)のタプル列。

    Raises:
        DirectiveError: 未閉じ / 未開始 / ネストの波括弧を検出した場合。
    """
    parts: list[tuple[Literal["lit", "dir"], str]] = []
    pos = 0
    length = len(template)
    while pos < length:
        open_at = template.find("{{", pos)
        if open_at == -1:
            _reject_stray_close(template, template[pos:])
            parts.append(("lit", template[pos:]))
            break
        literal = template[pos:open_at]
        _reject_stray_close(template, literal)
        if literal:
            parts.append(("lit", literal))
        close_at = template.find("}}", open_at + 2)
        if close_at == -1:
            raise DirectiveError(
                "directive is not closed: missing '}}'",
                context={"template": template},
            )
        body = template[open_at + 2 : close_at]
        if "{{" in body:
            raise DirectiveError(
                "nested directives are not allowed",
                context={"template": template},
            )
        parts.append(("dir", body))
        pos = close_at + 2
    return parts


def _reject_stray_close(template: str, literal: str) -> None:
    """リテラル中に閉じ波括弧 ``}}`` が現れたら未開始エラーとして拒否する。"""
    if "}}" in literal:
        raise DirectiveError(
            "unbalanced directive: '}}' without matching '{{'",
            context={"template": template},
        )


def parse_template(template: str) -> ParsedTemplate:
    """テンプレ文字列を :class:`ParsedTemplate` に解析する(ADR-0017)。

    ``{{...}}`` を抽出し、中身を自動判別して変数参照 / 生成スロットに振り分ける。
    生成スロットには出現順の安定 ID(``gen_0`` 等)を採番する。
    リテラルの波括弧は ``\\{\\{`` / ``\\}\\}`` でエスケープして表現できる。

    Args:
        template: テンプレ文字列(タイトル・説明文・サムネプロンプト等)。

    Returns:
        パース済みテンプレ(不変)。

    Raises:
        DirectiveError: 空 directive・ネスト・未閉じ・未開始の波括弧を検出した場合。
    """
    # エスケープを退避してから波括弧を走査(ADR-0017 のエスケープ表記)。
    escaped = template.replace(_ESCAPED_OPEN, _PH_OPEN).replace(_ESCAPED_CLOSE, _PH_CLOSE)

    segments: list[_Segment] = []
    variable_names: list[str] = []
    generative_slots: list[GenerativeSlot] = []

    for kind, raw in _split_directives(escaped):
        if kind == "lit":
            literal = _restore_escapes(raw)
            if literal:
                segments.append(literal)
            continue
        body = raw.strip()
        if not body:
            raise DirectiveError(
                "empty directive '{{}}' is not allowed",
                context={"template": template},
            )
        if _classify(body) == "var":
            slot = VariableSlot(name=body)
            segments.append(slot)
            if slot.name not in variable_names:
                variable_names.append(slot.name)
        else:
            gen = GenerativeSlot(
                slot_id=f"{_GEN_ID_PREFIX}{len(generative_slots)}",
                instruction=body,
            )
            segments.append(gen)
            generative_slots.append(gen)

    return ParsedTemplate(
        segments=tuple(segments),
        variable_names=tuple(variable_names),
        generative_slots=tuple(generative_slots),
    )


def _restore_escapes(text: str) -> str:
    """退避していたエスケープを実体の ``{{`` / ``}}`` に戻す。"""
    return text.replace(_PH_OPEN, "{{").replace(_PH_CLOSE, "}}")


def render_template(
    parsed: ParsedTemplate,
    *,
    context: Mapping[str, str],
    generated: Mapping[str, str],
) -> str:
    """パース済みテンプレを context と生成結果で展開する(ADR-0017)。

    変数参照は ``context`` から、生成ディレクティブは ``generated``(finisher LLM が
    ``slot_id`` をキーに展開した結果)から解決する。いずれか欠落していれば
    :class:`DirectiveError` を送出する(未定義変数の早期検出、ADR-0017)。

    Args:
        parsed: :func:`parse_template` の結果。
        context: 変数名 → 値の辞書。
        generated: ``slot_id`` → 生成テキストの辞書。

    Returns:
        展開後の文字列。

    Raises:
        DirectiveError: 未定義変数 / 生成結果欠落を検出した場合。
    """
    out: list[str] = []
    for segment in parsed.segments:
        out.append(_render_segment(segment, context=context, generated=generated))
    return "".join(out)


def _render_segment(
    segment: _Segment,
    *,
    context: Mapping[str, str],
    generated: Mapping[str, str],
) -> str:
    """1 セグメントを文字列に展開する。"""
    if isinstance(segment, VariableSlot):
        if segment.name not in context:
            raise DirectiveError(
                f"undefined variable: {segment.name!r}",
                context={"variable": segment.name},
            )
        return context[segment.name]
    if isinstance(segment, GenerativeSlot):
        if segment.slot_id not in generated:
            raise DirectiveError(
                f"missing generated text for slot {segment.slot_id!r}",
                context={"slot_id": segment.slot_id, "instruction": segment.instruction},
            )
        return generated[segment.slot_id]
    return segment


def variable_names_of(parsed: ParsedTemplate) -> Sequence[str]:
    """パース済みテンプレが要求する変数名(重複排除・出現順)を返す。"""
    return parsed.variable_names
