---

description: "Implementation task list for YouTube 音楽投稿自動化システム MVP"
---

# Tasks: YouTube 音楽投稿自動化システム

**Input**: Design documents from `specs/001-youtube-music-generator/`

**Prerequisites**: plan.md / spec.md / research.md / data-model.md / contracts/ / quickstart.md(全て揃い済)

**Tests**: 含む。 Constitution II(Critical Path TDD NON-NEGOTIABLE、 ADR-0027)に従い、 critical path は **test-first 必須 100% カバレッジ**、 それ以外は best effort 60-70%。

**Organization**: タスクは User Story ごとにグループ化、 独立実装 / 独立検証 / 独立デモが可能。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 並列実行可能(別ファイル、 未完依存タスクなし)
- **[Story]**: US1〜US7(spec.md User Stories)
- 絶対パスでなく リポジトリ相対パスで記載

## Path Conventions

モノレポ(ADR-0029):

- backend: `backend/src/ymg_backend/...`
- frontend: `frontend/...`
- GPU worker: `gpu_worker/src/ymg_gpu_worker/...`
- infra: `infra/...`
- ADR / spec: `specs/001-youtube-music-generator/...`

---

## Phase 1: Setup(共有インフラ初期化)

**Purpose**: モノレポ構造とビルド / lint / format / CI ツールの初期化。

- [x] T001 リポジトリ直下構造を作成: `backend/`, `frontend/`, `gpu_worker/`, `infra/{systemd,scripts}`, `data/{models,outputs,backups}` を git ignore で初期化
- [x] T002 [P] ルート `Makefile` 作成: `make up/down/deploy/migrate/restart-backend/restart-gpu/logs/panic-stop/help` ターゲットを stub で配置(ADR-0031)
- [x] T003 [P] ルート `docker-compose.yml` 作成: backend / frontend / postgres(pgvector イメージ) サービスを定義
- [x] T004 [P] `.env.example` 作成: quickstart.md §3 の全 env 変数を値抜きで列挙
- [x] T005 [P] `.gitignore` 作成: `.env`, `data/`, `**/node_modules/`, `**/.venv/`, `**/__pycache__/`, `**/uv.lock` 例外, `**/dist/`, `backups/`
- [x] T006 [P] backend `backend/pyproject.toml` 初期化: uv で Python 3.13、 FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2, APScheduler, loguru, fsspec, pyacoustid, cryptography, google-api-python-client, anthropic, openai, ollama, ruff, mypy, pytest, pytest-asyncio, httpx, respx, factory-boy, freezegun
- [x] T007 [P] gpu_worker `gpu_worker/pyproject.toml` 初期化: uv で Python 3.13、 FastAPI, torch(cu124), diffusers, Pillow, fsspec, pyacoustid(オプション), pytest
- [x] T008 [P] gpu_worker `gpu_worker/Dockerfile` 作成: nvidia/cuda:12.4-runtime-ubuntu22.04 ベース、 uv install、 ACE-Step + SDXL ランタイム前提(ADR-0031)
- [x] T009 [P] frontend `frontend/package.json` 初期化: Next.js 15 (App Router) + TypeScript 5、 shadcn/ui、 @tanstack/react-query、 recharts、 openapi-typescript、 vitest、 @playwright/test
- [x] T010 [P] backend `backend/ruff.toml` + `backend/mypy.ini`: lint / 型チェック設定
- [x] T011 [P] frontend `frontend/.eslintrc` + `frontend/tsconfig.json`: lint / 型チェック設定
- [x] T012 [P] `.github/workflows/ci.yml` 作成: backend (ruff + mypy + pytest), frontend (eslint + tsc + vitest), gpu_worker (`docker build` のみ確認)。 paths フィルタで分岐
- [x] T013 [P] `infra/scripts/healthcheck.sh` stub: backend / frontend / gpu_worker `/health` を curl で叩く
- [x] T014 backend `backend/alembic.ini` 初期化 + `backend/alembic/env.py`、 `backend/alembic/versions/` ディレクトリ作成
- [x] T015 [P] CLAUDE.md / .github copilot-instructions / .cursor rules を `specs/001-youtube-music-generator/plan.md` 参照に統一(既に CLAUDE.md は更新済、 他 IDE 系も確認)

---

## Phase 2: Foundational(全 User Story 共通の前提)

**Purpose**: 全 US が依存する基盤(DB スキーマ / config / logging / security / 抽象化層)。

**⚠️ CRITICAL**: このフェーズ完了まで User Story 実装は開始しない。

### DB / マイグレーション

- [x] T016 backend `backend/alembic/versions/001_initial.py` 作成: data-model.md の全 ENUM(plan_cycle, plan_status, post_status, gpu_job_type, gpu_job_status, dryrun_state, error_category, acoustid_status, youtube_privacy_status, llm_provider, llm_auth_mode)を DDL 化
- [x] T017 backend 001_initial.py に全テーブル DDL を追加(genres / plans / posts / plan_metric_snapshot / videos / audio_tracks / gpu_jobs / dryrun_outputs / oauth_credentials / usage_log / model_pricing / analytics_daily / comments / job_history / app_state / audit_log)
- [x] T018 backend 001_initial.py の seed: `genres` 6 件(ADR-0033)、 `app_state` 4 件(scheduler_enabled=false 等)、 `model_pricing` の初期単価表
- [x] T019 `make migrate` から alembic upgrade head が走り、 各テーブル / ENUM が postgres に展開されることを確認
- [x] T020 [P] backend `backend/src/ymg_backend/infrastructure/db/session.py`: SQLAlchemy 2.x AsyncSession + engine
- [x] T021 [P] backend `backend/src/ymg_backend/infrastructure/db/models/` 配下に各テーブルの ORM model を 1 ファイル 1 entity で配置(15 テーブル)

