"""``/prompts`` 管理エンドポイント (Polish, T135, FR-036, contracts/backend-api.yaml ``/prompts`` 系)。

プロンプトのバージョン一覧表示と本文プレビューを担う read-only な 2 route を提供する (いずれも
Basic 認証配下。 配線は ``main.py:_build_protected_router`` が ``include_router(router)`` する
後段の責務。 子ルータ側には認証依存を再付与しない — 共有契約 (a)):

- ``GET /prompts?area=`` — ``backend/prompts/<area>/<name>_v<N>.md`` を走査し、 area/name ごとに
  利用可能な version 一覧を返す (:class:`PromptSummary` のリスト)。 ``area`` 指定で絞り込める。
- ``GET /prompts/{prompt_name}?version=`` — ``prompt_name`` は ``"<area>/<name>"`` (バージョン
  接尾辞なし、 例 ``planner/system``)。 指定 version (省略時は最新) の本文を返す
  (:class:`PromptVersion`)。 欠落は 404 (FR-036)。

設計方針:

- 本モジュールは :class:`~ymg_backend.domain.prompts.PromptLoader` を再利用し、 ファイル走査 /
  バージョン解決 / 本文読込のロジックを重複定義しない。 手本は read 系の ``api/llm.py``。
- ``PromptLoader`` に「全 area/name 列挙」API は無いため、 一覧はハンドラ側で ``loader.root``
  配下を glob (``<area>/<name>_v<N>.md``) して area/name 集合を作り、 各々に
  ``loader.available_versions(area, name)`` を呼ぶ。
- ``GET /prompts/{prompt_name}`` は ``prompt_name.split("/")`` で area/name に分解して
  ``loader.load(area, name, version=version)`` を呼ぶ。 ``prompt_name`` はパス区切りを含むため
  FastAPI のパスコンバータ (``{prompt_name:path}``) で受ける。
- 本ルートは read-only で DB を触らないため ``session`` / ``commit`` を持たない (書込 / 編集は
  MVP 範囲外。 read + preview + version 切替のみ)。
- 例外写像 (ADR-0028 のエラー 5 区分): ``PromptNotFoundError`` (``quality``) → 404。 不正な
  ``prompt_name`` 形式 → 400 (パストラバーサル等はローダ側でも弾かれるが、 区切り個数の検証は
  本モジュールで先に行う)。
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from ymg_backend.core.security import BasicAuthUser
from ymg_backend.domain.prompts import PromptLoader, PromptNotFoundError

router: Final = APIRouter(prefix="/prompts", tags=["prompts"])

# Markdown プロンプトの拡張子 (loader._MARKDOWN_SUFFIX と一致させる。 一覧 glob 用)。
_MARKDOWN_SUFFIX: Final[str] = ".md"

# ``<name>_v<N>`` のバージョン接尾辞を区切るマーカー (一覧 glob で stem を name に戻す)。
_VERSION_MARKER: Final[str] = "_v"

# ``prompt_name`` (``"<area>/<name>"``) を分解したときに期待するセグメント数。
_PROMPT_NAME_SEGMENTS: Final[int] = 2


class PromptSummary(BaseModel):
    """``GET /prompts`` の 1 要素 (backend-api.yaml PromptSummary)。

    ``area`` / ``name`` ごとに利用可能な version 一覧 (昇順) と最新版 (``latest``) を持つ。
    """

    area: str
    name: str
    versions: list[int]
    latest: int | None = None


class PromptListResponse(BaseModel):
    """``GET /prompts`` のレスポンス (PromptSummary のリスト)。"""

    items: list[PromptSummary]


class PromptVersion(BaseModel):
    """``GET /prompts/{prompt_name}`` のレスポンス (backend-api.yaml PromptVersion)。

    ``name`` は :attr:`~ymg_backend.domain.prompts.ResolvedPrompt.ref` (``"planner/system_v1"``
    形式の安定 ID)、 ``content`` は Markdown 本文、 ``version`` は解決済みバージョンの文字列表現。
    """

    name: str
    version: str
    content: str
    updated_at: str | None = None


def _iter_area_name_pairs(loader: PromptLoader, area: str | None) -> list[tuple[str, str]]:
    """``loader.root`` 配下を glob し ``(area, name)`` の組を重複なし・安定順で返す。

    ``<area>/<name>_v<N>.md`` のファイル名から area (親ディレクトリ名) と name (バージョン
    接尾辞 ``_v<N>`` を除いた stem) を復元する。 ``area`` 指定時はそのエリアのみに絞り込む。
    バージョン接尾辞を持たない ``.md`` は対象外 (``_v`` を含まない stem はスキップ)。
    """
    pairs: set[tuple[str, str]] = set()
    pattern = f"{area}/*{_MARKDOWN_SUFFIX}" if area is not None else f"*/*{_MARKDOWN_SUFFIX}"
    for path in loader.root.glob(pattern):
        if not path.is_file():
            continue
        stem = path.name[: -len(_MARKDOWN_SUFFIX)]
        marker = stem.rfind(_VERSION_MARKER)
        if marker <= 0:
            continue  # ``_v`` を含まない / 先頭が ``_v`` のファイルは命名規則外。
        suffix = stem[marker + len(_VERSION_MARKER) :]
        if not suffix.isdigit():
            continue  # ``_v`` の後ろが数値でない (例 ``foo_version.md``) はスキップ。
        name = stem[:marker]
        pairs.add((path.parent.name, name))
    return sorted(pairs)


def _split_prompt_name(prompt_name: str) -> tuple[str, str]:
    """``"<area>/<name>"`` を ``(area, name)`` に分解する。

    Raises:
        HTTPException: 区切りが ``area/name`` の 2 セグメントでない場合 (400)。
    """
    segments = prompt_name.split("/")
    if len(segments) != _PROMPT_NAME_SEGMENTS or not all(segments):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"prompt_name は '<area>/<name>' 形式である必要があります (指定: {prompt_name!r})。"
            ),
        )
    return segments[0], segments[1]


@router.get(
    "",
    response_model=PromptListResponse,
    summary="List prompts and their available versions",
)
async def list_prompts(
    user: BasicAuthUser,
    area: Annotated[str | None, Query(examples=["planner"])] = None,
) -> PromptListResponse:
    """プロンプトの area/name と利用可能 version 一覧を返す (FR-036)。

    ``backend/prompts/<area>/<name>_v<N>.md`` を走査し、 area/name ごとに version (昇順) と
    最新版を列挙する。 ``area`` 指定でそのエリアのみに絞り込む。 候補が無い area/name は
    結果に含めない (部分セットアップを許容)。

    Args:
        user: Basic 認証済みユーザー名 (認証のみ目的)。
        area: 絞り込み対象エリア (``"planner"`` 等)。 省略時は全エリア。

    Returns:
        :class:`PromptListResponse` (area/name 昇順の :class:`PromptSummary` リスト)。
    """
    del user  # 認証のみ目的。
    loader = PromptLoader()
    items: list[PromptSummary] = []
    for pair_area, name in _iter_area_name_pairs(loader, area):
        versions = list(loader.available_versions(pair_area, name))
        if not versions:
            continue
        items.append(
            PromptSummary(
                area=pair_area,
                name=name,
                versions=versions,
                latest=max(versions),
            )
        )
    return PromptListResponse(items=items)


@router.get(
    "/{prompt_name:path}",
    response_model=PromptVersion,
    summary="Preview a prompt body (latest or explicit version)",
    responses={
        400: {"description": "prompt_name is not in '<area>/<name>' form."},
        404: {"description": "Prompt or version not found."},
    },
)
async def get_prompt(
    prompt_name: str,
    user: BasicAuthUser,
    version: Annotated[int | None, Query(ge=1, examples=[1])] = None,
) -> PromptVersion:
    """指定プロンプトの本文をプレビュー用に返す (FR-036)。

    ``prompt_name`` (``"<area>/<name>"``) を area/name に分解し、 ``version`` 省略時は最新版を
    解決して Markdown 本文を返す。 編集 / 書込は MVP 範囲外 (read + preview のみ)。

    Args:
        prompt_name: ``"<area>/<name>"`` (バージョン接尾辞なし、 例 ``"planner/system"``)。
        user: Basic 認証済みユーザー名 (認証のみ目的)。
        version: 明示バージョン (1 以上)。 省略時は最新版を解決する。

    Returns:
        :class:`PromptVersion` (``name`` は解決済み ref、 ``content`` は本文、 ``version`` は文字列)。

    Raises:
        HTTPException: ``prompt_name`` 形式不正は 400、 プロンプト / バージョン欠落は 404。
    """
    del user  # 認証のみ目的。
    area, name = _split_prompt_name(prompt_name)
    loader = PromptLoader()
    try:
        resolved = loader.load(area, name, version=version)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return PromptVersion(
        name=resolved.ref,
        version=str(resolved.version),
        content=resolved.text,
    )


__all__ = [
    "PromptListResponse",
    "PromptSummary",
    "PromptVersion",
    "router",
]
