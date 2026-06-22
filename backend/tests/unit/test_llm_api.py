"""``api/llm.py`` の単体テスト (US5, T117)。

外部依存を一切起動しない真の単体テスト:

- DB は ``execute`` / ``flush`` / ``commit`` を記録する fake セッション (Postgres 不要)。
  ``select`` は文 (Select) の形を見て app_state override 取得 / usage 集計を振り分け、
  ``insert`` は対象テーブル名で app_state upsert / audit_log を振り分けて記録する。
- ``resolve_provider_config`` は本物を使い (env / app_state override の解決ロジックを実検証)、
  ``get_settings`` は monkeypatch でテスト用 Settings に差し替える。
- usage 集計は fake セッションが固定の集計結果を返し、 ハンドラの集計→DTO 変換と予算計算を検証する。
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import HTTPException
from sqlalchemy import Insert, Select

from ymg_backend.api import llm as api_llm
from ymg_backend.core.config import Settings

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.asyncio


def _settings(**overrides: Any) -> Settings:
    """テスト用 Settings (provider/key を上書き可能)。 env 非依存に既定値で構築する。"""
    base: dict[str, Any] = {
        "llm_provider": "openai",
        "llm_auth_mode": "api_key",
        "openai_api_key": "sk-openai",
        "anthropic_api_key": "sk-anthropic",
        "monthly_budget_usd": 50.0,
    }
    base.update(overrides)
    return Settings(**base)


# --- fake DB セッション ------------------------------------------------------------


class _AppStateRow:
    """factory._read_app_state_overrides が読む ``row.key`` / ``row.value`` を持つ Row 風。"""

    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value


class _ColumnsResult:
    """``execute(select(...)).all()`` / ``scalar_one()`` を満たす最小ラッパ。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> Any:
        return self._rows[0][0]


class _FakeSession:
    """app_state override の select / upsert、 usage 集計の select、 audit insert を扱う。

    - ``execute(Select from app_state)`` → ``app_state`` dict から該当 key の (key, value) を返す。
    - ``execute(Select from usage_log)`` → ``usage_total`` / ``usage_rows`` を返す
      (列数で total 集計か by_provider 集計かを判定する)。
    - ``execute(Insert into app_state)`` → ``app_state`` を upsert。
    - ``execute(Insert into audit_log)`` → ``audit_rows`` に蓄積。
    """

    def __init__(
        self,
        *,
        app_state: dict[str, str] | None = None,
        usage_total: Decimal | int = 0,
        usage_rows: list[tuple[Any, ...]] | None = None,
    ) -> None:
        # app_state は factory 読取が一段 json.loads する前提なので JSON literal 文字列で持つ。
        self.app_state: dict[str, str] = dict(app_state or {})
        self.usage_total: Decimal | int = usage_total
        self.usage_rows: list[tuple[Any, ...]] = list(usage_rows or [])
        self.audit_rows: list[Mapping[str, Any]] = []
        self.commit_count = 0
        self.flush_count = 0

    async def execute(self, stmt: Any) -> _ColumnsResult:
        if isinstance(stmt, Select):
            return self._handle_select(stmt)
        if isinstance(stmt, Insert):
            self._apply_insert(stmt)
            return _ColumnsResult([])
        raise AssertionError(f"想定外の statement: {stmt!r}")

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:  # 互換のため (本テストでは未使用)
        pass

    def _handle_select(self, stmt: Select[Any]) -> _ColumnsResult:
        table_name = stmt.get_final_froms()[0].name  # type: ignore[attr-defined]
        if table_name == "app_state":
            # factory._read_app_state_overrides は row.key / row.value を読む。
            return _ColumnsResult([_AppStateRow(k, v) for k, v in self.app_state.items()])
        if table_name == "usage_log":
            n_cols = len(stmt.selected_columns)
            if n_cols == 1:  # total 集計
                return _ColumnsResult([(self.usage_total,)])
            return _ColumnsResult(self.usage_rows)  # by_provider 集計
        raise AssertionError(f"想定外の select 元テーブル: {table_name}")

    def _apply_insert(self, stmt: Insert) -> None:
        table_name = stmt.table.name
        params = stmt.compile().params
        if table_name == "app_state":
            # ハンドラは生 str を渡す。 factory 読取が json.loads するので JSON literal で保持。
            import json

            self.app_state[str(params["key"])] = json.dumps(params["value"])
        elif table_name == "audit_log":
            self.audit_rows.append(dict(params))
        else:
            raise AssertionError(f"想定外の insert 先テーブル: {table_name}")


# --- GET /llm/providers ------------------------------------------------------------


async def test_list_providers_reports_availability_and_models() -> None:
    """key 設定済みで available=True、 anthropic は api_key のみ、 各 provider の models を返す。"""
    settings = _settings(anthropic_api_key="")  # anthropic key 未設定 → available=False

    configs = await api_llm.list_providers(user="seita", settings=settings)

    by_name = {c.provider: c for c in configs}
    assert set(by_name) == {"openai", "anthropic", "ollama"}
    assert by_name["openai"].available is True
    assert by_name["openai"].auth_modes == ["api_key", "codex_oauth"]
    assert by_name["anthropic"].available is False
    assert by_name["anthropic"].auth_modes == ["api_key"]
    assert by_name["ollama"].available is True  # ローカルは常に available
    assert "claude-sonnet-4-6" in by_name["anthropic"].models
    assert "gpt-4.1" in by_name["openai"].models