### Config / Security / Logging

- [x] T022 [P] backend `backend/src/ymg_backend/core/config.py`: pydantic-settings で `.env` 読み込み(全 quickstart §3 変数)
- [x] T023 [P] backend `backend/src/ymg_backend/core/logging.py`: loguru JSON sink、 context binding(`video_id`, `genre`, `step`)
- [x] T024 [P] backend `backend/src/ymg_backend/core/security.py`: Basic auth dependency(FastAPI Depends で全 endpoint に効く形)、 Fernet 暗号化 / 復号ヘルパ
- [x] T025 [P] backend `backend/src/ymg_backend/domain/errors/__init__.py`: 5 error カテゴリ(transient/recoverable/fatal/compliance/quality)の Exception 階層 + `error_category` 解決ロジック(ADR-0028)

### Critical Path: Fernet 暗号化(US1 で利用)

- [x] T026 [P] [Foundation] **TEST FIRST** `backend/tests/critical/test_security_fernet.py`: 暗号化 / 復号 / 鍵不一致 / 改竄検出 のラウンドトリップを 100% カバー(Constitution II)
- [x] T027 T026 が fail することを確認 → backend `core/security.py` の Fernet 部分を実装 → T026 を green に

### LLM Provider 抽象化(ADR-0019、 contracts/llm-provider-interface.md)

- [x] T028 [P] [Foundation] backend `backend/src/ymg_backend/llm/base.py`: LlmProvider ABC + LlmMessage / LlmRequest[T] / LlmResponse[T] / LlmUsage / LlmError(generic Pydantic v2)
- [x] T029 [P] [Foundation] backend `backend/src/ymg_backend/llm/openai_provider.py`: Responses API + JSON Schema structured output + prompt caching 自動利用
- [x] T030 [P] [Foundation] backend `backend/src/ymg_backend/llm/anthropic_provider.py`: Messages API + tool_use 経由 structured output + `cache_control: ephemeral`、 **subscription auth mode を起動時拒否**
- [x] T031 [P] [Foundation] backend `backend/src/ymg_backend/llm/ollama_provider.py`: chat API + `model_validate_json` での後検証
- [x] T032 [P] [Foundation] backend `backend/src/ymg_backend/llm/pricing.py`: `model_pricing` テーブル参照 + `cost_usd` 計算(prompt caching 割引対応)
- [x] T033 [P] [Foundation] backend `backend/src/ymg_backend/llm/usage_writer.py`: 全 LLM 呼び出しを `usage_log` に永続化
- [x] T034 [Foundation] backend `backend/src/ymg_backend/llm/factory.py`: env / app_state から active provider 解決、 Anthropic + subscription 起動時拒否(FR-022)

### Critical Path: LLM Pydantic Validation

- [x] T035 [P] [Foundation] **TEST FIRST** `backend/tests/critical/test_llm_provider.py`: respx で OpenAI / Anthropic / Ollama レスポンスを mock、 Pydantic validation 失敗時の最大 2 回リトライ(temperature 低下)+ `recoverable` 例外、 Anthropic + subscription の起動時拒否、 prompt caching hit カウント、 cost 計算を 100% カバー
- [x] T036 T028〜T034 を T035 で driven の形で完成、 T035 green 化

### Storage 抽象化(ADR-0022)

- [x] T037 [P] [Foundation] backend `backend/src/ymg_backend/infrastructure/storage/fsspec_wrapper.py`: `file:// / s3:// / gs://` 統一インターフェース、 URI からの read/write/exists/delete

### Pydantic v2 ドメインモデル(ADR-0032)

- [x] T038 [P] [Foundation] backend `backend/src/ymg_backend/domain/plans/schemas.py`: DailyPlan / DailyPost / WeeklyPlan / ExperimentSlot / ReferencedMetrics / ExpectedKpi(data-model.md / ADR-0032 と完全整合)
- [x] T039 [P] [Foundation] backend `backend/src/ymg_backend/domain/plans/schemas.py` に validator: `genre` 辞書照合(context 注入)、 `genre_distribution` 合計 1.0、 `posts` 1〜2 件、 各 min_length 制約

### Critical Path: Directive Parser(ADR-0017)

- [x] T040 [P] [Foundation] **TEST FIRST** `backend/tests/critical/test_directive_parser.py`: `{{genre}}` 変数 / `{{12字以内の日本語サブタイト}}` 自由文 / mixed 入力 / 未定義変数エラー / 空 directive 拒否 を 100% カバー
- [x] T041 [Foundation] backend `backend/src/ymg_backend/domain/directive/parser.py` を T040 で driven の形で実装

### Prompt / Template Loaders

