"""GPU / torch 無しで動く決定論的なダミー生成ランナー (ADR-0031)。

``GPU_WORKER_DUMMY=1`` のとき ``WorkerRuntime`` がこのランナーへ振り分ける。
実モデル (ACE-Step / SDXL) ・torch・CUDA を一切 import せず、 固定の
サンプル素材を ``output_uri`` へ書き出して即座に成功させる。 ローカル開発 /
CI / E2E で実重み不在でも生成経路を通せるようにする目的。

中身はダミーだが、 リクエストの値 (``duration_sec`` / ``width`` / ``height``)
は尊重する:

- music: 標準ライブラリ ``wave`` で短いサイン波 WAV (16-bit PCM, mono) を生成。
  ``duration_sec`` ぶんの長さにするが、 容量を抑えるため上限でクランプする。
- image: ``width`` x ``height`` の単色 PNG を生成。 Pillow があればそれで、
  無ければ ``zlib`` + ``struct`` で最小の有効 PNG をバイト列で組み立てる。

決定論: 同一リクエスト (prompt / seed / 寸法) に対し常に同じバイト列を返す。
torch を呼ばないため VRAM は計測できず、 ``vram_peak_mb=None`` を返す。
"""

from __future__ import annotations

import hashlib
import io
import math
import struct
import time
import wave
import zlib
from typing import Final

from ymg_gpu_worker.api.schemas import ImageGenerateRequest, MusicGenerateRequest
from ymg_gpu_worker.infrastructure.storage import StorageAdapter
from ymg_gpu_worker.jobs.queue import JobKind, JobRequest, JobResult

_MODEL_NAME: Final = "dummy"
_SAMPLE_RATE: Final = 44100
# WAV の容量肥大を防ぐためのダミー音声長の上限 (秒)。 リクエストが長くても
# これでクランプする (中身はダミーのため長さの正確性は不要)。
_MAX_DUMMY_AUDIO_SEC: Final = 2
_BASE_FREQ_HZ: Final = 220.0
_AMPLITUDE: Final = 0.25  # クリップ回避のため控えめな振幅


class DummyRunner:
    """torch 無しの決定論的ダミー生成ランナー。

    ``generate`` は ``JobKind`` で music / image を分岐し、 ダミー素材を
    ``StorageAdapter`` で ``output_uri`` へ書き出して ``JobResult`` を返す。
    """

    __slots__ = ("_storage",)

    def __init__(self, storage: StorageAdapter) -> None:
        self._storage: Final[StorageAdapter] = storage

    @property
    def model_name(self) -> str:
        """``/health`` の models_loaded 表示用の識別子。"""
        return _MODEL_NAME

    def generate(self, kind: JobKind, request: JobRequest) -> JobResult:
        """ダミー素材を生成して ``output_uri`` へ書き出す。

        sync 関数 (既存ランナーと同じシグネチャ感) として実装し、 runtime 側で
        executor へ逃がせるようにする。 torch / CUDA は触らない。
        """
        started = time.monotonic()
        if kind is JobKind.MUSIC:
            assert isinstance(request, MusicGenerateRequest)
            payload = _dummy_wav(request)
        else:
            assert isinstance(request, ImageGenerateRequest)
            payload = _dummy_png(request)

        self._storage.write_bytes(request.output_uri, payload)
        duration_ms = int((time.monotonic() - started) * 1000)
        return JobResult(
            output_uri=request.output_uri,
            duration_ms=duration_ms,
            vram_peak_mb=None,
        )


def _seed_for(request: JobRequest) -> int:
    """リクエストから決定論的なシード値を導く (seed 未指定でも安定)。"""
    if request.seed is not None:
        return request.seed
    digest = hashlib.sha256(request.prompt.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def _dummy_wav(request: MusicGenerateRequest) -> bytes:
    """``duration_sec`` を尊重した短いサイン波 WAV (16-bit PCM, mono) を返す。

    周波数はシードでわずかに変え、 同一リクエストでは常に同一バイト列にする。
    """
    seconds = min(max(request.duration_sec, 1), _MAX_DUMMY_AUDIO_SEC)
    seed = _seed_for(request)
    freq = _BASE_FREQ_HZ + float(seed % 220)
    total_frames = seconds * _SAMPLE_RATE

    amplitude = int(_AMPLITUDE * 32767)
    step = 2.0 * math.pi * freq / _SAMPLE_RATE
    frames = bytearray()
    for index in range(total_frames):
        sample = int(amplitude * math.sin(step * index))
        frames += struct.pack("<h", sample)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_SAMPLE_RATE)
        wav.writeframes(bytes(frames))
    return buffer.getvalue()


def _dummy_png(request: ImageGenerateRequest) -> bytes:
    """``width`` x ``height`` の単色 PNG を返す。

    Pillow があればそれで生成し、 無ければ ``zlib`` + ``struct`` で最小の
    有効 PNG を組み立てる (どちらも決定論的)。
    """
    width = max(request.width, 1)
    height = max(request.height, 1)
    color = _color_for(request)
    try:
        from PIL import Image  # 任意依存: 無ければバイト列フォールバック
    except ImportError:
        return _encode_solid_png(width, height, color)

    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _color_for(request: ImageGenerateRequest) -> tuple[int, int, int]:
    """シードから決定論的な RGB 単色を導く。"""
    seed = _seed_for(request)
    red = (seed >> 16) & 0xFF
    green = (seed >> 8) & 0xFF
    blue = seed & 0xFF
    return (red, green, blue)


def _encode_solid_png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """Pillow 不在時の最小 PNG エンコーダ (8-bit RGB, 単色)。

    PNG 仕様: 署名 + IHDR + IDAT (zlib 圧縮された raw スキャンライン) + IEND。
    各スキャンラインはフィルタタイプ 0 (None) を先頭に付ける。
    """
    signature = b"\x89PNG\r\n\x1a\n"

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(color) * width
    raw = row * height
    idat = zlib.compress(raw, level=9)

    return (
        signature + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")
    )


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """PNG チャンク (length + tag + data + CRC32) を組み立てる。"""
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)
