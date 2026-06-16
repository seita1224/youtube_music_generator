"""MVP 判定の件数クエリが項目ごとに正しいテーブル/列を狙うかの単体テスト (T127)。

``test_mvp_checklist.py`` は green/red 写像・しきい値・形状を網羅する。 本テストはそれと
重複しない 2 点に絞って実装契約を固定する:

1. 各項目が流す ``select(func.count())...`` 文が、 想定どおり「対象 1 テーブルのみ」を
   FROM に取り、 期待する判定列 (``state`` / ``acoustid_status`` 等) を WHERE 条件に含む。
   = ある項目のクエリが別項目のテーブルを巻き込んでいないこと (取り違え検出)。
2. :func:`mvp_check_ids` が contract 固定順序の 6 id を返すこと (API 層が依存する公開順序)。

実 DB は起動せず、 ``execute`` 文を compile して SQL 文字列を introspect する in-memory
セッションで判定関数の発行クエリのみを観測する。
"""

from __future__ import annotations

from typing import Any

import pytest

from ymg_backend.domain.mvp_check.checklist import (
    evaluate_mvp_checklist,
    mvp_check_ids,
)

pytestmark = pytest.mark.unit

# id → (FROM に出るべきテーブル, WHERE 等に出るべき判定列。 列が無い項目は None)。
_EXPECTED_TABLE: dict[str, str] = {
    "dryrun_success": "dryrun_outputs",
    "acoustid_clear": "audio_tracks",
    "unlisted_post": "videos",
    "panic_stop": "audit_log",
    "oauth_refresh": "oauth_credentials",
    "slack_categories": "usage_log",
}
_EXPECTED_COLUMN: dict[str, str | None] = {
    "dryrun_success": "state",
    "acoustid_clear": "acoustid_status",
    "unlisted_post": "privacy_status",
    "panic_stop": "action",
    "oauth_refresh": "updated_at",
    "slack_categories": None,
}
# 各項目の SQL に「出てはいけない」他テーブル (取り違え検出)。
_ALL_TABLES: tuple[str, ...] = tuple(_EXPECTED_TABLE.values())


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _RecordingSession:
    """``execute`` した件数クエリを compile して SQL 文字列を順に記録する。"""

    def __init__(self) -> None:
        self.executed_sql: list[str] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        self.executed_sql.append(str(statement.compile()).lower())
        return _ScalarResult(0)


async def test_each_item_query_targets_only_its_table() -> None:
    """項目 i のクエリは対応テーブルのみを FROM に取り、 他 5 テーブルを巻き込まない。"""
    session = _RecordingSession()
    result = await evaluate_mvp_checklist(session)  # type: ignore[arg-type]

    # 発行順は items の順序と一致する前提 (1 項目 1 クエリ)。
    assert len(session.executed_sql) == len(result.items)
    for item, sql in zip(result.items, session.executed_sql, strict=True):
        expected_table = _EXPECTED_TABLE[item.id]
        assert expected_table in sql, f"{item.id}: {expected_table} が SQL に無い"
        for other in _ALL_TABLES:
            if other != expected_table:
                assert other not in sql, f"{item.id}: 他テーブル {other} を巻き込んでいる"


async def test_each_item_query_uses_expected_decision_column() -> None:
    """判定列を持つ項目は、 その列を WHERE 条件に含む (誤った列での件数を防ぐ)。"""
    session = _RecordingSession()
    result = await evaluate_mvp_checklist(session)  # type: ignore[arg-type]

    for item, sql in zip(result.items, session.executed_sql, strict=True):
        column = _EXPECTED_COLUMN[item.id]
        if column is not None:
            assert column in sql, f"{item.id}: 判定列 {column} が WHERE に無い"


def test_mvp_check_ids_fixed_contract_order() -> None:
    """公開 id ヘルパは contract 固定 6 値・固定順序を返す。"""
    assert list(mvp_check_ids()) == [
        "dryrun_success",
        "acoustid_clear",
        "unlisted_post",
        "panic_stop",
        "oauth_refresh",
        "slack_categories",
    ]
