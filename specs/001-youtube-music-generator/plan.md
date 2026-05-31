# Implementation Plan: YouTube 音楽投稿自動化システム

**Branch**: `001-youtube-music-generator` | **Date**: 2026-05-26 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-youtube-music-generator/spec.md`

## Summary

ローカル GPU マシン 1 台で AI 生成楽曲動画を継続投稿する自動化システム。 改善計画 LLM が日次 / 週次サイクルで投稿方針を決め、 ACE-Step が音楽、 SDXL がサムネ背景、 ffmpeg が 30 分動画を組み立て、 AcoustID + Chromaprint で事前チェック後 YouTube に投稿する。 dryrun と panic-stop で安全弁を構造化し、 5 カテゴリのエラーハンドリングで自律運用する。

技術アプローチ: backend(Python + FastAPI) + frontend(Next.js) + GPU worker(host 直 + systemd、 Dockerfile 用意でクラウド GPU 移行可) + PostgreSQL(+ pgvector) のモノレポ構成。 LLM Provider 抽象化(OpenAI / Anthropic / Ollama)、 ストレージ抽象化(fsspec)、 directive parser + Pydantic structured output、 prompt caching を活用したコスト最適化。

## Technical Context

**Language/Version**: Python 3.13(backend / GPU worker)、 TypeScript 5.x + Node.js 22 LTS(frontend)

**Primary Dependencies**:

- backend: FastAPI / SQLAlchemy 2.x + Alembic / Pydantic v2 / APScheduler / loguru / pyacoustid / cryptography(Fernet) / google-api-python-client / google-auth / slack_sdk / fsspec
- GPU worker: FastAPI / PyTorch(CUDA 12.x) / ACE-Step 1.5 / diffusers + Juggernaut XL v10 / Pillow
- frontend: Next.js 15 (App Router) / shadcn/ui / tanstack/query / recharts / openapi-typescript
- LLM SDK: anthropic / openai / ollama

**Storage**: PostgreSQL 16 + pgvector 拡張、 ファイルストレージは fsspec 抽象化(初期 `file://`、 移行容易な S3/R2 対応)

**Testing**:

- backend: pytest + pytest-asyncio + httpx + factory_boy + freezegun + respx(HTTP mock)、 critical path 100%、 その他 60-70%
- frontend: vitest + Testing Library + Playwright(E2E、 critical user flow)
- GPU worker: pytest、 GPU 実行系はスナップショット / mock

**Target Platform**:

- 開発 / 本番(共用): Ubuntu 22.04+ on RTX 3090 (24GB VRAM), 自宅 LAN
- 将来移行先: RunPod Pod / Serverless(Dockerfile 経由)

**Project Type**: Monorepo with backend (Python web service) + frontend (Next.js) + GPU worker (Python web service)

**Performance Goals**:

- 1 動画生成 = 30 分未満(目標 15-20 分): ACE-Step 6 トラック × ~2 分 + SDXL ~30 秒 + ffmpeg ~3 分 + アップロード ~5 分
- 改善計画 LLM 1 呼び出し ≤ 30 秒(prompt caching 効いた状態)
- 管理 UI SSE 配信遅延 ≤ 1 秒
- `make panic-stop` 完了 ≤ 60 秒(24 本想定)

**Constraints**:

- VRAM 24GB 上限 → ACE-Step (8〜12GB) + SDXL (~7GB fp16) の同時ロード可否を PoC で確認、 不可なら逐次切替
- YouTube API quota = 日 10,000 units(1 投稿 ≈ 1,600 units、 1 日 1〜2 本で十分)
- LLM 月予算(暫定): $50 / 月、 50/80/100% で Slack alert
- ストレージ: ローカル 1〜2 TB(動画は posted 後 N 日で削除)

**Scale/Scope**:

- 投稿規模: 月 30〜60 本(1 日 1〜2 本、 ADR-0004)
- 1 動画 = 30 分(5 分 × 6 トラック連結)
- 単一ユーザー / 単一チャネル運用(複数ユーザー対応は将来課題)
- 6 ジャンル初期辞書、 experiment_slot 経由で拡張

## Constitution Check

`.specify/memory/constitution.md` の原則(I〜VII)に対する評価。

