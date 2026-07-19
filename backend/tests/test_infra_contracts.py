"""インフラ / デプロイ系 FR の静的契約テスト。

cron / systemd / Makefile / シェル / ビルド系の FR は pytest で「実行」検証できないが、
ソース(Makefile / infra スクリプト / CI / .gitignore / 依存定義)への**静的アサーション**で
契約を担保できる。 これにより「pre-dump を消した」「.env をバックアップ include に足した」
「CI の docker build を外した」「OAuth scope を削った」等の退行を検出する。

これらは技術選定の自己言及ではなく、 **退行ガード**として機能する(値が変わると落ちる)。
個別 FR の代替検証手段は specs/001-youtube-music-generator/traceability.md を参照。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _section(text: str, header: str) -> str:
    """Makefile の ``header`` ターゲット行から次の空行までを返す。"""
    lines = text.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith(header):
            capturing = True
            continue
        if capturing:
            if line.strip() == "" or (line and not line.startswith(("\t", " "))):
                break
            out.append(line)
    return "\n".join(out)


@pytest.mark.fr("FR-080")
def test_backend_is_python_fastapi_uv_managed() -> None:
    """FR-080: backend は Python + FastAPI、 uv 管理(pyproject + uv.lock)。"""
    pyproject = _read("backend/pyproject.toml")
    assert "fastapi" in pyproject
    assert (_REPO / "backend" / "uv.lock").is_file()


@pytest.mark.fr("FR-081")
def test_frontend_is_nextjs() -> None:
    """FR-081: frontend は Next.js(App Router)。"""
    pkg = _read("frontend/package.json")
    assert '"next"' in pkg
    assert (_REPO / "frontend" / "app").is_dir()  # App Router


@pytest.mark.fr("FR-084")
def test_env_holding_fernet_key_is_gitignored() -> None:
    """FR-084: Fernet 鍵を載せる .env が .gitignore で除外されている。"""
    ignored = {line.strip() for line in _read(".gitignore").splitlines()}
    assert ".env" in ignored


@pytest.mark.fr("FR-086")
def test_openapi_typescript_codegen_is_wired() -> None:
    """FR-086: OpenAPI スキーマ → openapi-typescript で frontend 型を生成する。"""
    pkg = _read("frontend/package.json")
    assert "openapi-typescript" in pkg
    assert "backend-api.yaml" in pkg  # 契約 YAML を入力にしている


@pytest.mark.fr("FR-090")
def test_gpu_worker_is_a_separate_package() -> None:
    """FR-090: GPU worker は独立パッケージ(別 pyproject + 別 Dockerfile)に分離。"""
    assert (_REPO / "gpu_worker" / "pyproject.toml").is_file()
    assert (_REPO / "gpu_worker" / "Dockerfile").is_file()
    # backend とは別パッケージ名であること(同一プロセス相乗りでない)。
    worker_proj = _read("gpu_worker/pyproject.toml")
    assert "ymg" in worker_proj and "worker" in worker_proj.lower()


@pytest.mark.fr("FR-093")
def test_gpu_worker_dockerfile_built_in_ci() -> None:
    """FR-093: GPU worker の Dockerfile を CI で docker build する。"""
    assert (_REPO / "gpu_worker" / "Dockerfile").is_file()
    ci = _read(".github/workflows/ci.yml")
    assert "docker build" in ci
    assert "./gpu_worker" in ci


@pytest.mark.fr("FR-120")
def test_daily_backup_timer_and_pg_dump() -> None:
    """FR-120: 毎日 cron(systemd timer)で pg_dump + メタデータをコピーする。"""
    timer = _read("infra/systemd/ymg-backup.timer")
    assert "OnCalendar=" in timer  # 日次スケジュール
    backup = _read("infra/scripts/backup.sh")
    assert "pg_dump" in backup
    assert "rsync" in backup  # メタデータコピー


@pytest.mark.fr("FR-121")
def test_migrate_takes_predump_before_alembic() -> None:
    """FR-121: ``make migrate`` は alembic 実行前に pg_dump を取る。"""
    migrate = _section(_read("Makefile"), "migrate:")
    assert "pg_dump" in migrate
    assert "alembic upgrade head" in migrate
    # pre-dump が alembic より前に来ること。
    assert migrate.index("pg_dump") < migrate.index("alembic upgrade head")


@pytest.mark.fr("FR-122")
def test_backup_excludes_fernet_key() -> None:
    """FR-122: Fernet 鍵(.env / FERNET_KEY)をバックアップ対象から除外する。"""
    backup = _read("infra/scripts/backup.sh")
    # メタデータ allowlist + 既定 exclude。 .env / 鍵は include に無いので拾われない。
    assert "--exclude='*'" in backup
    # .env / FERNET を **include に足していない**(退行検出)。
    include_lines = [ln for ln in backup.splitlines() if "--include=" in ln]
    joined = "\n".join(include_lines)
    assert ".env" not in joined
    assert "FERNET" not in joined and "fernet" not in joined


@pytest.mark.fr("FR-123")
def test_deploy_is_a_manual_make_target() -> None:
    """FR-123: デプロイは Makefile 経由の手動実行(make deploy → deploy.sh)。"""
    makefile = _read("Makefile")
    assert "deploy:" in makefile
    assert "deploy.sh" in _section(makefile, "deploy:")
