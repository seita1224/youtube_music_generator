"""説明文(YouTube description)レンダラ(FR-052 / FR-053, ADR-0034 §(2))。

``templates/description/default.yaml`` の ``body`` を骨格に、最終的な説明文を 1 本合成する。
構成要素は 4 種:

1. **シーン説明(英 + 日)** — finisher LLM が ``description_directive`` を踏まえて生成。
2. **チャプター** — 6 トラック x 5 分のタイムスタンプ + トラックタイトル(英 + 日)を動的に組む。
   タイトルは finisher LLM 生成、タイムスタンプはテンプレの ``interval_seconds`` から機械的に算出。
3. **AI 開示固定文** — ``_shared/ai_disclosure.txt`` を**逐語**で挿入(FR-053: LLM は一切書き換えない)。
   チャンネル宣伝(``_shared/channel_promo.txt``)も同様に逐語で挿入する。
4. **ハッシュタグ 3 個** — ジャンル系 2 個 + シーン系 1 個を finisher LLM が生成。

テンプレ ``body`` 内の ``{{name}}`` は全て ASCII 識別子のため、ADR-0017 のパーサでは
:class:`VariableSlot` として解釈される。本レンダラは「LLM 生成値 + 固定ブロック + チャプター」を
1 つの context dict に集約し、:func:`render_template` で一括展開する(``{{自由文}}`` 生成スロットは
本テンプレには含まれない)。LLM 呼び出しは ``description_directive`` を共有 instruction とした
個別フィールド単位で行い、各フィールドの ``max_chars`` を制約として渡す。

エラー分類(ADR-0028)::

    - テンプレ ``body`` / ``chapters`` / ``llm_fields`` の構造不正 → :class:`QualityError`
      (該当部分のみスキップしデフォルト値で続行できる運用ミス。テンプレ破損自体は
      :class:`~ymg_backend.domain.templates.loader.TemplateLoadError`(fatal)でローダが先に弾く)。
    - finisher LLM の生成失敗は finisher 実装が送出する例外(quality 等)をそのまま伝播させる。

不変データ志向: テンプレ ``data`` は :class:`~types.MappingProxyType` で受け取り変更しない。
context dict は本関数内で構築して :func:`render_template` に渡すのみ。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from loguru import logger

from ymg_backend.domain.directive.parser import parse_template, render_template
from ymg_backend.domain.errors import QualityError
from ymg_backend.llm.base import FinisherRequest

if TYPE_CHECKING:
    from ymg_backend.domain.plans.schemas import DailyPost
    from ymg_backend.domain.templates.loader import GenreTemplate
    from ymg_backend.llm.base import FinisherClient

# テンプレ ``body`` 内で「本レンダラが埋める固定ブロック / 動的チャプター」のキー。
# これらは finisher LLM の生成対象ではない(FR-053: 固定文は LLM が書き換えない)。
_KEY_CHAPTERS: Final[str] = "chapters"
_KEY_AI_DISCLOSURE: Final[str] = "ai_disclosure"
_KEY_CHANNEL_PROMO: Final[str] = "channel_promo"

# ``shared`` マッピングから固定ブロックを引くときのキー(default.yaml ``shared`` 節と一致)。
_SHARED_AI_DISCLOSURE: Final[str] = "ai_disclosure"
_SHARED_CHANNEL_PROMO: Final[str] = "channel_promo"

# チャプター生成に使う LLM フィールド名(default.yaml ``llm_fields`` の ``repeat: 6`` 項目)。
_FIELD_TRACK_TITLE_EN: Final[str] = "track_title_en"
_FIELD_TRACK_TITLE_JA: Final[str] = "track_title_ja"

# repeat フィールドのデフォルト最大文字数(llm_fields に max_chars が無い場合の保険)。
_DEFAULT_MAX_CHARS: Final[int] = 40


def _require_str(data: Mapping[str, Any], key: str, *, where: str) -> str:
    """テンプレ ``data`` から文字列値を取り出す。欠落 / 型不一致は ``QualityError``。"""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise QualityError(
            f"description template field {key!r} must be a non-empty string",
            context={"where": where, "key": key, "type": type(value).__name__},
        )
    return value


def _require_mapping(data: Mapping[str, Any], key: str, *, where: str) -> Mapping[str, Any]:
    """テンプレ ``data`` からマッピング値を取り出す。欠落 / 型不一致は ``QualityError``。"""
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise QualityError(
            f"description template field {key!r} must be a mapping",
            context={"where": where, "key": key, "type": type(value).__name__},
        )
    return value


def _require_int(data: Mapping[str, Any], key: str, *, where: str) -> int:
    """テンプレ ``data`` から正の整数値を取り出す。欠落 / 非正は ``QualityError``。"""
    value = data.get(key)
    # bool は int のサブクラスのため明示的に除外する。
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise QualityError(
            f"description template field {key!r} must be a positive integer",
            context={"where": where, "key": key, "value": value},
        )
    return value


def _format_timestamp(total_seconds: int) -> str:
    """秒数を YouTube チャプター用タイムスタンプ(``M:SS`` / ``H:MM:SS``)に整形する。"""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _build_finisher_context(daily_post: DailyPost, genre: str) -> dict[str, str]:
    """finisher へ渡す共有 context(genre / mood / 視覚意図 / directive)を組む。"""
    return {
        "genre": genre,
        "mood": daily_post.mood,
        "visual_direction": daily_post.visual_direction,
        "description_directive": daily_post.description_directive,
    }


def _resolve_max_chars(field: Mapping[str, Any]) -> int:
    """``llm_fields`` 項目から ``max_chars`` を解決する(未指定は既定値)。"""
    raw = field.get("max_chars")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return _DEFAULT_MAX_CHARS


async def _generate_field(
    *,
    finisher: FinisherClient,
    field: Mapping[str, Any],
    daily_post: DailyPost,
    genre: str,
    index: int | None = None,
) -> str:
    """1 つの ``llm_fields`` 値を finisher で生成する。

    ``index`` を渡すと repeat フィールド(トラック別)としてトラック番号を instruction に付す。
    instruction は ``description_directive`` + フィールドの ``note`` を組み合わせ、
    生成対象が一意に定まるようにする。
    """
    name = _require_str(field, "name", where="llm_fields")
    note = field.get("note")
    note_text = note if isinstance(note, str) else ""
    track_hint = f" (Track {index})" if index is not None else ""
    instruction = (
        f"{daily_post.description_directive}\n"
        f"Generate the '{name}'{track_hint} field for a music video description. {note_text}"
    ).strip()
    req = FinisherRequest(
        instruction=instruction,
        context=_build_finisher_context(daily_post, genre),
        max_chars=_resolve_max_chars(field),
    )
    response = await finisher.render(req)
    return response.text.strip()


def _build_chapters_block(
    *,
    chapters_spec: Mapping[str, Any],
    track_titles_en: list[str],
    track_titles_ja: list[str],
) -> str:
    """チャプターブロック(ヘッダ + 各トラック行)を組む(FR-052)。

    タイムスタンプは ``interval_seconds`` x トラック index で機械的に算出し、
    トラックタイトル(英 / 日)は finisher 生成値を差し込む。
    行フォーマットは ``line_format``(``{{start}} {{track_title_en}} / {{track_title_ja}}``)。
    """
    interval = _require_int(chapters_spec, "interval_seconds", where="chapters")
    count = _require_int(chapters_spec, "count", where="chapters")
    header = _require_str(chapters_spec, "header", where="chapters")
    line_format = _require_str(chapters_spec, "line_format", where="chapters")

    if len(track_titles_en) != count or len(track_titles_ja) != count:
        raise QualityError(
            "chapter track titles count mismatch",
            context={
                "expected": count,
                "got_en": len(track_titles_en),
                "got_ja": len(track_titles_ja),
            },
        )

    parsed_line = parse_template(line_format)
    lines: list[str] = [header]
    for idx in range(count):
        start = _format_timestamp(interval * idx)
        line = render_template(
            parsed_line,
            context={
                "start": start,
                _FIELD_TRACK_TITLE_EN: track_titles_en[idx],
                _FIELD_TRACK_TITLE_JA: track_titles_ja[idx],
            },
            generated={},
        )
        lines.append(line)
    return "\n".join(lines)


async def _generate_repeat_titles(
    *,
    finisher: FinisherClient,
    field: Mapping[str, Any],
    count: int,
    daily_post: DailyPost,
    genre: str,
) -> list[str]:
    """``repeat: N`` のトラックタイトルフィールドを N 件生成する。"""
    return [
        await _generate_field(
            finisher=finisher,
            field=field,
            daily_post=daily_post,
            genre=genre,
            index=idx + 1,
        )
        for idx in range(count)
    ]


def _index_llm_fields(template: GenreTemplate) -> dict[str, Mapping[str, Any]]:
    """``llm_fields`` を ``name`` でインデックス化する。重複 / 不正は ``QualityError``。"""
    raw = template.data.get("llm_fields")
    if not isinstance(raw, list):
        raise QualityError(
            "description template 'llm_fields' must be a list",
            context={"genre": template.genre, "type": type(raw).__name__},
        )
    indexed: dict[str, Mapping[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise QualityError(
                "each 'llm_fields' entry must be a mapping",
                context={"genre": template.genre, "type": type(entry).__name__},
            )
        name = _require_str(entry, "name", where="llm_fields")
        indexed[name] = entry
    return indexed


async def render_description(
    *,
    daily_post: DailyPost,
    genre: str,
    finisher: FinisherClient,
    template: GenreTemplate,
    shared: Mapping[str, str],
) -> str:
    """説明文を合成する(FR-052 / FR-053)。

    Args:
        daily_post: 当該投稿の指示(``description_directive`` / ``mood`` / 視覚意図)。
        genre: ジャンル名(DB 表記)。finisher の context に注入する。
        finisher: ``{{自由文}}`` / LLM フィールド生成用クライアント(ADR-0032)。
        template: ``description`` カテゴリのテンプレ(``default.yaml``)。``data`` に
            ``body`` / ``chapters`` / ``llm_fields`` を含む。
        shared: 固定ブロック名 → 逐語テキストのマッピング(``ai_disclosure`` /
            ``channel_promo``)。FR-053: AI 開示文は LLM が書き換えないため、
            呼び出し側が ``TemplateLoader.load_shared_text`` で読んだ生テキストを渡す。

    Returns:
        合成済みの ``final_description``(合成メディア開示文 + チャプター + ハッシュタグを含む)。

    Raises:
        QualityError: テンプレ構造の不正 / 固定ブロック欠落 / チャプター件数不一致など。
    """
    body = _require_str(template.data, "body", where="body")
    chapters_spec = _require_mapping(template.data, _KEY_CHAPTERS, where="chapters")
    llm_fields = _index_llm_fields(template)

    parsed_body = parse_template(body)
    required = set(parsed_body.variable_names)

    # 1) 固定ブロック(逐語) — FR-053。shared から取得、欠落は QualityError。
    context: dict[str, str] = {}
    for ctx_key, shared_key in (
        (_KEY_AI_DISCLOSURE, _SHARED_AI_DISCLOSURE),
        (_KEY_CHANNEL_PROMO, _SHARED_CHANNEL_PROMO),
    ):
        if ctx_key not in required:
            continue
        text = shared.get(shared_key)
        if not isinstance(text, str) or not text:
            raise QualityError(
                f"required shared block {shared_key!r} is missing or empty",
                context={"shared_key": shared_key, "genre": genre},
            )
        context[ctx_key] = text.strip()

    # 2) チャプター(動的) — トラックタイトル 英 / 日 を 6 件ずつ生成してから組む。
    count = _require_int(chapters_spec, "count", where="chapters")
    track_titles_en = await _generate_repeat_titles(
        finisher=finisher,
        field=_require_repeat_field(llm_fields, _FIELD_TRACK_TITLE_EN, genre),
        count=count,
        daily_post=daily_post,
        genre=genre,
    )
    track_titles_ja = await _generate_repeat_titles(
        finisher=finisher,
        field=_require_repeat_field(llm_fields, _FIELD_TRACK_TITLE_JA, genre),
        count=count,
        daily_post=daily_post,
        genre=genre,
    )
    if _KEY_CHAPTERS in required:
        context[_KEY_CHAPTERS] = _build_chapters_block(
            chapters_spec=chapters_spec,
            track_titles_en=track_titles_en,
            track_titles_ja=track_titles_ja,
        )

    # 3) 残りの LLM フィールド(シーン説明 / ハッシュタグ等)を単発生成して埋める。
    static_keys = {_KEY_CHAPTERS, _KEY_AI_DISCLOSURE, _KEY_CHANNEL_PROMO}
    for name in required - static_keys - set(context):
        field = llm_fields.get(name)
        if field is None:
            raise QualityError(
                f"description body references unknown placeholder {name!r}",
                context={"placeholder": name, "genre": genre},
            )
        context[name] = await _generate_field(
            finisher=finisher,
            field=field,
            daily_post=daily_post,
            genre=genre,
        )

    rendered = render_template(parsed_body, context=context, generated={})
    logger.bind(genre=genre, step="render_description").debug(
        "rendered description: {} chars", len(rendered)
    )
    return rendered.strip()


def _require_repeat_field(
    llm_fields: Mapping[str, Mapping[str, Any]],
    name: str,
    genre: str,
) -> Mapping[str, Any]:
    """repeat フィールド(トラックタイトル)を取得する。欠落は ``QualityError``。"""
    field = llm_fields.get(name)
    if field is None:
        raise QualityError(
            f"description template missing repeat field {name!r}",
            context={"field": name, "genre": genre},
        )
    return field
