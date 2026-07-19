"""SDXL 背景画像生成ジョブの実行サービス (US1 / ADR-0031)。

1 投稿 (``Post``) につき背景画像 1 枚を GPU worker (SDXL) で生成する。``GpuJob``
(``job_type=image``) を 1 件投入し、``GET /jobs/{job_id}`` を ``poll_interval_sec`` 間隔で
ポーリングして ``succeeded`` を待ち、生成画像の ``output_uri`` を返す。サムネ合成
(``thumbnail_overlay``) はこの背景画像を入力に後段で行うため、ここでは
``Post.thumbnail_uri`` を書かない (背景画像 URI を返すのみ)。

設計方針 (共有契約 / ADR-0028):

- ステートレスな依存注入クラス。``__init__`` で ``GpuWorkerClient`` / ``StorageAdapter`` を
  受け取り、``submit_and_wait`` のみを公開する。``AsyncSession`` は引数で受け渡し、
  ``commit`` はオーケストレータ責務 (本サービスは ``flush`` まで)。
- GPU 投入は必ず ``GpuJob`` レコードを残す (``job_type`` / ``status`` / ``request_payload`` /
  ``output_uri`` / ``error_message`` / ``worker_endpoint``)。状態は worker の応答に合わせ
  ``queued → running → succeeded|failed`` を写像する。
- エラー分類は ADR-0028 の 5 区分を既存例外型で表現する。worker の ``failed`` は
  リクエスト固有の失敗として :class:`RecoverableError`、ポーリングのタイムアウトは
  一過性とみなし :class:`TransientError` を送出する。client 起因の通信失敗
  (``TransientError`` / ``RecoverableError``) はそのまま伝播する。
- 出力 URI は base_uri からの相対パス ``images/<post_id>/background.png`` を
  ``StorageAdapter.resolve_uri`` に渡して解決する (本番は base が
  ``file:///srv/ymg/outputs`` のため同配下へ、 テストは tmp 配下へ書かれる)。

外部依存 (実 GPU worker) は ``GpuWorkerClient`` (httpx) 経由でのみ触れ、テストでは
mock client を注入して実 HTTP を打たない。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from ymg_backend.core.logging import bind_context
from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.infrastructure.db.models import GpuJob
from ymg_backend.infrastructure.gpu_worker_client import (
    GpuWorkerClient,
    ImageGenerateRequest,
    JobStatus,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.domain.plans.schemas import DailyPost
    from ymg_backend.infrastructure.db.models import Post
    from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

# ポーリング既定値 (画像は数十秒〜数分で完了する想定のため timeout は 600 秒)。
_DEFAULT_POLL_INTERVAL_SEC: Final[float] = 5.0
_DEFAULT_TIMEOUT_SEC: Final[float] = 600.0

# ImageGenerateRequest.prompt は最小 10 文字 (worker 契約)。生成プロンプトが短い場合の
# 安全側パディングに用いる接尾辞 (視覚意図が極端に短いケースの境界保護)。
_PROMPT_MIN_LENGTH: Final[int] = 10
_PROMPT_PADDING: Final[str] = ", high quality album cover artwork"

# GpuJob.worker_endpoint (NOT NULL) を client から取得できない場合のフォールバック識別子。
_IMAGE_ENDPOINT_PATH: Final[str] = "/generate/image"

# worker のジョブ状態 (gpu_worker_client.JobStatus.status と一致)。
_STATUS_SUCCEEDED: Final[str] = "succeeded"
_STATUS_FAILED: Final[str] = "failed"


def _now() -> datetime:
    """timezone-aware な現在時刻 (UTC)。タイムスタンプ列の明示挿入に使う。"""
    return datetime.now(UTC)


def _resolve_worker_endpoint(client: GpuWorkerClient) -> str:
    """``GpuJob.worker_endpoint`` 用の worker URL を best-effort で解決する。

    ``GpuWorkerClient`` は base URL を公開プロパティとして持たない (``__slots__`` 内部値)
    ため、属性アクセスで取得を試み、取れない場合は正規パス識別子へフォールバックする。
    worker_endpoint は監査・障害切り分け用途であり、取得失敗で投入を止めない。
    """
    base_url = getattr(client, "_base_url", None)
    if isinstance(base_url, str) and base_url:
        return f"{base_url.rstrip('/')}{_IMAGE_ENDPOINT_PATH}"
    return _IMAGE_ENDPOINT_PATH


def _build_image_prompt(daily_post: DailyPost) -> str:
    """``DailyPost`` から SDXL 背景画像プロンプトを構築する (最小 10 文字を保証)。

    主軸は ``visual_direction`` (SDXL 用の意図テキスト、スキーマ上 min 10 文字)。
    ``thumbnail_directive`` があれば視覚補強として連結する (背景画像の方向付け)。
    境界保護として、万一プロンプトが最小長を下回る場合のみ汎用接尾辞でパディングする。
    """
    parts: list[str] = [daily_post.visual_direction.strip()]
    if daily_post.thumbnail_directive:
        directive = daily_post.thumbnail_directive.strip()
        if directive:
            parts.append(directive)
    prompt = ", ".join(part for part in parts if part)
    if len(prompt) < _PROMPT_MIN_LENGTH:
        prompt = f"{prompt}{_PROMPT_PADDING}"
    return prompt


class ImageJobRunner:
    """SDXL 背景画像生成ジョブの投入 + 完了待ちを担う依存注入サービス。

    Args:
        client: GPU worker への HTTP クライアント (``generate_image`` / ``get_job``)。
        storage: 出力 URI 解決に使うストレージアダプタ。
        poll_interval_sec: ``get_job`` ポーリング間隔 (秒)。既定 5.0。
        timeout_sec: ``succeeded`` を待つ最大秒数。超過で :class:`TransientError`。既定 600.0。

    Raises:
        ValueError: ``poll_interval_sec`` が負、または ``timeout_sec`` が非正の場合
            (``poll_interval_sec=0`` は即時ポーリングとして許可。music_jobs と整合)。
    """

    __slots__ = ("_client", "_poll_interval_sec", "_storage", "_timeout_sec")

    def __init__(
        self,
        client: GpuWorkerClient,
        storage: StorageAdapter,
        *,
        poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        if poll_interval_sec < 0:
            raise ValueError("poll_interval_sec must be >= 0")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be > 0")
        self._client: Final[GpuWorkerClient] = client
        self._storage: Final[StorageAdapter] = storage
        self._poll_interval_sec: Final[float] = poll_interval_sec
        self._timeout_sec: Final[float] = timeout_sec

    async def submit_and_wait(
        self,
        *,
        session: AsyncSession,
        post: Post,
        daily_post: DailyPost,
    ) -> str:
        """背景画像生成ジョブを 1 件投入し、完了を待って output_uri を返す。

        ``GpuJob`` を ``queued`` で永続化 → ``generate_image`` 投入 → ``running`` へ更新 →
        ``get_job`` ポーリング → ``succeeded`` で ``output_uri`` を確定して返す。投入・完了の
        各段階で ``GpuJob`` の状態列を更新し ``session.flush`` する (commit はオーケストレータ)。

        Args:
            session: 呼び出し側管理の ``AsyncSession`` (commit は呼び出し側)。
            post: 対象投稿。``post.id`` を出力 URI / ログ context / ``GpuJob`` 紐付けに使う。
            daily_post: 画像プロンプトの素材 (``visual_direction`` / ``thumbnail_directive``)。

        Returns:
            生成された背景画像の ``output_uri`` (``Post.thumbnail_uri`` の元画像)。

        Raises:
            RecoverableError: worker が ``failed`` を返した / 成功なのに ``output_uri`` 欠落。
            TransientError: ``timeout_sec`` 内に終端状態へ到達しなかった場合。
        """
        log = bind_context(genre=post.genre, step="image_generate", video_id=str(post.id))

        output_uri = self._storage.resolve_uri(f"images/{post.id}/background.png")
        prompt = _build_image_prompt(daily_post)
        request = ImageGenerateRequest(prompt=prompt, output_uri=output_uri)

        gpu_job = self._build_gpu_job(post=post, request=request)
        session.add(gpu_job)
        await session.flush()

        post.image_job_id = gpu_job.id

        log.info(
            "SDXL 画像ジョブを投入します",
            gpu_job_id=str(gpu_job.id),
            output_uri=output_uri,
        )
        accepted = await self._client.generate_image(request)

        gpu_job.status = "running"
        gpu_job.started_at = _now()
        await session.flush()

        final_status = await self._poll_until_terminal(job_id=accepted.job_id, log=log)
        await self._apply_terminal_status(session=session, gpu_job=gpu_job, status=final_status)

        resolved_uri = final_status.output_uri or output_uri
        log.info(
            "SDXL 画像ジョブが完了しました",
            gpu_job_id=str(gpu_job.id),
            output_uri=resolved_uri,
        )
        return resolved_uri

    def _build_gpu_job(self, *, post: Post, request: ImageGenerateRequest) -> GpuJob:
        """投入前の ``GpuJob`` レコード (status=queued) を構築する。"""
        return GpuJob(
            id=uuid.uuid4(),
            job_type="image",
            status="queued",
            request_payload=request.model_dump(mode="json", exclude_none=True),
            worker_endpoint=_resolve_worker_endpoint(self._client),
            created_at=_now(),
        )

    async def _poll_until_terminal(self, *, job_id: str, log: Any) -> JobStatus:
        """``get_job`` を ``poll_interval_sec`` 間隔で繰り返し、終端状態を返す。

        ``succeeded`` / ``failed`` で即座に返す。``timeout_sec`` 超過時は一過性とみなし
        :class:`TransientError` を送出する (ジョブ滞留はリトライで解消し得るため)。client
        起因の通信失敗 (``TransientError`` / ``RecoverableError``) はそのまま伝播する。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_sec
        while True:
            status = await self._client.get_job(job_id)
            if status.status in (_STATUS_SUCCEEDED, _STATUS_FAILED):
                return status
            if loop.time() >= deadline:
                raise TransientError(
                    "SDXL 画像ジョブが時間内に完了しませんでした",
                    context={
                        "job_id": job_id,
                        "timeout_sec": self._timeout_sec,
                        "last_status": status.status,
                    },
                )
            log.debug("SDXL 画像ジョブ進行中", job_id=job_id, status=status.status)
            await asyncio.sleep(self._poll_interval_sec)

    async def _apply_terminal_status(
        self,
        *,
        session: AsyncSession,
        gpu_job: GpuJob,
        status: JobStatus,
    ) -> None:
        """終端状態を ``GpuJob`` へ反映し、``failed`` / 出力欠落を例外へ写像する。

        Raises:
            RecoverableError: worker が ``failed`` を返した / ``succeeded`` だが
                ``output_uri`` が無い場合 (どちらもリクエスト固有で再投入が妥当)。
        """
        gpu_job.finished_at = _now()
        gpu_job.vram_peak_mb = status.vram_peak_mb
        gpu_job.duration_ms = status.duration_ms

        if status.status == _STATUS_FAILED:
            gpu_job.status = "failed"
            message = status.error.message if status.error else "unknown worker error"
            gpu_job.error_message = message
            await session.flush()
            raise RecoverableError(
                f"SDXL 画像ジョブが失敗しました: {message}",
                context={"job_id": status.job_id},
            )

        if not status.output_uri:
            gpu_job.status = "failed"
            gpu_job.error_message = "succeeded but output_uri is missing"
            await session.flush()
            raise RecoverableError(
                "SDXL 画像ジョブは succeeded ですが output_uri が空です",
                context={"job_id": status.job_id},
            )

        gpu_job.status = "succeeded"
        gpu_job.output_uri = status.output_uri
        await session.flush()


__all__ = ["ImageJobRunner"]
