"""GPU worker HTTP クライアント (ADR-0031 / contract: gpu-worker-api.yaml)。

backend は GPU worker (ACE-Step 音楽 + SDXL 画像) を直接 import せず、本クライアント
経由の HTTP API でのみ呼び出す (ADR-0031 「backend → GPU worker は HTTP のみ」)。worker の
場所は ``GPU_WORKER_BASE_URL`` (core.config.Settings.gpu_worker_base_url) で切り替えるため、
本クライアントは base URL 以外に worker 実装へ依存しない。

エンドポイント (gpu-worker-api.yaml):

- ``GET  /health``           — liveness + GPU 状態
- ``POST /generate/music``   — ACE-Step ジョブ投入 (202 Accepted)
- ``POST /generate/image``   — SDXL ジョブ投入 (202 Accepted)
- ``GET  /jobs/{job_id}``    — ジョブ状態ポーリング

リクエスト / レスポンスは contract の schema に対応する Pydantic v2 frozen モデルで
境界検証する。HTTP / ネットワーク障害は ADR-0028 のエラー分類に写像する:

- 接続失敗・タイムアウト・5xx → :class:`TransientError` (同一ジョブ内で最大 3 回リトライ)
- 4xx (worker からの拒否) → :class:`RecoverableError` (該当ジョブのみスキップ)
- ジョブ未検出 (404) → :class:`RecoverableError`

リトライは exponential backoff (1s→2s→4s, ADR-0028 TransientError) で最大 3 試行。
``client`` を外部注入できるようにし、テストでは mock transport / client を渡せる。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ymg_backend.domain.errors.errors import RecoverableError, TransientError

# --- HTTP / リトライ設定 (ADR-0028) ------------------------------------------------
# TransientError は同一ジョブ内で最大 3 回・exponential backoff (1s→2s→4s) でリトライ。
_MAX_ATTEMPTS: Final[int] = 3
_BACKOFF_BASE_SEC: Final[float] = 1.0

# 既定タイムアウト (秒)。ジョブ投入 / ポーリングは即時応答 (202 / 200) を前提とするため
# 短め。長時間 GPU 推論そのものは worker 側の非同期ジョブで進む (本クライアントは待たない)。
_DEFAULT_TIMEOUT_SEC: Final[float] = 30.0

# リトライ対象とみなす HTTP ステータス (5xx 全般を一過性とみなす)。
_RETRYABLE_STATUS_MIN: Final[int] = 500

GpuJobType = Literal["music", "image"]
GpuJobStatus = Literal["queued", "running", "succeeded", "failed"]
ErrorCategoryLiteral = Literal["transient", "recoverable", "fatal", "compliance", "quality"]


# --- contract schema 対応の Pydantic モデル --------------------------------------


class HealthResponse(BaseModel):
    """``GET /health`` のレスポンス (gpu-worker-api.yaml HealthResponse)。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    gpu_available: bool
    vram_free_mb: int
    vram_total_mb: int | None = None
    models_loaded: list[str] = Field(default_factory=list)
    worker_version: str | None = None


class MusicGenerateRequest(BaseModel):
    """``POST /generate/music`` のリクエスト (MusicGenerateRequest)。"""

    model_config = ConfigDict(frozen=True)

    prompt: str = Field(min_length=8)
    duration_sec: int = Field(ge=30, le=600)
    output_uri: str
    negative_prompt: str | None = None
    bpm: int | None = Field(default=None, ge=50, le=200)
    music_key: str | None = None
    seed: int | None = None


class ImageGenerateRequest(BaseModel):
    """``POST /generate/image`` のリクエスト (ImageGenerateRequest)。"""

    model_config = ConfigDict(frozen=True)

    prompt: str = Field(min_length=10)
    output_uri: str
    negative_prompt: str | None = None
    model: str = "juggernaut-xl-v10"
    width: int = 1280
    height: int = 720
    steps: int = Field(default=30, ge=10, le=80)
    cfg_scale: float = 6.0
    seed: int | None = None


class JobAccepted(BaseModel):
    """ジョブ投入受理 (202) のレスポンス (JobAccepted)。"""

    model_config = ConfigDict(frozen=True)

    job_id: str
    status: Literal["queued"]
    estimated_duration_sec: int | None = None


