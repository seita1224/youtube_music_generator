"""ImageJobRunner (domain/pipeline/image_jobs.py) の単体テスト (US1)。

契約 (共有契約書 / image_jobs):

- ``ImageJobRunner(client, storage, *, poll_interval_sec=5.0, timeout_sec=600.0)``。
- ``submit_and_wait(*, session, post, daily_post) -> str`` — ``GpuJob``(job_type=image) を
  1 件投入 → ``succeeded`` までポーリング → 背景画像 ``output_uri`` を返す。
- worker ``failed`` / 出力欠落は :class:`RecoverableError`、タイムアウトは
  :class:`TransientError`。``Post.thumbnail_uri`` は書かない (背景画像 URI を返すのみ)。

外部依存 (GPU worker) は ``GpuWorkerClient`` を mock し実 HTTP を打たない。``AsyncSession``
は add / flush を記録する軽量 fake で代替する (実 DB なし)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.domain.pipeline.image_jobs import ImageJobRunner
from ymg_backend.domain.plans.schemas import DailyPost
from ymg_backend.infrastructure.db.models import GpuJob, Post
from ymg_backend.infrastructure.gpu_worker_client import (
    ErrorDetail,
    ImageGenerateRequest,
    JobAccepted,
    JobStatus,
)

# pytest の ``asyncio_mode = "auto"`` (backend/pyproject.toml) により async テストは
# マークなしで認識される。sync テスト (constructor 検証) も同居するため module 全体への
# asyncio マークは付けない。


# --- テストダブル -----------------------------------------------------------------


@dataclass
class FakeSession:
    """add / flush のみを記録する軽量 ``AsyncSession`` 代替。"""

    added: list[Any] = field(default_factory=list)
    flush_count: int = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1


class FakeStorage:
    """``resolve_uri`` のみを提供する ``StorageAdapter`` 代替。

    実 adapter と同様に、相対パスを base_uri 配下へ解決する (protocol 付きは素通し)。
    runner は相対パスを渡す契約なので、 base を前置して絶対 URI を返す。
    """

    _BASE = "file:///srv/ymg/outputs/"

    def resolve_uri(self, path_or_uri: str) -> str:
        if "://" in path_or_uri:
            return path_or_uri
        return self._BASE + path_or_uri.lstrip("/")


class FakeGpuClient:
    """``generate_image`` / ``get_job`` を台本どおりに返す ``GpuWorkerClient`` 代替。"""

    _base_url = "http://worker.test:8001"

    def __init__(self, *, job_id: str, statuses: list[JobStatus]) -> None:
        self._job_id = job_id
        self._statuses = list(statuses)
        self.generate_calls: list[ImageGenerateRequest] = []
        self.get_job_calls: list[str] = []

    async def generate_image(self, request: ImageGenerateRequest) -> JobAccepted:
        self.generate_calls.append(request)
        return JobAccepted(job_id=self._job_id, status="queued")

    async def get_job(self, job_id: str) -> JobStatus:
        self.get_job_calls.append(job_id)
        return self._statuses.pop(0)


# --- フィクスチャ -----------------------------------------------------------------


def _make_post() -> Post:
    return Post(
        id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        position=0,
        genre="lo-fi-hip-hop",
        payload={},
        status="generating",
    )


def _make_daily_post(*, thumbnail_directive: str | None = None) -> DailyPost:
    return DailyPost(
        genre="lo-fi-hip-hop",
        mood="calm",
        visual_direction="a rainy neon city window at night, lo-fi aesthetic",
        title_directive="lo-fi study beats",
        description_directive="relaxing lo-fi hip hop beats to study and chill to",
        thumbnail_directive=thumbnail_directive,
    )


def _succeeded(job_id: str, output_uri: str) -> JobStatus:
    return JobStatus(job_id=job_id, status="succeeded", output_uri=output_uri, duration_ms=4200)


# --- テスト本体 -------------------------------------------------------------------


async def test_submit_and_wait_returns_output_uri_on_success() -> None:
    """succeeded で worker の output_uri を返し、GpuJob を succeeded に更新する。"""
    job_id = "job-img-1"
    out_uri = "file:///srv/ymg/outputs/images/p/background.png"
    client = FakeGpuClient(
        job_id=job_id,
        statuses=[
            JobStatus(job_id=job_id, status="running"),
            _succeeded(job_id, out_uri),
        ],
    )
    runner = ImageJobRunner(client, FakeStorage(), poll_interval_sec=0.0001, timeout_sec=5.0)  # type: ignore[arg-type]
    session = FakeSession()
    post = _make_post()

    result = await runner.submit_and_wait(
        session=session,  # type: ignore[arg-type]
        post=post,
        daily_post=_make_daily_post(),
    )

    assert result == out_uri
    gpu_jobs = [obj for obj in session.added if isinstance(obj, GpuJob)]
    assert len(gpu_jobs) == 1
    gpu_job = gpu_jobs[0]
    assert gpu_job.job_type == "image"
    assert gpu_job.status == "succeeded"
    assert gpu_job.output_uri == out_uri
    assert gpu_job.worker_endpoint == "http://worker.test:8001/generate/image"
    # Post には image_job_id のみ紐付け、thumbnail_uri は書かない (背景画像 URI を返すのみ)。
    assert post.image_job_id == gpu_job.id
    assert post.thumbnail_uri is None


@pytest.mark.fr("FR-004")
async def test_submit_builds_prompt_from_visual_direction() -> None:
    """FR-004: ImageGenerateRequest.prompt は visual_direction を主軸に構築される。"""
    job_id = "job-img-2"
    out_uri = "file:///srv/ymg/outputs/images/p/background.png"
    client = FakeGpuClient(job_id=job_id, statuses=[_succeeded(job_id, out_uri)])
    runner = ImageJobRunner(client, FakeStorage(), poll_interval_sec=0.0001)  # type: ignore[arg-type]

    await runner.submit_and_wait(
        session=FakeSession(),  # type: ignore[arg-type]
        post=_make_post(),
        daily_post=_make_daily_post(thumbnail_directive="bold neon title space"),
    )

    assert len(client.generate_calls) == 1
    sent = client.generate_calls[0]
    assert "rainy neon city window" in sent.prompt
    assert "bold neon title space" in sent.prompt
    assert len(sent.prompt) >= 10
    assert sent.model == "juggernaut-xl-v10"


async def test_worker_failed_raises_recoverable() -> None:
    """worker が failed を返したら RecoverableError、GpuJob は failed + error_message。"""
    job_id = "job-img-3"
    client = FakeGpuClient(
        job_id=job_id,
        statuses=[
            JobStatus(
                job_id=job_id,
                status="failed",
                error=ErrorDetail(category="recoverable", message="OOM on SDXL"),
            )
        ],
    )
    runner = ImageJobRunner(client, FakeStorage(), poll_interval_sec=0.0001)  # type: ignore[arg-type]
    session = FakeSession()

    with pytest.raises(RecoverableError, match="OOM on SDXL"):
        await runner.submit_and_wait(
            session=session,  # type: ignore[arg-type]
            post=_make_post(),
            daily_post=_make_daily_post(),
        )

    gpu_job = next(obj for obj in session.added if isinstance(obj, GpuJob))
    assert gpu_job.status == "failed"
    assert gpu_job.error_message == "OOM on SDXL"


async def test_succeeded_without_output_uri_raises_recoverable() -> None:
    """succeeded でも output_uri が欠落していれば RecoverableError。"""
    job_id = "job-img-4"
    client = FakeGpuClient(
        job_id=job_id,
        statuses=[JobStatus(job_id=job_id, status="succeeded", output_uri=None)],
    )
    runner = ImageJobRunner(client, FakeStorage(), poll_interval_sec=0.0001)  # type: ignore[arg-type]

    with pytest.raises(RecoverableError, match="output_uri"):
        await runner.submit_and_wait(
            session=FakeSession(),  # type: ignore[arg-type]
            post=_make_post(),
            daily_post=_make_daily_post(),
        )


async def test_timeout_raises_transient() -> None:
    """timeout_sec 内に終端状態へ到達しなければ TransientError。"""
    job_id = "job-img-5"
    # 常に running を返し続ける (終端に到達しない)。
    client = FakeGpuClient(
        job_id=job_id,
        statuses=[JobStatus(job_id=job_id, status="running") for _ in range(50)],
    )
    # 終端到達前に確実に deadline を超えるよう、極小だが正の timeout を渡す。
    runner = ImageJobRunner(client, FakeStorage(), poll_interval_sec=0.0001, timeout_sec=1e-6)  # type: ignore[arg-type]

    with pytest.raises(TransientError, match="時間内に完了"):
        await runner.submit_and_wait(
            session=FakeSession(),  # type: ignore[arg-type]
            post=_make_post(),
            daily_post=_make_daily_post(),
        )


def test_constructor_rejects_non_positive_intervals() -> None:
    """poll_interval_sec が負 / timeout_sec が非正なら ValueError (0 は許可、music_jobs と整合)。"""
    storage = FakeStorage()
    client = FakeGpuClient(job_id="x", statuses=[])
    with pytest.raises(ValueError, match="poll_interval_sec"):
        ImageJobRunner(client, storage, poll_interval_sec=-1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout_sec"):
        ImageJobRunner(client, storage, timeout_sec=-1.0)  # type: ignore[arg-type]
