"""SDXL 背景へのサムネテキストオーバーレイ合成 (ADR-0034 §(3)(6))。

GPU が生成した背景画像 (``base_image_uri``) に Pillow でジャンル別テキスト +
ジャンルバッジを重ね、 YouTube サムネ (1280x720 / JPEG) として書き出す。

レイアウトは 2 段階で解決する::

    - ジャンル別 (``templates/thumbnail/<genre>.yaml``): フォント名・文字色・縁取り色・
      縁取り幅・テキスト位置プリセット・バッジ背景色。呼び出し側が ``GenreTemplate`` で渡す。
    - ジャンル共通 (``templates/thumbnail/_shared/layout.json`` / ``badge.json``):
      キャンバス寸法・各要素の座標・フォントサイズ・バッジ形状。本モジュールが
      :class:`TemplateLoader` で読み込む。欠落しても落ちないよう内蔵の既定値へフォールバックする。

フォント解決 (ADR-0034 §(6))::

    SIL OFL のフォントファイルを ``templates/fonts/`` から名前で解決する。 T050 (バイナリ同梱) が
    未完でファイルが無い場合でも、 PIL 既定フォント (``ImageFont.load_default``) へフォールバックして
    処理を継続し、 WARNING を 1 度だけログする (運用上のサムネ品質低下は許容、 ADR-0034 「受容したリスク」)。

サイズ制約 (FR / YouTube 仕様)::

    サムネは 2MB 以内。 JPEG quality 90 を起点に、 超過時は quality を段階的に下げて 2MB 以内へ収める。

エラー分類 (ADR-0028)::

    背景画像の読込・デコード失敗、 合成の致命的な失敗は :class:`QualityError` (``quality``) で送出する。
    該当 post のみスキップしデフォルト処理で続行できる品質低下として扱う (サイクルは止めない)。

不変性方針: 入力テンプレ (``GenreTemplate.data`` は ``MappingProxyType``) は読み取り専用で参照し、
解決済みのレイアウト値はローカルの不変 dataclass / プリミティブへ写してから描画する。
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from loguru import logger
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from ymg_backend.domain.errors import QualityError
from ymg_backend.domain.templates.loader import (
    GenreTemplate,
    TemplateCategory,
    TemplateLoader,
    TemplateNotFoundError,
)

if TYPE_CHECKING:
    from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

# ``templates/fonts`` の既定ルート。本ファイル
# (``.../domain/render/thumbnail_overlay.py``) から ``backend/`` まで 4 階層遡る
# (``render`` → ``domain`` → ``ymg_backend`` → ``src`` → ``backend``)。
_FONTS_ROOT: Final[Path] = Path(__file__).resolve().parents[4] / "templates" / "fonts"

# 共通レイアウト / バッジ仕様のファイル名 (``_shared`` 配下、 ADR-0034 §(3))。
_LAYOUT_ASSET: Final[str] = "layout.json"
_BADGE_ASSET: Final[str] = "badge.json"

# 共通アセット欠落時に使う内蔵フォールバック (layout.json / badge.json と同値)。
# T050 未完や部分セットアップでも合成を継続できるようにする。
_FALLBACK_CANVAS: Final[Mapping[str, int]] = {"width": 1280, "height": 720, "quality": 90}
_FALLBACK_HEADLINE: Final[Mapping[str, int]] = {
    "font_size": 96,
    "max_width": 1120,
    "max_lines": 2,
    "line_spacing": 8,
    "x": 640,
    "y": 560,
}
_FALLBACK_BADGE: Final[Mapping[str, Any]] = {
    "max_chars": 12,
    "x": 48,
    "y": 48,
    "pad_x": 24,
    "pad_y": 12,
    "corner_radius": 12,
    "font_size": 40,
    "text_color": "#FFFFFF",
    "background_opacity": 0.6,
    "default_background_color": "#1A1A1A",
}

# JPEG 出力の起点品質と最小許容品質 (2MB 制約のための段階的劣化下限)。
_JPEG_QUALITY_START: Final[int] = 90
_JPEG_QUALITY_MIN: Final[int] = 60
_JPEG_QUALITY_STEP: Final[int] = 5

# YouTube サムネのサイズ上限 (2MB)。
_MAX_BYTES: Final[int] = 2 * 1024 * 1024

# フォント表示名 → ``templates/fonts`` 配下の候補ファイル名 (ADR-0034 §(6) / fonts/README.md)。
# 大文字小文字・ハイフン/アンダースコア差を吸収するため複数候補を順に試す。
_FONT_FILE_CANDIDATES: Final[Mapping[str, tuple[str, ...]]] = {
    "bebas neue": ("BebasNeue-Regular.ttf", "BebasNeue.ttf"),
    "cormorant garamond": ("CormorantGaramond-Regular.ttf", "CormorantGaramond.ttf"),
    "vt323": ("VT323-Regular.ttf", "VT323.ttf"),
    "playfair display": ("PlayfairDisplay-Regular.ttf", "PlayfairDisplay.ttf"),
    "space grotesk": ("SpaceGrotesk-Regular.ttf", "SpaceGrotesk.ttf"),
    "noto sans jp": (
        "NotoSansJP-Regular.ttf",
        "NotoSansJP-Regular.otf",
        "NotoSansJP.ttf",
    ),
}

# 描画で使う PIL フォント型 (TrueType もしくは既定ビットマップ)。
_PilFont = ImageFont.FreeTypeFont | ImageFont.ImageFont


@dataclass(frozen=True, slots=True)
class _LayoutSpec:
    """``_shared`` から解決した不変レイアウト値 (描画専用の写し)。"""

    canvas_width: int
    canvas_height: int
    quality: int
    headline_font_size: int
    headline_max_width: int
    headline_max_lines: int
    headline_line_spacing: int
    headline_x: int
    headline_y: int


@dataclass(frozen=True, slots=True)
class _BadgeSpec:
    """``_shared/badge.json`` から解決した不変バッジ値。"""

    max_chars: int
    x: int
    y: int
    pad_x: int
    pad_y: int
    corner_radius: int
    font_size: int
    text_color: str
    background_opacity: float


def _as_int(value: object, default: int) -> int:
    """マッピング値を ``int`` へ寄せる (型不一致は既定値へフォールバック)。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _as_float(value: object, default: float) -> float:
    """マッピング値を ``float`` へ寄せる (型不一致は既定値へフォールバック)。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _as_str(value: object, default: str) -> str:
    """マッピング値を ``str`` へ寄せる (型不一致は既定値へフォールバック)。"""
    return value if isinstance(value, str) else default


def _load_shared(loader: TemplateLoader) -> tuple[_LayoutSpec, _BadgeSpec]:
    """``_shared/layout.json`` / ``badge.json`` を解決して不変 spec を返す。

    欠落 (``TemplateNotFoundError``) は内蔵フォールバックで吸収し WARNING をログする。
    破損 (``TemplateLoadError`` = fatal) はそのまま伝播させる (設定ファイル破損 = サイクル停止)。
    """
    try:
        layout_raw = loader.load_shared_json(TemplateCategory.THUMBNAIL, _LAYOUT_ASSET)
    except TemplateNotFoundError:
        logger.warning(
            "shared thumbnail layout missing; using built-in fallback geometry",
            step="thumbnail",
            asset=_LAYOUT_ASSET,
        )
        layout_raw = {}
    try:
        badge_raw = loader.load_shared_json(TemplateCategory.THUMBNAIL, _BADGE_ASSET)
    except TemplateNotFoundError:
        logger.warning(
            "shared thumbnail badge missing; using built-in fallback geometry",
            step="thumbnail",
            asset=_BADGE_ASSET,
        )
        badge_raw = {}
    return _build_layout_spec(layout_raw), _build_badge_spec(badge_raw)


def _build_layout_spec(raw: Mapping[str, Any]) -> _LayoutSpec:
    """layout.json マッピング (または空) から ``_LayoutSpec`` を構築する。"""
    canvas = raw.get("canvas")
    canvas_map: Mapping[str, Any] = canvas if isinstance(canvas, Mapping) else {}
    elements = raw.get("elements")
    elements_map: Mapping[str, Any] = elements if isinstance(elements, Mapping) else {}
    headline = elements_map.get("headline")
    head_map: Mapping[str, Any] = headline if isinstance(headline, Mapping) else {}
    position = head_map.get("position")
    pos_map: Mapping[str, Any] = position if isinstance(position, Mapping) else {}
    return _LayoutSpec(
        canvas_width=_as_int(canvas_map.get("width"), _FALLBACK_CANVAS["width"]),
        canvas_height=_as_int(canvas_map.get("height"), _FALLBACK_CANVAS["height"]),
        quality=_as_int(canvas_map.get("quality"), _FALLBACK_CANVAS["quality"]),
        headline_font_size=_as_int(head_map.get("font_size"), _FALLBACK_HEADLINE["font_size"]),
        headline_max_width=_as_int(head_map.get("max_width"), _FALLBACK_HEADLINE["max_width"]),
        headline_max_lines=_as_int(head_map.get("max_lines"), _FALLBACK_HEADLINE["max_lines"]),
        headline_line_spacing=_as_int(
            head_map.get("line_spacing"), _FALLBACK_HEADLINE["line_spacing"]
        ),
        headline_x=_as_int(pos_map.get("x"), _FALLBACK_HEADLINE["x"]),
        headline_y=_as_int(pos_map.get("y"), _FALLBACK_HEADLINE["y"]),
    )


def _build_badge_spec(raw: Mapping[str, Any]) -> _BadgeSpec:
    """badge.json マッピング (または空) から ``_BadgeSpec`` を構築する。"""
    position = raw.get("position")
    pos_map: Mapping[str, Any] = position if isinstance(position, Mapping) else {}
    padding = raw.get("padding")
    pad_map: Mapping[str, Any] = padding if isinstance(padding, Mapping) else {}
    return _BadgeSpec(
        max_chars=_as_int(raw.get("max_chars"), _FALLBACK_BADGE["max_chars"]),
        x=_as_int(pos_map.get("x"), _FALLBACK_BADGE["x"]),
        y=_as_int(pos_map.get("y"), _FALLBACK_BADGE["y"]),
        pad_x=_as_int(pad_map.get("x"), _FALLBACK_BADGE["pad_x"]),
        pad_y=_as_int(pad_map.get("y"), _FALLBACK_BADGE["pad_y"]),
        corner_radius=_as_int(raw.get("corner_radius"), _FALLBACK_BADGE["corner_radius"]),
        font_size=_as_int(raw.get("font_size"), _FALLBACK_BADGE["font_size"]),
        text_color=_as_str(raw.get("text_color"), _FALLBACK_BADGE["text_color"]),
        background_opacity=_as_float(
            raw.get("background_opacity"), _FALLBACK_BADGE["background_opacity"]
        ),
    )


def _resolve_font(font_name: str, size: int) -> _PilFont:
    """フォント表示名から TrueType フォントを解決する。

    ``templates/fonts`` 配下の候補ファイルを順に試し、 見つからなければ PIL 既定フォントへ
    フォールバックして WARNING をログする (ADR-0034 §(6) / fonts/README.md)。
    """
    candidates = _FONT_FILE_CANDIDATES.get(font_name.strip().lower(), ())
    for filename in candidates:
        path = _FONTS_ROOT / filename
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                logger.warning(
                    "failed to load bundled font; falling back to default",
                    step="thumbnail",
                    font=font_name,
                    path=str(path),
                )
                break
    logger.warning(
        "thumbnail font not bundled; falling back to PIL default font",
        step="thumbnail",
        font=font_name,
        size=size,
    )
    return ImageFont.load_default(size=size)


def _wrap_text(
    text: str, font: _PilFont, draw: ImageDraw.ImageDraw, *, max_width: int, max_lines: int
) -> list[str]:
    """テキストを ``max_width`` (px) 以内へ折り返す (CJK は文字単位、 ラテンは語単位)。

    日本語見出しは空白区切りが無いことが多いため、 まず語 (空白) 単位で詰め、 1 語が幅を超える
    場合は文字単位でさらに割る。 ``max_lines`` を超えた末尾は末行に省略記号付きで畳む。
    """

    def width_of(s: str) -> float:
        return draw.textlength(s, font=font)

    lines: list[str] = []
    current = ""
    tokens = _tokenize_for_wrap(text)
    for token in tokens:
        candidate = current + token
        if width_of(candidate) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = token
        # 1 トークンが幅超過 (長い英単語等) の場合は文字単位で割る。
        while width_of(current) > max_width and len(current) > 1:
            split_at = _max_prefix_within(current, font, draw, max_width)
            lines.append(current[:split_at])
            current = current[split_at:]
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    kept[-1] = _truncate_with_ellipsis(kept[-1], font, draw, max_width)
    return kept


def _tokenize_for_wrap(text: str) -> list[str]:
    """折り返し用にトークン分割する。 空白を含む箇所は空白付きで保持する。"""
    tokens: list[str] = []
    buffer = ""
    for ch in text:
        buffer += ch
        if ch.isspace():
            tokens.append(buffer)
            buffer = ""
    if buffer:
        tokens.append(buffer)
    return tokens


def _max_prefix_within(text: str, font: _PilFont, draw: ImageDraw.ImageDraw, max_width: int) -> int:
    """``text`` の先頭から ``max_width`` 以内に収まる最大文字数を返す (>=1)。"""
    for i in range(1, len(text)):
        if draw.textlength(text[:i], font=font) > max_width:
            return max(1, i - 1)
    return len(text)


def _truncate_with_ellipsis(
    text: str, font: _PilFont, draw: ImageDraw.ImageDraw, max_width: int
) -> str:
    """末行が幅を超える場合に省略記号 (…) を付けて収める。"""
    ellipsis = "…"
    if draw.textlength(text + ellipsis, font=font) <= max_width:
        return text + ellipsis
    truncated = text
    while truncated and draw.textlength(truncated + ellipsis, font=font) > max_width:
        truncated = truncated[:-1]
    return (truncated + ellipsis) if truncated else ellipsis


def _draw_headline(
    canvas: Image.Image,
    *,
    text: str,
    font: _PilFont,
    spec: _LayoutSpec,
    fill: str,
    outline: str,
    outline_width: int,
) -> None:
    """中央下の見出しを縁取り付きで描画する (複数行は中央揃え)。"""
    draw = ImageDraw.Draw(canvas)
    lines = _wrap_text(
        text, font, draw, max_width=spec.headline_max_width, max_lines=spec.headline_max_lines
    )
    if not lines:
        return
    ascent, descent = _font_metrics(font)
    line_height = ascent + descent + spec.headline_line_spacing
    total_height = line_height * len(lines) - spec.headline_line_spacing
    # アンカー ``ms`` (水平中央 / ベースライン) 基準で、 ブロックを y を中心に上へ積む。
    start_top = spec.headline_y - total_height
    for i, line in enumerate(lines):
        baseline_y = start_top + line_height * i + ascent
        draw.text(
            (spec.headline_x, baseline_y),
            line,
            font=font,
            fill=fill,
            anchor="ms",
            stroke_width=max(0, outline_width),
            stroke_fill=outline,
        )


def _font_metrics(font: _PilFont) -> tuple[int, int]:
    """フォントの (ascent, descent) を取得する。 既定フォントは概算で補う。"""
    getmetrics = getattr(font, "getmetrics", None)
    if callable(getmetrics):
        ascent, descent = getmetrics()
        return int(ascent), int(descent)
    return 12, 3


def _draw_badge(
    canvas: Image.Image,
    *,
    text: str,
    font: _PilFont,
    badge: _BadgeSpec,
    background_color: str,
) -> None:
    """左上にジャンルバッジ (半透明角丸 + 文字) を描画する。"""
    if not text:
        return
    label = text[: badge.max_chars]
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    text_w = int(draw.textlength(label, font=font))
    ascent, descent = _font_metrics(font)
    text_h = ascent + descent
    rect_x0 = badge.x
    rect_y0 = badge.y
    rect_x1 = rect_x0 + text_w + badge.pad_x * 2
    rect_y1 = rect_y0 + text_h + badge.pad_y * 2
    rgba = _hex_to_rgba(background_color, badge.background_opacity)
    draw.rounded_rectangle(
        (rect_x0, rect_y0, rect_x1, rect_y1), radius=badge.corner_radius, fill=rgba
    )
    draw.text(
        (rect_x0 + badge.pad_x, rect_y0 + badge.pad_y),
        label,
        font=font,
        fill=badge.text_color,
        anchor="la",
    )
    canvas.alpha_composite(overlay)


def _hex_to_rgba(color: str, opacity: float) -> tuple[int, int, int, int]:
    """``#RRGGBB`` と不透明度 (0..1) から RGBA タプルを作る。 不正色は黒へフォールバック。"""
    value = color.lstrip("#")
    if len(value) == 6:
        try:
            r = int(value[0:2], 16)
            g = int(value[2:4], 16)
            b = int(value[4:6], 16)
        except ValueError:
            r, g, b = 0, 0, 0
    else:
        r, g, b = 0, 0, 0
    alpha = max(0, min(255, round(opacity * 255)))
    return r, g, b, alpha


