"""``/plans`` 業務ルータの単体テスト (api/plans.py)。

DB は in-memory の :class:`_FakeSession` で代替し、 Basic 認証は ``get_settings``
override で固定、 planner は実 LLM を起動せず ``_build_planner`` を stub に差し替える。
実 DB / 実 provider / HTTP は一切起動しない。

検証観点:

- 認証: 無認証は 401。
- ``GET /plans``: 一覧 + ``total`` 返却、 ``cycle`` / ``status`` 絞り込み。
- ``POST /plans``: daily 生成 (201) + audit + commit / 既存日付は 409 /
  ``force_regenerate=true`` で再生成 (201) / weekly は 422 / planner の
  ``QualityError`` は 422。
- ``GET /plans/{id}``: 取得 (200) / 不在 (404)。
- ``POST /plans/{id}/approve``: generated → approved (200) + approved_at 記録 /
  非 generated は 409 / 不在は 404。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.api import plans as plans_module
from ymg_backend.api.plans import router
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import require_basic_auth
from ymg_backend.domain.errors import QualityError
from ymg_backend.infrastructure.db.session import get_session

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)


@dataclass
class _FakePlan:
    """ORM ``Plan`` の最小スタンドイン (PlanResponse 写像用)。"""

    id: uuid.UUID
    cycle: str
    payload: dict[str, Any]
    rationale: str
    status: str
    llm_provider: str
    llm_model: str
    llm_prompt_version: str
    llm_cost_usd: Decimal
    created_at: datetime
    target_date: date | None = None
    target_week_start: date | None = None
    approved_at: datetime | None = None


class _FakeResult:
    """``session.execute`` の戻り。 scalar 値 / scalars().all()/first() を満たす。"""

    def __init__(self, rows: list[Any], *, scalar: Any | None = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


@dataclass
class _FakeSession:
    """list/get/count/insert(audit)/commit のみ実装する in-memory セッション。

    ``execute`` は組まれた ``select`` を ``str()`` 検査して count / 一覧 / 既存検索 /
    audit insert を判別する (本物の SQL は実行しない)。
    """

    rows: list[_FakePlan] = field(default_factory=list)
    commits: int = 0
    rollbacks: int = 0
    flushes: int = 0

    async def get(self, _model: Any, pk: uuid.UUID) -> _FakePlan | None:
        return next((r for r in self.rows if r.id == pk), None)

    async def execute(self, statement: Any) -> _FakeResult:
        sql = str(statement).lower()
        if sql.startswith("insert"):
            # audit_log の Core insert。 行は保持せず受理のみ。
            return _FakeResult([])
        if "count" in sql:
            return _FakeResult([], scalar=len(self._filtered(statement)))
        return _FakeResult(self._filtered(statement))

    def _filtered(self, statement: Any) -> list[_FakePlan]:
        compiled = statement.compile()
        params = compiled.params
        rows = list(self.rows)

        cycle_vals = {v for k, v in params.items() if "cycle" in k.lower()}
        if cycle_vals:
            rows = [r for r in rows if r.cycle in cycle_vals]
        status_vals = {v for k, v in params.items() if "status" in k.lower()}
        if status_vals:
            rows = [r for r in rows if r.status in status_vals]
        date_vals = {v for k, v in params.items() if "target_date" in k.lower()}
        if date_vals:
            rows = [r for r in rows if r.target_date in date_vals]

        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, _obj: Any) -> None:
        return None


def _make_plan(**overrides: Any) -> _FakePlan:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "cycle": "daily",
        "payload": {"cycle": "daily", "posts": []},
        "rationale": "retention 上位ジャンルへ寄せる方針。",
        "status": "generated",
        "llm_provider": "openai",
        "llm_model": "gpt-4.1",
        "llm_prompt_version": "planner/system_v1",
        "llm_cost_usd": Decimal("0.012"),
        "created_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        "target_date": date(2026, 6, 16),
    }
    base.update(overrides)
    return _FakePlan(**base)


@dataclass
class _PlannerStub:
    """``Planner.create_daily_plan`` を模す stub。 生成 plan を ``rows`` に追加する。"""

    session: _FakeSession
    raises: Exception | None = None
    calls: int = 0

    async def create_daily_plan(
        self,
        *,
        session: _FakeSession,
        target_date: date,
        allowed_genres: list[str],
    ) -> _FakePlan:
        del session, allowed_genres
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        plan = _make_plan(target_date=target_date, status="generated")
        self.session.rows.append(plan)
        return plan


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def planner_stub(session: _FakeSession) -> _PlannerStub:
    return _PlannerStub(session=session)


@pytest.fixture
def client(
    session: _FakeSession,
    planner_stub: _PlannerStub,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    # audit の Core insert / enabled ジャンル取得 / planner 構築を stub 化し、
    # 実 LLM provider・実 SQL を起動しない。
    async def _fake_audit(*_args: Any, **_kwargs: Any) -> uuid.UUID:
        return uuid.uuid4()

    async def _fake_genres(_session: Any) -> list[str]:
        return ["lo-fi hip-hop", "synthwave"]

    async def _fake_build_planner(_settings: Any, _session: Any) -> _PlannerStub:
        return planner_stub

    monkeypatch.setattr(plans_module, "write_audit_log", _fake_audit)
    monkeypatch.setattr(plans_module, "_load_enabled_genres", _fake_genres)
    monkeypatch.setattr(plans_module, "_build_planner", _fake_build_planner)

    app = FastAPI()
    protected = APIRouter(dependencies=[Depends(require_basic_auth)])
    protected.include_router(router)
    app.include_router(protected)

    app.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME,
        admin_password=SecretStr(_PASSWORD),
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


# --- auth ------------------------------------------------------------------
def test_list_plans_requires_auth(client: TestClient) -> None:
    resp = client.get("/plans")
    assert resp.status_code == 401


# --- GET /plans ------------------------------------------------------------
def test_list_plans_returns_items_and_total(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_make_plan(), _make_plan()]
    resp = client.get("/plans", auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["total"] == 2


def test_list_plans_filters_by_status(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_make_plan(status="approved"), _make_plan(status="generated")]
    resp = client.get("/plans", params={"status": "approved"}, auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "approved"


# --- POST /plans -----------------------------------------------------------
def test_generate_plan_creates_daily_plan(
    client: TestClient,
    session: _FakeSession,
    planner_stub: _PlannerStub,
) -> None:
    resp = client.post(
        "/plans",
        json={"cycle": "daily", "target_date": "2026-06-20"},
        auth=_AUTH,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["cycle"] == "daily"
    assert body["target_date"] == "2026-06-20"
    assert planner_stub.calls == 1
    assert session.commits == 1


def test_generate_plan_conflict_when_exists(client: TestClient, session: _FakeSession) -> None:
    session.rows = [_make_plan(target_date=date(2026, 6, 20))]
    resp = client.post(
        "/plans",
        json={"cycle": "daily", "target_date": "2026-06-20"},
        auth=_AUTH,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["category"] == "recoverable"


def test_generate_plan_force_regenerate_overrides_conflict(
    client: TestClient,
    session: _FakeSession,
    planner_stub: _PlannerStub,
) -> None:
    session.rows = [_make_plan(target_date=date(2026, 6, 20))]
    resp = client.post(
        "/plans",
        json={"cycle": "daily", "target_date": "2026-06-20", "force_regenerate": True},
        auth=_AUTH,
    )
    assert resp.status_code == 201
    assert planner_stub.calls == 1


def test_generate_plan_weekly_unsupported_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/plans",
        json={"cycle": "weekly", "target_date": "2026-06-20"},
        auth=_AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["category"] == "recoverable"


def test_generate_plan_quality_error_returns_422(
    client: TestClient,
    session: _FakeSession,
    planner_stub: _PlannerStub,
) -> None:
    planner_stub.raises = QualityError("genre dictionary validation failed")
    resp = client.post(
        "/plans",
        json={"cycle": "daily", "target_date": "2026-06-21"},
        auth=_AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["category"] == "quality"
    # 失敗時は commit せず rollback している。
    assert session.commits == 0
    assert session.rollbacks == 1


# --- GET /plans/{id} -------------------------------------------------------
def test_get_plan_returns_plan(client: TestClient, session: _FakeSession) -> None:
    plan = _make_plan()
    session.rows = [plan]
    resp = client.get(f"/plans/{plan.id}", auth=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["id"] == str(plan.id)


def test_get_plan_not_found_returns_404(client: TestClient) -> None:
    resp = client.get(f"/plans/{uuid.uuid4()}", auth=_AUTH)
    assert resp.status_code == 404


# --- POST /plans/{id}/approve ----------------------------------------------
def test_approve_plan_transitions_generated_to_approved(
    client: TestClient,
    session: _FakeSession,
) -> None:
    plan = _make_plan(status="generated")
    session.rows = [plan]
    resp = client.post(f"/plans/{plan.id}/approve", auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["approved_at"] is not None
    assert plan.status == "approved"
    assert plan.approved_at is not None
    assert session.commits == 1


def test_approve_plan_non_generated_returns_409(client: TestClient, session: _FakeSession) -> None:
    plan = _make_plan(status="approved")
    session.rows = [plan]
    resp = client.post(f"/plans/{plan.id}/approve", auth=_AUTH)
    assert resp.status_code == 409
    assert resp.json()["detail"]["category"] == "recoverable"


def test_approve_plan_missing_returns_404(client: TestClient) -> None:
    resp = client.post(f"/plans/{uuid.uuid4()}/approve", auth=_AUTH)
    assert resp.status_code == 404
