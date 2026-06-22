"""MusicJobRunner (domain/pipeline/music_jobs.py) の単体テスト (US1)。

契約 (US1 内部契約書):

- ``MusicJobRunner(client, storage, *, poll_interval_sec=5.0, timeout_sec=1800.0)``。
- ``submit_and_wait(session, post, daily_post, track_count=6) -> list[AudioTrack]`` —
  ``track_count`` 件の音楽ジョブを投入・ポーリングし、 成功トラックを永続化して返す。
- ``regenerate_track(session, post, track, daily_post) -> AudioTrack`` — 1 トラック再生成。

外部依存の扱い (起動禁止):

- GPU worker: respx で HTTP を mock し、 実 ``GpuWorkerClient`` をそのまま使う (投入〜ポーリング
  の実コードを通す)。 ``POST /generate/music`` → 202 JobAccepted、 ``GET /jobs/{id}`` →
  succeeded + output_uri (リクエスト本文の output_uri を反射)。
- DB: ``session.add`` / ``session.flush`` のみ呼ばれる契約のため、 実 DB ではなく記録専用の
  軽量 fake セッションを用いる (Postgres 固有型に依存しない真の単体テスト)。
- storage: ``StorageAdapter`` (base なし、 URI 透過解決) をそのまま使う。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.domain.pipeline.music_jobs import MusicJobRunner
from ymg_backend.domain.plans.schemas import DailyPost
from ymg_backend.infrastructure.db.models import AudioTrack, GpuJob, Post
from ymg_backend.infrastructure.gpu_worker_client import GpuWorkerClient
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

pytestmark = pytest.mark.asyncio

_GPU_BASE_URL = "http://gpu-worker.test"
_GENRE = "lo-fi hip-hop"


class _FakeSession:
    """``add`` / ``flush`` だけを記録する軽量セッション (DB なし)。

    music_jobs は ``session.add`` と ``await session.flush()`` しか呼ばないため、 これで
    永続化呼び出しを観測できる。 ``commit`` はオーケストレータ責務なので存在しない。
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flush_count = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1


def _as_session(session: _FakeSession) -> AsyncSession:
    """fake セッションを ``AsyncSession`` として渡す (music_jobs は add/flush のみ使用)。"""
    return cast("AsyncSession", session)


def _daily_post() -> DailyPost:
    return DailyPost(
        genre=_GENRE,
        mood="calm and warm late-night study vibe",
        bpm_range=(70, 90),
        visual_direction="dim lamplight over an open notebook and coffee",
        title_directive="lofi study beats",
        description_directive="relaxing lo-fi hip hop for focus and study sessions",
    )


def _post() -> Post:
    return Post(id=uuid.uuid4(), plan_id=uuid.uuid4(), position=0, genre=_GENRE, payload={})


def _register_success_mock(router: respx.MockRouter) -> dict[str, str]:
    """投入は 202、 ポーリングは即 succeeded。 output_uri はリクエスト本文を反射する。"""
    output_by_job: dict[str, str] = {}

    def _accept(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        job_id = str(uuid.uuid4())
        output_by_job[job_id] = payload["output_uri"]
        return httpx.Response(202, json={"job_id": job_id, "status": "queued"})

    def _status(request: httpx.Request) -> httpx.Response:
        job_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "job_id": job_id,
                "status": "succeeded",
                "output_uri": output_by_job.get(job_id, "file:///tmp/x.wav"),
                "duration_ms": 900,
                "vram_peak_mb": 7000,
            },
        )

    router.post(f"{_GPU_BASE_URL}/generate/music").mock(side_effect=_accept)
    router.get(url__regex=rf"{_GPU_BASE_URL}/jobs/.+").mock(side_effect=_status)
    return output_by_job


@pytest.mark.fr("FR-003")
async def test_submit_and_wait_creates_six_tracks_and_jobs() -> None:
    """FR-003: 6 トラック投入 → 全 succeeded で AudioTrack 6 件 + GpuJob 6 件を永続化する。"""
    session = _FakeSession()
    storage = StorageAdapter()
    post = _post()
    with respx.mock(assert_all_called=False) as router:
        _register_success_mock(router)
        async with GpuWorkerClient(_GPU_BASE_URL) as client:
            runner = MusicJobRunner(client, storage, poll_interval_sec=0.0, timeout_sec=5.0)
            tracks = await runner.submit_and_wait(
                session=_as_session(session), post=post, daily_post=_daily_post()
            )

    assert len(tracks) == 6
    # position は 0..5 (data-model.md の CHECK 制約に整合)。
    assert sorted(t.position for t in tracks) == [0, 1, 2, 3, 4, 5]
    assert all(t.acoustid_status == "not_checked" for t in tracks)
    assert all(t.audio_uri.endswith(".wav") for t in tracks)
    assert all(t.duration_sec > 0 for t in tracks)
    # bpm_range (70,90) の中央値 80 が代表 BPM として採用される。
    assert all(t.bpm == 80 for t in tracks)

    jobs = [o for o in session.added if isinstance(o, GpuJob)]
    persisted_tracks = [o for o in session.added if isinstance(o, AudioTrack)]
    assert len(jobs) == 6
    assert len(persisted_tracks) == 6
    assert all(j.job_type == "music" for j in jobs)
    assert all(j.status == "succeeded" for j in jobs)
    assert all(j.worker_endpoint for j in jobs)
    # 代表ジョブが post.music_job_id に記録される。
    assert post.music_job_id == jobs[0].id


