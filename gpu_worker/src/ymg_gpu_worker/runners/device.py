"""GPU / モデル配置に関する遅延ヘルパ。

torch は **関数内で遅延 import** する。 これにより torch 未インストールの環境
(ローカル開発機 / CI lint) でも import エラーにならない。 ``/health`` から VRAM
情報を取得する用途と、 各ランナーのデバイス決定で共用する。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_MODELS_DIR_ENV: Final = "YMG_MODELS_DIR"
_DEFAULT_MODELS_DIR: Final = "/models"
_MB: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GpuStatus:
    """GPU の可用性と VRAM 情報のスナップショット。"""

    available: bool
    vram_free_mb: int
    vram_total_mb: int | None


def models_dir() -> Path:
    """モデル重みのルートディレクトリを返す (``YMG_MODELS_DIR`` 既定 ``/models``)。"""
    return Path(os.environ.get(_MODELS_DIR_ENV, _DEFAULT_MODELS_DIR))


def probe_gpu() -> GpuStatus:
    """CUDA の可用性と空き / 総 VRAM (MB) を返す。

    torch 未インストールや CUDA 不在の環境では ``available=False`` /
    ``vram_free_mb=0`` を返し、 例外を送出しない (``/health`` を常に応答可能に保つ)。
    """
    try:
        import torch  # 遅延 import: torch 未インストールでも import 失敗させない
    except ImportError:
        return GpuStatus(available=False, vram_free_mb=0, vram_total_mb=None)

    if not torch.cuda.is_available():
        return GpuStatus(available=False, vram_free_mb=0, vram_total_mb=None)

    free_bytes, total_bytes = torch.cuda.mem_get_info()
    return GpuStatus(
        available=True,
        vram_free_mb=int(free_bytes) // _MB,
        vram_total_mb=int(total_bytes) // _MB,
    )


def peak_vram_mb() -> int | None:
    """現プロセスの CUDA ピーク確保量 (MB) を返す。 取得不能なら ``None``。"""
    try:
        import torch  # 遅延 import
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    return int(torch.cuda.max_memory_allocated()) // _MB
