"""normalize genre.role vocabulary to experiment/extension/main (FR-037/038).

``genres.role`` の語彙を spec.md FR-037/038・seed・rotation.py に統一する。
過去に api/genres.py の旧ラダー (primary/extended/experimental) で promote した結果
DB に残る旧語彙を、 新語彙 (main/extension/experiment) へ正規化する。

新規 DB は 001_initial の seed が既に新語彙のため、 本マイグレーションの UPDATE は
旧語彙の行のみに作用する (新語彙のみの環境では no-op)。

Revision ID: 002_normalize_genre_roles
Revises: 001_initial
Create Date: 2026-06-23

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "002_normalize_genre_roles"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 旧語彙 (api/genres.py の旧ラダー) → 新語彙 (spec/seed/rotation)。
_ROLE_MAP: tuple[tuple[str, str], ...] = (
    ("primary", "main"),
    ("extended", "extension"),
    ("experimental", "experiment"),
)


def upgrade() -> None:
    for old, new in _ROLE_MAP:
        op.execute(f"UPDATE genres SET role = '{new}' WHERE role = '{old}'")


def downgrade() -> None:
    for old, new in _ROLE_MAP:
        op.execute(f"UPDATE genres SET role = '{old}' WHERE role = '{new}'")
