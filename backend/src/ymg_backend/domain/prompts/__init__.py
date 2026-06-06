"""プロンプトのバージョン管理ローダ(ADR-0033 (5))。

``backend/prompts/<area>/<name>_v<N>.md`` を最新版 / 明示版で解決して読み込む。
few-shot JSON も同じ命名規則で解決できる。詳細実装は :mod:`ymg_backend.domain.prompts.loader`。
"""

from ymg_backend.domain.prompts.loader import (
    PromptLoader,
    PromptLoadError,
    PromptNotFoundError,
    ResolvedPrompt,
    available_versions,
    load_few_shot,
    load_prompt,
)

__all__ = [
    "PromptLoadError",
    "PromptLoader",
    "PromptNotFoundError",
    "ResolvedPrompt",
    "available_versions",
    "load_few_shot",
    "load_prompt",
]
