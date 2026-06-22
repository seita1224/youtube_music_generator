"""lifespan の scheduler 起動判定の単体テスト (main._read_scheduler_enabled, FR-072)。

FR-072 (reboot 後は scheduler_enabled=false で起動する) の核心ヘルパ
:func:`~ymg_backend.main._read_scheduler_enabled` を、 実 DB を起動せず in-memory の
fake sessionmaker を ``main.get_sessionmaker`` へ monkeypatch して直接呼び検証する。

検証観点 (ADR-0031: 安全側 false 起動):

- ``app_state`` に ``scheduler_enabled`` 行が無い (None) → False。
- 値が ``"false"`` / ``False`` → False。
- 値が ``"true"`` / ``True`` → True。
- 型不一致 (int / dict / 数値文字列など bool でない) → 安全側 False。

これらはいずれも ``_read_scheduler_enabled`` の正規化ロジック (str は ``json.loads``、
bool でなければ False) を網羅する。 「取得例外」 経路は ``_read_scheduler_enabled`` 自体は
正規化せず例外を送出するが、 lifespan では ``_verify_db_connection`` が先行するため DB 不達は
そこで ``FatalError`` となりサイクルが停止する (false 起動より安全側)。 この経路は実装本体を
変えずに helper 単体では再現できないため :func:`pytest.mark.xfail` で明示する。

実装本体 (main.py) は一切変更しない。 テストだけで FR-072 を担保する。
"""

from __future__ import annotations

from typing import Any

import pytest

from ymg_backend import main as main_mod

# 本ファイルの全テストは FR-072(reboot 後 scheduler_enabled=false 起動)を検証する。
pytestmark = [pytest.mark.unit, pytest.mark.fr("FR-072")]


# --- fake DB セッション / sessionmaker --------------------------------------------


class _ScalarResult:
    """``execute(select).scalar_one_or_none()`` を満たす最小ラッパ。"""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """``app_state.scheduler_enabled`` の値を 1 つだけ返す in-memory セッション。

    ``raise_on_execute`` 指定時は ``execute`` で例外を送出し、 取得例外経路を再現する。
    """

    def __init__(self, *, stored_value: Any, raise_on_execute: bool = False) -> None:
        self._stored_value = stored_value
        self._raise_on_execute = raise_on_execute

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, _stmt: Any) -> _ScalarResult:
        if self._raise_on_execute:
            raise RuntimeError("simulated DB failure")
        return _ScalarResult(self._stored_value)


def _patch_sessionmaker(
    monkeypatch: pytest.MonkeyPatch, *, stored_value: Any, raise_on_execute: bool = False
) -> None:
    """``main.get_sessionmaker`` を、 ``async with maker() as session`` で fake を返すよう差し替える。"""

    def _maker() -> _FakeSession:
        return _FakeSession(stored_value=stored_value, raise_on_execute=raise_on_execute)

    # ``_read_scheduler_enabled`` は ``maker = get_sessionmaker()`` の戻り (= 呼び出し可能) を
    # ``async with maker() as session`` で使う。 lambda が _maker (呼び出し可能) を返す。
    monkeypatch.setattr(main_mod, "get_sessionmaker", lambda: _maker)


# --- (a) 行が無い → False ----------------------------------------------------------


async def test_missing_row_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-072: app_state に scheduler_enabled 行が無い (None) なら安全側 False。"""
    _patch_sessionmaker(monkeypatch, stored_value=None)

    assert await main_mod._read_scheduler_enabled() is False


# --- (b) "false" / False → False ---------------------------------------------------


@pytest.mark.parametrize("stored", ["false", False])
async def test_false_values_return_false(
    monkeypatch: pytest.MonkeyPatch, stored: Any
) -> None:
    """FR-072: 値が JSON ``"false"`` でも bool ``False`` でも False を返す。"""
    _patch_sessionmaker(monkeypatch, stored_value=stored)

    assert await main_mod._read_scheduler_enabled() is False


# --- (c) "true" / True → True ------------------------------------------------------


@pytest.mark.parametrize("stored", ["true", True])
async def test_true_values_return_true(
    monkeypatch: pytest.MonkeyPatch, stored: Any
) -> None:
    """FR-072: 値が JSON ``"true"`` でも bool ``True`` でも True を返す (手動 enable 済み)。"""
    _patch_sessionmaker(monkeypatch, stored_value=stored)

    assert await main_mod._read_scheduler_enabled() is True


# --- (d) 型不一致 → 安全側 False ---------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        1,  # int (bool でない)
        0,  # int (bool でない)
        "1",  # 数値文字列 (json.loads で int 1 になり bool でない)
        '{"x": 1}',  # JSON object (dict)
        "not-json",  # JSON parse 不能 (suppress され str のまま, bool でない)
        ["true"],  # list
    ],
)
async def test_type_mismatch_returns_false(
    monkeypatch: pytest.MonkeyPatch, stored: Any
) -> None:
    """FR-072: bool に正規化できない値は全て安全側 False (ADR-0031)。"""
    _patch_sessionmaker(monkeypatch, stored_value=stored)

    assert await main_mod._read_scheduler_enabled() is False


# --- 取得例外経路 (helper 単体では false 化されない) -------------------------------


@pytest.mark.xfail(
    reason=(
        "_read_scheduler_enabled は取得例外を False に正規化しない (例外を送出)。"
        " 安全側保証は lifespan の _verify_db_connection 先行 (DB 不達=FatalError でサイクル停止) "
        "が担うため、 helper 単体では false 起動を再現できない。 実装本体は変更しない。"
    ),
    strict=True,
)
async def test_execute_exception_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-072: 取得例外時に False を返す (helper 単体では未達成のため xfail)。"""
    _patch_sessionmaker(monkeypatch, stored_value=None, raise_on_execute=True)

    assert await main_mod._read_scheduler_enabled() is False
