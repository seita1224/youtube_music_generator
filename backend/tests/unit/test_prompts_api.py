"""``api/prompts.py`` の単体テスト (Polish, T135, FR-036)。

外部依存を一切起動しない真の単体テスト:

- DB を触らない read-only ルートなので fake セッション不要。
- ``PromptLoader`` は本物を使うが、 ``monkeypatch`` で ``api.prompts.PromptLoader`` を一時
  ディレクトリ root のインスタンスを返すファクトリに差し替え、 ファイル I/O をテスト固有の
  プロンプト群に閉じる (``backend/prompts`` の実体には依存しない)。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from ymg_backend.api import prompts as api_prompts
from ymg_backend.domain.prompts import PromptLoader

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio


def _seed_prompts(root: Path) -> None:
    """テスト用のプロンプト群を ``<root>/<area>/<name>_v<N>.md`` 命名で配置する。"""
    planner = root / "planner"
    finisher = root / "finisher"
    planner.mkdir(parents=True)
    finisher.mkdir(parents=True)
    (planner / "system_v1.md").write_text("planner system v1", encoding="utf-8")
    (planner / "system_v2.md").write_text("planner system v2", encoding="utf-8")
    (finisher / "title_v1.md").write_text("finisher title v1", encoding="utf-8")
    # 命名規則外: バージョン接尾辞なし / JSON は一覧から除外されること。
    (planner / "notes.md").write_text("not a versioned prompt", encoding="utf-8")
    (planner / "few_shot_v1.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def patched_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], PromptLoader]:
    """``api.prompts.PromptLoader()`` を一時 root のローダに差し替え、 ファクトリを返す。"""
    root = tmp_path / "prompts"
    root.mkdir()
    _seed_prompts(root)

    def factory() -> PromptLoader:
        return PromptLoader(root)

    monkeypatch.setattr(api_prompts, "PromptLoader", factory)
    return factory


# --- GET /prompts ------------------------------------------------------------------


async def test_list_prompts_enumerates_versions_and_latest(
    patched_loader: Callable[[], PromptLoader],
) -> None:
    """area/name ごとに version (昇順) と latest を返し、 命名規則外ファイルは除外する。"""
    del patched_loader

    result = await api_prompts.list_prompts(user="seita", area=None)

    by_key = {(item.area, item.name): item for item in result.items}
    assert set(by_key) == {("planner", "system"), ("finisher", "title")}
    assert by_key[("planner", "system")].versions == [1, 2]
    assert by_key[("planner", "system")].latest == 2
    assert by_key[("finisher", "title")].versions == [1]
    assert by_key[("finisher", "title")].latest == 1
    # ``notes.md`` (接尾辞なし) / ``few_shot_v1.json`` (JSON) は含まれない。
    assert ("planner", "notes") not in by_key
    assert ("planner", "few_shot") not in by_key


async def test_list_prompts_filters_by_area(
    patched_loader: Callable[[], PromptLoader],
) -> None:
    """``area`` 指定でそのエリアのみに絞り込む。"""
    del patched_loader

    result = await api_prompts.list_prompts(user="seita", area="finisher")

    assert {item.area for item in result.items} == {"finisher"}
    assert [item.name for item in result.items] == ["title"]


# --- GET /prompts/{prompt_name} ----------------------------------------------------


async def test_get_prompt_returns_latest_when_version_omitted(
    patched_loader: Callable[[], PromptLoader],
) -> None:
    """version 省略時は最新版 (v2) の本文と ref を返す。"""
    del patched_loader

    result = await api_prompts.get_prompt(prompt_name="planner/system", user="seita", version=None)

    assert result.name == "planner/system_v2"
    assert result.version == "2"
    assert result.content == "planner system v2"


async def test_get_prompt_returns_explicit_version(
    patched_loader: Callable[[], PromptLoader],
) -> None:
    """version 指定時はそのバージョンの本文を返す。"""
    del patched_loader

    result = await api_prompts.get_prompt(prompt_name="planner/system", user="seita", version=1)

    assert result.name == "planner/system_v1"
    assert result.version == "1"
    assert result.content == "planner system v1"


async def test_get_prompt_missing_version_is_404(
    patched_loader: Callable[[], PromptLoader],
) -> None:
    """存在しない version は 404 (PromptNotFoundError 写像)。"""
    del patched_loader

    with pytest.raises(HTTPException) as exc:
        await api_prompts.get_prompt(prompt_name="planner/system", user="seita", version=99)

    assert exc.value.status_code == 404


async def test_get_prompt_unknown_name_is_404(
    patched_loader: Callable[[], PromptLoader],
) -> None:
    """存在しない area/name は 404。"""
    del patched_loader

    with pytest.raises(HTTPException) as exc:
        await api_prompts.get_prompt(prompt_name="planner/nonexistent", user="seita", version=None)

    assert exc.value.status_code == 404


async def test_get_prompt_malformed_name_is_400(
    patched_loader: Callable[[], PromptLoader],
) -> None:
    """``area/name`` の 2 セグメントでない prompt_name は 400。"""
    del patched_loader

    with pytest.raises(HTTPException) as exc:
        await api_prompts.get_prompt(prompt_name="planner", user="seita", version=None)

    assert exc.value.status_code == 400
