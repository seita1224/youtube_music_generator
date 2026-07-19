"""ディレクティブパーサ critical テスト (ADR-0017, Constitution II)。

要件 6.4 / 6.5 のテンプレ機能で ``{{...}}`` に混在する 2 種類のディレクティブ:

1. **変数参照**: 中身が単一識別子(``\\w+``)→ context dict で解決。
2. **生成ディレクティブ**: 自由文(空白・日本語の文章)→ finisher LLM 行きのスロット。

本テストは ADR-0017「実装(概略)」の自動判別仕様を network なしで 100% カバーする。
網羅対象: ``{{genre}}`` 変数 / ``{{12字以内の日本語サブタイト}}`` 自由文 / mixed 入力 /
未定義変数エラー / 空 directive 拒否 / エスケープ / ネスト禁止。
"""

from __future__ import annotations

import pytest

from ymg_backend.domain.directive import (
    DirectiveError,
    GenerativeSlot,
    ParsedTemplate,
    VariableSlot,
    parse_template,
    render_template,
    variable_names_of,
)

# --- 分類: 変数参照 ----------------------------------------------------------------


@pytest.mark.fr("FR-040")
def test_single_identifier_is_variable() -> None:
    """FR-040: 単一識別子 ``{{genre}}`` は変数参照として分類される。"""
    parsed = parse_template("{{genre}}")
    assert parsed.variable_names == ("genre",)
    assert parsed.generative_slots == ()


def test_variable_with_underscore_and_digits() -> None:
    # \w+ なのでアンダースコア・数字を含む識別子は変数扱い。
    parsed = parse_template("{{bpm_range_2}}")
    assert parsed.variable_names == ("bpm_range_2",)
    assert parsed.generative_slots == ()


def test_variable_body_is_stripped() -> None:
    # 前後空白のみ(中身は単一識別子)は変数として扱う(ADR-0017 IDENT_RE.fullmatch(strip()))。
    parsed = parse_template("{{  genre  }}")
    assert parsed.variable_names == ("genre",)
    assert parsed.generative_slots == ()


@pytest.mark.fr("FR-041")
def test_render_resolves_variable_from_context() -> None:
    """FR-041: 変数参照は context dict の値で解決される。"""
    parsed = parse_template("genre: {{genre}}")
    rendered = render_template(parsed, context={"genre": "lofi"}, generated={})
    assert rendered == "genre: lofi"


# --- 分類: 生成ディレクティブ(自由文) ---------------------------------------------


def test_japanese_free_text_is_generative() -> None:
    parsed = parse_template("{{12字以内の日本語サブタイト}}")
    assert parsed.variable_names == ()
    assert len(parsed.generative_slots) == 1
    slot = parsed.generative_slots[0]
    assert isinstance(slot, GenerativeSlot)
    assert slot.instruction == "12字以内の日本語サブタイト"


def test_free_text_with_space_is_generative() -> None:
    # 空白を含む内容は識別子にならないため生成ディレクティブ。
    parsed = parse_template("{{この曲の雰囲気を1文で}}")
    assert parsed.generative_slots[0].instruction == "この曲の雰囲気を1文で"
    assert parsed.variable_names == ()


def test_generative_slot_has_stable_id_by_order() -> None:
    parsed = parse_template("{{雰囲気を一言}}/{{キャッチコピー}}")
    ids = [slot.slot_id for slot in parsed.generative_slots]
    assert ids == ["gen_0", "gen_1"]


def test_render_resolves_generative_from_generated() -> None:
    parsed = parse_template("title: {{12字以内の日本語サブタイト}}")
    rendered = render_template(
        parsed,
        context={},
        generated={"gen_0": "夜更けのまどろみ"},
    )
    assert rendered == "title: 夜更けのまどろみ"


# --- mixed 入力 --------------------------------------------------------------------


def test_mixed_variable_and_generative() -> None:
    parsed = parse_template("[{{genre}}] {{この曲の雰囲気を1文で}} ({{bpm}})")
    assert parsed.variable_names == ("genre", "bpm")
    assert [slot.instruction for slot in parsed.generative_slots] == ["この曲の雰囲気を1文で"]
    assert parsed.generative_slots[0].slot_id == "gen_0"


