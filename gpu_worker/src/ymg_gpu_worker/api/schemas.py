"""GPU worker API の request / response スキーマ。

``contracts/gpu-worker-api.yaml`` (OpenAPI 3.1) と 1:1 で対応する Pydantic
モデル。 制約 (``minLength`` / ``minimum`` / ``maximum`` / ``default``) も
契約に合わせて再現し、 境界での入力検証を担保する。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

HealthStatus = Literal["ok", "degraded"]
JobState = Literal["queued", "running", "succeeded", "failed"]
ErrorCategory = Literal["transient", "recoverable", "fatal", "compliance", "quality"]


class HealthResponse(BaseModel):
    """``GET /health`` のレスポンス (HealthResponse)。"""

    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    gpu_available: bool
    vram_free_mb: int
    vram_total_mb: int | None = None
    models_loaded: list[str] = Field(default_factory=list)
    worker_version: str | None = None


class MusicGenerateRequest(BaseModel):
    """``POST /generate/music`` のリクエスト (MusicGenerateRequest)。"""

    model_config = ConfigDict(frozen=True)

    prompt: str = Field(min_length=8)
    negative_prompt: str | None = None
    duration_sec: int = Field(ge=30, le=600)
    bpm: int | None = Field(default=None, ge=50, le=200)
    music_key: str | None = None
    seed: int | None = None
    output_uri: str


class ImageGenerateRequest(BaseModel):
    """``POST /generate/image`` のリクエスト (ImageGenerateRequest)。"""

    model_config = ConfigDict(frozen=True)

    prompt: str = Field(min_length=10)
    negative_prompt: str | None = None
    model: str = Field(default="juggernaut-xl-v10")
    width: int = Field(default=1280)
    height: int = Field(default=720)
    steps: int = Field(default=30, ge=10, le=80)
    cfg_scale: float = Field(default=6.0)
    seed: int | None = None
    output_uri: str


class JobAccepted(BaseModel):
    """``202 Accepted`` のレスポンス (JobAccepted)。"""

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    status: Literal["queued"]
    estimated_duration_sec: int | None = None


class ErrorDetail(BaseModel):
    """エラー詳細 (ErrorDetail)。 4xx レスポンスや job 失敗時に使う。"""

    model_config = ConfigDict(frozen=True)

    category: ErrorCategory
    message: str
    retryable: bool | None = None


class JobStatus(BaseModel):
    """``GET /jobs/{job_id}`` のレスポンス (JobStatus)。"""

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    status: JobState
    output_uri: str | None = None
    duration_ms: int | None = None
    vram_peak_mb: int | None = None
    error: ErrorDetail | None = None
