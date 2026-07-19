# Quickstart — YouTube 音楽投稿自動化システム

> cold start からシステム稼働 + 初回 dryrun 投稿生成までの手順。 対象: 1 人運用、 ローカル GPU マシン(RTX 3090 / Ubuntu)。

## 0. 前提

- Ubuntu 22.04 LTS 以降(WSL2 でも可、 ただし systemd 設定は調整必要)
- NVIDIA driver 535+ がインストール済み(`nvidia-smi` で確認)
- 自宅 LAN 内、 LAN 信頼前提(ADR-0013)
- GitHub アカウント(Private repo クローン用)
- YouTube ブランドアカウント(Data API + Analytics API スコープ)
- Slack workspace(通知用、 incoming webhook)

確認コマンド:

```bash
nvidia-smi              # GPU 認識
docker --version        # 24+ 推奨
docker compose version
git --version
make --version
```

## 1. システムパッケージのインストール

```bash
sudo apt update
sudo apt install -y \
  build-essential pkg-config \
  ffmpeg \
  postgresql-client \
  libchromaprint-dev libchromaprint-tools \
  python3.13 python3.13-venv python3.13-dev \
  curl ca-certificates gnupg

# Node.js 24 LTS(NodeSource)
curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
sudo apt install -y nodejs

# Docker
# 公式手順: https://docs.docker.com/engine/install/ubuntu/

# NVIDIA Container Toolkit(将来コンテナ GPU 移行に備えて、 現状は host 直なので必須ではない)
# https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/

# uv (Python パッケージマネージャ、 ADR-0014)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. リポジトリのクローン

```bash
git clone git@github.com:seita1224/youtube_music_generator.git
cd youtube_music_generator
git checkout 001-youtube-music-generator   # MVP feature branch
```

## 3. `.env` 作成

```bash
cp .env.example .env
$EDITOR .env
```

最低限「これだけは値を入れる」4 つ: **`POSTGRES_PASSWORD` / `ADMIN_PASSWORD` / `FERNET_KEY` / `AUTH_SESSION_SECRET`**。
これらが空だと `make up` (compose の `:?` チェック) や backend / frontend 起動が失敗する。

必須項目 (テンプレ全体は `.env.example`):

```dotenv
# --- データ / ストレージ ---
DATA_ROOT=/srv/ymg                          # 動画/音楽/サムネ/モデル重みのルート
BACKUP_ROOT=/mnt/backup/ymg                 # ADR-0026 セカンダリディスク

# --- PostgreSQL ---
POSTGRES_HOST=postgres                      # コンテナ間は service 名。 host から alembic 直叩き時は localhost
POSTGRES_PORT=5432
POSTGRES_HOST_PORT=5432                      # ホスト公開ポート。 他プロジェクトの 5432 と衝突するなら変更 (例 5433)
POSTGRES_DB=ymg
POSTGRES_TEST_DB=ymg_test                    # integration テスト専用 DB (dev DB を壊さない)
POSTGRES_USER=ymg
POSTGRES_PASSWORD=__set_strong_value__       # ← 必須

# --- Fernet(ADR-0012)バックアップ対象外 ---
FERNET_KEY=__base64_44_chars__              # ← 必須。 YouTube OAuth + LLM API key 暗号化に共用 (ADR-0012 / ADR-0019)
                                            # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

# --- 管理 UI 認証(ADR-0013)---
ADMIN_USERNAME=admin
ADMIN_PASSWORD=__set_strong_value__          # ← 必須 (backend Basic + frontend ログイン検証。空は起動拒否)
AUTH_SESSION_SECRET=__set_strong_value__     # ← 必須。UTF-8 32 バイト以上。 `openssl rand -base64 48`
AUTH_COOKIE_SECURE=false                     # LAN HTTP は false。 HTTPS 時は true
# frontend compose は .env wholesale ではなく上記 + BACKEND_BASE_URL のみ注入する
# backend の /docs /redoc /openapi.json は無効。契約は specs/.../contracts/backend-api.yaml

# --- LLM Provider(ADR-0019)---
LLM_PROVIDER=ollama                         # openai / anthropic / ollama (ローカル推奨は ollama)
LLM_AUTH_MODE=api_key                       # api_key のみ正式サポート。 codex_oauth は未配線 (起動拒否 / PUT 422)
OPENAI_API_KEY=                             # 非空なら環境変数が SoT。空なら /llm から Fernet 暗号化 DB 保存可
ANTHROPIC_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434

# --- YouTube ---
YOUTUBE_CLIENT_ID=__google_cloud_oauth_id__
YOUTUBE_CLIENT_SECRET=__google_cloud_oauth_secret__
YOUTUBE_REDIRECT_URI=http://localhost:8000/auth/youtube/callback
YOUTUBE_CHANNEL_ID=UC...

