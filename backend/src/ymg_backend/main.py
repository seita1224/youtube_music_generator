"""FastAPI app factory (ADR-0009 / ADR-0013 / ADR-0038)。

責務:

1. **app factory** (:func:`create_app`): ルータ登録 + 認証適用 + lifespan 構成を
   1 箇所に集約する。 ``uvicorn ymg_backend.main:app`` で起動できるよう、 モジュール
   末尾に :data:`app` を公開する。
2. **Basic 認証の全 endpoint 適用** (ADR-0013): ``/health`` 以外は
   :func:`~ymg_backend.core.security.require_basic_auth` を依存に持つ「認証付きルータ」
   配下にぶら下げる。 ``/health`` は認証不要 (backend-api.yaml ``security: []``)。
3. **RequestValidationError の機密 input 除去** (ADR-0038)。
4. **lifespan**: 起動時に DB 接続を検証 (``SELECT 1``。 失敗は :class:`FatalError`)、
   shutdown 時に DB エンジンを破棄する。 スケジューラの再開 (ADR-0044: reboot 後
   自動再開) は枠スケジューラ実装時にここへ配線する。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import text
from starlette.types import Lifespan

from ymg_backend.api.health import router as health_router
from ymg_backend.core.config import Settings, get_settings
from ymg_backend.core.logging import setup_logging
from ymg_backend.core.security import require_basic_auth
from ymg_backend.core.validation_errors import register_validation_exception_handler
from ymg_backend.domain.errors.errors import FatalError
from ymg_backend.infrastructure.db.session import dispose_engine, get_sessionmaker

_API_TITLE: Final[str] = "YMG Backend API"
_API_VERSION: Final[str] = "2.0.0"


async def _verify_db_connection() -> None:
    """起動時に ``SELECT 1`` で DB 接続を検証する。

    Raises:
        FatalError: DB へ接続できない場合 (ADR-0028: DB 接続不可は fatal)。
    """
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise FatalError(
            "Database connection failed at startup; check POSTGRES_* settings.",
            context={"component": "startup_db"},
            original=exc,
        ) from exc


def _build_lifespan(settings: Settings) -> Lifespan[FastAPI]:
    """``settings`` を束ねた lifespan コンテキストマネージャを生成する。"""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(level=settings.log_level)
        await _verify_db_connection()
        try:
            yield
        finally:
            await dispose_engine()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI アプリケーションを生成する (app factory)。

    ``/health`` 以外の全 endpoint に Basic 認証を適用する (ADR-0013)。 新しい
    認証付きルータは :func:`_build_protected_router` の配下に ``include_router``
    して登録する。

    Args:
        settings: 設定オブジェクト。 ``None`` の場合は ``get_settings()`` を使う。

    Returns:
        構成済みの :class:`FastAPI` アプリケーション。
    """
    resolved = settings if settings is not None else get_settings()

    # /docs / redoc / openapi.json は公開しない(LAN でもスキーマ列挙を避ける)。
    # 契約スキーマは specs/002-slot-centric-redesign/contracts/backend-api.yaml が正。
    app = FastAPI(
        title=_API_TITLE,
        version=_API_VERSION,
        lifespan=_build_lifespan(resolved),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # ADR-0038: RequestValidationError の機密 input (api_key 等) を応答・ログから除去。
    register_validation_exception_handler(app)

    # /health は認証不要 (backend-api.yaml security: [])。
    app.include_router(health_router)

    # /health 以外は Basic 認証必須。 業務ルータ (schedule / slots / system 等) は
    # この親ルータ配下に include する (子ルータ側には認証依存を再付与しない)。
    app.include_router(_build_protected_router())

    return app


def _build_protected_router() -> APIRouter:
    """Basic 認証を全 endpoint に適用する親ルータを返す (ADR-0013)。"""
    return APIRouter(dependencies=[Depends(require_basic_auth)])


app: Final[FastAPI] = create_app()