### I. Compliance-First (NON-NEGOTIABLE)

- ✅ `containsSyntheticMedia=true` 必須化バリデーション(FR-006, FR-007)
- ✅ AcoustID 投稿前プレチェック(FR-010〜012)
- ✅ `make panic-stop` 緊急停止(FR-102)
- ✅ `compliance` エラーカテゴリで投稿停止 + private 化(FR-112)
- **判定**: 合致

### II. Test-First on Critical Paths (NON-NEGOTIABLE)

- ✅ critical path: AcoustID / containsSyntheticMedia / OAuth crypto / Pydantic validation / directive parser を 100%(ADR-0027)
- ✅ TDD は critical path 必須、 best effort 60-70%
- ⚠️ panic-stop の YouTube private 化は新規 critical、 テスト計画で `respx` mock + 統合テストを必須
- **判定**: 合致 (panic-stop テストを critical path に追加)

### III. dryrun-First Operational Safety (NON-NEGOTIABLE)

- ✅ dryrun MVP 必須機能(FR-060〜063)
- ✅ reboot 後 scheduler 手動 enable(FR-072)
- ✅ dryrun lifecycle state 管理(FR-061)
- **判定**: 合致

### IV. Provider / Resource Abstraction

- ✅ LLMProvider 抽象化(FR-020〜022)
- ✅ ストレージ fsspec(FR-087)
- ✅ GPU worker HTTP API 契約(FR-090〜094)
- ✅ Anthropic サブスク利用拒否(FR-022)
- **判定**: 合致

### V. Structured Errors with Explicit Categories

- ✅ 5 カテゴリ分類(FR-111)
- ✅ compliance / fatal の自動対応定義(FR-112, FR-113)
- **判定**: 合致

### VI. Structured Observability

- ✅ loguru JSON ログ(FR-110)
- ✅ usage_log(FR-025)
- ✅ plan_metric_snapshot(FR-035)
- ✅ プロンプトバージョン管理(FR-036)
- **判定**: 合致

### VII. ADR-Driven Decisions

- ✅ 34 ADR 既存、 新規判断時は ADR 必須
- **判定**: 合致

**Pre-Phase 0 Gate**: ✅ Pass(違反なし)

## Project Structure

### Documentation (this feature)

```text
specs/001-youtube-music-generator/
├── plan.md              # This file
├── spec.md              # Feature spec(grill 反映済み)
├── research.md          # Phase 0(grill 集約)
├── data-model.md        # Phase 1(DB スキーマ)
├── quickstart.md        # Phase 1(セットアップ手順)
├── contracts/
│   ├── backend-api.yaml          # FastAPI OpenAPI
│   ├── gpu-worker-api.yaml       # GPU worker HTTP
│   └── llm-provider-interface.md # LLMProvider 抽象
└── tasks.md             # Phase 2(/speckit-tasks で生成)
```

### Source Code (repository root) — モノレポ(ADR-0029)