def test_render_mixed_full() -> None:
    parsed = parse_template("[{{genre}}] {{サブタイトル}} @{{bpm}}bpm")
    rendered = render_template(
        parsed,
        context={"genre": "lofi", "bpm": "85"},
        generated={"gen_0": "雨上がりの午後"},
    )
    assert rendered == "[lofi] 雨上がりの午後 @85bpm"


def test_duplicate_variable_collected_once() -> None:
    parsed = parse_template("{{genre}}-{{genre}}")
    assert parsed.variable_names == ("genre",)
    rendered = render_template(parsed, context={"genre": "lofi"}, generated={})
    assert rendered == "lofi-lofi"


# --- 未定義変数エラー --------------------------------------------------------------


def test_render_unknown_variable_raises() -> None:
    parsed = parse_template("{{unknown_var}}")
    with pytest.raises(DirectiveError) as exc_info:
        render_template(parsed, context={}, generated={})
    assert "unknown_var" in str(exc_info.value)
    assert exc_info.value.context["variable"] == "unknown_var"


def test_render_missing_generated_slot_raises() -> None:
    parsed = parse_template("{{この曲の雰囲気を1文で}}")
    with pytest.raises(DirectiveError) as exc_info:
        render_template(parsed, context={}, generated={})
    assert exc_info.value.context["slot_id"] == "gen_0"


# --- 空 directive 拒否 -------------------------------------------------------------


def test_empty_directive_rejected() -> None:
    with pytest.raises(DirectiveError) as exc_info:
        parse_template("{{}}")
    assert "empty" in str(exc_info.value).lower()


def test_whitespace_only_directive_rejected() -> None:
    with pytest.raises(DirectiveError):
        parse_template("{{   }}")


# --- ネスト禁止 / 未閉じ ------------------------------------------------------------


def test_nested_directive_rejected() -> None:
    with pytest.raises(DirectiveError) as exc_info:
        parse_template("{{ {{x}} }}")
    assert "nest" in str(exc_info.value).lower()


def test_unbalanced_open_rejected() -> None:
    with pytest.raises(DirectiveError):
        parse_template("{{genre")


def test_unbalanced_close_rejected() -> None:
    with pytest.raises(DirectiveError):
        parse_template("genre}}")


# --- エスケープ \{\{ \}\} -----------------------------------------------------------


def test_escaped_braces_are_literal() -> None:
    parsed = parse_template(r"literal \{\{not a directive\}\}")
    assert parsed.variable_names == ()
    assert parsed.generative_slots == ()
    rendered = render_template(parsed, context={}, generated={})
    assert rendered == "literal {{not a directive}}"


def test_escape_mixed_with_directive() -> None:
    parsed = parse_template(r"\{\{lit\}\} {{genre}}")
    assert parsed.variable_names == ("genre",)
    rendered = render_template(parsed, context={"genre": "lofi"}, generated={})
    assert rendered == "{{lit}} lofi"


# --- 構造の不変性 ------------------------------------------------------------------


def test_parsed_template_is_immutable() -> None:
    parsed = parse_template("{{genre}}")
    assert isinstance(parsed, ParsedTemplate)
    with pytest.raises((AttributeError, TypeError)):
        parsed.variable_names = ("other",)  # type: ignore[misc]


def test_variable_slot_is_frozen() -> None:
    slot = VariableSlot(name="genre")
    with pytest.raises((AttributeError, TypeError)):
        slot.name = "other"  # type: ignore[misc]


def test_no_directive_returns_plain_text() -> None:
    parsed = parse_template("plain text only")
    assert parsed.variable_names == ()
    assert parsed.generative_slots == ()
    rendered = render_template(parsed, context={}, generated={})
    assert rendered == "plain text only"


def test_empty_template_renders_empty() -> None:
    parsed = parse_template("")
    rendered = render_template(parsed, context={}, generated={})
    assert rendered == ""


# --- 公開ヘルパ --------------------------------------------------------------------


def test_variable_names_of_returns_required_variables() -> None:
    parsed = parse_template("[{{genre}}] {{この曲の雰囲気}} @{{bpm}}")
    assert tuple(variable_names_of(parsed)) == ("genre", "bpm")
