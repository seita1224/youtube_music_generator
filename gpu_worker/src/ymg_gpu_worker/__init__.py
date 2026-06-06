"""YMG GPU worker (ACE-Step + SDXL).

ACE-Step (音楽) と SDXL (画像) の推論を HTTP API で提供する worker
パッケージ (ADR-0031)。 backend ↔ GPU worker は
``contracts/gpu-worker-api.yaml`` の契約で疎結合になっている。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