- [x] T042 [P] [Foundation] backend `backend/src/ymg_backend/domain/prompts/loader.py`: `backend/prompts/<area>/<name>_v<N>.md` を version 解決 + 読み込み
- [x] T043 [P] [Foundation] backend `backend/src/ymg_backend/domain/templates/loader.py`: `backend/templates/{title,description,thumbnail}/*.yaml` を YAML パース、 ジャンル → テンプレ解決
- [x] T044 [P] [Foundation] backend `backend/prompts/planner/system_v1.md` 配置(ADR-0033 system prompt 骨格を実体化)
- [x] T045 [P] [Foundation] backend `backend/prompts/planner/few_shot_v1.json` 配置(手書き DailyPlan サンプル 1 件)
- [x] T046 [P] [Foundation] backend `backend/prompts/finisher/title_v1.md` + `description_v1.md` 配置
- [x] T047 [P] [Foundation] backend `backend/templates/title/*.yaml` × 6 ジャンル(ADR-0034)
- [x] T048 [P] [Foundation] backend `backend/templates/description/default.yaml` + `_shared/{ai_disclosure,channel_promo}.txt`
- [x] T049 [P] [Foundation] backend `backend/templates/thumbnail/*.yaml` × 6 + `_shared/{layout,badge}.json`
- [ ] T050 [P] [Foundation] backend `backend/templates/fonts/` に SIL OFL ライセンスフォント 6 + Noto Sans JP を同梱

### audit_log Writer

- [x] T051 [P] [Foundation] backend `backend/src/ymg_backend/infrastructure/audit.py`: `audit_log` 書き込みヘルパ(actor / action / target / payload)

### FastAPI スケルトン

- [x] T052 [Foundation] backend `backend/src/ymg_backend/main.py`: FastAPI app factory、 Basic auth dependency 適用、 `/health` endpoint、 lifespan で DB connect / scheduler 起動準備、 `app_state.scheduler_enabled` 初期確認(false 起動、 ADR-0031)
- [x] T053 [Foundation] backend `backend/src/ymg_backend/api/health.py`: `GET /health` で DB / GPU worker / scheduler_enabled / dryrun_enabled / llm_provider を返す

### GPU Worker 骨格

- [x] T054 [P] [Foundation] gpu_worker `gpu_worker/src/ymg_gpu_worker/main.py`: FastAPI app + `/health`(gpu_available / vram_free_mb / models_loaded)
- [x] T055 [P] [Foundation] gpu_worker `gpu_worker/src/ymg_gpu_worker/api/generate.py`: `POST /generate/music` + `POST /generate/image` + `GET /jobs/{id}` の stub(contracts/gpu-worker-api.yaml と整合)
- [x] T056 [Foundation] gpu_worker `gpu_worker/src/ymg_gpu_worker/jobs/queue.py`: in-process job queue(asyncio Queue + dict による状態管理)
- [x] T057 [Foundation] gpu_worker `gpu_worker/src/ymg_gpu_worker/runners/acestep.py`: ACE-Step 1.5 ローダ + 1 リクエストごとに音楽生成(prompt / duration_sec / bpm / seed)
- [x] T058 [Foundation] gpu_worker `gpu_worker/src/ymg_gpu_worker/runners/sdxl.py`: SDXL 派生モデルローダ(default Juggernaut XL v10)+ 画像生成
- [x] T059 [P] [Foundation] gpu_worker `gpu_worker/src/ymg_gpu_worker/infrastructure/storage.py`: fsspec wrapper(backend と同等インターフェース)

### Backend → GPU Worker HTTP Client

- [x] T060 [P] [Foundation] backend `backend/src/ymg_backend/infrastructure/gpu_worker_client.py`: httpx.AsyncClient ラッパ、 `GPU_WORKER_BASE_URL` を env から、 retry / timeout / health チェック

### systemd Units

- [x] T061 [P] [Foundation] `infra/systemd/ymg-gpu-worker.service`: `ExecStart=uv run uvicorn ymg_gpu_worker.main:app --host 127.0.0.1 --port 8001`、 `Restart=on-failure`、 enable 推奨
- [x] T062 [P] [Foundation] `infra/systemd/ymg-stack.service`: `ExecStart=docker compose up -d backend frontend postgres`、 `Restart=on-failure`
- [x] T063 [P] [Foundation] `infra/systemd/ymg-backup.service` + `ymg-backup.timer`: 日次 03:00 で `infra/scripts/backup.sh` 実行(ADR-0026)

### Frontend スケルトン

- [x] T064 [P] [Foundation] frontend `frontend/app/layout.tsx` + `frontend/app/(admin)/layout.tsx`: shadcn/ui 適用、 ナビゲーション(Plans / Posts / Dryrun / Scheduler / Analytics / Prompts / LLM)
- [x] T065 [P] [Foundation] frontend `frontend/lib/auth.ts`: Basic 認証ヘッダー注入の fetch wrapper
- [x] T066 [P] [Foundation] frontend `frontend/lib/api/` + `package.json` script で `openapi-typescript ./contracts/backend-api.yaml -o lib/api/schema.ts` を実行できるよう設定
- [x] T067 [P] [Foundation] frontend `frontend/app/(admin)/page.tsx`: ダッシュボードホーム(scheduler 状態 + 直近 jobs 件数 + LLM provider 表示)
- [x] T068 [P] [Foundation] frontend `frontend/lib/api/health.ts` + ダッシュボードで backend `/health` を表示

### Compliance: containsSyntheticMedia バリデーション(US1 で消費)

- [x] T069 [P] [Foundation] **TEST FIRST** `backend/tests/critical/test_compliance_validation.py`: `containsSyntheticMedia=true` 設定が無い post を投稿しようとすると `compliance` 例外 + 投稿停止 + Slack 通知 + audit_log を 100% カバー
- [x] T070 [Foundation] backend `backend/src/ymg_backend/domain/compliance/validators.py` を T069 で driven 実装

**Checkpoint**: Foundation 完了 — 全 US の並列実装が可能

---