class ErrorDetail(BaseModel):
    """worker のエラー詳細 (ErrorDetail)。"""

    model_config = ConfigDict(frozen=True)

    category: ErrorCategoryLiteral
    message: str
    retryable: bool | None = None


class JobStatus(BaseModel):
    """``GET /jobs/{job_id}`` のレスポンス (JobStatus)。"""

    model_config = ConfigDict(frozen=True)

    job_id: str
    status: GpuJobStatus
    output_uri: str | None = None
    duration_ms: int | None = None
    vram_peak_mb: int | None = None
    error: ErrorDetail | None = None


# --- レスポンス検証ヘルパ ----------------------------------------------------------


def _raise_for_http_status(response: httpx.Response, *, context: Mapping[str, Any]) -> None:
    """HTTP ステータスをエラー分類へ写像する (ADR-0028)。

    5xx は :class:`TransientError` (リトライ可)、それ以外の 4xx は worker からの拒否として
    :class:`RecoverableError` に写像する。worker が ``ErrorDetail`` 本文を返す場合はその
    message を採用する。2xx (期待ステータスは呼び出し側が個別判定) はここでは触れない。
    """
    status_code = response.status_code
    if status_code < 400:
        return

    detail_message = _extract_error_message(response)
    full_context = {**context, "status_code": status_code}
    if status_code >= _RETRYABLE_STATUS_MIN:
        raise TransientError(
            f"GPU worker が {status_code} を返しました: {detail_message}",
            context=full_context,
        )
    raise RecoverableError(
        f"GPU worker がリクエストを拒否しました ({status_code}): {detail_message}",
        context=full_context,
    )


def _extract_error_message(response: httpx.Response) -> str:
    """エラーレスポンス本文から ``ErrorDetail.message`` を取り出す。

    本文が ``ErrorDetail`` として解釈できない場合は生テキスト (先頭 200 文字) を返す。
    例外送出はせず、エラーメッセージ組み立てに最善努力で寄与する。
    """
    try:
        detail = ErrorDetail.model_validate(response.json())
    except (ValueError, httpx.DecodingError):
        return response.text[:200]
    return detail.message


def _parse_json[T: BaseModel](
    response: httpx.Response,
    model: type[T],
    *,
    context: Mapping[str, Any],
) -> T:
    """成功レスポンス本文を ``model`` で検証する。

    JSON デコード失敗・schema 不整合は worker 契約違反として :class:`RecoverableError` に
    写像する (リトライしても同じ本文が返るため一過性ではない)。
    """
    try:
        payload = response.json()
    except ValueError as exc:
        raise RecoverableError(
            f"GPU worker のレスポンスが JSON ではありません: {exc}",
            context=dict(context),
            original=exc,
        ) from exc
    try:
        return model.model_validate(payload)
    except ValueError as exc:
        raise RecoverableError(
            f"GPU worker のレスポンスが {model.__name__} に整合しません: {exc}",
            context=dict(context),
            original=exc,
        ) from exc


