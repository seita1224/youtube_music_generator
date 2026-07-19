#!/usr/bin/env bash
# 緊急停止 (ADR-0031, T114): scheduler 停止 + 直近 N 時間の動画を private 化する。
# backend の POST /scheduler/panic-stop を Basic 認証 (ADMIN_USERNAME/ADMIN_PASSWORD) 付きで叩く。
# 使い方: bash infra/scripts/panic-stop.sh   または   make panic-stop
#
# 冪等・非破壊: このスクリプト自体は backend API を呼ぶだけで、 DB/ファイルを直接変更しない。
# 既定では候補列挙のみ (set_private を渡さない)。 実際の private 化は後続の手動操作で行う。
set -uo pipefail

# .env があれば Basic 認証情報・接続先を読む (healthcheck.sh 踏襲)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

BACKEND_BASE="${BACKEND_BASE_URL:-http://127.0.0.1:8000}"
PANIC_URL="${BACKEND_BASE%/}/scheduler/panic-stop"
ADMIN_USER="${ADMIN_USERNAME:-admin}"
ADMIN_PASS="${ADMIN_PASSWORD:-}"
DEFAULT_WINDOW_HOURS=24

echo "== panic-stop (緊急停止) =="
echo "  対象 backend : ${PANIC_URL}"
echo "  認証ユーザー : ${ADMIN_USER}"
echo

if [ -z "$ADMIN_PASS" ]; then
  echo "  ❌ ADMIN_PASSWORD が未設定です (.env を確認してください)。" >&2
  exit 1
fi

# window_hours の対話入力 (空 Enter で既定値)
read -r -p "直近何時間の動画を対象にしますか? [既定 ${DEFAULT_WINDOW_HOURS}]: " window_hours
window_hours="${window_hours:-$DEFAULT_WINDOW_HOURS}"
if ! [[ "$window_hours" =~ ^[0-9]+$ ]] || [ "$window_hours" -le 0 ]; then
  echo "  ❌ window_hours は正の整数で指定してください (入力値: '${window_hours}')。" >&2
  exit 1
fi

echo
echo "  ⚠️  scheduler を停止し、 直近 ${window_hours} 時間の投稿を private 化候補として列挙します。"
read -r -p "実行してよろしいですか? [yes/no]: " confirm
case "$confirm" in
  yes|YES|y|Y) ;;
  *) echo "  中止しました (何も変更していません)。"; exit 0 ;;
esac

# set_private は省略 (候補列挙のみ)。 実際に private 化したい youtube_video_id を
# 配列で渡す場合は payload の "set_private": [] を編集する。
payload="$(jq -n --argjson wh "$window_hours" '{window_hours: $wh, set_private: []}')"

echo
echo "== 実行中 =="
http_body="$(mktemp)"
trap 'rm -f "$http_body"' EXIT

http_code="$(curl -sS --max-time 30 \
  -o "$http_body" -w '%{http_code}' \
  -u "${ADMIN_USER}:${ADMIN_PASS}" \
  -H 'Content-Type: application/json' \
  -X POST "$PANIC_URL" \
  -d "$payload" 2>/dev/null)" || {
  echo "  ❌ backend へ接続できませんでした (${PANIC_URL})。" >&2
  exit 1
}

if [ "$http_code" != "200" ]; then
  echo "  ❌ panic-stop が失敗しました (HTTP ${http_code})。" >&2
  echo "----- response body -----" >&2
  cat "$http_body" >&2
  echo >&2
  exit 1
fi

echo "  ✅ panic-stop 完了 (HTTP ${http_code})"
echo
echo "== 結果 =="
if jq -e . "$http_body" >/dev/null 2>&1; then
  scheduler_enabled="$(jq -r '.scheduler_enabled' "$http_body")"
  updated_count="$(jq -r '.updated_count' "$http_body")"
  recent_count="$(jq -r '(.recent_videos | length)' "$http_body")"
  echo "  scheduler_enabled : ${scheduler_enabled}"
  echo "  updated_count     : ${updated_count}  (private 化した本数)"
  echo "  recent_videos     : ${recent_count} 件 (直近 ${window_hours} 時間の対象候補)"
  echo
  echo "  -- recent_videos (youtube_video_id / privacy_status / title) --"
  jq -r '.recent_videos[] | "    \(.youtube_video_id)  [\(.privacy_status)]  \(.title)"' "$http_body" 2>/dev/null || true
  echo
  echo "  ※ 候補を private 化するには set_private に youtube_video_id を渡して再実行してください。"
else
  echo "  (JSON 以外のレスポンス)"
  cat "$http_body"
  echo
fi
