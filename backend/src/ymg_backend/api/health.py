"""``GET /health`` — liveness + DB 到達性。

認証不要 (backend-api.yaml ``security: []``)。 依存状態の詳細
(gpu_worker / system_state / autonomy_level / llm_provider) は
`specs/002-slot-centric-redesign/contracts/backend-api.yaml` の
``HealthResponse`` に従って各コンポーネント実装時に拡張する。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.infrastructure.db.session import get_session

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """``GET /health`` の応答。"""

    status: Literal["ok", "degraded"]
    db: bool


@router.get("/health")
async def get_health(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HealthResponse:
    """backend の liveness と DB 到達性を返す。"""
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return HealthResponse(status="ok" if db_ok else "degraded", db=db_ok)
