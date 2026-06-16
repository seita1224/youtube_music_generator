"""``/mvp-check`` MVP リリース判定ルータ (T128, ADR-0035 §6)。

``GET /mvp-check`` を 1 本提供する (Basic 認証配下。 配線は ``main.py`` の
``_build_protected_router`` 配下で後段が ``include_router(router)`` する。 本モジュールは
素の ``APIRouter`` を module 変数 ``router`` で公開するだけで auth を個別付与しない)。

判定の業務ロジックは :func:`ymg_backend.domain.mvp_check.checklist.evaluate_mvp_checklist`
(T127) に委譲する。 本ルータは「呼び出し側」なので read-only であり ``commit`` しない
(MVP チェックは DB を一切書き換えない純粋な集計判定。 HTTP=トランザクション境界という
共有契約のうち「書込ハンドラが commit」の対偶で、 書込が無ければ commit も不要)。

設計方針 (``api/panic_stop.py`` の ``get_panic_stop_service`` / ``api/llm.py`` の read 系を踏襲):

- 判定関数は ``Depends`` 経由で解決し (:func:`get_checklist_evaluator`)、 composition root /
  テストが ``app.dependency_overrides`` で stub を注入できるようにする。 既定実装は
  ``domain.mvp_check.checklist`` の :func:`evaluate_mvp_checklist` をそのまま返す。
- domain の :class:`MvpChecklistResult` を contract (``backend-api.yaml`` ``MvpCheck`` /
  ``MvpCheckItem``) の Pydantic 応答 DTO へ写像する。 ``id`` / ``status`` の enum 型 (``MvpCheckId``
  / ``MvpCheckStatus``) は domain と共有し (``Literal``)、 二重定義しない。
"""

from __future__ import annotations

from typing import Annotated, Final, Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.security import BasicAuthUser
from ymg_backend.domain.mvp_check.checklist import (
    MvpCheckId,
    MvpChecklistResult,
    MvpCheckStatus,
)
from ymg_backend.infrastructure.db.session import get_session

router: Final = APIRouter(tags=["mvp"])

# contract 上の総項目数 (常に 6, ADR-0035 §6)。
_MVP_TOTAL: Final[int] = 6


class ChecklistEvaluator(Protocol):
    """判定関数の呼び出しシグネチャ (``Depends`` で注入する評価器)。

    具象関数 :func:`evaluate_mvp_checklist` に対しルータは構造 (Protocol) で結合する。
    テストは同シグネチャの stub を ``app.dependency_overrides`` で注入できる。
    """

    async def __call__(self, session: AsyncSession) -> MvpChecklistResult: ...


class MvpCheckItem(BaseModel):
    """contract ``MvpCheckItem`` の応答 DTO (1 判定項目)。"""

    id: MvpCheckId
    label: str
    status: MvpCheckStatus
    detail: str | None = None


class MvpCheckResponse(BaseModel):
    """``GET /mvp-check`` のレスポンス (contract ``MvpCheck``)。

    ``items`` は固定 6 件・固定順序、 ``completed`` は green 件数 (0..6)、 ``total`` は常に 6。
    """

    items: list[MvpCheckItem]
    completed: int
    total: int = _MVP_TOTAL


# ---------------------------------------------------------------------------------
# 依存性 (composition root / テストが override で stub を注入する。 既定は domain 関数)。
# ---------------------------------------------------------------------------------
def get_checklist_evaluator() -> ChecklistEvaluator:
    """MVP 判定関数を解決する依存性 (override 可)。

    既定では T127 の :func:`ymg_backend.domain.mvp_check.checklist.evaluate_mvp_checklist`
    を返す。 テストは ``app.dependency_overrides[get_checklist_evaluator]`` で stub を注入し、
    domain / DB を呼ばずにルータ単体を検証できる (``api/panic_stop.py`` の
    ``get_panic_stop_service`` と同方針)。

    Returns:
        ``async (session) -> MvpChecklistResult`` を満たす評価器。
    """
    from ymg_backend.domain.mvp_check.checklist import evaluate_mvp_checklist

    return evaluate_mvp_checklist


# ---------------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------------
@router.get(
    "/mvp-check",
    response_model=MvpCheckResponse,
    operation_id="getMvpCheck",
    summary="MVP release readiness checklist (6 items, ADR-0035)",
)
async def get_mvp_check(
    user: BasicAuthUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    evaluator: Annotated[ChecklistEvaluator, Depends(get_checklist_evaluator)],
) -> MvpCheckResponse:
    """ADR-0035 の MVP 6 項目を DB から判定し、 各項目 green/red と完了数を返す。

    判定は :func:`evaluate_mvp_checklist` (T127) に委譲する read-only 操作で、 DB を
    書き換えないため ``commit`` しない。 結果の固定 6 項目 (id 順序固定) をそのまま
    contract DTO へ写像する。

    Args:
        user: Basic 認証済みユーザー名 (read-only のため未使用)。
        session: DB セッション (判定関数へ渡す。 本ハンドラは書込 / commit しない)。
        evaluator: MVP 判定関数 (Depends で解決, override 可)。

    Returns:
        :class:`MvpCheckResponse` (6 項目の green/red + 完了数 + total=6)。
    """
    del user  # 認証済みであることのみ要求。 値は使わない (read-only)。
    result = await evaluator(session)
    return _to_response(result)


def _to_response(result: MvpChecklistResult) -> MvpCheckResponse:
    """domain 判定結果を contract ``MvpCheck`` DTO へ写像する。"""
    items = [
        MvpCheckItem(id=item.id, label=item.label, status=item.status) for item in result.items
    ]
    return MvpCheckResponse(items=items, completed=result.completed, total=result.total)


__all__ = [
    "ChecklistEvaluator",
    "MvpCheckItem",
    "MvpCheckResponse",
    "get_checklist_evaluator",
    "router",
]