# --- PUT /llm/providers ------------------------------------------------------------


async def test_set_provider_anthropic_subscription_is_400() -> None:
    """Anthropic + 非 api_key (subscription) は永続化前に 400 (FR-022)。"""
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await api_llm.set_provider(
            body=api_llm.LlmProviderPutBody(provider="anthropic", auth_mode="codex_oauth"),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 400
    assert session.commit_count == 0  # 書込なし
    assert session.audit_rows == []


@pytest.mark.fr("FR-021")
async def test_set_provider_codex_oauth_non_openai_is_400() -> None:
    """FR-021: codex_oauth は openai のみ。 ollama + codex_oauth は 400。"""
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await api_llm.set_provider(
            body=api_llm.LlmProviderPutBody(provider="ollama", auth_mode="codex_oauth"),
            user="seita",
            session=session,  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 400


async def test_set_provider_switch_persists_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    """openai→anthropic 切替で app_state 2 キー upsert + audit + commit。"""
    monkeypatch.setattr(api_llm, "get_settings", _settings)
    # 現在は env 既定 (openai/api_key)。 anthropic/api_key へ切替。
    session = _FakeSession()

    state = await api_llm.set_provider(
        body=api_llm.LlmProviderPutBody(provider="anthropic", auth_mode="api_key"),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.provider == "anthropic"
    assert state.auth_mode == "api_key"
    assert state.model == "claude-sonnet-4-6"  # factory の既定モデル
    assert session.app_state["llm_provider"] == '"anthropic"'  # JSON literal で格納
    assert session.app_state["llm_auth_mode"] == '"api_key"'
    assert session.commit_count == 1
    row = session.audit_rows[0]
    assert row["action"] == "llm_provider_changed"
    assert row["actor"] == "seita"
    assert row["payload"]["from"]["provider"] == "openai"
    assert row["payload"]["to"]["provider"] == "anthropic"


async def test_set_provider_same_value_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """同値要求 (openai/api_key→openai/api_key) は no-op (書込 / audit なし)。"""
    monkeypatch.setattr(api_llm, "get_settings", _settings)
    session = _FakeSession()  # env 既定 = openai/api_key

    state = await api_llm.set_provider(
        body=api_llm.LlmProviderPutBody(provider="openai", auth_mode="api_key"),
        user="seita",
        session=session,  # type: ignore[arg-type]
    )

    assert state.provider == "openai"
    assert session.commit_count == 0
    assert session.audit_rows == []


# --- GET /llm/usage ----------------------------------------------------------------


async def test_get_usage_aggregates_and_computes_budget_pct() -> None:
    """合計コスト + provider 別内訳 + 予算進捗% を返す (budget=50 で total=12.5 → 25%)。"""
    settings = _settings(monthly_budget_usd=50.0)
    session = _FakeSession(
        usage_total=Decimal("12.500000"),
        usage_rows=[
            ("openai", Decimal("10.000000"), Decimal("1000"), Decimal("200"), Decimal("500")),
            ("anthropic", Decimal("2.500000"), Decimal("300"), Decimal("0"), Decimal("150")),
        ],
    )

    usage = await api_llm.get_usage(
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
        month="2026-05",
    )

    assert usage.month == "2026-05"
    assert usage.total_cost_usd == pytest.approx(12.5)
    assert usage.budget_usd == pytest.approx(50.0)
    assert usage.budget_pct == pytest.approx(25.0)
    assert usage.by_provider["openai"].cost_usd == pytest.approx(10.0)
    assert usage.by_provider["openai"].prompt_tokens == 1000
    assert usage.by_provider["anthropic"].completion_tokens == 150


async def test_get_usage_zero_budget_avoids_division_by_zero() -> None:
    """monthly_budget_usd=0 のとき budget_pct は 0 (ゼロ除算回避)。"""
    settings = _settings(monthly_budget_usd=0.0)
    session = _FakeSession(usage_total=Decimal("5.000000"))

    usage = await api_llm.get_usage(
        user="seita",
        session=session,  # type: ignore[arg-type]
        settings=settings,
        month="2026-05",
    )

    assert usage.total_cost_usd == pytest.approx(5.0)
    assert usage.budget_pct == pytest.approx(0.0)


async def test_get_usage_rejects_out_of_range_month() -> None:
    """``YYYY-MM`` だが月が 13 など範囲外なら 422 (``Query(pattern=...)`` は通過する形式)。"""
    settings = _settings()
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await api_llm.get_usage(
            user="seita",
            session=session,  # type: ignore[arg-type]
            settings=settings,
            month="2026-13",
        )

    assert exc.value.status_code == 422