```text
youtube_music_generator/
├── README.md
├── requirements.md
├── CLAUDE.md
├── Makefile                           # ADR-0031: make up/deploy/migrate/panic-stop/...
├── docker-compose.yml
├── .env.example
├── .gitignore
│
├── docs/
│   ├── adr/                  # 0001-0035 + 0000-template
│   ├── requirements.md       # 要件定義(旧 REQUIREMENTS.md)
│
├── specs/                             # speckit
│   └── 001-youtube-music-generator/
│
├── backend/                           # FastAPI
│   ├── pyproject.toml                 # uv (ADR-0014)
│   ├── uv.lock
│   ├── alembic.ini
│   ├── src/
│   │   └── ymg_backend/
│   │       ├── api/                   # FastAPI routers
│   │       │   ├── plans.py
│   │       │   ├── posts.py
│   │       │   ├── dryrun.py
│   │       │   ├── scheduler.py
│   │       │   ├── analytics.py
│   │       │   ├── health.py
│   │       │   └── sse.py
│   │       ├── core/                  # app factory, config, security
│   │       │   ├── config.py          # pydantic-settings
│   │       │   ├── security.py        # Basic auth, Fernet
│   │       │   └── logging.py         # loguru setup
│   │       ├── domain/                # ビジネスロジック
│   │       │   ├── plans/             # DailyPlan/WeeklyPlan (ADR-0032)
│   │       │   ├── directive/         # parser (ADR-0017)
│   │       │   ├── compliance/        # AcoustID, containsSyntheticMedia
│   │       │   ├── errors/            # 5 カテゴリ (ADR-0028)
│   │       │   └── templates/         # YAML テンプレ展開
│   │       ├── llm/                   # LLMProvider 抽象 (ADR-0018, 0019)
│   │       │   ├── base.py
│   │       │   ├── openai_provider.py
│   │       │   ├── anthropic_provider.py
│   │       │   ├── ollama_provider.py
│   │       │   └── pricing.py
│   │       ├── infrastructure/
│   │       │   ├── db/                # SQLAlchemy models, Alembic
│   │       │   ├── storage/           # fsspec wrapper (ADR-0022)
│   │       │   ├── youtube/           # Data API + Analytics API
│   │       │   ├── slack/             # 通知
│   │       │   ├── gpu_worker_client.py  # HTTP to GPU worker
│   │       │   └── scheduler.py       # APScheduler
│   │       └── main.py                # FastAPI app
│   ├── alembic/
│   │   └── versions/
│   ├── prompts/                       # ADR-0033 バージョン管理
│   │   ├── planner/system_v1.md
│   │   ├── planner/few_shot_v1.json
│   │   ├── finisher/title_v1.md
│   │   └── finisher/description_v1.md
│   ├── templates/                     # ADR-0034
│   │   ├── title/*.yaml × 6
│   │   ├── description/default.yaml
│   │   ├── description/_shared/
│   │   ├── thumbnail/*.yaml × 6
│   │   ├── thumbnail/_shared/
│   │   └── fonts/                     # SIL OFL 同梱
│   └── tests/
│       ├── unit/
│       ├── integration/
│       └── critical/                  # 100% 必須(ADR-0027)
│
├── gpu_worker/                        # ACE-Step + SDXL (ADR-0031)
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── Dockerfile                     # クラウド移行用に最初から用意
│   ├── src/
│   │   └── ymg_gpu_worker/
│   │       ├── api/                   # FastAPI: /generate/music, /generate/image, /jobs/{id}, /health
│   │       ├── runners/
│   │       │   ├── acestep.py
│   │       │   └── sdxl.py
│   │       ├── jobs/                  # in-process queue
│   │       └── main.py
│   └── tests/
│
├── frontend/                          # Next.js (App Router)
│   ├── package.json
│   ├── next.config.ts
│   ├── tsconfig.json
│   ├── app/
│   │   ├── (admin)/
│   │   │   ├── plans/                 # DailyPlan/WeeklyPlan 一覧/詳細
│   │   │   ├── dryrun/                # レビュー画面
│   │   │   ├── scheduler/             # ON/OFF
│   │   │   ├── analytics/             # retention 等
│   │   │   ├── prompts/               # プロンプト介入
│   │   │   └── jobs/                  # SSE 進捗
│   │   └── api/                       # API route proxy(if needed)
│   ├── lib/
│   │   ├── api/                       # generated by openapi-typescript
│   │   └── auth/                      # Basic 認証ヘッダ
│   └── tests/
│       ├── unit/
│       └── e2e/                       # Playwright critical flow
│
├── infra/                             # ADR-0031
│   ├── systemd/
│   │   ├── ymg-stack.service
│   │   └── ymg-gpu-worker.service
│   ├── scripts/
│   │   ├── deploy.sh
│   │   ├── healthcheck.sh
│   │   ├── backup.sh                  # ADR-0026
│   │   └── panic-stop.sh
│   └── nginx/                         # (オプション)
│
└── data/                              # gitignore
    ├── models/                        # ACE-Step + SDXL 重み
    ├── outputs/                       # 動画/音楽/サムネ生成物
    └── backups/                       # ADR-0026 ローカル 2nd disk マウント
```

**Structure Decision**: Web application(monorepo with backend + frontend)に GPU worker を追加した 3-process 構成。 ADR-0029 のモノレポ + ディレクトリ分割方針を反映。 `infra/` で systemd unit と運用スクリプト、 `./adr/` で意思決定履歴、 `specs/` で speckit ライフサイクル成果物を管理。 `data/` は実行時生成物で `.gitignore`。