## Phase 3: User Story 1 — 日次サイクルで自動投稿が回ること(P1) 🎯 MVP

**Goal**: ジャンル選定 → 6 トラック生成 → AcoustID → SDXL → ffmpeg 30 分動画 → YouTube 投稿 が日次で自動実行される。

**Independent Test**: 1 ジャンルでテンプレ手動設定 + dryrun=OFF(ローカル検証時のみ unlisted を強制)+ 1 本投稿 → YouTube に動画が公開、 説明文に AI 開示、 30 分尺で再生可能。

### Tests for User Story 1(critical path test-first)

- [x] T071 [P] [US1] **TEST FIRST** `backend/tests/critical/test_acoustid_precheck.py`: AcoustID API mock(respx)で clear / hit / api_error の各レスポンスに対する prechecker の挙動を 100% カバー
- [x] T072 [P] [US1] **TEST FIRST** `backend/tests/integration/test_daily_cycle_pipeline.py`: GPU worker mock + youtube mock で日次サイクル E2E(plan → music × 6 → AcoustID → image → video → upload)を流す
- [x] T073 [P] [US1] **TEST FIRST** `backend/tests/critical/test_planner_schema.py`: 改善計画 LLM 出力が DailyPlan スキーマで validation される / 辞書外ジャンル拒否 / `posts` 1〜2 制約 / rationale min_length を 100% カバー

### 改善計画 LLM 呼び出し

- [x] T074 [P] [US1] backend `backend/src/ymg_backend/domain/plans/planner.py`: System prompt(prompt_loader)+ User prompt(集計サマリ + 履歴)+ LlmProvider 呼び出し + plan_metric_snapshot 書き込み + plans 永続化
- [x] T075 [P] [US1] backend `backend/src/ymg_backend/domain/plans/finisher.py`: directive parser で抽出した `{{自由文}}` 単位に Haiku 級 LLM を呼ぶ仕上げサービス
- [x] T076 [US1] backend `backend/src/ymg_backend/api/plans.py`: `GET /plans` + `POST /plans`(cycle/target_date 指定で planner 起動)+ `POST /plans/{id}/approve`(contracts/backend-api.yaml 準拠)

### 音楽生成パイプライン

- [x] T077 [P] [US1] backend `backend/src/ymg_backend/domain/pipeline/music_jobs.py`: 1 post = 6 tracks の job dispatcher、 gpu_worker_client 経由で `POST /generate/music` × 6、 `gpu_jobs` 永続化、 完了ポーリング
- [x] T078 [P] [US1] backend `backend/src/ymg_backend/domain/compliance/acoustid.py`: pyacoustid + Chromaprint で 6 track 並列チェック、 fingerprint を `audio_tracks` に記録、 hit 時は単独 re-gen request、 連続 3 回 hit でジャンル一時停止(FR-010, FR-011, FR-012)

### サムネ / ビジュアル生成

- [x] T079 [P] [US1] backend `backend/src/ymg_backend/domain/pipeline/image_jobs.py`: gpu_worker `POST /generate/image` 呼び出し、 SDXL 背景画像取得、 thumbnail_uri を `posts` に保存
- [x] T080 [P] [US1] backend `backend/src/ymg_backend/domain/pipeline/thumbnail_overlay.py`: Pillow で SDXL 背景にジャンル別 YAML(`templates/thumbnail/*.yaml`)に従ってテキストオーバーレイ + バッジ合成、 出力 JPEG quality 90、 YouTube 2MB 制限内

### ffmpeg 動画合成

- [x] T081 [US1] backend `backend/src/ymg_backend/domain/pipeline/video_compose.py`: 6 トラックを `ffmpeg acrossfade 3〜5秒` で連結 → SDXL 背景 + `showwaves` overlay を audio に重ねて 30 分 mp4 を生成(ADR-0003, ADR-0015)

### タイトル / 説明文レンダリング

- [x] T082 [P] [US1] backend `backend/src/ymg_backend/domain/render/title.py`: ジャンル YAML テンプレ + directive parser + finisher LLM で `final_title` 生成(FR-051、 60 字制約検証)
- [x] T083 [P] [US1] backend `backend/src/ymg_backend/domain/render/description.py`: default テンプレ + chapters 動的生成(6 トラックタイトル英 + 日)+ 静的 AI 開示固定文 + ハッシュタグ 3 個で `final_description` 生成(FR-052, FR-053)

### YouTube アップローダ + Compliance ゲート

- [x] T084 [US1] backend `backend/src/ymg_backend/infrastructure/youtube/uploader.py`: google-api-python-client で `videos.insert`、 `status.containsSyntheticMedia=true` 必須セット、 OAuth トークンを `oauth_credentials` から Fernet 復号して使用(FR-006, FR-007, FR-100)
- [x] T085 [US1] backend `backend/src/ymg_backend/infrastructure/youtube/oauth.py`: refresh token 経由のアクセストークン取得、 期限切れ前自動 refresh、 一度きりの OAuth flow 用 CLI(`make youtube-auth`)
- [x] T086 [US1] backend `backend/src/ymg_backend/infrastructure/youtube/compliance_gate.py`: アップロード直前に T070 のバリデータを必ず通す(`compliance` ガードレイヤ)

### 日次スケジューラ

