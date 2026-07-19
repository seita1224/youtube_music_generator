"""ダミー GPU 生成経路 (settings + runners.dummy + runtime 分岐) の単体テスト。

検証対象 (契約: ``GPU_WORKER_DUMMY=1`` のとき torch 無しで succeeded):

- ``dummy_mode_enabled`` が ``GPU_WORKER_DUMMY`` を正しく解釈する。
- ``WorkerRuntime`` がダミーモード時に ``JobQueue`` 経由で music / image を
  ``status=succeeded`` にし、 ``output_uri`` へ実ファイル (有効な WAV / PNG) を
  書き出す。
- ``models_loaded`` が ``["dummy"]`` を返す (``/health`` 用)。

外部依存なし: torch / CUDA / 実モデルは一切 import しない。 ストレージは
``file://`` (一時ディレクトリ) を使い、 async consumer は ``asyncio.run`` で
駆動する (pytest-asyncio に依存しない)。
"""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path
from uuid import UUID

import pytest

from ymg_gpu_worker.api.schemas import ImageGenerateRequest, MusicGenerateRequest
from ymg_gpu_worker.infrastructure.storage import StorageAdapter
from ymg_gpu_worker.jobs.queue import JobKind, JobRecord, JobRequest
from ymg_gpu_worker.runtime import WorkerRuntime
from ymg_gpu_worker.settings import DUMMY_MODE_ENV, dummy_mode_enabled

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("  ON ", True),
        ("yes", True),
        ("0", False),
        ("", False),
        ("false", False),
        (None, False),
    ],
)
def test_dummy_mode_enabled_parses_env(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    """``GPU_WORKER_DUMMY`` の真偽解釈が想定どおりであること。"""
    if value is None:
        monkeypatch.delenv(DUMMY_MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(DUMMY_MODE_ENV, value)
    assert dummy_mode_enabled() is expected


async def _run_to_completion(
    runtime: WorkerRuntime, kind: JobKind, request: JobRequest
) -> JobRecord:
    """1 件投入して consumer を回し、 終端状態の ``JobRecord`` を返す。"""
    runtime.start()
    try:
        record = runtime.queue.submit(kind, request)
        job_id: UUID = record.job_id
        for _ in range(200):  # 最大 ~2s 待つ (ダミーは即時完了する想定)
            current = runtime.queue.get(job_id)
            assert current is not None
            if current.status in ("succeeded", "failed"):
                return current
            await asyncio.sleep(0.01)
        pytest.fail("dummy job did not reach a terminal state in time")
    finally:
        await runtime.stop()


def test_dummy_music_job_succeeds_and_writes_wav(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ダミーモードで music job が succeeded になり、 有効な WAV が書き出される。"""
    monkeypatch.setenv(DUMMY_MODE_ENV, "1")
    base_uri = tmp_path.as_uri() + "/"
    storage = StorageAdapter(base_uri)
    runtime = WorkerRuntime(storage)
    assert runtime.models_loaded() == ["dummy"]

    out = tmp_path / "audio" / "track.wav"
    request = MusicGenerateRequest(
        prompt="lofi chill beats for studying",
        duration_sec=30,
        output_uri=out.as_uri(),
    )
    record = asyncio.run(_run_to_completion(runtime, JobKind.MUSIC, request))

    assert record.status == "succeeded"
    assert record.error is None
    assert record.output_uri == out.as_uri()
    assert out.exists()

    with wave.open(io.BytesIO(out.read_bytes()), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() > 0


def test_dummy_image_job_succeeds_and_writes_png(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ダミーモードで image job が succeeded になり、 有効な PNG が書き出される。"""
    monkeypatch.setenv(DUMMY_MODE_ENV, "1")
    base_uri = tmp_path.as_uri() + "/"
    storage = StorageAdapter(base_uri)
    runtime = WorkerRuntime(storage)

    out = tmp_path / "image" / "thumb.png"
    request = ImageGenerateRequest(
        prompt="a serene mountain lake at dawn",
        width=64,
        height=48,
        output_uri=out.as_uri(),
    )
    record = asyncio.run(_run_to_completion(runtime, JobKind.IMAGE, request))

    assert record.status == "succeeded"
    assert record.error is None
    assert out.exists()
    assert out.read_bytes().startswith(_PNG_SIGNATURE)


def test_dummy_output_is_deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """同一リクエストは同一バイト列を生成する (決定論)。"""
    monkeypatch.setenv(DUMMY_MODE_ENV, "1")
    storage = StorageAdapter(tmp_path.as_uri() + "/")
    runtime = WorkerRuntime(storage)

    out_a = tmp_path / "a.png"
    out_b = tmp_path / "b.png"

    def _request(output_uri: str) -> ImageGenerateRequest:
        return ImageGenerateRequest(
            prompt="identical deterministic prompt text",
            width=32,
            height=32,
            seed=7,
            output_uri=output_uri,
        )

    # 同一ループ内で 2 件を処理する (JobQueue の asyncio.Queue は最初の
    # イベントループに束縛されるため、 asyncio.run を分けない)。
    async def _drive_both() -> None:
        await _run_to_completion(runtime, JobKind.IMAGE, _request(out_a.as_uri()))
        await _run_to_completion(runtime, JobKind.IMAGE, _request(out_b.as_uri()))

    asyncio.run(_drive_both())
    assert out_a.read_bytes() == out_b.read_bytes()
