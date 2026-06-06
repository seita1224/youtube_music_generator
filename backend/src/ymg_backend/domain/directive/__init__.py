"""ディレクティブパーサ(自動判別方式、ADR-0017)。

``{{...}}`` を唯一の構文とし、中身を ``\\w+`` の自動判別で変数参照 / 生成ディレクティブに
振り分ける。公開 API はパース・レンダリングと、その中間表現・専用例外。詳細実装は
:mod:`ymg_backend.domain.directive.parser`。
"""

from __future__ import annotations

from ymg_backend.domain.directive.parser import (
    DirectiveError,
    GenerativeSlot,
    ParsedTemplate,
    VariableSlot,
    parse_template,
    render_template,
    variable_names_of,
)

__all__ = [
    "DirectiveError",
    "GenerativeSlot",
    "ParsedTemplate",
    "VariableSlot",
    "parse_template",
    "render_template",
    "variable_names_of",
]
