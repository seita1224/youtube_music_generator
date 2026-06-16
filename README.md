# youtube_music_generator

ローカル GPU (RTX 3090) 上で AI 生成楽曲動画を継続投稿する自動化システム。

- **音楽生成:** ACE-Step 1.5
- **画像生成:** Stable Diffusion 系
- **マスターLLM:** クラウドAPI(Claude / GPT 等、切替可)
- **指紋認識:** AcoustID + Chromaprint
- **バックエンド:** Python (FastAPI)
- **管理UI:** Next.js (App Router)

## アーキテクチャ概要

docker compose (backend / frontend / postgres) + host 直 GPU worker (systemd) の
**ハイブリッド構成** (ADR-0031)。 GPU worker は HTTP API 契約 + fsspec ストレージで
backend と疎結合し、 `GPU_WORKER_BASE_URL` を差し替えるだけで RunPod 等クラウド GPU へ
移行できる (backend/frontend のコード変更不要)。

```text
                ┌──────────────────────── docker compose ───────────────────────┐
  ブラウザ ──→  │  frontend (Next.js :3000)  ──→  backend (FastAPI :8000)        │
  (Basic 認証)  │                                   │   │                        │
                │                  PostgreSQL :5432 ─┘   │  APScheduler (in-proc) │
                │                  (pgvector)            │                        │
                └────────────────────────────────────────┼───────────────────────┘
                                                          │ HTTP (GPU_WORKER_BASE_URL)
                                                          ▼
                                      GPU worker (host 直 + systemd :8001)
                                      ACE-Step (音楽) + SDXL (画像)
                                          ↕ fsspec (file:// / s3://)
                                      共有ストレージ (/srv/ymg/outputs …)
```

- **計画 → 生成 → 投稿** のパイプライン中核: `backend/.../domain/pipeline/daily_cycle.py`
- **マスターLLM** は計画/タイトル/説明の生成のみ担当 (ADR-0008)。 投稿は dryrun を既定とし、
  人間が一手挟む運用 (ADR-0007 / ADR-0035)。
- 詳細な構成判断は ADR (下記) と [plan.md](specs/001-youtube-music-generator/plan.md) を参照。

## クイックスタート (docker 起動)

```bash
cp .env.example .env          # 最低 POSTGRES_PASSWORD / ADMIN_PASSWORD / FERNET_KEY を設定
make up                       # backend / frontend / postgres を起動
make migrate                  # alembic upgrade head (事前 pg_dump 込, ADR-0031)
make healthcheck              # backend / frontend / gpu_worker の /health を確認
```

- 管理 UI: `http://localhost:3000` (Basic 認証 = `ADMIN_USERNAME` / `ADMIN_PASSWORD`)
- frontend のホスト公開ポートは既定 **3000**。 ポート占有や Docker Desktop の転送 stuck 時は
  `.env` の `FRONTEND_HOST_PORT` を変更 (例 **3001**)。
- postgres も同様に `POSTGRES_HOST_PORT` で衝突回避できる (既定 5432)。
- 完全な手順 (モデル重みDL / OAuth / GPU worker 起動) は
  [quickstart.md](specs/001-youtube-music-generator/quickstart.md) を参照。
- 運用コマンド一覧は `make help`。

## ドキュメント

- [plan.md](specs/001-youtube-music-generator/plan.md) — 実装計画
- [spec.md](specs/001-youtube-music-generator/spec.md) — 機能仕様
- [screen-spec.md](specs/001-youtube-music-generator/screen-spec.md) — 画面仕様
- [quickstart.md](specs/001-youtube-music-generator/quickstart.md) — cold start 手順
- [data-model.md](specs/001-youtube-music-generator/data-model.md) — データモデル
- [contracts/](specs/001-youtube-music-generator/contracts/) — API 契約 (GPU worker / backend)
- [perf-notes.md](specs/001-youtube-music-generator/perf-notes.md) — 並列化余地 / prompt caching 計測
- [infra/runbooks/runpod-migration.md](infra/runbooks/runpod-migration.md) — RunPod 移行 runbook

- [requirements.md](specs/001-youtube-music-generator/requirements.md) — 要件定義 v2(最新スナップショット)
- [specs/001-youtube-music-generator/adr/](specs/001-youtube-music-generator/adr/) — Architecture Decision Records
  - [0001 システム構成](specs/001-youtube-music-generator/adr/0001-system-architecture-python-backend-nextjs-frontend.md)
  - [0002 マスターLLM クラウドAPI](specs/001-youtube-music-generator/adr/0002-master-llm-cloud-api-with-abstraction-layer.md)
  - [0003 動画フォーマット 30分](specs/001-youtube-music-generator/adr/0003-video-format-30min-via-six-track-stitching.md)
  - [0004 投稿規模 段階拡大](specs/001-youtube-music-generator/adr/0004-posting-volume-staged-from-1-2-per-day.md)
  - [0005 AcoustID 事前チェック](specs/001-youtube-music-generator/adr/0005-content-id-pre-check-with-acoustid.md)
  - [0006 日次/週次サイクル](specs/001-youtube-music-generator/adr/0006-cycle-structure-daily-and-weekly.md)
  - [0007 dryrun モード](specs/001-youtube-music-generator/adr/0007-dryrun-mode-as-mvp-requirement.md)
  - [0008 LLM 責務範囲](specs/001-youtube-music-generator/adr/0008-llm-responsibility-scope.md)

## ステータス

PoC 前の設計フェーズ。詳細は [requirements.md §11 次のアクション](specs/001-youtube-music-generator/requirements.md#11-次のアクション) を参照。

ADR は 0035 件まで蓄積(`0035` は staged rollout 方針)。 仕様は [spec.md](specs/001-youtube-music-generator/spec.md)、 実装計画は [plan.md](specs/001-youtube-music-generator/plan.md) を参照。

## ライセンス

Private