## Phase Plan

### Phase 0: Outline & Research ✅(本ファイル + research.md で完了)

- ADR 34 件と grill-me セッションを research.md に集約
- NEEDS CLARIFICATION 残ゼロ(将来 ADR としてリストアップ済み)

### Phase 1: Design & Contracts ✅(本ファイル + data-model.md + contracts/ + quickstart.md)

1. **data-model.md**: DB スキーマ(SQLAlchemy / Alembic 想定の DDL + ER 構造)
2. **contracts/backend-api.yaml**: FastAPI が露出する OpenAPI(管理 UI 連携 + frontend 型生成)
3. **contracts/gpu-worker-api.yaml**: GPU worker HTTP API(ADR-0031 の契約)
4. **contracts/llm-provider-interface.md**: LLMProvider 抽象 interface 定義(ADR-0018, 0019)
5. **quickstart.md**: 開発者(自分)が cold start からシステム稼働までを再現する手順
6. **Agent context update**: CLAUDE.md に SPECKIT START/END マーカーで plan.md 参照を挿入

### Phase 2: Task generation(`/speckit-tasks` で実行 — 本コマンド対象外)

## Post-Phase 1 Constitution Check Re-eval

Phase 1 成果物を踏まえた再評価。

- ✅ I. Compliance: data-model.md で `videos.contains_synthetic_media` 必須カラム + check constraint、 contracts/backend-api.yaml で投稿前バリデーションエンドポイント定義
- ✅ II. Test-First: critical path テストを `backend/tests/critical/` 配下に分離、 panic-stop の YouTube private 化テストも含む
- ✅ III. dryrun-First: contracts/backend-api.yaml で dryrun レビュー API を定義、 data-model.md で `dryrun_outputs.state` enum 型
- ✅ IV. Abstraction: contracts/llm-provider-interface.md + contracts/gpu-worker-api.yaml で interface 化、 fsspec は infrastructure/storage モジュール
- ✅ V. Structured Errors: data-model.md に `job_history.error_category` enum、 errors モジュールでカテゴリ別ハンドラ
- ✅ VI. Observability: data-model.md に `usage_log` / `plan_metric_snapshot`、 loguru で全構造化
- ✅ VII. ADR: 既存 34 ADR を spec.md / research.md / data-model.md / contracts が参照

**Post-Phase 1 Gate**: ✅ Pass(新規違反なし)

## Complexity Tracking

> 構成上の複雑性は ADR で受容済み。 新規違反なし。

| 受容項目 | 理由 | より単純な代替が採用されなかった理由 |
| --- | --- | --- |
| backend + frontend + gpu_worker の 3-process 構成 | GPU worker 分離は RunPod 移行可能性の要件(ADR-0031) | 単一プロセスだと GPU 切り替えコストが膨大 |
| docker compose + systemd 並走 | GPU は host 直、 それ以外はコンテナ化(ADR-0031 ハイブリッド) | 全コンテナ化は driver/cuda トラブルコスト、 全 systemd は pgvector セットアップ難 |
| LLM Provider 抽象層 | OpenAI/Anthropic/Ollama 切替必須(ADR-0019) | 単一 Provider 固定は SPOF + 単価上昇リスク |
| 改善計画 LLM + 仕上げ LLM の 2 段 | コスト分離 + hallucination 局所化(ADR-0032) | 1 LLM で全部書かせると 1 ハズしで 1 動画分の GPU 損失 |
| dryrun lifecycle state 5 種類 | 不可逆 YouTube 投稿の安全弁(ADR-0025) | bool フラグだと「無反応」と「明示拒否」の区別なし |

## Next: Phase 2

`/speckit-tasks` 実行で tasks.md を生成し、 実装フェーズに入る。 タスクは ADR-0027 のテスト戦略(critical path 100%)に従い、 TDD で進める。

## References

- [spec.md](./spec.md)
- [research.md](./research.md)
- [data-model.md](./data-model.md)
- [quickstart.md](./quickstart.md)
- [contracts/](./contracts/)
- [.specify/memory/constitution.md](../../.specify/memory/constitution.md)
- [requirements.md](./requirements.md)
- [./adr/](./adr/)
