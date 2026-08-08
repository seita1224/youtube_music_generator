#!/usr/bin/env bash
# 日次バックアップ (T130, ADR-0026 / FR-120 / FR-122)。
# 1) PostgreSQL を pg_dump (圧縮 -Fc) で ${BACKUP_ROOT}/db/db-<ts>.dump へ取得
# 2) 動画/サムネの「メタデータ」(.json/.txt 等の小さい付随物) を rsync でコピー
#    大容量の動画/音声本体 (.mp4/.wav 等) は BACKUP_INCLUDE_MEDIA=1 のときだけ含める (任意)
#
# Fernet 鍵 (FERNET_KEY / .env) は **絶対にバックアップに含めない** (ADR-0026):
#   - .env / FERNET_KEY を読まないし、 コピー対象にもしない
#   - DB ダンプと同じディスクに鍵を置かない前提を崩さない
#
# 冪等・非破壊: 既存ダンプは上書きせず (タイムスタンプ付き)、 rsync は --update で削除を伝播しない。
# 使い方: bash infra/scripts/backup.sh   または   make backup   または   systemd ymg-backup.service
set -euo pipefail

# .env があれば接続情報のみ読む (healthcheck.sh 踏襲)。
# 注意: FERNET_KEY もここで環境に載るが、 本スクリプトは一切バックアップに書き出さない。
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# --- 設定 (.env の値を踏襲、 既定はタスク指定どおり) ---
BACKUP_ROOT="${BACKUP_ROOT:-/srv/ymg/backups}"
DATA_ROOT="${DATA_ROOT:-./data}"
OUTPUTS_DIR="${OUTPUTS_DIR:-${DATA_ROOT%/}/outputs}"
PG_USER="${POSTGRES_USER:-ymg}"
PG_DB="${POSTGRES_DB:-ymg}"
COMPOSE="${COMPOSE:-docker compose}"
TS="$(date +%Y%m%d-%H%M%S)"

# 大容量本体を含めるか (既定: 含めない = メタデータのみ)。 1/true で本体も同期。
INCLUDE_MEDIA="${BACKUP_INCLUDE_MEDIA:-0}"

DB_DIR="${BACKUP_ROOT%/}/db"
MEDIA_DIR="${BACKUP_ROOT%/}/media"

echo "== backup (ADR-0026) =="
echo "  BACKUP_ROOT     : ${BACKUP_ROOT}"
echo "  DB              : ${PG_DB} (user=${PG_USER})"
echo "  outputs         : ${OUTPUTS_DIR}"
echo "  include media   : ${INCLUDE_MEDIA} (0=メタデータのみ)"
echo "  ※ Fernet 鍵 (FERNET_KEY/.env) はバックアップに含めません (ADR-0026)"
echo

# バックアップ先を用意 (冪等)。
mkdir -p "$DB_DIR" "$MEDIA_DIR"

# --- 1) PostgreSQL ダンプ (pg_dump -Fc, 圧縮ロジカルダンプ) ---
# docker compose 内の postgres で実行し、 ホスト側ファイルへリダイレクトする (-T = 非 TTY)。
dump_path="${DB_DIR}/db-${TS}.dump"
tmp_dump="${dump_path}.partial"
echo "== 1) pg_dump → ${dump_path} =="
if $COMPOSE exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$tmp_dump"; then
  # 完了したダンプだけを最終名へ (中断時に壊れた .dump を残さない = 非破壊)。
  mv -f "$tmp_dump" "$dump_path"
  echo "  ✅ DB ダンプ完了 ($(du -h "$dump_path" | cut -f1))"
else
  rm -f "$tmp_dump"
  echo "  ❌ pg_dump に失敗しました (postgres コンテナが起動しているか確認してください)。" >&2
  exit 1
fi

# --- 2) 動画/サムネのメタデータ同期 (rsync, 非破壊) ---
# 既定では .json/.txt/.yaml/.yml/.md だけを拾い、 動画/音声本体は除外する。
# --update で「より新しいものだけ上書き」かつ削除を伝播しない (冪等・非破壊)。
echo
echo "== 2) メタデータ同期 → ${MEDIA_DIR} =="
if [ -d "$OUTPUTS_DIR" ]; then
  if command -v rsync >/dev/null 2>&1; then
    rsync_filters=(
      --archive --update --human-readable
      --prune-empty-dirs
    )
    if [ "$INCLUDE_MEDIA" = "1" ] || [ "$INCLUDE_MEDIA" = "true" ]; then
      echo "  (BACKUP_INCLUDE_MEDIA 有効: 動画/音声本体も同期します)"
      rsync "${rsync_filters[@]}" "${OUTPUTS_DIR%/}/" "${MEDIA_DIR%/}/"
    else
      # メタデータ拡張子のみ include、 それ以外のファイルは exclude (ディレクトリは残す)。
      rsync "${rsync_filters[@]}" \
        --include='*/' \
        --include='*.json' --include='*.txt' \
        --include='*.yaml' --include='*.yml' --include='*.md' \
        --exclude='*' \
        "${OUTPUTS_DIR%/}/" "${MEDIA_DIR%/}/"
    fi
    echo "  ✅ メタデータ同期完了"
  else
    echo "  ⚠️  rsync が見つかりません。 メタデータ同期をスキップしました (DB ダンプは成功)。" >&2
  fi
else
  echo "  (outputs ディレクトリが無いためスキップ: ${OUTPUTS_DIR})"
fi

echo
echo "== backup 完了 =="
echo "  DB    : ${dump_path}"
echo "  media : ${MEDIA_DIR}"
echo
echo "  ※ 復元は make restore-db DUMP=${dump_path} (T131) を参照。"
echo "  ※ Fernet 鍵は別ディスク/パスワードマネージャに保管 (ADR-0026)。"
