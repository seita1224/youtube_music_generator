"""in-process な GPU job キューと状態管理 (T056)。

``asyncio.Queue`` で投入順を制御し、 ``dict`` で job ごとの状態
(``queued`` / ``running`` / ``succeeded`` / ``failed``) を保持する。 GPU は
逐次実行が前提のため、 単一の consumer タスクが 1 件ずつ取り出して実行する。

設計方針:

- 状態 (``JobRecord``) は不変。 更新は ``dataclasses.replace`` で新インスタンスへ
  差し替え、 dict のエントリを丸ごと入れ替える (in-place 変更を避ける)。
- 実際の生成処理 (ACE-Step / SDXL) は ``JobHandler`` として外部から注入する。
  キュー自体は torch / diffusers に依存しない。
- 例外は捕捉して ``failed`` 状態に落とす。 worker ループは継続する。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from ymg_gpu_worker.api.schemas import (
    ErrorDetail,
    ImageGenerateRequest,
    JobState,
    MusicGenerateRequest,
)


class JobKind(StrEnum):
    """job の種別。"""

    MUSIC = "music"
    IMAGE = "image"


JobRequest = MusicGenerateRequest | ImageGenerateRequest


@dataclass(frozen=True, slots=True)
class JobResult:
    """生成成功時の結果 (runner が返す)。"""

    output_uri: str
    duration_ms: int
    vram_peak_mb: int | None = None


# runner シグネチャ: (kind, request) -> JobResult。 async / sync 双方を許容するため
# Awaitable を返す Callable として扱う (sync runner は run_in_executor 経由で包む)。
JobHandler = Callable[[JobKind, JobRequest], Awaitable[JobResult]]


@dataclass(frozen=True, slots=True)
class JobRecord:
    """単一 job の不変な状態スナップショット。"""

    job_id: UUID
    kind: JobKind
    request: JobRequest
    status: JobState = "queued"
    output_uri: str | None = None
    duration_ms: int | None = None
    vram_peak_mb: int | None = None
    error: ErrorDetail | None = None
    created_at: float = field(default_factory=time.monotonic)


_DEFAULT_MAXSIZE: Final = 0  # 0 = 無制限 (asyncio.Queue 既定)


class JobQueue:
    """asyncio.Queue + dict による in-process job ストア。

    1 つの consumer タスク (``start`` で起動) が job を逐次実行する。
    ``submit`` は即座に ``JobRecord`` (``queued``) を返し、 実行は非同期に進む。
    """

    __slots__ = ("_handler", "_queue", "_records", "_worker")

    def __init__(self, handler: JobHandler, *, maxsize: int = _DEFAULT_MAXSIZE) -> None:
        self._handler: Final[JobHandler] = handler
        self._queue: Final[asyncio.Queue[UUID]] = asyncio.Queue(maxsize=maxsize)
        self._records: Final[dict[UUID, JobRecord]] = {}
        self._worker: asyncio.Task[None] | None = None

    def submit(self, kind: JobKind, request: JobRequest) -> JobRecord:
        """job を登録してキューへ投入する。 ``queued`` 状態の record を返す。"""
        job_id = uuid4()
        record = JobRecord(job_id=job_id, kind=kind, request=request)
        self._records[job_id] = record
        self._queue.put_nowait(job_id)
        return record

    def get(self, job_id: UUID) -> JobRecord | None:
        """job_id に対応する最新の record を返す。 未知なら ``None``。"""
        return self._records.get(job_id)

    def start(self) -> None:
        """consumer タスクを起動する (冪等)。"""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="ymg-gpu-job-consumer")

    async def stop(self) -> None:
        """consumer タスクを停止し、 完了を待つ。"""
        worker = self._worker
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        finally:
            self._worker = None

    async def _run(self) -> None:
        """キューから job を 1 件ずつ取り出して実行する consumer ループ。"""
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(job_id)
            finally:
                self._queue.task_done()

    async def _process(self, job_id: UUID) -> None:
        """単一 job を実行し、 成否に応じて状態を更新する。"""
        record = self._records.get(job_id)
        if record is None:
            return
        self._records[job_id] = replace(record, status="running")
        try:
            result = await self._handler(record.kind, record.request)
        except Exception as exc:  # noqa: BLE001 — runner の例外を failed 状態へ集約する
            self._records[job_id] = replace(
                record,
                status="failed",
                error=ErrorDetail(
                    category="fatal",
                    message=str(exc) or exc.__class__.__name__,
                    retryable=False,
                ),
            )
            return
        self._records[job_id] = replace(
            record,
            status="succeeded",
            output_uri=result.output_uri,
            duration_ms=result.duration_ms,
            vram_peak_mb=result.vram_peak_mb,
        )