# --- AcoustID(ADR-0005)---
ACOUSTID_API_KEY=__from_acoustid_org__

# --- Slack ---
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# --- GPU worker(ADR-0031)---
GPU_WORKER_BASE_URL=http://127.0.0.1:8001

# --- 動作モード ---
DRYRUN_DEFAULT=true                          # 初期は dryrun 推奨
MONTHLY_BUDGET_USD=50
```

**重要(ADR-0012, ADR-0026):**

- `FERNET_KEY` は **DB ダンプと同じディスクには絶対に置かない**(LAN 内別マシン or 物理 USB に backup)
- `.env` は **`.gitignore` で除外済み**、 リポジトリにコミットされない
- バックアップでは Fernet 鍵を **除外**(`infra/scripts/backup.sh` 内で明示)

## 4. データディレクトリ + バックアップディスク準備

```bash
sudo mkdir -p /srv/ymg/{models,outputs,backups}
sudo chown -R $USER:$USER /srv/ymg

# セカンダリディスクをマウント(ADR-0026)
# /etc/fstab に /mnt/backup を永続化、 詳細は infra/backup.md 参照
sudo mkdir -p /mnt/backup/ymg
sudo chown -R $USER:$USER /mnt/backup/ymg
```

## 5. モデル重みのダウンロード

```bash
# ACE-Step 1.5
huggingface-cli download ACE-Step/ACE-Step-v1-3.5B --local-dir /srv/ymg/models/acestep

# Juggernaut XL v10(ADR-0016)
huggingface-cli download RunDiffusion/Juggernaut-XL-v10 --local-dir /srv/ymg/models/sdxl/juggernaut-xl-v10
```

VRAM 構成確認用に最低 1 つは入れる、 他派生モデルは運用後に追加。

## 6. 依存インストール

```bash
# Backend
cd backend && uv sync --frozen && cd ..

# GPU worker
cd gpu_worker && uv sync --frozen && cd ..

# Frontend
cd frontend && npm ci && cd ..
```

## 7. PostgreSQL 起動 + 初期マイグレーション

```bash
# docker compose で postgres 起動
docker compose up -d postgres

# DB ready 待ち
until docker compose exec -T postgres pg_isready -U ymg; do sleep 1; done

# Alembic 初期マイグレーション(seed: genres 6 件 + app_state + model_pricing)
make migrate
# make migrate 未実装の段階では同等の生コマンドで代用できる:
#   docker compose exec -T backend uv run alembic upgrade head
```

`make migrate` は事前に `pg_dump` を `${BACKUP_ROOT}/pre-migrate-<ts>.dump` に取る(ADR-0031)。 初回は空 DB なので空ダンプになる。

> **integration テストは dev DB を壊さない:** テストは専用の `POSTGRES_TEST_DB`(既定 `ymg_test`)を
> 使う。 `make migrate` / `make up` が触る dev DB(`POSTGRES_DB`=`ymg`)とは別 DB なので、
> `make test` を流しても投稿履歴や app_state は消えない。

## 8. YouTube OAuth 初期化

```bash
# 一度だけ手動で OAuth flow を完走させる
make youtube-auth
# → ブラウザが開き Google アカウントで承認
# → backend が Fernet 暗号化して oauth_credentials テーブルに保存
```

スコープ(ADR-0021):

- `https://www.googleapis.com/auth/youtube.upload`
- `https://www.googleapis.com/auth/youtube`
- `https://www.googleapis.com/auth/yt-analytics.readonly`

## 9. GPU worker を systemd で起動

```bash
sudo cp infra/systemd/ymg-gpu-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ymg-gpu-worker

# ヘルスチェック
curl -fsS http://127.0.0.1:8001/health | jq
# 期待:
# {
#   "status": "ok",
#   "gpu_available": true,
#   "vram_free_mb": 20000,
#   "vram_total_mb": 24576,
#   "models_loaded": []
# }
```

## 10. backend + frontend を docker compose で起動

```bash
make up
# 内部実装:
# docker compose up -d backend frontend
# → backend が起動、 GPU worker と DB の health を確認

curl -fsS -u admin:__pass__ http://127.0.0.1:8000/health | jq
# {
#   "status": "ok",
#   "db": "ok",
#   "gpu_worker": "ok",
#   "scheduler_enabled": false,
#   "dryrun_enabled": true,
#   "llm_provider": "openai"
# }

# 管理 UI
open http://localhost:3000          # /login で ADMIN_* を入力 (既定ポート 3000)
# ポートを占有されている / Docker Desktop の転送が stuck する場合は
# .env の FRONTEND_HOST_PORT を変更 (例 3001) → make up し直し → http://localhost:3001
```

