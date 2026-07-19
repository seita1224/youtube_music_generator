"""タイトル / 説明文 / サムネのテンプレローダ(ADR-0034)。

``backend/templates/{title,description,thumbnail}/*.yaml`` を YAML パースし、
ジャンル名からジャンル別テンプレを解決する。ジャンル共通の素材は ``_shared/`` 配下に置かれ
(``description/_shared/ai_disclosure.txt`` 等、ADR-0034 (5))、本ローダから読み出せる。

ジャンル名の正規化(ADR-0034 (5) / data-model.md ``genres`` 節)::

    DB の ``genres.name`` は空白入りの表記("lo-fi hip-hop" 等)だが、テンプレファイルは
    ハイフン区切りの slug("lo-fi-hip-hop.yaml")で配置される。本ローダは小文字化 + 空白 →
    ハイフン化でこの差を吸収する。``_shared`` は予約名でジャンルとして解決できない。

エラー分類(ADR-0028)::

    - テンプレファイル欠落 → :class:`TemplateNotFoundError`(``quality``)。該当部分のみ
      スキップしデフォルト値で続行できる運用ミス。
    - YAML 破損 / 期待した構造でない → :class:`TemplateLoadError`(``fatal``)。
      設定ファイル破損に相当し、サイクル停止 + CRITICAL 通知の対象。

ファイル I/O は境界で検証し(:func:`_safe_slug` / :func:`_load_yaml_mapping`)、解析結果は
:class:`MappingProxyType` で不変化して呼び出し側の偶発的な変更を防ぐ(不変性方針)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from ymg_backend.domain.errors import FatalError, QualityError

# ``backend/templates`` の既定ルート。本ファイル(``.../domain/templates/loader.py``)から
# ``backend/`` まで 4 階層遡る(``templates`` → ``domain`` → ``ymg_backend`` → ``src`` → ``backend``)。
_DEFAULT_TEMPLATES_ROOT: Final[Path] = Path(__file__).resolve().parents[4] / "templates"

# ジャンルとして解決できない予約ディレクトリ名(ADR-0034 (5))。
_SHARED_DIR_NAME: Final[str] = "_shared"

# YAML テンプレの拡張子。
_YAML_SUFFIX: Final[str] = ".yaml"


class TemplateLoadError(FatalError):
    """テンプレ定義の破損(ADR-0028 ``fatal``)。

    YAML 構文エラー・期待したマッピング構造でない・JSON 破損など、設定ファイル破損に
    相当する不整合。サイクル停止 + CRITICAL 通知の対象。付随情報(``path`` 等)は基底の
    ``context`` に格納する。
    """


class TemplateNotFoundError(QualityError):
    """要求したテンプレ / 共通素材が存在しない(ADR-0028 ``quality``)。

    ジャンル別テンプレや ``_shared`` 素材の欠落。該当部分のみスキップしデフォルト値で
    続行できる運用ミスとして扱う。
    """


class TemplateCategory(StrEnum):
    """テンプレのカテゴリ(配置ディレクトリ名と一致、ADR-0034 (5))。"""

    TITLE = "title"
    DESCRIPTION = "description"
    THUMBNAIL = "thumbnail"


@dataclass(frozen=True, slots=True)
class GenreTemplate:
    """ジャンル別テンプレの不変表現。

    ``data`` は YAML をパースしたマッピングを不変化したもの(``MappingProxyType``)。
    title なら ``template`` / ``example``、thumbnail なら ``font_primary`` / ``color_text`` 等が入る
    (ADR-0034 (1)(3))。スキーマはカテゴリ依存のため検証は呼び出し側に委ねる。
    """

    category: TemplateCategory
    genre: str
    data: Mapping[str, Any]


def _safe_slug(genre: str) -> str:
    """ジャンル名をテンプレファイルの slug に正規化する(ADR-0034 (5))。

    小文字化 + 連続空白のハイフン化で DB 表記("lo-fi hip-hop")とファイル名
    ("lo-fi-hip-hop")の差を吸収する。空文字・``_shared`` 等の予約名・パス区切りを含む
    入力は :class:`TemplateNotFoundError` で拒否し、ディレクトリトラバーサルを防ぐ。
    """
    normalized = "-".join(genre.strip().lower().split())
    if not normalized:
        raise TemplateNotFoundError(
            "genre must not be empty",
            context={"genre": genre},
        )
    if normalized == _SHARED_DIR_NAME or "/" in normalized or "\\" in normalized:
        raise TemplateNotFoundError(
            f"invalid genre slug: {normalized!r}",
            context={"genre": genre, "slug": normalized},
        )
    return normalized


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    """YAML ファイルを安全に読み込み、トップレベルがマッピングであることを検証する。

    Raises:
        TemplateLoadError: 構文エラー / 非マッピング(リスト・スカラ)の場合。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TemplateLoadError(
            f"failed to parse YAML template: {path}",
            context={"path": str(path)},
            original=exc,
        ) from exc
    if raw is None:
        raise TemplateLoadError(
            f"empty YAML template: {path}",
            context={"path": str(path)},
        )
    if not isinstance(raw, dict):
        raise TemplateLoadError(
            f"YAML template must be a mapping, got {type(raw).__name__}: {path}",
            context={"path": str(path), "type": type(raw).__name__},
        )
    return MappingProxyType(dict(raw))


