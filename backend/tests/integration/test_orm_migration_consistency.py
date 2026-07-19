"""ORM models と alembic マイグレーションの実スキーマ整合性ゲート (B)。

T021 の ORM models(`infrastructure/db/models/`)が、 T016-T018 の手書き DDL
マイグレーション(`alembic/versions/001_initial.py`)を実 DB に適用した結果と
一致することを `alembic.autogenerate.compare_metadata` で検証する。

差分が出る = ORM とマイグレーションがドリフトしている(将来の autogenerate や
ORM クエリが実スキーマと食い違う)ことを意味するため、 差分ゼロを必須とする。

実 PostgreSQL を必要とする integration テスト。 DB へ接続できない環境では skip。
CI の backend job は postgres service を提供するため、 そこでは必ず実行される。
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from ymg_backend.infrastructure.db.models import Base

pytestmark = pytest.mark.integration


def _sync_url() -> str:
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


@pytest.mark.fr("FR-082")
def test_orm_matches_migration() -> None:
    """FR-082: ORM models と alembic 実スキーマの差分ゼロ整合性ゲート。"""
    engine = create_engine(_sync_url())

    try:
        engine.connect().close()
    except OperationalError as exc:  # DB 未起動 / 未到達なら skip
        pytest.skip(f"postgres へ接続できないため skip: {exc}")

    # マイグレーションを head まで適用(べき等。 空 DB なら全テーブル作成)
    command.upgrade(Config("alembic.ini"), "head")

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        diffs = compare_metadata(ctx, Base.metadata)

    assert diffs == [], "ORM models と alembic 実スキーマに差分があります:\n" + "\n".join(
        repr(d) for d in diffs
    )
