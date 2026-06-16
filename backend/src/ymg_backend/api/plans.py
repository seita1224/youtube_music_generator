"""``/plans`` 業務ルータ (T076, contracts/backend-api.yaml ``/plans`` 群)。

改善計画 (DailyPlan) の一覧・生成・取得・承認を提供する管理 UI 向け endpoint。
すべて Basic 認証配下 (配線は後段の ``_build_protected_router`` が親で付与する。
本ルータには ``require_basic_auth`` を再付与しない — 共有契約 (c) 準拠)。

エンドポイント:

- ``GET  /plans``                : cycle / status / limit で絞った一覧 (``{items, total}``)。
- ``POST /plans``                : planner LLM を起動して新規 ``Plan`` を生成 (201)。
- ``GET  /plans/{plan_id}``      : 単一 ``Plan`` の取得。
- ``POST /plans/{plan_id}/approve``: ``generated → approved`` の状態遷移 + audit。

設計方針:

- DB セッションは共有契約 (a) のとおり ``Depends(get_session)`` で受ける
  (commit は呼び出し側責務。本ルータは状態を確定する endpoint で明示 commit する)。
- planner の実体は ``domain.pipeline.planner.Planner`` (= ``PlanGenerator`` 別名層)。
  LLM provider は ``create_llm_provider(settings, session=...)`` で active provider を解決。
  これらは差し替え可能なモジュールレベル factory (``_build_planner``) 経由で呼ぶため、
  テストは実 LLM/GPU を起動せず provider/planner を mock できる。
- ドメイン例外 (``YmgError`` 派生 / ``LlmError``) は ``ErrorResponse`` 形 (category/
  message/detail) の HTTP エラーへ翻訳する。``QualityError`` (planner が辞書外ジャンル等を
  返した) は contract の 422 に対応させる。
- ``Plan`` の cycle は contract 上 ``daily|weekly`` だが、US1 内部契約の planner は daily
  のみを実装する。weekly は 422 (planner 未対応) を返し、誤った成功を避ける。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.domain.errors import ErrorCategory, YmgError, resolve_category
from ymg_backend.domain.pipeline.planner import Planner
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import Genre, Plan
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.llm.base import LlmError
from ymg_backend.llm.factory import create_llm_provider

router: Final = APIRouter(prefix="/plans", tags=["plans"])

# 一覧の上限 (DoS / プロンプト肥大を避ける安全弁。contract 既定 20)。
_DEFAULT_LIST_LIMIT: Final[int] = 20
_MAX_LIST_LIMIT: Final[int] = 100

# 承認可能な遷移元 (generated のみ approve 可。それ以外は 409)。
_APPROVABLE_FROM: Final[str] = "generated"

# 状態遷移先 (approve)。
_APPROVED_STATUS: Final[str] = "approved"

# JST (Asia/Tokyo, UTC+9)。approved_at は JST 基準で記録する (target_date が JST のため整合)。
_JST: Final[timezone] = timezone(timedelta(hours=9))

PlanCycle = Literal["daily", "weekly"]
PlanStatus = Literal["generated", "approved", "executing", "completed", "failed"]


# ---------------------------------------------------------------------------
# レスポンス / リクエストスキーマ (backend-api.yaml schemas.Plan に整合)
# ---------------------------------------------------------------------------
class PlanResponse(BaseModel):
    """``Plan`` の API 表現 (backend-api.yaml ``schemas.Plan``)。"""

    id: uuid.UUID
    cycle: str
    target_date: date | None = None
    target_week_start: date | None = None
    payload: dict[str, Any]
    rationale: str
    status: str
    llm_provider: str
    llm_model: str
    llm_prompt_version: str
    llm_cost_usd: float
    created_at: datetime
    approved_at: datetime | None = None


class PlanListResponse(BaseModel):
    """``GET /plans`` のレスポンス封筒 (``{items, total}``)。"""

    items: list[PlanResponse]
    total: int


class GeneratePlanRequest(BaseModel):
    """``POST /plans`` のリクエストボディ (cycle / target_date / force_regenerate)。"""

    cycle: PlanCycle
    target_date: date
    force_regenerate: bool = False


class ErrorBody(BaseModel):
    """``ErrorResponse`` 封筒 (backend-api.yaml ``schemas.ErrorResponse``)。"""

    category: str
    message: str
    detail: dict[str, Any] | None = None


def _to_response(plan: Plan) -> PlanResponse:
    """ORM ``Plan`` を ``PlanResponse`` に写像する (不変な新規オブジェクト生成)。"""
    return PlanResponse(
        id=plan.id,
        cycle=plan.cycle,
        target_date=plan.target_date,
        target_week_start=plan.target_week_start,
        payload=dict(plan.payload),
        rationale=plan.rationale,
        status=plan.status,
        llm_provider=plan.llm_provider,
        llm_model=plan.llm_model,
        llm_prompt_version=plan.llm_prompt_version,
        llm_cost_usd=float(plan.llm_cost_usd),
        created_at=plan.created_at,
        approved_at=plan.approved_at,
    )


def _error_detail(
    category: ErrorCategory,
    message: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``ErrorResponse`` 形の ``HTTPException.detail`` dict を組む。"""
    body = ErrorBody(category=category.value, message=message, detail=detail)
    return body.model_dump()


