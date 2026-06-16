"""compose_thumbnail (domain/render/thumbnail_overlay.py) の単体テスト。

契約 (共有契約書 US1 thumbnail_overlay)::

    compose_thumbnail(*, base_image_uri, title_text, genre, template, storage, output_uri) -> str

外部依存は ``StorageAdapter`` のみで、 本テストではメモリ上の fake で代替する
(実ファイル I/O / GPU / ネットワークは打たない)。 Pillow の描画は実 PIL で実行する
(純 CPU 処理で副作用が無いため mock しない)。

検証ポイント:

- 戻り値が ``storage.resolve_uri(output_uri)`` と一致する。
- 書き出されたバイト列が有効な JPEG で、 1280x720・2MB 以内に収まる。
- 背景読込失敗 / デコード失敗が ``QualityError`` (category=quality) になる。
- フォント未配置でも例外を出さずフォールバックして合成が完了する。
"""

from __future__ import annotations

import io
from pathlib import Path
from types import MappingProxyType

import pytest
from PIL import Image

from ymg_backend.domain.errors import ErrorCategory, QualityError
from ymg_backend.domain.render import thumbnail_overlay
from ymg_backend.domain.render.thumbnail_overlay import compose_thumbnail
from ymg_backend.domain.templates.loader import (
    GenreTemplate,
    TemplateCategory,
    TemplateLoader,
)


class _FakeStorage:
    """メモリ上で read/write/resolve を満たす ``StorageAdapter`` 互換 fake。"""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._files: dict[str, bytes] = dict(files or {})
        self.written: dict[str, bytes] = {}

    def resolve_uri(self, path_or_uri: str) -> str:
        return path_or_uri

    def read_bytes(self, path_or_uri: str) -> bytes:
        return self._files[path_or_uri]

    def write_bytes(self, path_or_uri: str, data: bytes) -> None:
        self.written[path_or_uri] = data


def _png_bytes(width: int = 1280, height: int = 720, color: str = "#224466") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _template(genre: str = "lo-fi hip-hop") -> GenreTemplate:
    return GenreTemplate(
        category=TemplateCategory.THUMBNAIL,
        genre=genre,
        data=MappingProxyType(
            {
                "font_primary": "Bebas Neue",
                "font_secondary": "Noto Sans JP",
                "color_text": "#FFFFFF",
                "color_outline": "#000000",
                "outline_width": 4,
                "text_position": "center-bottom",
                "genre_badge_color": "#1A1A1A",
            }
        ),
    )


_BASE_URI = "file:///srv/ymg/outputs/image/post-1/base.png"
_OUT_URI = "file:///srv/ymg/outputs/thumbnail/post-1/thumb.jpg"


def test_compose_returns_resolved_uri_and_writes_jpeg() -> None:
    storage = _FakeStorage({_BASE_URI: _png_bytes()})
    result = compose_thumbnail(
        base_image_uri=_BASE_URI,
        title_text="夜のローファイ作業用BGM",
        genre="lo-fi hip-hop",
        template=_template(),
        storage=storage,  # type: ignore[arg-type]
        output_uri=_OUT_URI,
    )
    assert result == _OUT_URI
    assert _OUT_URI in storage.written
    data = storage.written[_OUT_URI]
    # 有効な JPEG であること。
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "JPEG"
        assert img.size == (1280, 720)


def test_output_is_within_2mb() -> None:
    storage = _FakeStorage({_BASE_URI: _png_bytes()})
    compose_thumbnail(
        base_image_uri=_BASE_URI,
        title_text="と て も 長 い 日 本 語 の サ ブ タ イ ト ル " * 3,
        genre="synthwave",
        template=_template("synthwave"),
        storage=storage,  # type: ignore[arg-type]
        output_uri=_OUT_URI,
    )
    assert len(storage.written[_OUT_URI]) <= 2 * 1024 * 1024


