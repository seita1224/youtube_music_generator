"""SQLAlchemy 2.x async engine / session (T020)。

DB URL は env から組み立てる (DATABASE_URL があれば優先、 なければ POSTGRES_*)。
alembic/env.py の `_database_url()` と同じ前提を共有するが、 こちらは asyncpg ドライバを使う
(alembic は同期 psycopg、 アプリ実行時は async)。

将来 `core/config.py` (T022) が入ったら `_database_url()` をそこへ委譲する。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _database_url() -> str:
    """env から async (asyncpg) 用の DB URL を組み立てる。

    DATABASE_URL があれば優先する。 同期ドライバ指定 (psycopg) を含む URL が来た場合でも
    asyncpg へ正規化し、 アプリ側は常に async ドライバで接続する。
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return _normalize_async_url(url)
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


def _normalize_async_url(url: str) -> str:
    """同期ドライバ付き / ドライバ無しの URL を asyncpg ドライバ付きに正規化する。"""
    sync_prefixes = (
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
        "postgres://",
        "postgresql://",
    )
    for prefix in sync_prefixes:
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """プロセス単位で 1 つの AsyncEngine を返す。

    `pool_pre_ping=True` で reboot 後の切断済みコネクションを検出する (ADR-0031)。
    """
    return create_async_engine(
        _database_url(),
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """AsyncSession ファクトリを返す。"""
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI の Depends 用セッションプロバイダ。

    コミットは呼び出し側の責務とし、 例外時は rollback してから再送出する。
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """エンジンのコネクションプールを破棄する (lifespan shutdown 用)。"""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
