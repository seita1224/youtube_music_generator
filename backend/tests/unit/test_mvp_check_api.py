"""``/mvp-check`` ルータの単体テスト (api/mvp_check.py, T128)。

DB は触れず (評価器を stub に差し替え)、 Basic 認証は ``require_basic_auth`` override で
固定する。 実 DB / domain の checklist 実装 (T127) / HTTP は一切起動しない。 ルータは判定
関数を ``Depends(get_checklist_evaluator)`` で受けるため、 同形 stub を注入してルータ単体の
「認証 → 評価器呼び出し → contract DTO 写像 → read-only (commit 無し)」だけを検証する。

検証観点:

- 認証必須 (未認証は 401)。
- 正常系: 評価器を ``session`` 付きで呼び、 200。 ``items`` (id/label/status) / ``completed`` /
  ``total`` を contract 通り写像。 read-only なので ``commit`` は呼ばれない。
- ``status`` enum は ``green`` / ``red`` のみ通る。 不正値 (``yellow``) は 500 (応答検証で弾く)。
- 全 6 項目 green で ``completed == 6``、 全 red で ``completed == 0``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ymg_backend.api.mvp_check import get_checklist_evaluator, router
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.security import require_basic_auth
from ymg_backend.infrastructure.db.session import get_session

pytestmark = pytest.mark.unit

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)

_EXPECTED_IDS: tuple[str, ...] = (
    "dryrun_success",
    "acoustid_clear",
    "unlisted_post",
    "panic_stop",
    "oauth_refresh",
    "slack_categories",
)


@dataclass(frozen=True)
class _FakeItem:
    """domain ``MvpChecklistResult.items`` の各要素を模す軽量 stub。"""

    id: str
    label: str
    status: str


@dataclass(frozen=True)
class _FakeResult:
    """domain :func:`evaluate_mvp_checklist` 戻り値を模す軽量 stub。"""

    items: list[_FakeItem]
    completed: int
    total: int = 6


@dataclass
class _SpySession:
    """``commit`` 呼び出し回数のみ記録する in-memory セッション (評価器が stub なので I/O 無し)。"""

    commits: int = 0

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class _SpyEvaluator:
    """評価器 stub。 戻りと、 受け取った ``session`` を記録する。"""

    result: _FakeResult
    received_sessions: list[Any] = field(default_factory=list)

    async def __call__(self, session: Any) -> _FakeResult:
        self.received_sessions.append(session)
        return self.result


def _settings() -> Settings:
    return Settings(admin_username=_USERNAME, admin_password=SecretStr(_PASSWORD))


def _result(statuses: dict[str, str]) -> _FakeResult:
    """id -> status の指定から固定 6 項目順の ``_FakeResult`` を組む。"""
    items = [_FakeItem(id=i, label=f"label-{i}", status=statuses[i]) for i in _EXPECTED_IDS]
    completed = sum(1 for it in items if it.status == "green")
    return _FakeResult(items=items, completed=completed)


def _all(status: str) -> dict[str, str]:
    return dict.fromkeys(_EXPECTED_IDS, status)


def _build_client(
    session: _SpySession,
    *,
    evaluator: _SpyEvaluator | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[require_basic_auth] = lambda: _USERNAME
    if evaluator is not None:
        app.dependency_overrides[get_checklist_evaluator] = lambda: evaluator
    return TestClient(app)


# --- auth -------------------------------------------------------------------------


def test_requires_auth() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: _SpySession()
    app.dependency_overrides[get_settings] = _settings
    client = TestClient(app)
    resp = client.get("/mvp-check")
    assert resp.status_code == 401


# --- happy path -------------------------------------------------------------------


def test_returns_items_completed_total_and_does_not_commit() -> None:
    """評価器結果を contract 通り写像し、 read-only なので commit しない。"""
    statuses = {
        "dryrun_success": "green",
        "acoustid_clear": "red",
        "unlisted_post": "green",
        "panic_stop": "red",
        "oauth_refresh": "green",
        "slack_categories": "red",
    }
    evaluator = _SpyEvaluator(result=_result(statuses))
    session = _SpySession()
    client = _build_client(session, evaluator=evaluator)

    resp = client.get("/mvp-check", auth=_AUTH)

    assert resp.status_code == 200
    payload = resp.json()
    # 固定 6 項目・固定順序で id/label/status を写像。
    assert [it["id"] for it in payload["items"]] == list(_EXPECTED_IDS)
    assert {it["id"]: it["status"] for it in payload["items"]} == statuses
    assert payload["items"][0]["label"] == "label-dryrun_success"
    # completed は green 件数、 total は常に 6。
    assert payload["completed"] == 3
    assert payload["total"] == 6
    # 評価器は session を受け取って 1 回呼ばれる。
    assert len(evaluator.received_sessions) == 1
    assert evaluator.received_sessions[0] is session
    # read-only: commit は呼ばれない。
    assert session.commits == 0


def test_all_green_completed_six() -> None:
    evaluator = _SpyEvaluator(result=_result(_all("green")))
    client = _build_client(_SpySession(), evaluator=evaluator)

    resp = client.get("/mvp-check", auth=_AUTH)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["completed"] == 6
    assert all(it["status"] == "green" for it in payload["items"])


def test_all_red_completed_zero() -> None:
    evaluator = _SpyEvaluator(result=_result(_all("red")))
    client = _build_client(_SpySession(), evaluator=evaluator)

    resp = client.get("/mvp-check", auth=_AUTH)

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["completed"] == 0
    assert all(it["status"] == "red" for it in payload["items"])


# --- contract DTO 形状 -------------------------------------------------------------


def test_detail_is_omitted_when_absent() -> None:
    """domain 結果に detail が無い場合、 応答 item の ``detail`` は null (省略可フィールド)。"""
    evaluator = _SpyEvaluator(result=_result(_all("green")))
    client = _build_client(_SpySession(), evaluator=evaluator)

    resp = client.get("/mvp-check", auth=_AUTH)

    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["detail"] is None


def test_invalid_status_rejected_by_dto_mapping() -> None:
    """enum 外 ``status`` は contract DTO 写像時点で弾く (500、 contract 逸脱を漏らさない)。"""
    items = [_FakeItem(id=i, label=f"label-{i}", status="green") for i in _EXPECTED_IDS]
    items[0] = _FakeItem(id="dryrun_success", label="x", status="yellow")
    evaluator = _SpyEvaluator(result=_FakeResult(items=items, completed=5))
    # TestClient は既定でサーバ例外を再送出する。 500 を観測するため抑止する。
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: _SpySession()
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[require_basic_auth] = lambda: _USERNAME
    app.dependency_overrides[get_checklist_evaluator] = lambda: evaluator
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/mvp-check", auth=_AUTH)

    assert resp.status_code == 500