def test_non_1280x720_base_is_resized() -> None:
    storage = _FakeStorage({_BASE_URI: _png_bytes(width=1024, height=576)})
    compose_thumbnail(
        base_image_uri=_BASE_URI,
        title_text="リサイズ確認",
        genre="ambient",
        template=_template("ambient"),
        storage=storage,  # type: ignore[arg-type]
        output_uri=_OUT_URI,
    )
    with Image.open(io.BytesIO(storage.written[_OUT_URI])) as img:
        assert img.size == (1280, 720)


def test_missing_base_image_raises_quality_error() -> None:
    storage = _FakeStorage({})  # base URI を意図的に欠落させる
    with pytest.raises(QualityError) as excinfo:
        compose_thumbnail(
            base_image_uri=_BASE_URI,
            title_text="読込失敗",
            genre="chillhop",
            template=_template("chillhop"),
            storage=storage,  # type: ignore[arg-type]
            output_uri=_OUT_URI,
        )
    assert excinfo.value.category is ErrorCategory.QUALITY


def test_corrupt_base_image_raises_quality_error() -> None:
    storage = _FakeStorage({_BASE_URI: b"not-an-image"})
    with pytest.raises(QualityError) as excinfo:
        compose_thumbnail(
            base_image_uri=_BASE_URI,
            title_text="デコード失敗",
            genre="piano solo",
            template=_template("piano solo"),
            storage=storage,  # type: ignore[arg-type]
            output_uri=_OUT_URI,
        )
    assert excinfo.value.category is ErrorCategory.QUALITY


def test_unbundled_font_falls_back_without_error() -> None:
    """フォント未配置 (実環境はバイナリ未同梱) でも例外を出さず合成できる。"""
    storage = _FakeStorage({_BASE_URI: _png_bytes()})
    # font_primary に未知名を渡し、 候補ファイルが無い経路を踏ませる。
    template = GenreTemplate(
        category=TemplateCategory.THUMBNAIL,
        genre="future garage",
        data=MappingProxyType(
            {
                "font_primary": "Nonexistent Font 999",
                "font_secondary": "Also Missing Font",
                "color_text": "#A0D8EF",
                "color_outline": "#0A1929",
                "outline_width": 4,
                "genre_badge_color": "#1A1A1A",
            }
        ),
    )
    result = compose_thumbnail(
        base_image_uri=_BASE_URI,
        title_text="フォントフォールバック",
        genre="future garage",
        template=template,
        storage=storage,  # type: ignore[arg-type]
        output_uri=_OUT_URI,
    )
    assert result == _OUT_URI
    assert _OUT_URI in storage.written


def test_missing_shared_assets_fall_back_to_builtin_geometry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_shared/layout.json`` / ``badge.json`` 欠落でも内蔵既定値で合成できる。

    既定 ``TemplateLoader`` の root を空ディレクトリへ差し替え、 共有アセット欠落経路
    (``TemplateNotFoundError`` → フォールバック) を踏ませる。
    """
    empty_loader = TemplateLoader(tmp_path)
    monkeypatch.setattr(thumbnail_overlay, "TemplateLoader", lambda *a, **k: empty_loader)
    storage = _FakeStorage({_BASE_URI: _png_bytes()})
    result = compose_thumbnail(
        base_image_uri=_BASE_URI,
        title_text="共有アセット欠落フォールバック",
        genre="lo-fi hip-hop",
        template=_template(),
        storage=storage,  # type: ignore[arg-type]
        output_uri=_OUT_URI,
    )
    assert result == _OUT_URI
    with Image.open(io.BytesIO(storage.written[_OUT_URI])) as img:
        assert img.size == (1280, 720)


def test_long_title_wraps_and_truncates_to_max_lines() -> None:
    """長い単一トークンの見出しが折り返し + 省略され、 例外なく合成できる。"""
    storage = _FakeStorage({_BASE_URI: _png_bytes()})
    # 空白の無い長い文字列 = 文字単位の強制分割 + max_lines 省略経路を踏む。
    result = compose_thumbnail(
        base_image_uri=_BASE_URI,
        title_text="あ" * 120,
        genre="lo-fi hip-hop",
        template=_template(),
        storage=storage,  # type: ignore[arg-type]
        output_uri=_OUT_URI,
    )
    assert result == _OUT_URI
    assert _OUT_URI in storage.written
