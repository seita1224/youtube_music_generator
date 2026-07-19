"""Alembic 環境設定。

DB URL は .env の POSTGRES_* から組み立てる (DATABASE_URL があれば優先)。
初期マイグレーション (T016-T018) は手書き DDL のため target_metadata は None。
将来 autogenerate を使う場合は ymg_backend.infrastructure.db.models の Base.metadata を割り当てる。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    # Alembic は同期ドライバ (psycopg v3) で実行する
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


config.set_main_option("sqlalchemy.url", _database_url())

# 手書き DDL マイグレーションのため現状は None (将来 autogenerate 時に差し替え)
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
