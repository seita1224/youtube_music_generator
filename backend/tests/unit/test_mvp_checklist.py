"""MVP 6 項目チェックリスト判定ロジックの単体テスト (T127, ADR-0035 §6 / TDD 先行)。

ADR-0035 が定める MVP リリース判定の 6 項目を、 ``domain/mvp_check/checklist.py`` の
判定関数が DB 状態から ``green`` / ``red`` に正しく写像することを網羅検証する。 実 DB /
docker は一切起動せず、 ``execute`` した ``select(func.count())...`` 文の対象テーブル名を
introspect して件数を返す in-memory の :class:`_CountSession` でドメインロジックのみを切り出す。

判定 6 項目 (固定 id / 順序、 contract ``MvpCheckItem.id`` の enum6 と一致):

==================  =========================================  ====================================
id                  green 条件 (本テストが固定する判定契約)        判定ソース (テーブル / 列)
==================  =========================================  ====================================
``dryrun_success``  dryrun が ``posted`` に到達した output >= 3   ``dryrun_outputs.state == 'posted'``
``acoustid_clear``  ``clear`` の track >= 1                      ``audio_tracks.acoustid_status``
``unlisted_post``   ``unlisted`` の video >= 1                   ``videos.privacy_status``
``panic_stop``      ``action == 'panic_stop'`` の行 >= 1         ``audit_log.action``
``oauth_refresh``   refresh で更新された credential >= 1         ``oauth_credentials`` (updated>created)
``slack_categories``Slack 5 カテゴリ分の送信記録 >= 5            送信記録テーブル (件数 >= 5)
==================  =========================================  ====================================

実装方針 (T127 が満たすべき公開契約):

- ``evaluate_mvp_checklist(session) -> MvpChecklistResult`` を ``async`` で公開する。
- 結果は ``items`` (固定 6 件・固定順序、 各 ``id`` / ``label`` / ``status`` を持つ)、 ``completed``
  (green 件数 0..6)、 ``total`` (== 6) を持つ。
- 各項目は対応テーブルへの件数クエリ結果がしきい値以上なら ``green``、 未満なら ``red``。
  ``dryrun_success`` のみしきい値 3、 ``slack_categories`` は 5、 残り 4 項目は 1。

``checklist`` 未実装の TDD RED 段階では import 不能のため module ごと skip する。
"""

from __future__ import annotations

from typing import Any

import pytest

checklist_mod = pytest.importorskip("ymg_backend.domain.mvp_check.checklist")

pytestmark = pytest.mark.unit


# --- 判定契約 (id -> green に必要な最小件数) --------------------------------------
# 各項目を独立に green / red にできるよう、 id ごとの「対象テーブル名」と「green しきい値」を
# テスト側で固定する。 テーブル名は ORM ``__tablename__`` と一致 (compile した SQL に出る)。
_TABLE_BY_ID: dict[str, str] = {
    "dryrun_success": "dryrun_outputs",
    "acoustid_clear": "audio_tracks",
    "unlisted_post": "videos",
    "panic_stop": "audit_log",
    "oauth_refresh": "oauth_credentials",
    "slack_categories": "usage_log",
}
_GREEN_THRESHOLD: dict[str, int] = {
    "dryrun_success": 3,
    "acoustid_clear": 1,
    "unlisted_post": 1,
    "panic_stop": 1,
    "oauth_refresh": 1,
    "slack_categories": 5,
}
# contract ``MvpCheck`` の固定 6 値・固定順序。
_EXPECTED_IDS: tuple[str, ...] = (
    "dryrun_success",
    "acoustid_clear",
    "unlisted_post",
    "panic_stop",
    "oauth_refresh",
    "slack_categories",
)


# --- in-memory fakes --------------------------------------------------------------


class _ScalarResult:
    """``scalar_one`` / ``scalar`` のみ実装する execute 戻り値スタンドイン。"""

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def scalar(self) -> int:
        return self._value


class _CountSession:
    """``execute`` した件数クエリの対象テーブル名から件数を引く in-memory セッション。

    判定関数は項目ごとに ``select(func.count())...`` を 1 本ずつ流す前提。 本フェイクは文を
    文字列へ compile し、 ``counts`` の各テーブル名が SQL に現れたらその件数を ``_ScalarResult``
    で返す。 これにより「どのテーブルを何件と見せるか」をテストが完全に制御でき、 6 項目を
    独立に green / red へ振れる。
    """

    def __init__(self, counts: dict[str, int]) -> None:
        # 既定 0 件 (= 全項目 red) を土台に、 指定テーブルだけ件数を上書きする。
        self._counts: dict[str, int] = dict.fromkeys(_TABLE_BY_ID.values(), 0)
        self._counts.update(counts)
        self.executed_sql: list[str] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        sql = str(statement.compile(compile_kwargs={"literal_binds": False}))
        self.executed_sql.append(sql)
        for table, count in self._counts.items():
            # ``from "videos"`` / ``FROM videos`` 双方に当たるよう小文字化して部分一致。
            if table in sql.lower():
                return _ScalarResult(count)
        return _ScalarResult(0)


