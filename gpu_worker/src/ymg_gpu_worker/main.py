"""GPU worker FastAPI アプリケーション (T054)。

``/health`` (gpu_available / vram_free_mb / models_loaded) と生成 API
(``api.generate``) を束ねる。 backend ↔ worker は
``contracts/gpu-worker-api.yaml`` の契約で疎結合 (ADR-0031)。

エントリポイント: ``ymg_gpu_worker.main:app`` (Dockerfile / systemd 共通)。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI

from ymg_gpu_worker import __version__
from ymg_gpu_worker.api.generate import router as generate_router
from ymg_gpu_worker.api.schemas import HealthResponse
from ymg_gpu_worker.infrastructure.storage import StorageAdapter
from ymg_gpu_worker.runners.device import probe_gpu
from ymg_gpu_worker.runtime import WorkerRuntime

_STORAGE_BASE_URI_ENV: Final = "STORAGE_BASE_URI"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """ランタイム (queue + runners) を構築し、 job consumer を起動 / 停止する。"""
    base_uri = os.environ.get(_STORAGE_BASE_URI_ENV) or None
    storage = StorageAdapter(base_uri)
    runtime = WorkerRuntime(storage)
    app.state.runtime = runtime
    runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


def create_app() -> FastAPI:
    """FastAPI アプリを構築する (テストからも再利用可能)。"""
    application = FastAPI(
        title="YMG GPU Worker API",
        version=__version__,
        lifespan=lifespan,
    )

    @application.get(
        "/health", response_model=HealthResponse, summary="Liveness + GPU status"
    )
    def health() -> HealthResponse:
        """liveness + GPU 状態を返す。

        GPU が利用不可、 もしくは VRAM 取得不能でも 200 で ``degraded`` を返し、
        backend 側の死活監視が常に応答を受け取れるようにする。
        """
        gpu = probe_gpu()
        runtime: WorkerRuntime = application.state.runtime
        return HealthResponse(
            status="ok" if gpu.available else "degraded",
            gpu_available=gpu.available,
            vram_free_mb=gpu.vram_free_mb,
            vram_total_mb=gpu.vram_total_mb,
            models_loaded=runtime.models_loaded(),
            worker_version=__version__,
        )

    application.include_router(generate_router)
    return application


app = create_app()