## 11. 初回 dryrun: DailyPlan 生成 → 動画生成 → 承認待ち

ブラウザの管理 UI で:

1. **Plans → "新規生成"** ボタン
   - cycle=daily, target_date=明日 を選択 → `POST /plans` 発火
   - 改善計画 LLM が走る(初期は analytics データなしのため few-shot 中心の出力)
   - 生成完了 → `plans.status="generated"` で UI に表示
2. **Plan 詳細画面で内容を確認** → 問題なければ "承認"
3. **Scheduler → "ON にする"** ボタン
   - `app_state.scheduler_enabled=true`
   - **dryrun_enabled が true なので投稿はされない、 動画生成だけ走る**
4. **しばらく待つ**(目安 15-20 分: ACE-Step ×6 + SDXL + ffmpeg)
   - `/jobs/stream` SSE で進捗監視可能(管理 UI に進捗バー表示)
5. **dryrun_outputs → 該当エントリで再生 + 承認 / 否認**
   - 承認 → YouTube に投稿(`status.containsSyntheticMedia=true` 確実に設定)
   - 否認 → 削除 + 否認理由を次の planner LLM 入力に活用

## 12. 検証: 投稿後の挙動確認

```bash
# YouTube Studio で動画確認
# - 公開済み
# - "Altered or synthetic content" ラベルが表示されている(containsSyntheticMedia=true 反映)
# - サムネがバイリンガル + Pillow オーバーレイで描画されている

# DB 確認
docker compose exec postgres psql -U ymg -d ymg -c \
  "SELECT youtube_video_id, title, posted_at, privacy_status, contains_synthetic_media FROM videos ORDER BY posted_at DESC LIMIT 5;"

# audit log
docker compose exec postgres psql -U ymg -d ymg -c \
  "SELECT actor, action, target_id, created_at FROM audit_log ORDER BY created_at DESC LIMIT 10;"
```

## 13. 定常運用 cron 設定

```bash
sudo cp infra/systemd/ymg-backup.timer /etc/systemd/system/
sudo cp infra/systemd/ymg-backup.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ymg-backup.timer
# 毎日 03:00 に pg_dump + 動画メタデータを /mnt/backup/ymg にコピー(ADR-0026)
```

## 14. 緊急停止のドリル(運用前に 1 回試す)

```bash
# scheduler を一旦止めて、 panic-stop 経路の動作確認
make panic-stop
# - 確認プロンプト → "yes"
# - 直近 24h の動画一覧表示
# - private に変更したい動画を選択(初回はテスト動画 1 本のみ)
# - audit_log に記録される
```

## 15. ロールバック手順(確認)

```bash
# 何か壊した場合
git log --oneline
git revert <bad-sha>
git push origin 001-youtube-music-generator
make deploy
# → healthcheck.sh 全 green になることを確認
```

## トラブルシュート

| 症状 | 対処 |
|---|---|
| `nvidia-smi` で 24GB 認識せず | NVIDIA driver 再インストール、 reboot |
| `make migrate` 失敗 | `/srv/ymg/backups/pre-migrate-*.sql` から手動 restore |
| GPU worker `/health` 不通 | `journalctl -u ymg-gpu-worker -f` でログ確認 |
| backend が GPU worker に届かない | `GPU_WORKER_BASE_URL` の値、 firewall、 systemd 状態 |
| LLM validation 失敗連発 | `usage_log` で provider/model 確認、 prompt_version 切替 |
| AcoustID API quota | 無料枠は十分なはず、 連発するなら一旦 ジャンル一時停止 |
| YouTube quota 超過 | 翌日まで待機(automatic recovery) |
| dryrun_outputs が溜まりすぎ | retention ジョブが 7 日後に auto_expired にする、 手動で `/dryrun/outputs?state=pending` で確認 |

## 次のステップ

- ACE-Step PoC(2-3 日): 6 トラック連結の主観品質確認
- 月 30〜60 本のペースで投稿し、 1 か月運用後に SC-010 を数値確定する(将来 ADR)
- experiment_slot で新ジャンル投入を試す(ADR-0033)
- analytics_daily が貯まったら few-shot を dynamic 選別に切替(ADR-0033 B3 昇格、 将来 ADR)

## 関連ドキュメント

- [plan.md](./plan.md)
- [spec.md](./spec.md)
- [data-model.md](./data-model.md)
- [contracts/](./contracts/)
- [requirements.md](./requirements.md)
- [./adr/](./adr/)
