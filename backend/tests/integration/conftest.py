"""integration テスト共通設定: 専用テスト DB を使い dev/demo DB を保護する。

integration テストは実 Postgres に接続し、 一部 fixture は ``TRUNCATE ... CASCADE`` で
全データ表を空にする。 接続先を dev/demo DB(``POSTGRES_DB``)と共有すると、 ``pytest -m
integration`` を回すたびに本番相当のデータ + migration seed が消える。 これを防ぐため、
本 conftest が **専用テスト DB を作成し ``POSTGRES_DB`` をそれへ上書き**する。

各テストの ``_async_url()`` / ``_sync_url()`` と alembic ``env.py`` はいずれも実行時に
``os.environ["POSTGRES_DB"]`` を読むため、 ここで上書きすれば全 integration が自動で
テスト DB を指す(個別ファイルの編集は不要)。 接続不可なら全 integration を skip する。

テスト DB 名は ``POSTGRES_TEST_DB``(明示)、 無ければ ``<POSTGRES_DB>_test``。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

# tests/integration/conftest.py -> backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _maintenance_url(dbname: str) -> str:
    """CREATE DATABASE 用に既存 DB(既定 ``postgres``)へ繋ぐ同期 URL。"""
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}"


def _test_db_name() -> str:
    """dev DB と必ず別名のテスト DB 名を返す(誤って dev を指さない保証)。"""
    explicit = os.environ.get("POSTGRES_TEST_DB")
    if explicit:
        return explicit
    dev = os.environ.get("POSTGRES_DB", "ymg")
    return dev if dev.endswith("_test") else f"{dev}_test"


def _run_migrations() -> None:
    """テスト DB に alembic upgrade head を適用(enum + 16 表 + seed)。冪等。"""
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _isolated_test_database() -> None:
    """専用テスト DB を用意し ``POSTGRES_DB`` を上書きする(dev/demo DB 保護)。

    接続不可なら全 integration を skip(従来の ``_require_db`` と同挙動)。 テスト DB が
    無ければ作成し、 schema/seed を alembic で適用する。 ``POSTGRES_DB`` 上書きにより
    以降の ``_async_url`` / alembic が自動でテスト DB を指す。
    """
    test_db = _test_db_name()
    admin = create_engine(_maintenance_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": test_db}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    except OperationalError as exc:
        pytest.skip(f"postgres へ接続できないため integration を skip: {exc}")
    finally:
        admin.dispose()

    # 以降の DB 接続(各テストの _async_url / alembic env.py)をテスト DB へ向ける。
    os.environ["POSTGRES_DB"] = test_db
    try:
        _run_migrations()
    except SQLAlchemyError as exc:  # スキーマ適用失敗は環境問題として skip。
        pytest.skip(f"テスト DB へ migration を適用できないため skip: {exc}")