class GpuWorkerClient:
    """GPU worker (ADR-0031) への薄い ``httpx.AsyncClient`` ラッパ。

    Args:
        base_url: worker の base URL (``GPU_WORKER_BASE_URL``)。例: ``http://127.0.0.1:8001``。
        timeout_sec: 各 HTTP 呼び出しのタイムアウト秒。既定 30 秒。
        max_attempts: 一過性失敗時の最大試行回数 (初回含む)。既定 3 (ADR-0028)。
        client: 注入する ``httpx.AsyncClient`` (テスト用)。省略時は ``base_url`` から生成し、
            その場合は所有権を本インスタンスが持つ (``aclose`` でクローズ)。

    本クライアントは async コンテキストマネージャとして利用でき、終了時に内部 client を
    クローズする (外部注入された client は呼び出し側の所有とみなしクローズしない)。
    """

    __slots__ = ("_base_url", "_client", "_max_attempts", "_owns_client")

    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        max_attempts: int = _MAX_ATTEMPTS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._base_url: Final[str] = base_url.rstrip("/")
        self._max_attempts: Final[int] = max_attempts
        self._owns_client: Final[bool] = client is None
        self._client: Final[httpx.AsyncClient] = (
            client
            if client is not None
            else httpx.AsyncClient(base_url=self._base_url, timeout=timeout_sec)
        )

    async def __aenter__(self) -> GpuWorkerClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """内部生成した ``httpx.AsyncClient`` をクローズする (外部注入時は何もしない)。"""
        if self._owns_client:
            await self._client.aclose()

    async def health(self) -> HealthResponse:
        """``GET /health`` を呼び出し worker の liveness + GPU 状態を返す。

        Raises:
            TransientError: 接続失敗・タイムアウト・5xx の場合 (リトライ後)。
            RecoverableError: 4xx またはレスポンス契約違反の場合。
        """
        response = await self._request("GET", "/health", context={"op": "health"})
        return _parse_json(response, HealthResponse, context={"op": "health"})

    async def is_healthy(self) -> bool:
        """worker が到達可能かつ ``status == "ok"`` かを bool で返す (例外を送出しない)。

        health チェック用途。到達不能・degraded・契約違反はすべて ``False``。
        """
        try:
            health = await self.health()
        except (TransientError, RecoverableError):
            return False
        return health.status == "ok"

    async def generate_music(self, request: MusicGenerateRequest) -> JobAccepted:
        """``POST /generate/music`` で ACE-Step 音楽生成ジョブを投入する。

        Returns:
            投入受理された ``JobAccepted`` (status == "queued")。
        """
        return await self._submit_job("/generate/music", request, context={"op": "generate_music"})

    async def generate_image(self, request: ImageGenerateRequest) -> JobAccepted:
        """``POST /generate/image`` で SDXL 画像生成ジョブを投入する。

        Returns:
            投入受理された ``JobAccepted`` (status == "queued")。
        """
        return await self._submit_job("/generate/image", request, context={"op": "generate_image"})

    async def get_job(self, job_id: str) -> JobStatus:
        """``GET /jobs/{job_id}`` でジョブ状態をポーリングする。

        Args:
            job_id: 投入時に払い出された UUID 文字列。空文字は不正入力として拒否する。

        Raises:
            ValueError: ``job_id`` が空の場合 (境界での入力検証)。
            TransientError: 接続失敗・タイムアウト・5xx の場合 (リトライ後)。
            RecoverableError: 404 (未検出) またはレスポンス契約違反の場合。
        """
        if not job_id:
            raise ValueError("job_id must be a non-empty string")
        context = {"op": "get_job", "job_id": job_id}
        response = await self._request("GET", f"/jobs/{job_id}", context=context)
        return _parse_json(response, JobStatus, context=context)

    async def _submit_job(
        self,
        path: str,
        request: BaseModel,
        *,
        context: Mapping[str, Any],
    ) -> JobAccepted:
        """ジョブ投入 (POST) 共通処理。リクエストを JSON 化し受理レスポンスを検証する。"""
        payload = request.model_dump(mode="json", exclude_none=True)
        response = await self._request("POST", path, json=payload, context=context)
        return _parse_json(response, JobAccepted, context=context)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        context: Mapping[str, Any],
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """HTTP 呼び出しを exponential backoff 付きでリトライ実行する (ADR-0028)。

        ``TransientError`` (接続/タイムアウト/5xx) のみリトライし、最大 ``max_attempts`` 試行。
        最終試行も失敗した場合はその ``TransientError`` を送出する。``RecoverableError`` は
        リトライせず即時送出する。
        """
        last_error = TransientError(
            f"GPU worker への {method} {path} が試行されませんでした",
            context=dict(context),
        )
        for attempt in range(self._max_attempts):
            attempt_context = {**context, "attempt": attempt + 1}
            try:
                response = await self._client.request(method, path, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = TransientError(
                    f"GPU worker への {method} {path} が通信失敗しました: {exc}",
                    context=attempt_context,
                    original=exc,
                )
            else:
                try:
                    _raise_for_http_status(response, context=attempt_context)
                except TransientError as exc:
                    last_error = exc
                else:
                    return response

            if attempt + 1 < self._max_attempts:
                await asyncio.sleep(_BACKOFF_BASE_SEC * (2**attempt))

        raise last_error
