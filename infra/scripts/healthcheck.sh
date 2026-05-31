#!/usr/bin/env bash
# backend / frontend / gpu_worker の /health を確認 (T013, deploy 末尾でも使う ADR-0031)。
# 使い方: bash infra/scripts/healthcheck.sh   または   make healthcheck
set -uo pipefail

# .env があれば Basic 認証情報を読む
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi

BACKEND_URL="${BACKEND_HEALTH_URL:-http://127.0.0.1:8000/health}"
FRONTEND_URL="${FRONTEND_HEALTH_URL:-http://127.0.0.1:3000}"
GPU_URL="${GPU_WORKER_BASE_URL:-http://127.0.0.1:8001}/health"
ADMIN_USER="${ADMIN_USERNAME:-admin}"
ADMIN_PASS="${ADMIN_PASSWORD:-}"

fail=0

check() {
  local name="$1" url="$2"; shift 2
  if curl -fsS --max-time 5 "$@" "$url" >/dev/null 2>&1; then
    echo "  ✅ ${name}: ${url}"
  else
    echo "  ❌ ${name}: ${url} (unreachable)"
    fail=1
  fi
}

echo "== healthcheck =="
check "backend " "$BACKEND_URL" -u "${ADMIN_USER}:${ADMIN_PASS}"
check "frontend" "$FRONTEND_URL"
check "gpu_work" "$GPU_URL"

if [ "$fail" -ne 0 ]; then
  echo "== 1 つ以上が不通 =="
  exit 1
fi
echo "== all green =="
