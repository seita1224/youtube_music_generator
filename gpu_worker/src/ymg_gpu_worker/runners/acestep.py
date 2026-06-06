"""ACE-Step 1.5 ランナー (T057)。

ローダ + 1 リクエストごとの音楽生成 (prompt / duration_sec / bpm / seed)。

重要: torch / ACE-Step パイプラインは **生成関数の内部で遅延 import** する。
ローカル開発機に torch / ACE-Step 未インストールでも、 このモジュールの import
自体は成功する (ADR-0031)。 モデル重みは ``YMG_MODELS_DIR/acestep`` に配置する
前提 (quickstart §5)。
"""

from __future__ import annotations

import time
from typing import Any, Final

from ymg_gpu_worker.api.schemas import MusicGenerateRequest
from ymg_gpu_worker.infrastructure.storage import StorageAdapter
from ymg_gpu_worker.jobs.queue import JobResult
from ymg_gpu_worker.runners.device import models_dir, peak_vram_mb

_MODEL_SUBDIR: Final = "acestep"
_MODEL_NAME: Final = "acestep-1.5"
_SAMPLE_RATE: Final = 44100


class AceStepRunner:
    """ACE-Step 1.5 パイプラインの薄いラッパ。

    パイプラインは初回 ``generate`` 時に遅延ロードし、 以後は再利用する
    (VRAM 上に常駐させ、 ロード時間を償却する)。
    """

    __slots__ = ("_pipeline", "_storage")

    def __init__(self, storage: StorageAdapter) -> None:
        self._storage: Final[StorageAdapter] = storage
        self._pipeline: Any | None = None

    @property
    def model_name(self) -> str:
        """models_loaded 表示用のモデル識別子。"""
        return _MODEL_NAME

    @property
    def is_loaded(self) -> bool:
        """パイプラインがロード済みか。"""
        return self._pipeline is not None

    def _load(self) -> Any:
        """ACE-Step パイプラインを遅延ロードする。

        torch / ACE-Step は関数内で import する (モジュール先頭では import しない)。
        """
        if self._pipeline is not None:
            return self._pipeline

        import torch  # 遅延 import
        from acestep.pipeline import AceStepPipeline  # type: ignore[import-not-found]

        checkpoint = str(models_dir() / _MODEL_SUBDIR)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        pipeline = AceStepPipeline.from_pretrained(checkpoint)
        pipeline = pipeline.to(device)
        self._pipeline = pipeline
        return pipeline

    def generate(self, request: MusicGenerateRequest) -> JobResult:
        """1 リクエスト分の音楽を生成し、 ``output_uri`` へ WAV を書き出す。

        prompt / duration_sec / bpm / seed をパイプラインへ渡し、 生成した波形を
        WAV (PCM) として ``StorageAdapter`` で保存する。
        """
        import torch  # 遅延 import

        pipeline = self._load()
        started = time.monotonic()

        generator: Any | None = None
        if request.seed is not None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            generator = torch.Generator(device=device).manual_seed(request.seed)

        result = pipeline(
            prompt=request.prompt,
            negative_prompt=request.negative_prompt,
            duration_sec=request.duration_sec,
            bpm=request.bpm,
            music_key=request.music_key,
            generator=generator,
        )

        wav_bytes = _encode_wav(
            result.audio, sample_rate=getattr(result, "sample_rate", _SAMPLE_RATE)
        )
        self._storage.write_bytes(request.output_uri, wav_bytes)

        duration_ms = int((time.monotonic() - started) * 1000)
        return JobResult(
            output_uri=request.output_uri,
            duration_ms=duration_ms,
            vram_peak_mb=peak_vram_mb(),
        )


def _encode_wav(audio: Any, *, sample_rate: int) -> bytes:
    """生成された波形を 16-bit PCM WAV (bytes) へエンコードする。

    ``numpy`` は標準で torch 環境に同梱されるが、 ここでも遅延 import し、
    torch 不在のローカル環境で本モジュールが import できる状態を保つ。
    """
    import io
    import wave

    import numpy as np

    samples = np.asarray(audio, dtype=np.float32)
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype("<i2")

    channels = 1 if pcm.ndim == 1 else pcm.shape[0]
    interleaved = pcm if pcm.ndim == 1 else pcm.T.reshape(-1)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(interleaved.tobytes())
    return buffer.getvalue()