def _encode_jpeg_within_limit(image: Image.Image, *, start_quality: int) -> bytes:
    """RGB 画像を JPEG 化し、 2MB 以内へ収まる最大品質のバイト列を返す。

    ``start_quality`` を起点に :data:`_JPEG_QUALITY_MIN` まで段階的に下げる。 最小品質でも
    超過する場合は (これ以上落とせないため) 最小品質のバイト列をそのまま返す。
    """
    quality = start_quality
    data = b""
    while quality >= _JPEG_QUALITY_MIN:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= _MAX_BYTES:
            return data
        quality -= _JPEG_QUALITY_STEP
    logger.warning(
        "thumbnail exceeds 2MB even at minimum quality; emitting minimum-quality output",
        step="thumbnail",
        bytes=len(data),
        quality=_JPEG_QUALITY_MIN,
    )
    return data


def compose_thumbnail(
    *,
    base_image_uri: str,
    title_text: str,
    genre: str,
    template: GenreTemplate,
    storage: StorageAdapter,
    output_uri: str,
) -> str:
    """SDXL 背景にテキスト + バッジを合成し YouTube サムネ (JPEG) を書き出す。

    Args:
        base_image_uri: GPU が生成した背景画像の URI (``StorageAdapter`` で読み出す)。
        title_text: 見出しに使う日本語サブタイトル (中央下に縁取りで描画)。
        genre: ジャンル名 (DB 表記 "lo-fi hip-hop" 等)。バッジ文字に使う。
        template: ジャンル別サムネテンプレ (``font_primary`` / ``color_text`` 等)。
        storage: 入出力ストレージアダプタ。
        output_uri: 出力先 URI (``file:///srv/ymg/outputs/thumbnail/<post_id>/<name>``)。

    Returns:
        合成済みサムネの解決済み URI (``Post.thumbnail_uri`` に格納)。

    Raises:
        QualityError: 背景画像の読込 / デコード失敗、 合成失敗 (該当 post のみスキップ可能)。
    """
    data = template.data
    font_primary = _as_str(data.get("font_primary"), "Noto Sans JP")
    font_secondary = _as_str(data.get("font_secondary"), "Noto Sans JP")
    color_text = _as_str(data.get("color_text"), "#FFFFFF")
    color_outline = _as_str(data.get("color_outline"), "#000000")
    outline_width = _as_int(data.get("outline_width"), 4)
    badge_bg = _as_str(data.get("genre_badge_color"), _FALLBACK_BADGE["default_background_color"])

    layout, badge = _load_shared(TemplateLoader())

    try:
        raw = storage.read_bytes(base_image_uri)
    except Exception as exc:  # I/O 失敗は品質低下として一括分類
        raise QualityError(
            "failed to read thumbnail base image",
            context={"base_image_uri": base_image_uri, "genre": genre},
            original=exc if isinstance(exc, Exception) else None,
        ) from exc

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            base = opened.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise QualityError(
            "failed to decode thumbnail base image",
            context={"base_image_uri": base_image_uri, "genre": genre},
            original=exc,
        ) from exc

    target_size = (layout.canvas_width, layout.canvas_height)
    if base.size != target_size:
        base = base.resize(target_size, Image.Resampling.LANCZOS)

    headline_font = _resolve_font(font_secondary, layout.headline_font_size)
    badge_font = _resolve_font(font_primary, badge.font_size)

    _draw_headline(
        base,
        text=title_text,
        font=headline_font,
        spec=layout,
        fill=color_text,
        outline=color_outline,
        outline_width=outline_width,
    )
    _draw_badge(base, text=genre, font=badge_font, badge=badge, background_color=badge_bg)

    rgb = base.convert("RGB")
    jpeg_bytes = _encode_jpeg_within_limit(rgb, start_quality=_JPEG_QUALITY_START)

    resolved = storage.resolve_uri(output_uri)
    try:
        storage.write_bytes(output_uri, jpeg_bytes)
    except Exception as exc:  # 出力 I/O 失敗も品質低下として分類
        raise QualityError(
            "failed to write composed thumbnail",
            context={"output_uri": output_uri, "genre": genre},
            original=exc if isinstance(exc, Exception) else None,
        ) from exc

    logger.info(
        "composed thumbnail",
        step="thumbnail",
        genre=genre,
        output_uri=resolved,
        bytes=len(jpeg_bytes),
    )
    return resolved
