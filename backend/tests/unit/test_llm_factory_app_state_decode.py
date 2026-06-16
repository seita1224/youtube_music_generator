"""Unit テスト: factory の app_state JSONB 値デコード (T116 unit, US5)。

切替の往復一致を支える ``_decode_app_state_value``(開始値=app_state.value を正規化する
private helper, ``llm/factory.py``)を単体で検証する。asyncpg は JSONB を「decode 済み
Python オブジェクト」か「生 JSON 文字列」のどちらでも返しうるため、両形式と異常値の
扱いをここで固定する(critical 側の切替テストの前提を成立させる薄い保証)。
"""

from __future__ import annotations

import pytest

from ymg_backend.llm.factory import _decode_app_state_value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"openai"', "openai"),  # seed 形式 = JSON literal 文字列(一段デコードで unwrap)
        ('"anthropic"', "anthropic"),
        ("openai", "openai"),  # 既に decode 済みの生 str(json.loads 失敗 → そのまま採用)
        ("ollama", "ollama"),
    ],
)
def test_decode_returns_provider_string(raw: str, expected: str) -> None:
    """JSON literal / 生 str いずれも provider 名 str を返す(切替読取の往復一致)。"""
    assert _decode_app_state_value(raw) == expected


@pytest.mark.parametrize("raw", [None, 123, True, {"k": "v"}, ["openai"]])
def test_decode_returns_none_for_non_string(raw: object) -> None:
    """str に解決できない値は None(= env 値を採用させる)で返す。"""
    assert _decode_app_state_value(raw) is None


def test_decode_unwraps_only_one_level() -> None:
    """二重エンコード(``'\"\\\"x\\\"\"'``)は一段だけ解いて内側 JSON 文字列を str として返す。"""
    # json.dumps(json.dumps("x")) 相当 = '"\\"x\\""'。一段 loads で '"x"' になる。
    assert _decode_app_state_value('"\\"x\\""') == '"x"'
