#!/usr/bin/env bash
# デプロイ (T132 / T134, ADR-0031 (2))。 Makefile から `bash infra/scripts/deploy.sh` で呼ばれる。
#
# フロー (各段の失敗で即停止: set -euo pipefail):
#   1. git fetch --prune → git reset --hard origin/<branch> (ADR-0031 の本体に準拠)
#   2. 事前 pg_dump (backup.sh があれば流用、 無ければ pg_dump 単発) → alembic upgrade head
#   3. docker compose build backend frontend
#   4. docker compose up -d (backend frontend postgres)
#   5. GPU worker (systemd 前提) を再起動。 unit が無ければ skip
#   6. healthcheck.sh で /health 全 green を確認
#
# ロールバックは ADR-0031 (4): git revert <sha> → make deploy / make restore-db DUMP=...
set -euo pipefail

# .env があれば接続情報を読む (各 step のデフォルト用)。
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

COMPOSE="${COMPOSE:-docker compose}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
GPU_WORKER_UNIT="${GPU_WORKER_UNIT:-ymg-gpu-worker}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PG_USER="${POSTGRES_USER:-ymg}"
PG_DB="${POSTGRES_DB:-ymg}"
BACKUP_ROOT="${BACKUP_ROOT:-/srv/ymg/backups}"
TS="$(date +%Y%m%d-%H%M%S)"

echo "== deploy (ADR-0031) =="
echo "  branch       : ${DEPLOY_BRANCH}"
echo "  compose      : ${COMPOSE}"
echo "  gpu unit     : ${GPU_WORKER_UNIT}"
echo

# --- 1) git pull (fetch + reset --hard) ---
echo "== 1) git fetch --prune && reset --hard origin/${DEPLOY_BRANCH} =="
git fetch --prune
git reset --hard "origin/${DEPLOY_BRANCH}"
echo "  ✅ HEAD = $(git rev-parse --short HEAD)"

# --- 2) 事前 pg_dump → DB マイグレーション ---
echo
echo "== 2) 事前バックアップ + alembic upgrade head =="
if [ -x "${SCRIPT_DIR}/backup.sh" ] || [ -f "${SCRIPT_DIR}/backup.sh" ]; then
  echo "  backup.sh で事前バックアップを取得します。"
  bash "${SCRIPT_DIR}/backup.sh"
else
  # backup.sh が無い環境向けフォールバック: pre-migrate-<ts>.dump を取る (ADR-0031 (4))。
  pre_dir="${BACKUP_ROOT%/}/db"
  mkdir -p "$pre_dir"
  pre_dump="${pre_dir}/pre-migrate-${TS}.dump"
  echo "  pre-migrate ダンプ → ${pre_dump}"
  $COMPOSE exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "${pre_dump}.partial"
  mv -f "${pre_dump}.partial" "$pre_dump"
fi
echo "  alembic upgrade head を実行します。"
$COMPOSE exec -T backend uv run alembic upgrade head
echo "  ✅ マイグレーション完了"

# --- 3) イメージ再ビルド ---
echo
echo "== 3) docker compose build backend frontend =="
$COMPOSE build backend frontend
echo "  ✅ build 完了"

# --- 4) スタック起動 ---
echo
echo "== 4) docker compose up -d =="
$COMPOSE up -d postgres backend frontend
echo "  ✅ up 完了"

# --- 5) GPU worker (systemd) 再起動 (無ければ skip) ---
echo
echo "== 5) GPU worker (${GPU_WORKER_UNIT}) 再起動 =="
if command -v systemctl >/dev/null 2>&1 \
  && systemctl list-unit-files "${GPU_WORKER_UNIT}.service" >/dev/null 2>&1 \
  && systemctl list-unit-files "${GPU_WORKER_UNIT}.service" 2>/dev/null | grep -q "${GPU_WORKER_UNIT}.service"; then
  sudo systemctl restart "${GPU_WORKER_UNIT}"
  echo "  ✅ ${GPU_WORKER_UNIT} を再起動しました"
else
  echo "  (systemd unit ${GPU_WORKER_UNIT}.service が無いため skip。 host 直 worker は手動再起動してください)"
fi

# --- 6) ヘルスチェック ---
echo
echo "== 6) healthcheck =="
bash "${SCRIPT_DIR}/healthcheck.sh"

echo
echo "== deploy 完了 (HEAD=$(git rev-parse --short HEAD)) =="