# ---------------------------------------------------------------------------
# planner 起動 (差し替え可能 factory)
# ---------------------------------------------------------------------------
async def _build_planner(settings: Settings, session: AsyncSession) -> Planner:
    """active provider を解決し ``Planner`` を構築する (テストで差し替え可能)。

    ``create_llm_provider`` は ``app_state`` の provider 切替 (ADR-0019) を反映するため
    ``session`` を渡す。本関数をモジュールレベルに置くことで、テストは実 LLM provider を
    起動せず ``_build_planner`` 自体を monkeypatch して planner を注入できる。
    """
    provider = await create_llm_provider(settings, session=session)
    return Planner(provider)


async def _load_enabled_genres(session: AsyncSession) -> list[str]:
    """``Genre.enabled=True`` のジャンル名一覧を取得する (planner の許可辞書)。"""
    stmt = select(Genre.name).where(Genre.enabled.is_(True)).order_by(Genre.name)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# GET /plans
# ---------------------------------------------------------------------------
@router.get("", response_model=PlanListResponse, summary="List plans (filter by cycle / status)")
async def list_plans(
    session: Annotated[AsyncSession, Depends(get_session)],
    cycle: Annotated[PlanCycle | None, Query()] = None,
    status_filter: Annotated[PlanStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIST_LIMIT)] = _DEFAULT_LIST_LIMIT,
) -> PlanListResponse:
    """cycle / status で絞った ``Plan`` 一覧を新しい順 (created_at desc) に返す。

    ``total`` はフィルタ適用後の総件数 (limit 前)。limit は表示件数の上限。
    """
    conditions = []
    if cycle is not None:
        conditions.append(Plan.cycle == cycle)
    if status_filter is not None:
        conditions.append(Plan.status == status_filter)

    total_stmt = select(func.count()).select_from(Plan)
    list_stmt = select(Plan).order_by(Plan.created_at.desc()).limit(limit)
    for cond in conditions:
        total_stmt = total_stmt.where(cond)
        list_stmt = list_stmt.where(cond)

    total = (await session.execute(total_stmt)).scalar_one()
    plans = (await session.execute(list_stmt)).scalars().all()
    return PlanListResponse(items=[_to_response(p) for p in plans], total=int(total))


# ---------------------------------------------------------------------------
# POST /plans
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=PlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new plan via improvement-plan LLM",
    responses={
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorBody},
    },
)
async def generate_plan(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[GeneratePlanRequest, Body()],
) -> PlanResponse:
    """planner LLM を起動して新規 ``Plan`` を生成し 201 で返す。

    重複ガード: 同 ``(cycle, target_date)`` の ``Plan`` が既存で ``force_regenerate=False``
    なら 409 (``force_regenerate=True`` で再生成を許可)。weekly cycle は US1 planner 未対応
    のため 422。planner が辞書外ジャンル等を返した品質低下 (``QualityError``) も 422。
    """
    if body.cycle == "weekly":
        # US1 内部契約の planner は daily のみ実装。weekly は未対応として明示的に拒否。
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_error_detail(
                ErrorCategory.RECOVERABLE,
                "weekly plan generation is not supported in this build",
                {"cycle": body.cycle},
            ),
        )

    existing = await _find_existing_daily_plan(session, body.target_date)
    if existing is not None and not body.force_regenerate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_error_detail(
                ErrorCategory.RECOVERABLE,
                "plan already exists for target_date (use force_regenerate=true)",
                {"plan_id": str(existing.id), "target_date": body.target_date.isoformat()},
            ),
        )

    allowed_genres = await _load_enabled_genres(session)
    planner = await _build_planner(settings, session)

    try:
        plan_row = await planner.create_daily_plan(
            session=session,
            target_date=body.target_date,
            allowed_genres=allowed_genres,
        )
    except YmgError as exc:
        # QualityError (辞書外ジャンル等) → 422、その他ドメイン例外 → 502 系へ翻訳。
        await session.rollback()
        raise _http_from_domain_error(exc) from exc
    except LlmError as exc:
        await session.rollback()
        raise _http_from_llm_error(exc) from exc

    await write_audit_log(
        session,
        action="plan_generated",
        target_type="plan",
        target_id=str(plan_row.id),
        payload={
            "cycle": "daily",
            "target_date": body.target_date.isoformat(),
            "force_regenerate": body.force_regenerate,
            "llm_provider": plan_row.llm_provider,
            "llm_cost_usd": float(plan_row.llm_cost_usd),
        },
    )
    await session.commit()
    await session.refresh(plan_row)
    logger.info(
        "plans.generate_plan created plan",
        plan_id=str(plan_row.id),
        target_date=body.target_date.isoformat(),
        regenerated=existing is not None,
    )
    return _to_response(plan_row)