# --- helpers ----------------------------------------------------------------------


async def _evaluate(counts: dict[str, int]) -> Any:
    """指定件数の DB 状態で判定関数を実行し結果オブジェクトを返す。"""
    session = _CountSession(counts)
    return await checklist_mod.evaluate_mvp_checklist(session)


def _status_by_id(result: Any) -> dict[str, str]:
    return {item.id: item.status for item in result.items}


def _all_green_counts() -> dict[str, int]:
    """全 6 項目が green になる最小件数 (= 各しきい値ちょうど)。"""
    return {_TABLE_BY_ID[i]: _GREEN_THRESHOLD[i] for i in _EXPECTED_IDS}


# --- 形状・順序・total -------------------------------------------------------------


async def test_result_shape_fixed_six_items_in_order() -> None:
    """items は固定 6 件・固定 id 順、 total は常に 6。"""
    result = await _evaluate({})
    assert [item.id for item in result.items] == list(_EXPECTED_IDS)
    assert result.total == 6
    assert len(result.items) == 6


async def test_each_item_has_label() -> None:
    """各項目は非空の label を持つ (UI 表示用)。"""
    result = await _evaluate(_all_green_counts())
    for item in result.items:
        assert isinstance(item.label, str)
        assert item.label != ""


# --- 全 red / 全 green の両端 ------------------------------------------------------


async def test_empty_db_all_red_completed_zero() -> None:
    """空 DB (全件 0) では 6 項目すべて red、 completed == 0。"""
    result = await _evaluate({})
    assert _status_by_id(result) == dict.fromkeys(_EXPECTED_IDS, "red")
    assert result.completed == 0


async def test_all_conditions_met_all_green_completed_six() -> None:
    """全項目のしきい値を満たすと 6 項目すべて green、 completed == 6。"""
    result = await _evaluate(_all_green_counts())
    assert _status_by_id(result) == dict.fromkeys(_EXPECTED_IDS, "green")
    assert result.completed == 6


# --- 各項目を独立に green / red へ -------------------------------------------------


@pytest.mark.parametrize("target_id", _EXPECTED_IDS)
async def test_only_one_item_green_when_only_its_table_filled(target_id: str) -> None:
    """対象テーブルだけ件数を満たすと、 その項目のみ green・残り 5 項目は red。"""
    counts = {_TABLE_BY_ID[target_id]: _GREEN_THRESHOLD[target_id]}
    result = await _evaluate(counts)
    statuses = _status_by_id(result)
    assert statuses[target_id] == "green"
    for other in _EXPECTED_IDS:
        if other != target_id:
            assert statuses[other] == "red"
    assert result.completed == 1


@pytest.mark.parametrize("target_id", _EXPECTED_IDS)
async def test_item_red_when_just_below_threshold(target_id: str) -> None:
    """しきい値 -1 件では当該項目は red (境界条件)。"""
    below = _GREEN_THRESHOLD[target_id] - 1
    counts = {_TABLE_BY_ID[target_id]: below}
    result = await _evaluate(counts)
    assert _status_by_id(result)[target_id] == "red"


# --- しきい値の境界 (dryrun=3, slack=5) を個別に固定 ------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "red"), (1, "red"), (2, "red"), (3, "green"), (4, "green")],
)
async def test_dryrun_success_threshold_is_three(count: int, expected: str) -> None:
    """``dryrun_success`` は posted が 3 件以上で green (2 件以下は red)。"""
    result = await _evaluate({"dryrun_outputs": count})
    assert _status_by_id(result)["dryrun_success"] == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "red"), (4, "red"), (5, "green"), (6, "green")],
)
async def test_slack_categories_threshold_is_five(count: int, expected: str) -> None:
    """``slack_categories`` は 5 カテゴリ分 (5 件以上) で green (4 件以下は red)。"""
    result = await _evaluate({"usage_log": count})
    assert _status_by_id(result)["slack_categories"] == expected


@pytest.mark.parametrize(
    "single_id",
    ["acoustid_clear", "unlisted_post", "panic_stop", "oauth_refresh"],
)
async def test_single_record_items_green_at_one(single_id: str) -> None:
    """しきい値 1 の 4 項目は 1 件で green、 0 件で red。"""
    table = _TABLE_BY_ID[single_id]
    assert _status_by_id(await _evaluate({table: 1}))[single_id] == "green"
    assert _status_by_id(await _evaluate({table: 0}))[single_id] == "red"


# --- completed は green 件数と一致 -------------------------------------------------


async def test_completed_equals_green_count() -> None:
    """3 項目だけ満たすと completed == 3 (green 件数と一致)。"""
    counts = {
        _TABLE_BY_ID["acoustid_clear"]: 1,
        _TABLE_BY_ID["unlisted_post"]: 1,
        _TABLE_BY_ID["panic_stop"]: 1,
    }
    result = await _evaluate(counts)
    greens = sum(1 for item in result.items if item.status == "green")
    assert result.completed == greens == 3
