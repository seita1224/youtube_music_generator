"""プロンプトのバージョン管理ローダ(ADR-0033 (5))。

``backend/prompts/<area>/<name>_v<N>.md`` を配置し、``<area>/<name>`` を指定して
最新版(``N`` 最大)または明示版を解決して読み込む。``planner`` の few-shot のように
JSON で持つ素材(``<name>_v<N>.json``)も同じ命名規則で解決できる。

バージョン記録(ADR-0033 (5) / data-model.md ``plans.llm_prompt_version``)::

    解決済みプロンプトは ``"planner/system_v1"`` 形式の安定 ID(:attr:`ResolvedPrompt.ref`)を
    持ち、これを ``plans.llm_prompt_version`` に記録する。後で v1 / v2 の A/B 比較に使う。

エラー分類(ADR-0028)::

    - プロンプト / バージョン欠落 → :class:`PromptNotFoundError`(``quality``)。該当部分のみ
      スキップしデフォルトで続行できる運用ミス。
    - few-shot JSON の破損 → :class:`PromptLoadError`(``fatal``)。設定ファイル破損に相当し、
      サイクル停止 + CRITICAL 通知の対象。

``area`` / ``name`` / ``version`` は境界で検証し(:func:`_safe_segment`)、パストラバーサルを防ぐ。
プロセス常駐ではなく日次サイクル起動が前提のため、キャッシュより単純さを優先しファイル I/O の
たびに読み直す(:class:`~ymg_backend.domain.templates.loader.TemplateLoader` と同方針)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ymg_backend.domain.errors import FatalError, QualityError

# ``backend/prompts`` の既定ルート。本ファイル(``.../domain/prompts/loader.py``)から
# ``backend/`` まで 4 階層遡る(``prompts`` → ``domain`` → ``ymg_backend`` → ``src`` → ``backend``)。
_DEFAULT_PROMPTS_ROOT: Final[Path] = Path(__file__).resolve().parents[4] / "prompts"

# Markdown プロンプト / JSON 素材の拡張子。
_MARKDOWN_SUFFIX: Final[str] = ".md"
_JSON_SUFFIX: Final[str] = ".json"

# ``area`` / ``name`` セグメントの許容文字(英数・ハイフン・アンダースコア)。
# パス区切り・``..``・空文字を弾き、ディレクトリトラバーサルを防ぐ。
_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+")

# ``<name>_v<N>`` のバージョン接尾辞(``N`` は 1 以上の整数)を抽出する。
_VERSION_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<name>.+)_v(?P<version>\d+)$")

# JSON のトップレベルとして許容する型(object / array いずれも few-shot で使い得る)。
# ``type`` 文は遅延評価のため右辺の自己参照(再帰)も問題なく定義できる。
type JsonValue = Mapping[str, "JsonValue"] | Sequence["JsonValue"] | str | int | float | bool | None


class PromptLoadError(FatalError):
    """プロンプト素材の破損(ADR-0028 ``fatal``)。

    few-shot JSON の構文エラーなど、設定ファイル破損に相当する不整合。サイクル停止 +
    CRITICAL 通知の対象。付随情報(``path`` 等)は基底の ``context`` に格納する。
    """


class PromptNotFoundError(QualityError):
    """要求したプロンプト / バージョンが存在しない(ADR-0028 ``quality``)。

    プロンプトファイルやバージョンの欠落。該当部分のみスキップしデフォルトで続行できる
    運用ミスとして扱う。
    """


@dataclass(frozen=True, slots=True)
class ResolvedPrompt:
    """解決済みプロンプトの不変表現。

    ``ref`` は ``"planner/system_v1"`` 形式の安定 ID で、``plans.llm_prompt_version`` に
    そのまま記録できる(ADR-0033 (5))。``text`` は Markdown 本文(UTF-8)。
    """

    area: str
    name: str
    version: int
    text: str

    @property
    def ref(self) -> str:
        """``"<area>/<name>_v<version>"`` 形式のバージョン参照 ID。"""
        return f"{self.area}/{self.name}_v{self.version}"


def _safe_segment(segment: str, *, kind: str) -> str:
    """``area`` / ``name`` セグメントを検証する。

    英数・ハイフン・アンダースコアのみ許容し、パス区切り・``..``・空文字を拒否して
    ディレクトリトラバーサルを防ぐ。

    Raises:
        PromptNotFoundError: セグメントが不正な場合。
    """
    if not _SEGMENT_RE.fullmatch(segment):
        raise PromptNotFoundError(
            f"invalid {kind}: {segment!r}",
            context={kind: segment},
        )
    return segment


def _scan_versions(directory: Path, name: str, suffix: str) -> dict[int, Path]:
    """``<name>_v<N><suffix>`` を走査し ``{N: path}`` を返す。

    ディレクトリが存在しない場合は空 dict を返す(部分セットアップを許容)。
    ``N`` が非整数 / 0 以下のファイルは無視する。
    """
    if not directory.is_dir():
        return {}
    versions: dict[int, Path] = {}
    for path in directory.glob(f"{name}_v*{suffix}"):
        if not path.is_file():
            continue
        match = _VERSION_SUFFIX_RE.fullmatch(path.name[: -len(suffix)])
        if match is None or match.group("name") != name:
            continue
        version = int(match.group("version"))
        if version >= 1:
            versions[version] = path
    return versions


class PromptLoader:
    """バージョン付きプロンプト / few-shot 素材を解決するローダ(ADR-0033 (5))。

    ``root`` を差し替えることでユニットテストから一時ディレクトリを指せる。既定は
    ``backend/prompts``。``version`` 省略時は最新版(``N`` 最大)を解決する。
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root: Final[Path] = (root or _DEFAULT_PROMPTS_ROOT).resolve()

    @property
    def root(self) -> Path:
        """プロンプトルートディレクトリ。"""
        return self._root

    def _area_dir(self, area: str) -> Path:
        """エリアのディレクトリを返す(``<root>/planner`` 等)。"""
        return self._root / _safe_segment(area, kind="area")

    def _resolve_version(
        self,
        area: str,
        name: str,
        *,
        suffix: str,
        version: int | None,
    ) -> tuple[int, Path]:
        """``<area>/<name>_v<N><suffix>`` のバージョンとパスを解決する。

        ``version`` 指定時はそのバージョンのみ、省略時は最大バージョンを選ぶ。

        Raises:
            PromptNotFoundError: バージョンが存在しない / 候補が一つもない場合。
        """
        safe_name = _safe_segment(name, kind="name")
        directory = self._area_dir(area)
        versions = _scan_versions(directory, safe_name, suffix)
        if version is not None:
            path = versions.get(version)
            if path is None:
                raise PromptNotFoundError(
                    f"prompt version not found: {area}/{safe_name}_v{version}{suffix}",
                    context={"area": area, "name": safe_name, "version": version},
                )
            return version, path
        if not versions:
            raise PromptNotFoundError(
                f"no prompt versions found: {area}/{safe_name}_v*{suffix}",
                context={"area": area, "name": safe_name, "suffix": suffix},
            )
        resolved = max(versions)
        return resolved, versions[resolved]

    def load(self, area: str, name: str, *, version: int | None = None) -> ResolvedPrompt:
        """Markdown プロンプトを解決して読み込む(ADR-0033 (5))。

        Args:
            area: プロンプト領域(``"planner"`` / ``"finisher"`` 等)。
            name: プロンプト名(``"system"`` / ``"title"`` 等、バージョン接尾辞は付けない)。
            version: 明示バージョン。``None`` なら最新版を解決する。

        Returns:
            :class:`ResolvedPrompt`(``text`` は Markdown 本文、``ref`` はバージョン参照 ID)。

        Raises:
            PromptNotFoundError: 対応するバージョンが存在しない場合。
        """
        resolved_version, path = self._resolve_version(
            area, name, suffix=_MARKDOWN_SUFFIX, version=version
        )
        return ResolvedPrompt(
            area=_safe_segment(area, kind="area"),
            name=_safe_segment(name, kind="name"),
            version=resolved_version,
            text=path.read_text(encoding="utf-8"),
        )

    def load_json(self, area: str, name: str, *, version: int | None = None) -> JsonValue:
        """few-shot 等の JSON 素材を解決して読み込む(ADR-0033 (5) / ADR-0033 few_shot)。

        トップレベルが object / array いずれの few-shot 形式でも受け付ける(検証は呼び出し側)。

        Args:
            area: プロンプト領域。
            name: 素材名(``"few_shot"`` 等)。
            version: 明示バージョン。``None`` なら最新版を解決する。

        Returns:
            パース済み JSON 値。

        Raises:
            PromptNotFoundError: 対応するバージョンが存在しない場合。
            PromptLoadError: JSON が破損している場合。
        """
        _, path = self._resolve_version(area, name, suffix=_JSON_SUFFIX, version=version)
        try:
            parsed: JsonValue = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PromptLoadError(
                f"failed to parse JSON prompt asset: {path}",
                context={"path": str(path)},
                original=exc,
            ) from exc
        return parsed

    def available_versions(
        self,
        area: str,
        name: str,
        *,
        suffix: str = _MARKDOWN_SUFFIX,
    ) -> tuple[int, ...]:
        """``<area>/<name>_v<N><suffix>`` の存在バージョンを昇順で返す。

        候補が無い場合は空タプルを返す(部分セットアップを許容)。
        """
        safe_name = _safe_segment(name, kind="name")
        versions = _scan_versions(self._area_dir(area), safe_name, suffix)
        return tuple(sorted(versions))


def load_prompt(
    area: str,
    name: str,
    *,
    version: int | None = None,
    root: Path | None = None,
) -> ResolvedPrompt:
    """既定ローダで Markdown プロンプトを解決するショートカット。

    繰り返し呼ぶ場合は :class:`PromptLoader` を生成して使い回す方が無駄が少ない。
    """
    return PromptLoader(root).load(area, name, version=version)


def load_few_shot(
    area: str,
    name: str,
    *,
    version: int | None = None,
    root: Path | None = None,
) -> JsonValue:
    """既定ローダで few-shot JSON を解決するショートカット。"""
    return PromptLoader(root).load_json(area, name, version=version)


def available_versions(
    area: str,
    name: str,
    *,
    suffix: str = _MARKDOWN_SUFFIX,
    root: Path | None = None,
) -> Iterable[int]:
    """既定ローダで利用可能なバージョンを返すショートカット。"""
    return PromptLoader(root).available_versions(area, name, suffix=suffix)
