"""Unit テスト: 月次予算アラート (domain/budget/alert.py, US5 T119, FR-026)。

``check_budget_and_alert`` の閾値判定と重複通知抑止を、 実 DB / 実 Slack を使わず検証する。

- ``AsyncSession`` は ``execute`` の戻り値を呼出順に差し替えるスパイ (``FakeSession``) で代替する。
  本実装の execute 順は: (1) cost SUM → scalar_one、 (2) dedup state 読取 → all()、
  (3,4) dedup upsert x2。 通知が起きない経路では (2) 以降が無いケースもある。
- ``SlackNotifier`` は ``notify`` を記録するだけのスパイに差し替える (HTTP を打たない)。
- ``commit`` / ``flush`` は no-op スタブ。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from ymg_backend.core.config import Settings
from ymg_backend.domain.budget.alert import check_budget_and_alert
from ymg_backend.domain.errors.errors import NotificationLevel

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _ScalarResult:
    """cost SUM 用の execute 戻り値 (``scalar_one`` のみ)。"""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _RowsResult:
    """dedup state 読取用の execute 戻り値 (``all`` のみ)。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """``execute`` を呼出順に応じた結果へ差し替えるスパイ。

    ``results`` に詰めた戻り値を順に返す。 戻り値不要な execute (upsert) には ``None`` を渡す。
    ``commit`` / ``flush`` は呼出回数だけ記録する。
    """

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self._index = 0
        self.commits = 0
        self.flushes = 0
        self.executed = 0

    async def execute(self, _statement: Any) -> Any:
        result = self._results[self._index] if self._index < len(self._results) else None
        self._index += 1
        self.executed += 1
        return result

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1


class SpyNotifier:
    """``notify`` の引数を記録するだけの :class:`SlackNotifier` 代替。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify(self, *, level: NotificationLevel, message: str, context: Any = None) -> None:
        self.calls.append({"level": level, "message": message, "context": context})


def _row(key: str, value: Any) -> Any:
    """app_state 1 行スタブ (``.key`` / ``.value``)。"""
    from types import SimpleNamespace

    return SimpleNamespace(key=key, value=value)


def _settings(budget: float) -> Settings:
    return Settings(
        monthly_budget_usd=budget,
        admin_password=SecretStr("test-admin-password"),
        _env_file=None,
    )


_NOW = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


# ===========================================================================
# 閾値跨ぎで通知が出る
# ===========================================================================
@pytest.mark.fr("FR-026")
async def test_alerts_when_crossing_80_pct_first_time() -> None:
    """FR-026: 80% を初めて跨いだら ERROR で 1 通知し、 dedup state を upsert + commit する。"""
    session = FakeSession(
        _ScalarResult(Decimal("42.00")),  # SUM: 42/50 = 84%
        _RowsResult([]),  # dedup state: 未記録 → last=0
        None,  # upsert last_threshold
        None,  # upsert alert_month
    )
    notifier = SpyNotifier()

    await check_budget_and_alert(session, notifier, _settings(50.0), now=_NOW)  # type: ignore[arg-type]

    assert len(notifier.calls) == 1
    assert notifier.calls[0]["level"] == NotificationLevel.ERROR
    assert notifier.calls[0]["context"] == {"month": "2026-06", "threshold_pct": 80}
    assert session.commits == 1


async def test_alerts_critical_with_100_pct() -> None:
    """100% 超過は CRITICAL (mention 相当) で通知する。"""
    session = FakeSession(
        _ScalarResult(Decimal("55.00")),  # 55/50 = 110%
        _RowsResult([]),
        None,
        None,
    )
    notifier = SpyNotifier()

    await check_budget_and_alert(session, notifier, _settings(50.0), now=_NOW)  # type: ignore[arg-type]

    assert len(notifier.calls) == 1
    assert notifier.calls[0]["level"] == NotificationLevel.CRITICAL


# ===========================================================================
# 重複抑止 (同一閾値 / 降順往復)
# ===========================================================================
async def test_no_realert_for_same_threshold_same_month() -> None:
    """同月で既に 80% を通知済みなら、 再度 84% でも通知しない (cron 毎回の再送抑止)。"""
    session = FakeSession(
        _ScalarResult(Decimal("42.00")),  # 84% → threshold 80
        _RowsResult(
            [_row("budget_alert_last_threshold", 80), _row("budget_alert_month", "2026-06")]
        ),
    )
    notifier = SpyNotifier()

    await check_budget_and_alert(session, notifier, _settings(50.0), now=_NOW)  # type: ignore[arg-type]

    assert notifier.calls == []
    assert session.commits == 0  # 通知も書込も無し


async def test_no_realert_on_downward_swing() -> None:
    """80% 通知済みから 50% 台へ戻っても、 50% は再通知しない (降順往復抑止)。"""
    session = FakeSession(
        _ScalarResult(Decimal("30.00")),  # 30/50 = 60% → threshold 50
        _RowsResult(
            [_row("budget_alert_last_threshold", 80), _row("budget_alert_month", "2026-06")]
        ),
    )
    notifier = SpyNotifier()

    await check_budget_and_alert(session, notifier, _settings(50.0), now=_NOW)  # type: ignore[arg-type]

    assert notifier.calls == []


async def test_realert_when_threshold_escalates() -> None:
    """50% 通知済みから 80% へ上がったら、 より高い閾値で再通知する。"""
    session = FakeSession(
        _ScalarResult(Decimal("42.00")),  # 84% → threshold 80
        _RowsResult(
            [_row("budget_alert_last_threshold", 50), _row("budget_alert_month", "2026-06")]
        ),
        None,
        None,
    )
    notifier = SpyNotifier()

    await check_budget_and_alert(session, notifier, _settings(50.0), now=_NOW)  # type: ignore[arg-type]

    assert len(notifier.calls) == 1
    assert notifier.calls[0]["context"]["threshold_pct"] == 80


async def test_month_change_resets_threshold() -> None:
    """前月に 100% 通知済みでも、 月が変われば閾値はリセットされ当月分を通知する。"""
    session = FakeSession(
        _ScalarResult(Decimal("30.00")),  # 60% → threshold 50
        _RowsResult(
            [_row("budget_alert_last_threshold", 100), _row("budget_alert_month", "2026-05")]
        ),
        None,
        None,
    )
    notifier = SpyNotifier()

    await check_budget_and_alert(session, notifier, _settings(50.0), now=_NOW)  # type: ignore[arg-type]

    assert len(notifier.calls) == 1
    assert notifier.calls[0]["context"]["threshold_pct"] == 50


# ===========================================================================
# 通知しない経路
# ===========================================================================
async def test_no_alert_below_50_pct() -> None:
    """50% 未満は閾値未達のため通知も dedup 読取もしない (SUM の 1 回だけ execute)。"""
    session = FakeSession(_ScalarResult(Decimal("10.00")))  # 20%
    notifier = SpyNotifier()

    await check_budget_and_alert(session, notifier, _settings(50.0), now=_NOW)  # type: ignore[arg-type]

    assert notifier.calls == []
    assert session.executed == 1  # SUM のみ。 dedup 読取に進まない


async def test_skips_when_budget_zero() -> None:
    """monthly_budget_usd=0 はチェック自体をスキップ (execute も通知もしない)。"""
    session = FakeSession()
    notifier = SpyNotifier()

    await check_budget_and_alert(session, notifier, _settings(0.0), now=_NOW)  # type: ignore[arg-type]

    assert notifier.calls == []
    assert session.executed == 0