def _load_json_mapping(path: Path) -> Mapping[str, Any]:
    """JSON ファイルを読み込み、トップレベルがマッピングであることを検証する。

    Raises:
        TemplateLoadError: 構文エラー / 非マッピングの場合。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateLoadError(
            f"failed to parse JSON shared asset: {path}",
            context={"path": str(path)},
            original=exc,
        ) from exc
    if not isinstance(raw, dict):
        raise TemplateLoadError(
            f"JSON shared asset must be an object, got {type(raw).__name__}: {path}",
            context={"path": str(path), "type": type(raw).__name__},
        )
    return MappingProxyType(dict(raw))


class TemplateLoader:
    """ジャンル別テンプレと共通素材を解決するローダ(ADR-0034)。

    ``root`` を差し替えることでユニットテストから一時ディレクトリを指せる。既定は
    ``backend/templates``。解析結果はファイル I/O のたびに読み直す(プロセス常駐の常時稼働
    ではなく日次サイクル起動が前提のため、キャッシュより単純さを優先)。
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root: Final[Path] = (root or _DEFAULT_TEMPLATES_ROOT).resolve()

    @property
    def root(self) -> Path:
        """テンプレルートディレクトリ。"""
        return self._root

    def _category_dir(self, category: TemplateCategory) -> Path:
        """カテゴリのテンプレディレクトリを返す(``<root>/title`` 等)。"""
        return self._root / category.value

    def load_genre_template(self, category: TemplateCategory, genre: str) -> GenreTemplate:
        """ジャンル別テンプレを解決して読み込む(ADR-0034)。

        Args:
            category: テンプレカテゴリ(title / description / thumbnail)。
            genre: ジャンル名(DB 表記 "lo-fi hip-hop" 等)。内部で slug 化する。

        Returns:
            :class:`GenreTemplate`(``data`` は不変マッピング)。

        Raises:
            TemplateNotFoundError: 対応する ``<slug>.yaml`` が存在しない場合。
            TemplateLoadError: YAML が破損 / 非マッピングの場合。
        """
        slug = _safe_slug(genre)
        path = self._category_dir(category) / f"{slug}{_YAML_SUFFIX}"
        if not path.is_file():
            raise TemplateNotFoundError(
                f"template not found for genre {genre!r} in category {category.value!r}",
                context={"category": category.value, "genre": genre, "slug": slug},
            )
        data = _load_yaml_mapping(path)
        return GenreTemplate(category=category, genre=genre, data=data)

    def available_genres(self, category: TemplateCategory) -> tuple[str, ...]:
        """カテゴリ配下のジャンル slug(``_shared`` を除く)を昇順で返す。

        ディレクトリが存在しない場合は空タプルを返す(部分セットアップを許容)。
        """
        directory = self._category_dir(category)
        if not directory.is_dir():
            return ()
        slugs = sorted(
            p.stem
            for p in directory.glob(f"*{_YAML_SUFFIX}")
            if p.is_file() and p.stem != _SHARED_DIR_NAME
        )
        return tuple(slugs)

    def _shared_dir(self, category: TemplateCategory) -> Path:
        """カテゴリの ``_shared`` ディレクトリを返す。"""
        return self._category_dir(category) / _SHARED_DIR_NAME

    def load_shared_text(self, category: TemplateCategory, name: str) -> str:
        """``_shared`` 配下のテキスト素材を読み込む(ADR-0034 (2))。

        ``ai_disclosure.txt`` / ``channel_promo.txt`` 等の固定文。

        Args:
            category: テンプレカテゴリ。
            name: ファイル名(拡張子込み、例 ``"ai_disclosure.txt"``)。

        Returns:
            ファイル内容(UTF-8)。

        Raises:
            TemplateNotFoundError: 素材が存在しない場合。
        """
        path = self._resolve_shared_path(category, name)
        return path.read_text(encoding="utf-8")

    def load_shared_json(self, category: TemplateCategory, name: str) -> Mapping[str, Any]:
        """``_shared`` 配下の JSON 素材を読み込む(ADR-0034 (3))。

        ``layout.json`` / ``badge.json`` 等の共通レイアウト仕様。

        Args:
            category: テンプレカテゴリ。
            name: ファイル名(拡張子込み、例 ``"layout.json"``)。

        Returns:
            不変マッピング。

        Raises:
            TemplateNotFoundError: 素材が存在しない場合。
            TemplateLoadError: JSON が破損 / 非マッピングの場合。
        """
        path = self._resolve_shared_path(category, name)
        return _load_json_mapping(path)

    def _resolve_shared_path(self, category: TemplateCategory, name: str) -> Path:
        """``_shared`` 配下の素材パスを検証して返す。

        パス区切り・``..`` を含む名前はトラバーサル防止のため拒否する。

        Raises:
            TemplateNotFoundError: 名前が不正 / 素材が存在しない場合。
        """
        if not name or "/" in name or "\\" in name or name == ".." or name.startswith("."):
            raise TemplateNotFoundError(
                f"invalid shared asset name: {name!r}",
                context={"category": category.value, "name": name},
            )
        path = self._shared_dir(category) / name
        if not path.is_file():
            raise TemplateNotFoundError(
                f"shared asset not found: {name!r} in category {category.value!r}",
                context={"category": category.value, "name": name},
            )
        return path


def load_genre_template(
    category: TemplateCategory,
    genre: str,
    *,
    root: Path | None = None,
) -> GenreTemplate:
    """既定ローダでジャンル別テンプレを解決するショートカット。

    繰り返し呼ぶ場合は :class:`TemplateLoader` を生成して使い回す方が無駄が少ない。
    """
    return TemplateLoader(root).load_genre_template(category, genre)


def available_genres(
    category: TemplateCategory,
    *,
    root: Path | None = None,
) -> Iterable[str]:
    """既定ローダで利用可能なジャンル slug を返すショートカット。"""
    return TemplateLoader(root).available_genres(category)
