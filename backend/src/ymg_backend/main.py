"""FastAPI app factory (T052, ADR-0009 / ADR-0013 / ADR-0031)。

責務:

1. **app factory** (:func:`create_app`): ルータ登録 + 認証適用 + lifespan 構成を
   1 箇所に集約する。 ``uvicorn ymg_backend.main:app`` で起動できるよう、 モジュール
   末尾に :data:`app` を公開する。
2. **Basic 認証の全 endpoint 適用** (ADR-0013): ``/health`` 以外は
   :func:`~ymg_backend.core.security.require_basic_auth` を依存に持つ「認証付きルータ」
   配下にぶら下げる。 ``/health`` は認証不要 (backend-api.yaml ``security: []``) なので
   認証なしルータとして登録する。
3. **lifespan** (ADR-0031): 起動時に
   - DB 接続を検証 (``SELECT 1``。 失敗は :class:`FatalError`、 DB 不可はサイクル停止)
   - scheduler 起動準備 (instance 構築のみ。 ``add_job`` はしない)
   - ``app_state.scheduler_enabled`` を確認 (reboot 後は **false 起動が既定**、 手動 enable)
   shutdown 時に scheduler を停止し DB エンジンを破棄する。

設計方針:

- 副作用を持つ依存 (engine / scheduler) は ``app.state`` に保持し、 lifespan で
  ライフサイクルを閉じる (不変オブジェクトのみモジュールレベルに置く)。
- scheduler は ``app_state.scheduler_enabled=true`` のときのみ ``start()`` する。
  job 登録 (日次 cron 等) は US1 の scheduler 実装 (T087) の責務で、 ここでは
  「起動準備」までに留める (ADR-0031: false 起動が既定)。
- ``app_state`` 参照は ORM 層 (T021) に依存せず、 ``api/health.py`` の Core Table を
  再利用して疎結合を保つ。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, Depends, FastAPI
from loguru import logger
from sqlalchemy import select, text
from starlette.types import Lifespan

from ymg_backend.api.analytics import router as analytics_router
from ymg_backend.api.dryrun import router as dryrun_router
from ymg_backend.api.genres import router as genres_router
from ymg_backend.api.health import app_state_table
from ymg_backend.api.health import router as health_router
from ymg_backend.api.plans import router as plans_router
from ymg_backend.api.posts import router as posts_router
from ymg_backend.api.scheduler import router as scheduler_router
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.logging import setup_logging
from ymg_backend.core.security import require_basic_auth
from ymg_backend.domain.errors.errors import FatalError
from ymg_backend.infrastructure.db.session import dispose_engine, get_sessionmaker

_API_TITLE: Final[str] = "YMG Backend API"
_API_VERSION: Final[str] = "1.0.0"

# app_state のキー (data-model.md §app_state seed / 001_initial.py `_seed_app_state`)。
_KEY_SCHEDULER_ENABLED: Final[str] = "scheduler_enabled"


async def _verify_db_connection() -> None:
    """起動時に ``SELECT 1`` で DB 接続を検証する (ADR-0031)。

    Raises:
        FatalError: DB へ接続できない場合 (ADR-0028: DB 接続不可は fatal、
            サイクル全体停止カテゴリ)。
    """
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        # 種別を問わず DB 不達は fatal に正規化する (ADR-0028)。
        raise FatalError(
            "Database connection failed at startup; check POSTGRES_* settings.",
            context={"component": "startup_db"},
            original=exc,
        ) from exc


async def _read_scheduler_enabled() -> bool:
    """``app_state.scheduler_enabled`` を読む (取得不能/型不一致は false, ADR-0031)。"""
    sessionmaker = get_sessionmaker()
    stmt = select(app_state_table.c.value).where(app_state_table.c.key == _KEY_SCHEDULER_ENABLED)
    async with sessionmaker() as session:
        raw = (await session.execute(stmt)).scalar_one_or_none()
    value: object = raw
    if isinstance(value, str):
        with contextlib.suppress(ValueError, TypeError):
            value = json.loads(value)
    return value if isinstance(value, bool) else False


def _build_scheduler() -> AsyncIOScheduler:
    """scheduler instance を構築する (起動準備のみ、 job 登録はしない, ADR-0031)。"""
    return AsyncIOScheduler(timezone="Asia/Tokyo")


def _build_lifespan(settings: Settings) -> Lifespan[FastAPI]:
    """``settings`` を束ねた lifespan コンテキストマネージャを生成する。"""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(level=settings.log_level)
        await _verify_db_connection()

        # scheduler 起動準備: instance のみ構築。 job 登録は T087 の責務。
        scheduler = _build_scheduler()
        app.state.scheduler = scheduler
        # NOTE: ``SchedulerService`` (job 出し入れ) の注入は別途の合成ルート (composition
        # root) タスクが担う。 ``DailyCycleOrchestrator`` の組み立てに planner/music/
        # uploader 等 10 サービスの構築を要し、 本配線タスク (router include) の範囲外。
        # 未注入でも ``PUT /scheduler`` は ``getattr(app.state, "scheduler_service", None)``
        # で best-effort に degrade する (フラグ永続化 + audit は実行、 job 操作のみ skip)。
        # 注入時は ``app.state.scheduler_service = SchedulerService(scheduler, cycle_runner=…)``。

        # ADR-0031: reboot 後は false 起動が既定。 enabled のときだけ start する。
        scheduler_enabled = await _read_scheduler_enabled()
        if scheduler_enabled:
            scheduler.start()
            logger.bind(component="lifespan").info("scheduler started (scheduler_enabled=true)")
        else:
            logger.bind(component="lifespan").info(
                "scheduler not started (scheduler_enabled=false, ADR-0031)"
            )

        try:
            yield
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            await dispose_engine()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI アプリケーションを生成する (app factory)。

    ``/health`` 以外の全 endpoint に Basic 認証を適用する (ADR-0013)。 新しい
    認証付きルータは :func:`_build_protected_router` 経由で束ねた親ルータに
    ``include_router`` して登録する。

    Args:
        settings: 設定オブジェクト。 ``None`` の場合は ``get_settings()`` を使う。

    Returns:
        構成済みの :class:`FastAPI` アプリケーション。
    """
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(
        title=_API_TITLE,
        version=_API_VERSION,
        lifespan=_build_lifespan(resolved),
    )

    # /health は認証不要 (backend-api.yaml security: [])。
    app.include_router(health_router)

    # /health 以外は Basic 認証必須。 親ルータに認証依存を付け、 後続の保護対象
    # ルータ (plans / posts / scheduler 等, T076 以降) はこの配下に include する。
    protected = _build_protected_router()
    app.include_router(protected)

    return app


def _build_protected_router() -> APIRouter:
    """Basic 認証を全 endpoint に適用する親ルータを返す (ADR-0013)。

    ``dependencies=[Depends(require_basic_auth)]`` を親に付けることで、 配下の
    全ルータ・全 endpoint に認証が効く。 業務ルータ (plans / posts / scheduler /
    dryrun / genres / analytics) はここに ``router.include_router(...)`` で追加する。
    子ルータ側には認証依存を再付与しない (共有契約 (c): 認証は親が付与)。
    """
    router = APIRouter(dependencies=[Depends(require_basic_auth)])
    router.include_router(plans_router)
    router.include_router(posts_router)
    router.include_router(scheduler_router)
    router.include_router(dryrun_router)
    router.include_router(genres_router)
    router.include_router(analytics_router)
    return router


app: Final[FastAPI] = create_app()