- [x] T087 [US1] backend `backend/src/ymg_backend/infrastructure/scheduler.py`: APScheduler AsyncIOScheduler、 `app_state.scheduler_enabled=true` でのみ jobs を add、 日次 cron(JST 朝 / 夕実行可能な 2 slot)+ post 実行 job(plan → pipeline → upload)
- [x] T088 [US1] backend `backend/src/ymg_backend/api/scheduler.py`: `GET/PUT /scheduler`、 `PUT /scheduler` で `enabled` 切替時に audit_log + scheduler add/remove + dryrun→投稿モード切替時の audit 記録(ADR-0035)

### Posts API + Retry

- [x] T089 [P] [US1] backend `backend/src/ymg_backend/api/posts.py`: `GET /posts` + `GET /posts/{id}` + `POST /posts/{id}/retry`(失敗 post の再実行 / 初回手動 1 本投稿時にも使う、 ADR-0035)

### Slack 通知

- [x] T090 [P] [US1] backend `backend/src/ymg_backend/infrastructure/slack/notifier.py`: 単一 webhook、 メッセージ冒頭にカテゴリ prefix `[FATAL]/[COMPLIANCE]/[TRANSIENT]/[RECOVERABLE]/[QUALITY]`、 fatal/compliance に `<!channel>` mention(FR-114, FR-115)

**Checkpoint**: US1 単独で MVP として「投稿が回る」状態が完成。 dryrun=ON 既定なので、 デフォルトは dryrun_outputs に格納されるが US2 完了で承認 / 投稿が可能になる。

---

## Phase 4: User Story 2 — dryrun レビューワークフロー(P1)

**Goal**: dryrun=ON で生成された動画を管理 UI でレビュー、 承認 → 本投稿、 否認 → 削除 + 否認理由を planner LLM 入力に活用。

**Independent Test**: 管理 UI で dryrun=ON、 1 サイクル実行 → `dryrun_outputs` 一覧から動画再生 → 承認 / 否認操作 → 状態遷移と audit_log 確認。

### Tests for User Story 2

- [x] T091 [P] [US2] **TEST FIRST** `backend/tests/integration/test_dryrun_lifecycle.py`: pending → approved → posted / pending → rejected / pending → auto_expired (7 日 freezegun)を網羅
- [x] T092 [P] [US2] **TEST FIRST** `frontend/tests/e2e/dryrun_review.spec.ts`: Playwright で dryrun ページ → 動画再生 → 承認 → 投稿完了表示

### Backend

- [x] T093 [P] [US2] backend `backend/src/ymg_backend/domain/dryrun/service.py`: dryrun_outputs 状態遷移サービス(approve / reject / auto_expire / post)
- [x] T094 [P] [US2] backend `backend/src/ymg_backend/api/dryrun.py`: `GET /dryrun/outputs` + `POST /dryrun/outputs/{id}/approve` + `POST /dryrun/outputs/{id}/reject`(reason 必須)
- [x] T095 [P] [US2] backend `backend/src/ymg_backend/domain/dryrun/retention_job.py`: 日次 APScheduler ジョブで pending → 7 日後 auto_expired + 動画ファイル削除(FR-062)
- [x] T096 [US2] backend `backend/src/ymg_backend/domain/plans/planner.py` に否認理由の context 注入経路を追加(FR-063)

### Frontend

- [x] T097 [P] [US2] frontend `frontend/app/(admin)/dryrun/page.tsx`: pending 一覧 + サムネプレビュー + 再生プレーヤー
- [x] T098 [P] [US2] frontend `frontend/app/(admin)/dryrun/[id]/page.tsx`: 詳細画面、 承認 / 否認(reason テキスト入力)ボタン

**Checkpoint**: US2 完了で MVP に投稿 OK / NG の安全弁が入る。

---

## Phase 5: User Story 3 — 週次サイクルで改善計画が更新されること(P1)

**Goal**: 週 1 回 analytics 取得 → WeeklyPlan 生成 → 管理 UI 承認 → 翌週の DailyPlan に反映。 新ジャンル採用 / 削除推奨も判定。

**Independent Test**: 過去 1 週間分の analytics_daily 投入 → 週次ジョブ手動実行 → WeeklyPlan が retention 引用付き rationale で生成 → 承認 → 翌日 DailyPlan に反映確認。

### Tests for User Story 3

- [x] T099 [P] [US3] **TEST FIRST** `backend/tests/integration/test_weekly_cycle.py`: 1 週間分の analytics fixture → WeeklyPlan 生成 → `genre_distribution` 合計 1.0、 `referenced_metrics.window_days=7`、 `experiment_slot` 判定(FR-037/038)
- [x] T100 [P] [US3] **TEST FIRST** `backend/tests/critical/test_youtube_analytics_client.py`: Data API + Analytics API の respx mock で retention / views / impressions / ctr / traffic_sources の取得を 100% カバー

### YouTube Analytics

- [x] T101 [US3] backend `backend/src/ymg_backend/infrastructure/youtube/analytics_client.py`: Data API + Analytics API で動画ごとの retention / views / impressions / ctr / traffic_sources を取得、 `analytics_daily` に upsert(ADR-0021, FR-101)
- [x] T102 [P] [US3] backend `backend/src/ymg_backend/infrastructure/youtube/comments_client.py`: コメント取得 → `comments` に保存
- [x] T103 [P] [US3] backend `backend/src/ymg_backend/domain/analytics/summary.py`: planner LLM 入力用集計サマリ(各動画の retention / views / 公開日 / ジャンル を表形式)

### WeeklyPlan + ジャンルローテーション

