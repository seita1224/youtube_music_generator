"""タイトル / 説明文 / サムネのテンプレローダ(ADR-0034)。

``backend/templates/{title,description,thumbnail}/*.yaml`` を YAML パースし、
ジャンル名からジャンル別テンプレを解決する。``_shared`` 配下の共通素材
(AI 開示文・レイアウト JSON 等)も読み出せる。詳細実装は
:mod:`ymg_backend.domain.templates.loader`。
"""

from __future__ import annotations

from ymg_backend.domain.templates.loader import (
    GenreTemplate,
    TemplateCategory,
    TemplateLoader,
    TemplateLoadError,
    TemplateNotFoundError,
    available_genres,
    load_genre_template,
)

__all__ = [
    "GenreTemplate",
    "TemplateCategory",
    "TemplateLoadError",
    "TemplateLoader",
    "TemplateNotFoundError",
    "available_genres",
    "load_genre_template",
]
