"""推論ランナー (ACE-Step / SDXL)。

重要: torch / diffusers は **モジュール先頭で import しない**。 ローカル開発機に
torch 未インストールでも import 時に失敗しないよう、 各ランナーは生成関数の
内部で遅延 import する (ADR-0031, pyproject の注記)。
"""

from __future__ import annotations
