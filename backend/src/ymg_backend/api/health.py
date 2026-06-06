"""``GET /health`` endpoint (T053, contracts/backend-api.yaml HealthResponse)。

backend の liveness と主要依存の状態を返す。 認証不要 (backend-api.yaml の
``/health`` は ``security: []``)。

返却フィールド (HealthResponse スキーマと整合):

- ``status``: ``ok`` / ``degraded`` (依存のどれかが degraded/unreachable なら degraded)
- ``db``: ``ok`` / ``degraded`` (``SELECT 1`` の成否)
- ``gpu_worker``: ``ok`` / ``unreachable`` (GPU worker ``/health`` への HTTP 到達性)
- ``scheduler_enabled``: ``app_state.scheduler_enabled`` (ADR-0031, 既定 false)
- ``dryrun_enabled``: ``app_state.dryrun_enabled`` (ADR-0035, 既定 true)
- ``llm_provider``: factory が env / ``app_state`` から解決した active provider 名

設計方針:

- ORM model 層 (T021) に依存しないよう、 ``app_state`` 参照は SQLAlchemy Core の
  軽量 Table 定義を本モジュールに閉じて持つ (``llm/factory.py`` / ``infrastructure/audit.py``
  と同じ疎結合方針)。
- GPU worker の HTTP client (T060) は未実装のため、 本モジュール内で ``httpx`` を
  直接叩いて到達性のみ判定する (timeout 短め)。 client 実装が入ったらそちらへ委譲する。
- health は監視用途で副作用なし。 依存呼び出しの例外はすべて握りつぶし、
  該当依存を degraded / unreachable に倒す (health 自体は常に 200 を返す)。
"""

from __future__ import annotations

import contextlib
import json
from typing import Annotated, Final, Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Column, MetaData, String, Table, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.infrastructure.db.session import get_session
from ymg_backend.llm.factory import resolve_provider_config

router: Final = APIRouter(tags=["health"])

# GPU worker /health への到達性チェック timeout (秒)。 health は即応すべきなので短め。
_GPU_HEALTH_TIMEOUT_SEC: Final[float] = 2.0

# app_state のキー (data-model.md §app_state seed / 001_initial.py `_seed_app_state`)。
_KEY_SCHEDULER_ENABLED: Final[str] = "scheduler_enabled"
_KEY_DRYRUN_ENABLED: Final[str] = "dryrun_enabled"

# app_state.value (JSONB) への疎結合参照用 Core Table (ORM 層非依存)。
_metadata: Final[MetaData] = MetaData()


def _build_app_state_table() -> Table:
    """``app_state`` を参照する Core Table を構築する (JSONB は遅延 import)。"""
    from sqlalchemy.dialects.postgresql import JSONB as _JSONB

    return Table(
        "app_state",
        _metadata,
        Column("key", String, primary_key=True, nullable=False),
        Column("value", _JSONB, nullable=False),
    )


app_state_table: Final[Table] = _build_app_state_table()


class HealthResponse(BaseModel):
    """``/health`` のレスポンス (backend-api.yaml HealthResponse)。"""

    status: Literal["ok", "degraded"]
    db: Literal["ok", "degraded"]
    gpu_worker: Literal["ok", "unreachable"]
    scheduler_enabled: bool
    dryrun_enabled: bool
    llm_provider: str


def _decode_app_state_bool(raw: object) -> bool | None:
    """app_state.value (JSONB) を bool に正規化する。

    asyncpg は JSONB を Python オブジェクトにデコード済みで返す場合と、 生 JSON
    文字列で返す場合がある。 文字列なら一段 JSON デコードを試み、 最終的に bool の
    場合のみ採用する (それ以外は ``None``)。
    """
    value: object = raw
    if isinstance(value, str):
        with contextlib.suppress(ValueError, TypeError):
            value = json.loads(value)
    return value if isinstance(value, bool) else None


async def _read_flags(session: AsyncSession) -> tuple[bool, bool]:
    """``app_state`` から scheduler_enabled / dryrun_enabled を読む。

    取得不能・型不一致のキーは安全側の既定 (scheduler=false / dryrun=true,
    ADR-0031 / ADR-0035) を採用する。

    Returns:
        ``(scheduler_enabled, dryrun_enabled)`` のタプル。
    """
    stmt = select(app_state_table.c.key, app_state_table.c.value).where(
        app_state_table.c.key.in_((_KEY_SCHEDULER_ENABLED, _KEY_DRYRUN_ENABLED))
    )
    rows = (await session.execute(stmt)).all()
    flags: dict[str, bool] = {}
    for row in rows:
        decoded = _decode_app_state_bool(row.value)
        if decoded is not None:
            flags[row.key] = decoded
    return (
        flags.get(_KEY_SCHEDULER_ENABLED, False),
        flags.get(_KEY_DRYRUN_ENABLED, True),
    )


async def _check_db(session: AsyncSession) -> Literal["ok", "degraded"]:
    """``SELECT 1`` で DB 到達性を判定する (例外時は degraded)。"""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        # health は依存例外を握りつぶし degraded に倒す (副作用なしの監視用途)。
        return "degraded"
    return "ok"


async def _check_gpu_worker(base_url: str) -> Literal["ok", "unreachable"]:
    """GPU worker ``/health`` への HTTP 到達性を判定する (例外/非2xx は unreachable)。"""
    url = f"{base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=_GPU_HEALTH_TIMEOUT_SEC) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return "unreachable"
    return "ok" if response.is_success else "unreachable"


async def _resolve_llm_provider(settings: Settings, session: AsyncSession) -> str:
    """active LLM provider 名を解決する (例外時は env の provider 名を返す)。"""
    try:
        config = await resolve_provider_config(settings, session=session)
    except Exception:
        # health は解決失敗でも env の provider 名を提示する (常に 200)。
        return settings.llm_provider
    return config.provider


@router.get(
    "/health", response_model=HealthResponse, summary="Backend liveness + dependency status"
)
async def get_health(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HealthResponse:
    """backend の liveness と依存状態を返す (認証不要)。

    依存呼び出しは独立しており、 いずれかが degraded/unreachable でも 200 を返す
    (全体 ``status`` のみ degraded に倒す)。 監視・管理 UI の health 表示用。
    """
    db_status = await _check_db(session)
    gpu_worker_status = await _check_gpu_worker(settings.gpu_worker_base_url)
    scheduler_enabled, dryrun_enabled = await _read_flags(session)
    llm_provider = await _resolve_llm_provider(settings, session)

    overall: Literal["ok", "degraded"] = (
        "ok" if db_status == "ok" and gpu_worker_status == "ok" else "degraded"
    )

    return HealthResponse(
        status=overall,
        db=db_status,
        gpu_worker=gpu_worker_status,
        scheduler_enabled=scheduler_enabled,
        dryrun_enabled=dryrun_enabled,
        llm_provider=llm_provider,
    )