# ---------------------------------------------------------------------------
# GET /plans/{plan_id}
# ---------------------------------------------------------------------------
@router.get(
    "/{plan_id}",
    response_model=PlanResponse,
    summary="Get a single plan",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
)
async def get_plan(
    session: Annotated[AsyncSession, Depends(get_session)],
    plan_id: uuid.UUID,
) -> PlanResponse:
    """単一の ``Plan`` を取得する (存在しなければ 404)。"""
    plan = await _get_plan_or_404(session, plan_id)
    return _to_response(plan)


# ---------------------------------------------------------------------------
# POST /plans/{plan_id}/approve
# ---------------------------------------------------------------------------
@router.post(
    "/{plan_id}/approve",
    response_model=PlanResponse,
    summary="Approve a plan -> unlocks daily cycle execution",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
    },
)
async def approve_plan(
    session: Annotated[AsyncSession, Depends(get_session)],
    plan_id: uuid.UUID,
) -> PlanResponse:
    """``generated → approved`` へ遷移し ``approved_at`` を記録、audit を残す。

    遷移元が ``generated`` 以外なら 409 (冪等な再承認も拒否し、状態機械を厳格に保つ)。
    commit は本 endpoint の責務 (状態確定点)。
    """
    plan = await _get_plan_or_404(session, plan_id)
    if plan.status != _APPROVABLE_FROM:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_error_detail(
                ErrorCategory.RECOVERABLE,
                f"plan must be in '{_APPROVABLE_FROM}' to approve (current: '{plan.status}')",
                {"plan_id": str(plan_id), "status": plan.status},
            ),
        )

    plan.status = _APPROVED_STATUS
    plan.approved_at = datetime.now(tz=_JST)
    await session.flush()
    await write_audit_log(
        session,
        action="plan_approved",
        target_type="plan",
        target_id=str(plan_id),
        payload={"cycle": plan.cycle},
    )
    await session.commit()
    await session.refresh(plan)
    logger.info("plans.approve_plan approved plan", plan_id=str(plan_id))
    return _to_response(plan)


# ---------------------------------------------------------------------------
# 内部ヘルパ
# ---------------------------------------------------------------------------
async def _get_plan_or_404(session: AsyncSession, plan_id: uuid.UUID) -> Plan:
    """``plan_id`` で ``Plan`` を取得する (無ければ 404 ``ErrorResponse``)。"""
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_error_detail(
                ErrorCategory.RECOVERABLE,
                "plan not found",
                {"plan_id": str(plan_id)},
            ),
        )
    return plan


async def _find_existing_daily_plan(session: AsyncSession, target_date: date) -> Plan | None:
    """同 ``target_date`` の daily ``Plan`` を 1 件取得する (重複ガード用)。"""
    stmt = (
        select(Plan)
        .where(Plan.cycle == "daily")
        .where(Plan.target_date == target_date)
        .order_by(Plan.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


def _http_from_domain_error(exc: YmgError) -> HTTPException:
    """``YmgError`` 派生を ``ErrorResponse`` 形の HTTP エラーに翻訳する。

    ``QUALITY`` (planner が辞書外ジャンル等を返した) は contract の 422、それ以外
    (recoverable / transient 等) は上流依存の失敗として 502 にマップする。
    """
    category = exc.category
    if category == ErrorCategory.QUALITY:
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        http_status = status.HTTP_502_BAD_GATEWAY
    return HTTPException(
        status_code=http_status,
        detail=_error_detail(category, exc.message, dict(exc.context) or None),
    )


def _http_from_llm_error(exc: LlmError) -> HTTPException:
    """provider の ``LlmError`` を ``ErrorResponse`` 形の HTTP エラーに翻訳する。

    ``FATAL`` (provider 設定不正等) は 500、その他 (transient / recoverable) は上流依存
    失敗として 502 にマップする。
    """
    category = resolve_category(exc)
    if category == ErrorCategory.FATAL:
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    else:
        http_status = status.HTTP_502_BAD_GATEWAY
    return HTTPException(
        status_code=http_status,
        detail=_error_detail(category, exc.message, {"retryable": exc.retryable}),
    )


__all__ = [
    "ErrorBody",
    "GeneratePlanRequest",
    "PlanListResponse",
    "PlanResponse",
    "router",
]