- [x] T104 [US3] backend `backend/src/ymg_backend/domain/plans/weekly_planner.py`: WeeklyPlan 専用 planner、 avoid_genres / experiment_slots を取得して prompt context 化
- [x] T105 [P] [US3] backend `backend/src/ymg_backend/domain/genres/rotation.py`: 翌週 DailyPlan 発行時に WeeklyPlan を反映(avoid_genres / experiment_slots 適用)
- [x] T106 [P] [US3] backend `backend/src/ymg_backend/domain/genres/recommend.py`: experiment_slot 投入 ≥ 4 本 + 経過 ≥ 14 日で 主力ジャンル平均 retention 比 ≥ 80% → 「採用推奨」、 < 60% → 「削除推奨」(FR-037)
- [x] T107 [P] [US3] backend `backend/src/ymg_backend/api/genres.py`: `POST /genres/{name}/promote` + `POST /genres/{name}/disable`、 承認時に `genres.role` 遷移 + audit_log(FR-038)

### 週次スケジューラ + Analytics 取得ジョブ

- [x] T108 [US3] backend `backend/src/ymg_backend/infrastructure/scheduler.py` に週次 job(月曜朝) + analytics 取得日次 job を追加(投稿モード前でも analytics 取得は走らせる、 ADR-0035)

### Frontend

- [x] T109 [P] [US3] frontend `frontend/app/(admin)/plans/page.tsx`: Plan 一覧(cycle filter)+ 詳細 / 承認
- [x] T110 [P] [US3] frontend `frontend/app/(admin)/analytics/page.tsx`: ジャンル別 retention / views チャート(recharts)、 採用 / 削除推奨ジャンル UI

**Checkpoint**: US3 完了で「観察 → 改善」ループが回る。

---

## Phase 6: User Story 4 — コンプラ事故時の緊急停止(P1)

**Goal**: `make panic-stop` で scheduler 停止 + 直近 24h 動画一括 private 化、 audit 記録。

