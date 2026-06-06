"""生成 API ルータ (T055)。

``contracts/gpu-worker-api.yaml`` の ``POST /generate/music`` /
``POST /generate/image`` / ``GET /jobs/{job_id}`` を実装する。 リクエストは
``JobQueue`` へ投入し、 ``202 Accepted`` + ``job_id`` を即時返す。 進捗は
``GET /jobs/{job_id}`` でポーリングする。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ymg_gpu_worker.api.schemas import (
    ErrorDetail,
    ImageGenerateRequest,
    JobAccepted,
    JobStatus,
    MusicGenerateRequest,
)
from ymg_gpu_worker.jobs.queue import JobKind, JobRecord
from ymg_gpu_worker.runtime import WorkerRuntime

router = APIRouter()


def get_runtime(request: Request) -> WorkerRuntime:
    """app.state からランタイムを取得する FastAPI 依存。"""
    runtime: WorkerRuntime = request.app.state.runtime
    return runtime


RuntimeDep = Annotated[WorkerRuntime, Depends(get_runtime)]


def _to_accepted(record: JobRecord) -> JobAccepted:
    """``JobRecord`` を ``JobAccepted`` レスポンスへ変換する。"""
    return JobAccepted(job_id=record.job_id, status="queued")


def _to_status(record: JobRecord) -> JobStatus:
    """``JobRecord`` を ``JobStatus`` レスポンスへ変換する。"""
    return JobStatus(
        job_id=record.job_id,
        status=record.status,
        output_uri=record.output_uri,
        duration_ms=record.duration_ms,
        vram_peak_mb=record.vram_peak_mb,
        error=record.error,
    )


@router.post(
    "/generate/music",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAccepted,
    summary="Submit a music generation job (ACE-Step)",
)
def generate_music(payload: MusicGenerateRequest, runtime: RuntimeDep) -> JobAccepted:
    """音楽生成 job をキューへ投入する。"""
    record = runtime.queue.submit(JobKind.MUSIC, payload)
    return _to_accepted(record)


@router.post(
    "/generate/image",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAccepted,
    summary="Submit an image generation job (SDXL)",
)
def generate_image(payload: ImageGenerateRequest, runtime: RuntimeDep) -> JobAccepted:
    """画像生成 job をキューへ投入する。"""
    record = runtime.queue.submit(JobKind.IMAGE, payload)
    return _to_accepted(record)


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatus,
    summary="Poll job status",
)
def get_job(job_id: UUID, runtime: RuntimeDep) -> JobStatus:
    """job の状態を返す。 未知の job_id は ``404``。"""
    record = runtime.queue.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorDetail(category="fatal", message="job not found").model_dump(),
        )
    return _to_status(record)
