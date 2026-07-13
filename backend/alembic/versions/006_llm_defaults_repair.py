"""Repair legacy LLM app_state default mismatches (forward-only).

001 初期 seed が ``llm_provider=openai``、 005 が ``llm_model=qwen2.5:3b`` (Ollama 専用)
のみ挿入した環境では provider/model が不整合になる。 本 migration は **既知の
レガシー既定値の組合せ** のみ決定的に修復し、 意図的な provider/model 選択は
上書きしない。

修復対象:
- ``llm_auth_mode=codex_oauth`` → ``api_key`` (未配線)。 **先に**正規化する
- その後 ``llm_provider=openai`` + ``llm_model=qwen2.5:3b`` + ``llm_auth_mode=api_key``
  → ``llm_provider=ollama``
  (``openai + qwen2.5:3b + codex_oauth`` も上の正規化後に三重一致となり
  ``ollama + qwen2.5:3b + api_key`` へ到達する)

Revision ID: 006_llm_defaults_repair
Revises: 005_llm_provider_secrets
Create Date: 2026-07-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_llm_defaults_repair"
down_revision: str | None = "005_llm_provider_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Codex OAuth は未配線のため app_state 上も api_key へ正規化する。
    # provider 修復より先に実行し、 openai + qwen + codex_oauth も三重一致に乗せる。
    op.execute(
        sa.text(
            """
            UPDATE app_state
            SET value = '"api_key"'::jsonb,
                updated_at = now()
            WHERE key = 'llm_auth_mode'
              AND value = '"codex_oauth"'::jsonb
            """
        )
    )

    # 001 openai seed + 005 qwen model seed の既知不整合のみ修復。
    op.execute(
        sa.text(
            """
            UPDATE app_state AS provider_row
            SET value = '"ollama"'::jsonb,
                updated_at = now()
            WHERE provider_row.key = 'llm_provider'
              AND provider_row.value = '"openai"'::jsonb
              AND EXISTS (
                SELECT 1
                FROM app_state AS model_row
                WHERE model_row.key = 'llm_model'
                  AND model_row.value = '"qwen2.5:3b"'::jsonb
              )
              AND EXISTS (
                SELECT 1
                FROM app_state AS auth_row
                WHERE auth_row.key = 'llm_auth_mode'
                  AND auth_row.value = '"api_key"'::jsonb
              )
            """
        )
    )


def downgrade() -> None:
    # ADR-0031: down は運用しない (secrets / 意図的選択の復元不可)。
    raise NotImplementedError("006_llm_defaults_repair downgrade is not supported")
