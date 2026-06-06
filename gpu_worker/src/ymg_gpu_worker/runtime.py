"""worker のランタイム結線 (queue + runners + storage)。

FastAPI app の ``lifespan`` で 1 つ構築し、 ``app.state`` に置く。 API ルータは
``request.app.state.runtime`` 経由でアクセスする。 runner は同期 (torch がブロック
する) のため、 job handler 内で ``run_in_executor`` に逃がしてイベントループを
塞がない。
"""

from __future__ import annotations

import asyncio
from typing import Final

from ymg_gpu_worker.api.schemas import ImageGenerateRequest, MusicGenerateRequest
from ymg_gpu_worker.infrastructure.storage import StorageAdapter
from ymg_gpu_worker.jobs.queue import JobKind, JobQueue, JobRequest, JobResult
from ymg_gpu_worker.runners.acestep import AceStepRunner
from ymg_gpu_worker.runners.sdxl import SdxlRunner


class WorkerRuntime:
    """job キューと各ランナーを束ねるランタイム。"""

    __slots__ = ("_acestep", "_queue", "_sdxl", "_storage")

    def __init__(self, storage: StorageAdapter) -> None:
        self._storage: Final[StorageAdapter] = storage
        self._acestep: Final[AceStepRunner] = AceStepRunner(storage)
        self._sdxl: Final[SdxlRunner] = SdxlRunner(storage)
        self._queue: Final[JobQueue] = JobQueue(self._dispatch)

    @property
    def queue(self) -> JobQueue:
        """job キュー。"""
        return self._queue

    def models_loaded(self) -> list[str]:
        """ロード済みモデルの識別子一覧 (``/health`` 用)。"""
        loaded: list[str] = []
        if self._acestep.is_loaded:
            loaded.append(self._acestep.model_name)
        sdxl_model = self._sdxl.loaded_model
        if sdxl_model is not None:
            loaded.append(sdxl_model)
        return loaded

    def start(self) -> None:
        """job consumer を起動する。"""
        self._queue.start()

    async def stop(self) -> None:
        """job consumer を停止する。"""
        await self._queue.stop()

    async def _dispatch(self, kind: JobKind, request: JobRequest) -> JobResult:
        """job をランナーへ振り分ける。 同期処理は executor に逃がす。"""
        loop = asyncio.get_running_loop()
        if kind is JobKind.MUSIC:
            assert isinstance(request, MusicGenerateRequest)
            return await loop.run_in_executor(None, self._acestep.generate, request)
        assert isinstance(request, ImageGenerateRequest)
        return await loop.run_in_executor(None, self._sdxl.generate, request)