**Independent Test**: unlisted で 1 本投稿 → `make panic-stop` → 一覧表示 → 確認 → private 化 + audit_log 記録 を確認(ADR-0035 MVP 完了条件 #4)。

### Tests for User Story 4

- [x] T111 [P] [US4] **TEST FIRST** `backend/tests/critical/test_panic_stop.py`: scheduler 停止、 直近 24h の動画リスト取得、 YouTube `videos.update(privacyStatus=private)` 呼び出し(respx mock)、 audit_log 記録を 100% カバー

### Backend + CLI

- [x] T112 [US4] backend `backend/src/ymg_backend/domain/panic_stop/service.py`: scheduler_enabled = false → scheduler 内 pending job を pause → 直近 N 時間動画リスト → privacyStatus=private 一括変更 → audit_log
- [x] T113 [US4] backend `backend/src/ymg_backend/api/panic_stop.py`: `POST /scheduler/panic-stop`(window_hours / set_private リストを受ける)
- [x] T114 [US4] `infra/scripts/panic-stop.sh` + `Makefile` の `make panic-stop`: 対話プロンプト → backend `/scheduler/panic-stop` 呼び出し
- [x] T115 [P] [US4] frontend `frontend/app/(admin)/scheduler/page.tsx` に "panic-stop" ボタン + 確認ダイアログ(window_hours 指定可)

**Checkpoint**: US4 完了で MVP 完了条件 #4 が満たせる。

---

## Phase 7: User Story 5 — マスター LLM プロバイダ切替(P2)

**Goal**: env / API でランタイム切替、 Anthropic + subscription は起動時拒否。

**Independent Test**: `LLM_PROVIDER=anthropic LLM_AUTH_MODE=api_key` で再起動 → 同じ DailyPlan 入力で出力差分が usage_log に記録される。

### Tests for User Story 5

- [x] T116 [P] [US5] **TEST FIRST** `backend/tests/critical/test_llm_provider_swap.py`(T035 と統合): `/llm/providers` 経由の切替で active provider が変わり、 不正組合せ(Anthropic + subscription)は 400

### Backend + Frontend

- [x] T117 [P] [US5] backend `backend/src/ymg_backend/api/llm.py`: `GET /llm/providers` + `PUT /llm/providers` + `GET /llm/usage?month=YYYY-MM`(月次集計)
- [x] T118 [P] [US5] frontend `frontend/app/(admin)/llm/page.tsx`: provider / model / auth_mode 切替 UI + 月次コスト + 予算進捗バー
- [x] T119 [P] [US5] backend `backend/src/ymg_backend/domain/budget/alert.py`: 月次予算 50/80/100% 監視 + Slack 通知(FR-026)

**Checkpoint**: US5 完了で provider 比較実験が回せる。

---

## Phase 8: User Story 6 — 管理 UI でのオーケストレーション可視化(P2)

**Goal**: SSE で各 step の進捗を秒単位更新、 ジャンル単位で並行可視化。

**Independent Test**: 管理 UI で 1 サイクル発火 → 各 step が green / red で遷移 → SSE 経由でリアルタイム更新が見える。

### Backend SSE

- [x] T120 [US6] backend `backend/src/ymg_backend/api/sse.py`: `GET /jobs/stream` で job_history + 進行中ジョブを text/event-stream 配信(ADR-0023)
- [x] T121 [P] [US6] backend `backend/src/ymg_backend/infrastructure/event_bus.py`: pipeline 各 step が EventBus に投げる軽量パブサブ(asyncio Queue)

### Frontend

- [x] T122 [P] [US6] frontend `frontend/app/(admin)/jobs/page.tsx`: SSE 受信 + ジャンル列 × step 行のグリッド + 状態色
- [x] T123 [P] [US6] frontend `frontend/lib/api/sse.ts`: EventSource ラッパ(再接続込み)

**Checkpoint**: US6 完了で運用時の障害切り分けが速くなる。

---

## Phase 9: User Story 7 — クラウド GPU(RunPod 等)への移行(P3)

**Goal**: backend / frontend のコード変更なしで GPU worker をクラウドに移行可能と確認。

**Independent Test**: GPU worker Dockerfile を `docker build` → RunPod Pod デプロイ → `GPU_WORKER_BASE_URL` 変更 → 同じ動画生成が走る。

### Validation

- [ ] T124 [P] [US7] `.github/workflows/ci.yml` に gpu_worker `docker build` ステップを追加し、 PR 毎に image build が green になることを確認(ADR-0031)
- [ ] T125 [P] [US7] `infra/runbooks/runpod-migration.md`: RunPod Pod / Serverless のセットアップ + `docker push` + env 切替手順
- [ ] T126 [US7] backend `backend/tests/integration/test_gpu_worker_swap.py`: `GPU_WORKER_BASE_URL` を mock サーバに向けて切替、 backend 再起動なしで設定リロード(`app_state` 経由 or 起動時 env 必須かは設計判断)

**Checkpoint**: US7 完了で「移行できる契約」が CI でも担保される。

---

## Phase 10: Polish & Cross-Cutting Concerns

**Purpose**: MVP 完了条件 / 横断的な品質。

### MVP 完了 checklist 自動化(ADR-0035, Clarifications #1)

> 注: MVP 完了チェックは **本開発作業の運用ゲート判定基準** であり、 製品の UI 機能ではない(screen-spec.md §0 参照)。 専用 UI ページは作らない。 backend API + Dashboard 隅の小インジケーターのみ提供。

- [ ] T127 [P] backend `backend/src/ymg_backend/domain/mvp_check/checklist.py`: 6 項目 checklist の自動判定(dryrun 3 本連続 / AcoustID 全 clear / unlisted 1 本投稿実績 / panic-stop 予行 / OAuth refresh / Slack 5 カテゴリ動作)
- [ ] T128 [P] backend `backend/src/ymg_backend/api/mvp_check.py`: `GET /mvp-check` で各項目の green / red を返す(CLI / curl から query 用、 主に seita 自身が確認)
- [ ] T129 [P] frontend Dashboard(`frontend/app/(admin)/page.tsx`)に **小インジケーター** として `x / 6 完了` のサマリを表示(専用ページは作らない)

### Backup

- [ ] T130 [P] `infra/scripts/backup.sh`: `pg_dump` + 動画 / 音楽メタデータコピー、 Fernet 鍵は除外(ADR-0026, FR-120, FR-122)
- [ ] T131 [P] `Makefile` の `make backup` / `make restore-db DUMP=...` ターゲット実装

### Deploy / Lifecycle

- [ ] T132 [P] `Makefile` の `make deploy` 実装: git pull → migrate(pre-dump 込)→ image rebuild → docker compose up → gpu_worker restart → healthcheck(ADR-0031)
- [ ] T133 [P] `infra/systemd/ymg-stack.service` + `ymg-gpu-worker.service` の最終調整、 enable 設定確認
- [ ] T134 [P] `infra/scripts/deploy.sh` を Makefile からも呼べる薄いラッパに調整

### Frontend Prompts UI

- [ ] T135 [P] frontend `frontend/app/(admin)/prompts/page.tsx`: prompt version 切替 / 編集 / プレビュー UI(FR-036)

### Documentation

- [ ] T136 [P] `specs/001-youtube-music-generator/quickstart.md` 実機検証 + 追記
- [ ] T137 [P] `README.md` トップにアーキ図リンク追加
- [ ] T138 [P] `specs/001-youtube-music-generator/adr/` index の自動生成 script(将来のため)

### Critical Path Coverage 計測

- [ ] T139 backend `make test-critical` で `backend/tests/critical/` を pytest-cov で 100% カバレッジ強制(Constitution II)
- [ ] T140 [P] `.github/workflows/ci.yml` で critical テストが 100% で通らないと merge ブロック

### Security 強化

- [ ] T141 [P] `.github/workflows/secrets-scan.yml`: gitleaks 等で `.env` 系の混入検査
- [ ] T142 [P] backend `backend/src/ymg_backend/core/security.py` の Basic auth: timing attack 対策(`secrets.compare_digest`)

### Performance

- [ ] T143 [P] backend pipeline で並列化可能箇所(SDXL ↔ AcoustID 並列等)の整理
- [ ] T144 [P] backend Anthropic prompt caching hit 率を usage_log で測定、 90% 未満なら system block の構成見直し

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup(Phase 1)**: 依存なし、 即開始可能
- **Foundational(Phase 2)**: Setup 完了 → 全 US の前提
- **US1 〜 US7(Phase 3 〜 9)**: Foundational 完了後、 並列可能(下記 within-story 順序あり)
- **Polish(Phase 10)**: 最低限 US1 + US2 + US4 完了後に着手可能、 一部は CI 系で先行可

### User Story 間の依存

- **US1 (P1)**: Foundational のみ依存。 dryrun=ON 既定で進めるため US2 完了前でも MVP 構築可
- **US2 (P1)**: US1 と並列可。 US1 の posts / dryrun_outputs を消費するため、 結合テストは US1 後
- **US3 (P1)**: US1 で生成された posts / videos に依存(analytics は実投稿後にしか取れない)、 ただし fixture で先行開発可
- **US4 (P1)**: US1 の YouTube uploader + Fernet が前提
- **US5 (P2)**: Foundational のみ依存、 並列可
- **US6 (P2)**: US1 の job 発火が前提だが SSE 基盤は独立、 並列可
- **US7 (P3)**: Foundational のみ依存、 CI 整備で並列可

### Within Each User Story

- Tests(critical path)→ 失敗確認 → 実装 → green
- Schema → Service → Endpoint → Integration
- Backend → Frontend(OpenAPI 型生成後)

### Parallel Opportunities

- Phase 1: T002〜T013 が並列
- Phase 2: T020 / T021 系、 T028〜T031 / T037 / T038 / T042〜T050 / T054〜T059 / T061〜T067 が並列
- Phase 3(US1): T071〜T073 のテストは並列、 T077 / T078 / T079 / T082 / T083 / T089 / T090 は並列
- Phase 4(US2): T091 / T092 テスト並列、 T093 / T094 / T095 / T097 / T098 並列
- Phase 5(US3): T099 / T100 テスト並列、 T101〜T107 / T109 / T110 は部分並列(分析 → planner → rotation の順序あり)
- Phase 6〜10: 大半が独立タスクで並列可

---

## Parallel Example: User Story 1

```bash
# T071, T072, T073 のテストを並列で開始(別ファイル):
Task: "Write AcoustID precheck critical test in backend/tests/critical/test_acoustid_precheck.py"
Task: "Write daily cycle integration test in backend/tests/integration/test_daily_cycle_pipeline.py"
Task: "Write planner schema critical test in backend/tests/critical/test_planner_schema.py"

# テスト fail 確認後、 並列可能な実装を一気に走らせる:
Task: "Implement planner in backend/src/ymg_backend/domain/plans/planner.py"
Task: "Implement music jobs in backend/src/ymg_backend/domain/pipeline/music_jobs.py"
Task: "Implement AcoustID precheck in backend/src/ymg_backend/domain/compliance/acoustid.py"
Task: "Implement image jobs in backend/src/ymg_backend/domain/pipeline/image_jobs.py"
Task: "Implement thumbnail overlay in backend/src/ymg_backend/domain/pipeline/thumbnail_overlay.py"
Task: "Implement title render in backend/src/ymg_backend/domain/render/title.py"
Task: "Implement description render in backend/src/ymg_backend/domain/render/description.py"
Task: "Implement Slack notifier in backend/src/ymg_backend/infrastructure/slack/notifier.py"
```

---

## Implementation Strategy

### MVP First(US1 + US2 + US4 で最小本投稿可能ライン)

1. **Phase 1 Setup** 完走(T001〜T015)
2. **Phase 2 Foundational** 完走(T016〜T070、 critical テスト T026/T035/T040/T069 込)
3. **Phase 3 US1**(T071〜T090): MVP 本体 = 日次サイクル
4. **Phase 4 US2**(T091〜T098): dryrun レビュー
5. **Phase 6 US4**(T111〜T115): panic-stop = 投稿モード切替の前提
6. **Polish 一部**(T127〜T129 MVP checklist UI、 T130 backup)
7. **ADR-0035 のチェックリスト 6 項目を完走**(T127 の `/mvp-check` 全 green)
8. **手動 1 本投稿 → 目視確認 → posting mode 切替**

### Incremental Delivery

- **Sprint 1**: Setup + Foundational(Phase 1-2)
- **Sprint 2**: US1 完了 → dryrun で動画ファイルが手に入る
- **Sprint 3**: US2 + US4 完了 → 投稿モード切替準備完了
- **Sprint 4**: US3 完了 → 改善ループ稼働
- **Sprint 5**: US5 + US6 完了 → 運用品質向上
- **Sprint 6**: US7 + Polish 完了 → クラウド移行可能性検証 + ドキュメント整備

### Parallel Team Strategy

1 人運用前提だが、 並列を意識した順序付け:

1. Setup + Foundational は逐次(基盤確定が先)
2. Foundational 完了後:
   - Track A: US1 → US2 → US4 → MVP checklist(主投稿経路)
   - Track B: US3(analytics + 週次改善ループ、 fixture で先行)
   - Track C: US5 + US6(運用品質、 backend ↔ frontend をワーカ単位で)
3. US7 + Polish は MVP 完了後に集中

---

## Critical Path Test 一覧(Constitution II / ADR-0027)

| Critical Path | TEST FIRST Task |
| --- | --- |
| Fernet 暗号化 / 復号 | T026 |
| LLM Pydantic validation | T035, T073 |
| Directive parser | T040 |
| containsSyntheticMedia バリデーション | T069 |
| AcoustID プレチェック | T071 |
| YouTube Analytics クライアント | T100 |
| panic-stop YouTube private 化 | T111 |
| LLM provider 切替(Anthropic + subscription 拒否) | T116(T035 と統合) |

これら 8 件は 100% カバレッジ強制(T139 / T140 で gate)、 他は best effort 60-70%。

---

## Notes

- [P] = 別ファイル / 依存なし
- [Story] = US1〜US7 のいずれかに紐付け
- すべての critical path タスクは RED → GREEN の順で進める(test 先に書いて fail させてから実装)
- コミットは ADR-0030 の `<type>(<scope>): <description>` 規約に従う
- 各 phase の checkpoint で動作確認、 必要なら ADR を増分更新
- 不可逆操作(投稿 / panic-stop 実機検証)は dryrun / unlisted で先行確認
