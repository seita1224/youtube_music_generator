"""Add llm_provider_secrets + app_state.llm_model seed (forward-only).

OpenAI / Anthropic API key を Fernet 暗号化して DB に write-only 保存するテーブルを追加する。
実行時の解決順位は env 非空 > DB > none (ADR-0019)。 Ollama は対象外。

あわせて ``app_state.llm_model`` を seed する (未設定なら既定 ``qwen2.5:3b``)。
既存行は上書きしない (ON CONFLICT DO NOTHING)。

Revision ID: 005_llm_provider_secrets
Revises: 004_job_history_schema_align
Create Date: 2026-07-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005_llm_provider_secrets"
down_revision: str | None = "004_job_history_schema_align"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_provider_secrets",
        sa.Column("provider", sa.Text(), primary_key=True, nullable=False),
        sa.Column("api_key_encrypted", postgresql.BYTEA(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "provider IN ('openai', 'anthropic')",
            name="ck_llm_provider_secrets_provider",
        ),
    )

    # llm_model は既存環境に無い場合のみ挿入 (データ破壊なし)。
    op.execute(
        sa.text(
            "INSERT INTO app_state (key, value) VALUES "
            "('llm_model', CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO NOTHING"
        ).bindparams(value='"qwen2.5:3b"')
    )


def downgrade() -> None:
    # ADR-0031: down は運用しないが、 ローカル検証用に対称操作を残す。
    op.execute(sa.text("DELETE FROM app_state WHERE key = 'llm_model'"))
    op.drop_table("llm_provider_secrets")