async def test_submit_and_wait_failed_job_raises_transient() -> None:
    """worker が failed (カテゴリ未申告) を返すと TransientError を送出する。"""
    session = _FakeSession()
    post = _post()

    def _accept(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"job_id": str(uuid.uuid4()), "status": "queued"})

    def _status(request: httpx.Request) -> httpx.Response:
        job_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "job_id": job_id,
                "status": "failed",
                "error": {"category": "transient", "message": "ACE-Step OOM"},
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{_GPU_BASE_URL}/generate/music").mock(side_effect=_accept)
        router.get(url__regex=rf"{_GPU_BASE_URL}/jobs/.+").mock(side_effect=_status)
        async with GpuWorkerClient(_GPU_BASE_URL) as client:
            runner = MusicJobRunner(client, StorageAdapter(), poll_interval_sec=0.0)
            with pytest.raises(TransientError):
                await runner.submit_and_wait(
                    session=_as_session(session), post=post, daily_post=_daily_post()
                )

    # 失敗ジョブも GpuJob として記録され、 status=failed + error_message が入る。
    jobs = [o for o in session.added if isinstance(o, GpuJob)]
    assert jobs and jobs[-1].status == "failed"
    assert jobs[-1].error_message == "ACE-Step OOM"


@pytest.mark.fr("FR-087")
async def test_regenerate_track_resets_acoustid_and_increments_count() -> None:
    """FR-087: 再生成で audio_uri 差し替え・acoustid_status を not_checked に戻し count を +1 する。"""
    session = _FakeSession()
    post = _post()
    track = AudioTrack(
        id=uuid.uuid4(),
        post_id=post.id,
        position=2,
        audio_uri="file:///srv/ymg/outputs/music/old/track_2.wav",
        duration_sec=180,
        bpm=80,
        acoustid_status="hit",
        acoustid_response={"matched": True},
        fingerprint_hash="deadbeef",
        regenerated_count=0,
    )
    with respx.mock(assert_all_called=False) as router:
        _register_success_mock(router)
        async with GpuWorkerClient(_GPU_BASE_URL) as client:
            runner = MusicJobRunner(client, StorageAdapter(), poll_interval_sec=0.0)
            updated = await runner.regenerate_track(
                session=_as_session(session), post=post, track=track, daily_post=_daily_post()
            )

    assert updated is track
    assert updated.acoustid_status == "not_checked"
    assert updated.acoustid_response is None
    assert updated.fingerprint_hash is None
    assert updated.regenerated_count == 1
    assert "old" not in updated.audio_uri


async def test_regenerate_track_over_limit_raises_recoverable() -> None:
    """再生成上限到達済みのトラックは RecoverableError を送出する (HTTP は打たない)。"""
    session = _FakeSession()
    post = _post()
    track = AudioTrack(
        id=uuid.uuid4(),
        post_id=post.id,
        position=0,
        audio_uri="file:///srv/ymg/outputs/music/x/track_0.wav",
        duration_sec=180,
        acoustid_status="hit",
        regenerated_count=3,
    )
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{_GPU_BASE_URL}/generate/music").mock(
            return_value=httpx.Response(202, json={"job_id": "x", "status": "queued"})
        )
        async with GpuWorkerClient(_GPU_BASE_URL) as client:
            runner = MusicJobRunner(client, StorageAdapter(), poll_interval_sec=0.0)
            with pytest.raises(RecoverableError):
                await runner.regenerate_track(
                    session=_as_session(session), post=post, track=track, daily_post=_daily_post()
                )
        assert route.call_count == 0


async def test_invalid_track_count_rejected() -> None:
    """track_count が範囲外 (0 / 7) なら ValueError で即時拒否する。"""
    async with GpuWorkerClient(_GPU_BASE_URL) as client:
        runner = MusicJobRunner(client, StorageAdapter(), poll_interval_sec=0.0)
        with pytest.raises(ValueError):
            await runner.submit_and_wait(
                session=_as_session(_FakeSession()),
                post=_post(),
                daily_post=_daily_post(),
                track_count=0,
            )
        with pytest.raises(ValueError):
            await runner.submit_and_wait(
                session=_as_session(_FakeSession()),
                post=_post(),
                daily_post=_daily_post(),
                track_count=7,
            )
