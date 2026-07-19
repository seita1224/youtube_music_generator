"""TemplateLoader (domain/templates/loader.py) のセキュリティ境界テスト (FR-050)。

FR-050: ジャンルごとのタイトル / 説明文 / サムネテンプレを ``templates/`` に保有する
(ADR-0034)。

本テストはローダのファイル I/O 境界を検証する:

- (a) 実在ジャンル / カテゴリの ``load_genre_template`` 成功 (実テンプレを読む)。
- (b) パストラバーサル (``../`` / 区切り文字 / ``_shared`` 予約名) が拒否される
  (``_safe_slug`` / ``_resolve_shared_path``)。
- (c) 壊れた / 欠落 / 空 / 非マッピング YAML が ``TemplateLoadError`` (fatal) になる。
- (d) ``available_genres`` が 6 ジャンルを返す (title / thumbnail カテゴリ)。

破損テンプレは ``tmp_path`` に書き出した一時ディレクトリをルートに差し替えて読ませる。
実テンプレ (b 一部 / a / d) は既定ルート ``backend/templates`` をそのまま使う。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ymg_backend.domain.templates.loader import (
    TemplateCategory,
    TemplateLoader,
    TemplateLoadError,
    TemplateNotFoundError,
)

# 既定の ``backend/templates`` に同梱済みの 6 ジャンル slug (title / thumbnail カテゴリ)。
_EXPECTED_GENRES: tuple[str, ...] = (
    "ambient",
    "chillhop",
    "future-garage",
    "lo-fi-hip-hop",
    "piano-solo",
    "synthwave",
)


# --- (a) 実在テンプレの読み込み成功 ------------------------------------------------


@pytest.mark.fr("FR-050")
def test_load_genre_template_reads_real_default_description() -> None:
    """FR-050: 実在の description/default.yaml を解決して読み込める。"""
    loader = TemplateLoader()
    template = loader.load_genre_template(TemplateCategory.DESCRIPTION, "default")
    assert template.category is TemplateCategory.DESCRIPTION
    assert template.genre == "default"
    # default.yaml の ``body`` キーが不変マッピングとして読めている。
    assert "body" in template.data


@pytest.mark.fr("FR-050")
def test_load_genre_template_normalizes_db_genre_to_slug() -> None:
    """FR-050: DB 表記 'Lo-Fi Hip-Hop' を slug 'lo-fi-hip-hop' に正規化して読む。"""
    loader = TemplateLoader()
    # 空白 / 大文字混じりの DB 表記でも実テンプレに解決される (_safe_slug)。
    template = loader.load_genre_template(TemplateCategory.TITLE, "Lo-Fi Hip-Hop")
    assert template.genre == "Lo-Fi Hip-Hop"
    assert "template" in template.data


# --- (b) パストラバーサル / 予約名の拒否 -------------------------------------------


@pytest.mark.fr("FR-050")
@pytest.mark.parametrize(
    "malicious",
    [
        "../secrets",
        "../../etc/passwd",
        "foo/bar",
        "foo\\bar",
        "_shared",
    ],
)
def test_load_genre_template_rejects_traversal_slug(malicious: str) -> None:
    """FR-050: 区切り文字・``..``・予約名 ``_shared`` を含む genre は拒否される。"""
    loader = TemplateLoader()
    with pytest.raises(TemplateNotFoundError):
        loader.load_genre_template(TemplateCategory.TITLE, malicious)


@pytest.mark.fr("FR-050")
def test_load_genre_template_rejects_empty_genre() -> None:
    """FR-050: 空文字 / 空白のみの genre は拒否される。"""
    loader = TemplateLoader()
    with pytest.raises(TemplateNotFoundError):
        loader.load_genre_template(TemplateCategory.TITLE, "   ")


@pytest.mark.fr("FR-050")
@pytest.mark.parametrize(
    "malicious",
    [
        "../ai_disclosure.txt",
        "sub/ai_disclosure.txt",
        "sub\\ai_disclosure.txt",
        "..",
        ".hidden",
    ],
)
def test_load_shared_text_rejects_traversal_name(malicious: str) -> None:
    """FR-050: 共有素材名のパストラバーサル (``_resolve_shared_path``) を拒否する。"""
    loader = TemplateLoader()
    with pytest.raises(TemplateNotFoundError):
        loader.load_shared_text(TemplateCategory.DESCRIPTION, malicious)


# --- (c) 破損 / 欠落 / 非マッピング YAML は TemplateLoadError (fatal) ----------------


def _make_template_root(tmp_path: Path, category: str, slug: str, content: str) -> TemplateLoader:
    """``tmp_path`` 配下に ``<category>/<slug>.yaml`` を書き、それをルートにしたローダを返す。"""
    category_dir = tmp_path / category
    category_dir.mkdir(parents=True, exist_ok=True)
    (category_dir / f"{slug}.yaml").write_text(content, encoding="utf-8")
    return TemplateLoader(tmp_path)


@pytest.mark.fr("FR-050")
def test_load_genre_template_broken_yaml_raises_load_error(tmp_path: Path) -> None:
    """FR-050: YAML 構文エラーは fatal な TemplateLoadError になる。"""
    loader = _make_template_root(
        tmp_path, "title", "broken", "title: [unclosed\n  bad: : :"
    )
    with pytest.raises(TemplateLoadError):
        loader.load_genre_template(TemplateCategory.TITLE, "broken")


@pytest.mark.fr("FR-050")
def test_load_genre_template_non_mapping_yaml_raises_load_error(tmp_path: Path) -> None:
    """FR-050: トップレベルがリスト / スカラの YAML は TemplateLoadError になる。"""
    loader = _make_template_root(tmp_path, "title", "listy", "- a\n- b\n")
    with pytest.raises(TemplateLoadError):
        loader.load_genre_template(TemplateCategory.TITLE, "listy")


@pytest.mark.fr("FR-050")
def test_load_genre_template_empty_yaml_raises_load_error(tmp_path: Path) -> None:
    """FR-050: 空ファイル (None パース) も TemplateLoadError になる。"""
    loader = _make_template_root(tmp_path, "title", "empty", "")
    with pytest.raises(TemplateLoadError):
        loader.load_genre_template(TemplateCategory.TITLE, "empty")


@pytest.mark.fr("FR-050")
def test_load_genre_template_missing_file_raises_not_found(tmp_path: Path) -> None:
    """FR-050: テンプレファイル欠落は quality 系 TemplateNotFoundError になる。"""
    loader = TemplateLoader(tmp_path)  # 空ルート: どのジャンルも未配置
    with pytest.raises(TemplateNotFoundError):
        loader.load_genre_template(TemplateCategory.TITLE, "ambient")


# --- (d) available_genres が 6 ジャンルを返す --------------------------------------


@pytest.mark.fr("FR-050")
@pytest.mark.parametrize("category", [TemplateCategory.TITLE, TemplateCategory.THUMBNAIL])
def test_available_genres_returns_six_genres(category: TemplateCategory) -> None:
    """FR-050: title / thumbnail カテゴリは同梱の 6 ジャンル slug を返す (_shared 除外)。"""
    loader = TemplateLoader()
    genres = loader.available_genres(category)
    assert genres == _EXPECTED_GENRES
    assert len(genres) == 6
    # 予約名 ``_shared`` はジャンルとして含まれない。
    assert "_shared" not in genres
