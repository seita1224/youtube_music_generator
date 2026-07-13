"""共通 Declarative Base と ENUM ヘルパ (T021)。

ORM は `alembic/versions/001_initial.py` の手書き DDL / `data-model.md` と
完全整合させる。 PostgreSQL native ENUM 型は migration 側で既に CREATE TYPE 済みのため、
ORM カラムは `create_type=False` で既存型を参照するだけにする
(autogenerate / メタデータ生成時の重複 CREATE TYPE を防ぐ)。

`Base.metadata` は将来 `alembic/env.py` の autogenerate 対象に割り当てられる前提
(env.py のコメント参照)。 そのため ENUM 名 / 値 / カラム属性は migration と一字一句揃える。
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.orm import DeclarativeBase

# --- ENUM 定義 (data-model.md §Enums / 001_initial.py `_ENUMS` + 003) ---
# migration 側と完全一致させること。 順序も DDL の宣言順 / ADD VALUE 順を保持する。
_ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "plan_cycle": ("daily", "weekly"),
    # music_generated は 003_music_generation_jobs で ENUM 末尾に ADD VALUE される。
    "plan_status": (
        "generated",
        "approved",
        "executing",
        "completed",
        "failed",
        "music_generated",
    ),
    "post_status": (
        "pending",
        "generating",
        "generated",
        "posting",
        "posted",
        "failed",
        "music_generated",
    ),
    "gpu_job_type": ("music", "image"),
    "gpu_job_status": ("queued", "running", "succeeded", "failed"),
    "dryrun_state": ("pending", "approved", "rejected", "auto_expired", "posted"),
    "error_category": ("transient", "recoverable", "fatal", "compliance", "quality"),
    "acoustid_status": ("not_checked", "clear", "hit", "api_error"),
    "youtube_privacy_status": ("public", "unlisted", "private", "deleted"),
    "llm_provider": ("openai", "anthropic", "ollama"),
    "llm_auth_mode": ("api_key", "codex_oauth"),
}


def pg_enum(name: str) -> ENUM:
    """既存の PostgreSQL ENUM 型を参照する (`create_type=False`)。

    migration が CREATE TYPE 済みのため、 ORM 側では型を生成・DROP しない。
    """
    return ENUM(*_ENUM_VALUES[name], name=name, create_type=False)


class Base(DeclarativeBase):
    """全 ORM モデルの共通 Declarative Base。"""
