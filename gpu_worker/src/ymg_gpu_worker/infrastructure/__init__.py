"""GPU worker のインフラ層 (ストレージ抽象など)。"""

from __future__ import annotations

from ymg_gpu_worker.infrastructure.storage import StorageAdapter

__all__ = ["StorageAdapter"]
