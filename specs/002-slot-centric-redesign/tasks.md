---

description: "Implementation task list for 枠中心モデルへの再設計 (Slot-Centric Redesign)"
---

# Tasks: 枠中心モデルへの再設計

**Input**: Design documents from `specs/002-slot-centric-redesign/`

**Prerequisites**: plan.md / spec.md / research.md / data-model.md / contracts/ / quickstart.md(全て揃い済)

**Tests**: 含む。憲法 II v2.0.0(Critical Path TDD NON-NEGOTIABLE)に従い、critical path 8 件(plan.md の表)は **test-first 必須 100% カバレッジ**、それ以外は best effort 60-70%。状態機械 / 物化は hypothesis の property test を併用。

**Organization**: User Story ごとにグループ化(spec.md US1〜US7)。移植タスクは移植元(`main@9c42624` の 001 実装)を明記。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 並列実行可能(別ファイル、未完依存タスクなし)
- **[Story]**: US1〜US7(spec.md User Stories)
- **[移植]**: 001 実装からのコード移植(テストも一緒に移植)

## Path Conventions

モノレポ(ADR-0029 継承): backend `backend/src/ymg_backend/...`、frontend `frontend/...`、gpu_worker `gpu_worker/...`(**変更なし**)、infra `infra/...`

---

## Phase 1: Setup(旧オーケストレーションの撤去と下地)

**Purpose**: in-place 置き換えの下地。旧概念(Plan / dryrun / Post / panic-stop / mvp-check)のコードを撤去し、移植対象モジュールだけを残してビルド可能にする。旧実装の参照は git 履歴(`main@9c42624`)で行う。

- [ ] T001 backend 旧オーケストレーション削除: `domain/plans/` `domain/dryrun/` `domain/panic_stop/` `domain/mvp_check/` `domain/genres/`(rotation / recommend)`domain/pipeline/`(music_jobs 等は Phase 3 で移植改修するため `_legacy` 参照用に一時退避せず削除)、旧 `api/*`、`infrastructure/scheduler.py` の旧 job 配線を削除。`main.py` を `/health` のみの最小スケルトンに置換(セッション認証 / Basic は温存)
- [ ] T002 backend 旧テスト削除: `tests/` から旧オーケストレーション対象を削除(移植対象 critical テスト = fernet / llm_provider / directive_parser / acoustid / compliance_validation は残す)。`tests/property/` ディレクトリ新設
- [ ] T003 backend 旧 alembic versions を削除し、新チェーン用に `alembic/versions/` を空にする(env.py は継承)
- [ ] T004 [P] frontend 旧画面削除: `app/(admin)/plans/` `dryrun/` `jobs/` `scheduler/` `analytics/` `genres/` `llm/` `prompts/` と旧ダッシュボード `page.tsx` を削除、ナビゲーションを新 7 画面(編成表 / 枠詳細 / タイムライン / ジャンル / 分析 / プロンプト / 設定)の placeholder に置換(auth BFF `app/api/` `app/login/` は温存)
- [ ] T005 [P] `.env.example` 更新: `PUBLISH_WARN_PER_DAY` / `YMG_ADMIN_TOKEN` / `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `LEGACY_DATABASE_URL` 追加、`DRYRUN_DEFAULT` 削除(quickstart §1)
- [ ] T006 [P] backend `pyproject.toml` に typer / slack-bolt / hypothesis を uv add(research R-2 / R-3 / R-5。版は lockfile 確定時に最新安定を確認)
- [ ] T007 [P] `Makefile` 更新: `panic-stop` ターゲット削除(ADR-0044 置換)、`migrate-legacy` 追加、他ターゲット温存。`infra/scripts/panic-stop.sh` 削除
- [ ] T008 [P] `.github/workflows/ci.yml` のテストパス / カバレッジ設定を新構成(critical 8 件 + property)に更新(ゲート強制は T086)

---

## Phase 2: Foundational(全 US 共通の前提)

**Purpose**: 新スキーマ / 状態機械 / 成果物 DAG / システム状態ガード / 移植基盤。

**⚠️ CRITICAL**: このフェーズ完了まで User Story 実装は開始しない。

### DB / マイグレーション

- [ ] T010 backend `alembic/versions/0001_slot_baseline.py`: data-model.md の全 ENUM + 全テーブル + CHECK 制約 + インデックス + seed(genres 6 件 / system_state=running / autonomy_state=l0 / model_pricing)
- [ ] T011 [P] backend `infrastructure/db/models/` に新 ORM(1 entity 1 ファイル: slot_pattern / slot_pattern_row / slot_exception / slot / artifact / approval_record / system_state / autonomy_state / genre / genre_weekly_allocation / video / analytics_daily / comment / metrics_snapshot / slot_timeline / audit_log / usage_log / model_pricing / oauth_credentials / llm_provider_secrets)
- [ ] T012 `make migrate` で新 DB に展開されること + ORM ↔ マイグレーション整合スモークを確認
- [ ] T013 [P] backend `core/config.py` に新 env(T005 分)を追加
- [ ] T014 [P] backend `core/logging.py` の bind キーを `slot_id` / `genre` / `stage` に変更

### Critical Path: 状態機械(critical #7 の遷移ガード部分)

- [ ] T015 [P] [Foundation] **TEST FIRST** `backend/tests/critical/test_state_machine.py`: T1〜T18 の全許可遷移、それ以外の全組合せ拒否、INV-1 / INV-3 / INV-4、`current_stage` は `in_production` のみ、`publish_block_reason` は `approved` のみ、slot_timeline への遷移記録を 100% カバー
- [ ] T016 [Foundation] backend `domain/slots/state_machine.py` を T015 で driven 実装(遷移の唯一の入口。直接 UPDATE を書かない)
- [ ] T017 [P] [Foundation] `backend/tests/property/test_state_machine_props.py`: hypothesis で任意の操作列後に INV が保持されることを検証

### Critical Path: 成果物 DAG と無効化(critical #8)

- [ ] T018 [P] [Foundation] **TEST FIRST** `backend/tests/critical/test_artifact_invalidation.py`: DAG 単一定義からの推移的無効化(ADR-0046 の適用例 6 行を全網羅)、トラック単体再生成で inspection / mix / chapters / video / package が必ず無効化、文言のみで video / thumbnail 保持、承認失効(INV-5 → T17)、版 / 来歴の記録を 100% カバー
- [ ] T019 [Foundation] backend `domain/artifacts/dag.py` + `invalidation.py` + `store.py` を T018 で driven 実装

### システム状態と参照点(critical #6 の土台)

- [ ] T020 [P] [Foundation] **TEST FIRST** `backend/tests/critical/test_system_state_guards.py`: 参照点 2 箇所 — StageRunner(`stopped` で工程開始拒否)/ Publisher 入口(`publish_paused` / `stopped` で公開拒否 + `publish_block_reason=system_paused` 設定)を 100% カバー
- [ ] T021 [Foundation] backend `domain/safety/system_state.py`(1 行 read/write + 監査)+ `domain/production/stage_runner.py`(全工程がこれ経由で実行)を T020 で driven 実装

### 移植(テストも一緒に移植、旧テストを green のまま維持)

- [ ] T022 [P] [Foundation] [移植] `core/security.py`(Fernet + セッション / Basic)+ `tests/critical/test_security_fernet.py`(critical #3)
- [ ] T023 [P] [Foundation] [移植] `llm/` 一式(base / openai / anthropic / ollama / pricing / usage_writer / factory)+ `tests/critical/test_llm_provider.py`(critical #4、Anthropic サブスク起動拒否込み)。usage_log の context を `slot_id` / `stage` に変更
- [ ] T024 [P] [Foundation] [移植] `domain/directive/parser.py` + `tests/critical/test_directive_parser.py`(critical #5)
- [ ] T025 [P] [Foundation] [移植] `domain/compliance/`(acoustid.py / validators.py / audio_qa)+ `tests/critical/test_acoustid_precheck.py` + `test_compliance_validation.py`(critical #1 / #2)。「連続 3 回ヒットでジャンル一時停止」を `genres.is_paused` に配線
- [ ] T026 [P] [Foundation] [移植] `infrastructure/`: storage(fsspec)/ gpu_worker_client / event_bus / audit / slack/notifier(5 カテゴリ prefix)/ youtube(uploader / oauth / analytics_client / comments_client / compliance_gate)
- [ ] T027 [P] [Foundation] [移植] `prompts/` `templates/` 資産 + loaders(planner → planning 読み替え、ADR-0050)
- [ ] T028 [P] [Foundation] [移植] `domain/errors/`(5 分類、ADR-0028)

### スケルトン

- [ ] T029 [Foundation] backend `main.py` lifespan: DB 接続 / scheduler **自動再開**(FR-046。`system_state` は読み取るだけで変更しない)/ `/health` 新形式(db / gpu_worker / system_state / autonomy_level / llm_provider)
- [ ] T030 [P] [Foundation] frontend `openapi-typescript` を `specs/002-slot-centric-redesign/contracts/backend-api.yaml` に向けて型生成、`lib/api/` の fetch wrapper を新 API に更新

**Checkpoint**: Foundation 完了 — 全 US の並列実装が可能

---

## Phase 3: User Story 1 — 編成表で枠を組むと制作が自動で回ること(P1)🎯 MVP

**Goal**: パターン編集 → 03:00 物化(決定論)→ 制作ライン(企画 → 生成 → 検査 → パッケージング)が EDF 順で自動進行し `awaiting_approval` に達する。

**Independent Test**: spec.md US1 の Independent Test(枠行 1 本 → 物化 → `awaiting_approval` 到達)。

### Tests for User Story 1

- [ ] T031 [P] [US1] **TEST FIRST** `backend/tests/property/test_materializer_props.py`: hypothesis で物化の決定論(INV-6: 同一パターン・例外・日付 → 同一枠集合)、枠 ID 不変、パターン変更が `empty` 将来枠のみに反映、例外(skip / genre_override / adhoc_add)の適用を検証
- [ ] T032 [P] [US1] **TEST FIRST** `backend/tests/integration/test_production_line.py`: GPU worker mock + LLM mock で 1 枠が planning → generating → inspecting → packaging → `awaiting_approval` まで流れること、EDF 順(公開予定の早い順、同時刻は物化順)、`blocked`(ジャンル一時停止 / `stopped`)→ 解消で再開を網羅

### 実装

- [ ] T033 [US1] backend `domain/slots/patterns.py`(パターン新 version 保存 / 現行取得)+ `api/schedule.py`(GET/PUT /pattern、GET/POST/DELETE /exceptions、GET /schedule 週表示 + 未物化日プレビュー + `publish_warn`)
- [ ] T034 [US1] backend `domain/slots/materializer.py`: 03:00 JST 物化(純関数 + UPSERT、例外適用、`approval_deadline_at` 確定)を T031 で driven 実装
- [ ] T035 [US1] backend `domain/objective/genre_select.py` 初版: おまかせ選択(role 主力 / 拡張のみ、ADR-0047 基準。確定値 3 本未満はガードレール判定を保留し配分遅れ優先で選択)+ `genre_reason` 生成(判断数値込み、FR-074)
- [ ] T036 [US1] backend `domain/production/line.py` + `stages/planning.py`: 企画 LLM(prompt loader + 実績スナップショット `metrics_snapshots` 書き込み + plan artifact 保存)
- [ ] T037 [US1] backend `domain/production/edf_queue.py` + `stages/generating.py`: [移植改修] 旧 music_jobs を track artifact(版 / 来歴)ベースに改修、GPU worker へ 1 ジョブずつ EDF 順投入(R-7、gpu-worker-api.yaml 無変更)
- [ ] T038 [US1] backend `domain/production/stages/inspecting.py`: 指紋 + Audio QA(T025 移植物の呼び出し)、不合格トラックの自動再生成(リトライ上限超過で `failed`、FR-023)、inspection artifact 保存
- [ ] T039 [US1] backend `domain/production/stages/packaging.py`: [移植改修] サムネ(image_jobs + thumbnail_overlay)/ mix + 動画合成(video_compose)/ 文言(render/title + description、directive parser + 仕上げ LLM)を artifact ベースに改修、package artifact 生成
- [ ] T040 [US1] backend `infrastructure/scheduler.py` 新実装: APScheduler(single-flight 継承、ADR-0011)+ 物化 job(03:00)+ 枠駆動の制作開始 job(T2)+ SSE イベント配線
- [ ] T041 [US1] backend `api/slots.py` GET 系(一覧 / 詳細: artifacts + inspection_summary + timeline)+ `api/sse.py` + `api/timeline.py`
- [ ] T042 [P] [US1] frontend `app/(admin)/page.tsx` 編成表: 週間カレンダー(枠の色 = 状態 / `current_stage`)、パターン編集、例外操作、警告表示(FR-015)
- [ ] T043 [P] [US1] frontend `app/(admin)/slots/[id]/page.tsx` 枠詳細(読み取り): 工程ごとの「使った / できた」、おまかせ理由表示、SSE 更新

**Checkpoint**: 枠が自動で `awaiting_approval` に到達する(公開はまだできない)。

---

## Phase 4: User Story 2 — 公開ゲート 1 点で承認 / 却下できること(P1)

**Goal**: 承認 → 予定時刻公開 / 遅延公開、却下 → `rejected_pending` + 理由が企画 LLM 入力へ、期限超過 → 自動 `skipped`。INV-2 が公開の唯一の入口。

**Independent Test**: spec.md US2 の Independent Test。

### Tests for User Story 2

- [ ] T044 [P] [US2] **TEST FIRST** `backend/tests/critical/test_publish_invariants.py`(critical #7 完成): INV-2 の 5 条件(AI 開示 ∧ 指紋 CLEAR ∧ 有効な承認記録 ∧ `publish_block_reason=null` ∧ `running`)の全欠落パターンで公開拒否、公開経路が `publisher.py` 以外に無いこと(API / scheduler / 遅延公開 / quota 復帰の全てが同一関数を通る)を 100% カバー
- [ ] T045 [P] [US2] **TEST FIRST** `backend/tests/integration/test_publish_gate.py`: 承認 → `approved` → 時刻到来で `published`、却下(理由必須)→ `rejected_pending` → 次回企画入力に理由が入る、遅延公開(期限内承認で即時公開)、期限超過で T12 / T14 / T16 の自動 `skipped`(freezegun)

### 実装

- [ ] T046 [US2] backend `domain/publish/gate.py`: 承認 / 却下(approval_records 記録、actor=human)+ `api/slots.py` に approve / reject / skip / start を追加
- [ ] T047 [US2] backend `domain/publish/publisher.py`: INV-2 ガード + youtube uploader(T026 移植物)呼び出し + `videos` 記録 + compliance_gate 通過を T044 で driven 実装
- [ ] T048 [US2] backend `domain/slots/deadlines.py` + scheduler job: 承認期限判定(毎時)+ 公開実行 job(時刻到来 / 遅延公開)
- [ ] T049 [US2] backend `domain/publish/quota.py`: quotaExceeded / 429 検知 → `publish_block_reason=quota_exhausted` 待機 → リセット後に古い枠から公開(FR-016)。警告閾値は `PUBLISH_WARN_PER_DAY`(research R-1)
- [ ] T050 [US2] backend `api/slots.py` に成果物ファイル配信(`GET /slots/{id}/artifacts/{id}/file`、認証付き、410 = 保持期間超過)
- [ ] T051 [P] [US2] frontend 枠詳細に公開ゲート UI: プレビュー(動画再生 / タイトル / 説明文 / チャプター)+ 検査サマリ + 承認 / 却下(理由入力)+ 承認済み取消(却下扱い)

**Checkpoint**: L0 で「編成 → 制作 → 承認 → 公開」の最小経路が閉じる。

---

## Phase 5: User Story 3 — 止めたいときに止まり、事故時は勝手に止まること(P1)

**Goal**: 2 段階停止(CLI + Slack)、compliance 自動停止、heartbeat。

**Independent Test**: spec.md US3 の Independent Test。

### Tests for User Story 3

- [ ] T052 [P] [US3] **TEST FIRST** `backend/tests/critical/test_compliance_auto_stop.py`(critical #6): 3 事象(公開後指紋一致 / YouTube ポリシー通知 / `containsSyntheticMedia` 未設定検知)それぞれで、人の操作なしに `publish_paused` + 該当動画 private 化 + L0 降格 + Slack 通知(`[COMPLIANCE]` prefix + `<!channel>`)が走ることを 100% カバー
- [ ] T053 [P] [US3] **TEST FIRST** `backend/tests/integration/test_stop_controls.py`: `pause-publishing` 中は制作継続 + 公開のみ停止、`stopped` 中は進行中工程の完走 + 新規開始なし、`resume` 後の遅延公開、再起動後の `system_state` 維持 + scheduler 自動再開

### 実装

- [ ] T054 [US3] backend `domain/safety/auto_stop.py` を T052 で driven 実装(エラー 5 分類との配線: `fatal` → `stopped`、FR-044)
- [ ] T055 [US3] backend `cli.py`: `ymg stop` / `pause-publishing` / `resume`(Typer、確認プロンプト付き、`YMG_ADMIN_TOKEN` で `PUT /system/state` を呼ぶ)+ `api/system.py`(GET は UI 用、PUT は管理トークン必須)
- [ ] T056 [US3] backend `infrastructure/slack/commands.py`: Bolt Socket Mode 常駐タスク(R-2)。stop / pause / resume ボタン + コマンドを T055 と同一サービス層に配線(3 秒 ack)
- [ ] T057 [US3] backend `domain/safety/heartbeat.py` + scheduler job(07:30 JST): 当日公開予定枠 / 要確認件数 / システム状態を Slack 送信(FR-045)
- [ ] T058 [P] [US3] frontend 設定画面: システム状態の**表示のみ**(現在状態 + 停止コマンドの案内、操作ボタンなし、FR-042)

**Checkpoint**: 憲法 I の停止手段が新モデルで完備。**ここまでで MVP(L0 運用開始可能)**。

---

## Phase 6: User Story 4 — 実績で解錠される自動運転レベル(P2)

**Goal**: L0/L1/L2、実績昇格、無条件降格、取り下げ。

**Independent Test**: spec.md US4 の Independent Test。

### Tests for User Story 4

- [ ] T059 [P] [US4] **TEST FIRST** `backend/tests/integration/test_autonomy.py`: L0→L1 判定(直近 10 枠連続無修正承認、却下 / やり直しでリセット、検査自動不合格 0)、L1→L2 判定(30 日 + 取り下げ 0 + compliance 0)、未達時の PUT 409 + 達成状況数値、L1 でのシステム承認記録(actor=system)+ `awaiting_approval` スキップ(T6)、降格の無条件即時、compliance 発動時の自動 L0 降格

### 実装

- [ ] T060 [US4] backend `domain/autonomy/levels.py`: 判定を approval_records / slot_timeline / audit_log から**毎回導出**(data-model.md)、`api/system.py` に GET/PUT /system/autonomy、監査ログ記録
- [ ] T061 [US4] backend `domain/autonomy/withdraw.py`: 取り下げ = private 化 → `withdrawn`(T18)+ `api/slots.py` に withdraw + Slack 取り下げボタン
- [ ] T062 [US4] backend L1 / L2 の公開ゲート自動化: packaging 完了時にシステム承認 → `approved` 直行(T6)、L1 公開通知に取り下げ導線(「あと N 時間」= SLA 表示、Block Kit)
- [ ] T063 [P] [US4] frontend 設定画面のレベル UI: 未達レベル選択不可 + 未達理由 + 達成状況(「あと N 枠」)、降格ボタン(即時)

**Checkpoint**: 実績を積めば L1 / L2 で自動公開が回る。

---

## Phase 7: User Story 5 — 工程単位のやり直しと安全な部分再生成(P2)

**Goal**: DAG 由来の部分やり直し、承認失効、プロンプトオーバーライド。

**Independent Test**: spec.md US5 の Independent Test。

### Tests for User Story 5

- [ ] T064 [P] [US5] **TEST FIRST** `backend/tests/integration/test_rerun.py`: トラック 03 単体(他 5 本 + サムネ + 文言保持 → 検査から再走行)、文言のみ(video 保持 → package のみ再生成)、企画から(全部)、`approved` でのやり直し → T17 + 承認失効 → 再ゲート、`published` は 409、`rejected_pending` からの任意工程やり直し(T13)、プロンプトオーバーライドが provenance に残ること

### 実装

- [ ] T065 [US5] backend rerun サービス(`domain/production/line.py` に再投入経路)+ `api/slots.py` /rerun(無効化された成果物一覧を返す)
- [ ] T066 [US5] backend プロンプト管理: `api/prompts.py`(一覧 / 新 version 保存 / **test-run = 枠に影響しない試し実行**)+ 枠の `prompt_overrides` 適用
- [ ] T067 [P] [US5] frontend 枠詳細のやり直し UI: 「この操作で作り直されるもの」を DAG から自動生成した確認文言で表示(ADR-0046)、トラック単体 / 文言のみ / 工程単位の導線、やり直し履歴(タイムライン)
- [ ] T068 [P] [US5] frontend `app/(admin)/prompts/page.tsx`: 工程 1:1 のプロンプト管理 + version 切替 + 試しに 1 件生成

**Checkpoint**: 却下 → 直して再ゲートのループが GPU 時間を無駄にせず回る。

---

## Phase 8: User Story 6 — 目的関数に基づく分析と企画フィードバック(P2)

**Goal**: 総視聴時間 + 維持率ガードレール、おまかせ本実装、配分縮小、推奨、視聴者の声。

**Independent Test**: spec.md US6 の Independent Test。

### Tests for User Story 6

- [ ] T069 [P] [US6] **TEST FIRST** `backend/tests/integration/test_objective_function.py`: ガードレール(直近実績中央値 × 0.8)、2 週連続割れ → 配分自動縮小(撤退なし)、昇格 / 撤退推奨の算出(提示のみ)、確定値のみ使用(7 日未満除外、`is_final`)、サンプル 3 本未満は統計判断対象外、全判断に evidence(数値 + 理由)が付くこと

### 実装

- [ ] T070 [US6] backend [移植改修] analytics 取得 job: analytics_client(T026)で日次取得 → `analytics_daily` upsert + `is_final` 判定(公開 7 日)
- [ ] T071 [US6] backend `domain/objective/function.py` + `genre_select.py` 本実装(T035 を確定値ベースに置換)+ `recommend.py`(配分縮小 / 昇格・撤退推奨)+ `genre_weekly_allocations` 週次 job
- [ ] T072 [US6] backend [移植改修] comments 取得 + センチメント要約(LLM)→ `comments.sentiment` + 実績スナップショットに反映(FR-076)。書き込み系 API 不使用
- [ ] T073 [US6] backend `api/analytics.py`(overview / genres / voices)+ `api/genres.py`(一覧 / 追加 / role 遷移 = 運営判断 + audit、recommendations)
- [ ] T074 [P] [US6] frontend `app/(admin)/analytics/page.tsx`: 目的カード / ガードレール下限ライン / 「暫定」表記 / 視聴者の声(読み取りのみと明示)
- [ ] T075 [P] [US6] frontend `app/(admin)/genres/page.tsx`: ポートフォリオ、role 遷移(運営判断)、推奨 + evidence 表示

**Checkpoint**: 「観察 → 企画」のループが目的関数で説明可能になる。

---

## Phase 9: User Story 7 — 旧システムからの移行(P3)

**Goal**: 公開実績のみの 1 回きり移行。移植物の独立性確認。

**Independent Test**: spec.md US7 の Independent Test。

### Tests for User Story 7

- [ ] T076 [P] [US7] **TEST FIRST** `backend/tests/integration/test_migrate_legacy.py`: 旧スキーマ fixture → `videos`(公開済みのみ、slot_id NULL)/ `analytics_daily` / `genres` / `model_pricing` / `oauth_credentials` が移行され、Plan / dryrun / job_history 由来のデータが**入らない**こと(R-4)

### 実装

- [ ] T077 [US7] backend `cli.py` に `ymg migrate-legacy`(`LEGACY_DATABASE_URL` 読み取り専用接続、冪等、実行サマリをタイムラインに記録)
- [ ] T078 [US7] 移植モジュールの独立性検査: `ymg_backend` 内に旧概念(plans / dryrun / posts / panic_stop)への import が残っていないことを CI の静的チェック(ruff banned-api or grep gate)で強制

**Checkpoint**: 過去実績が目的関数(US6)に流れる。

---

## Phase 10: Polish & Cross-Cutting Concerns

### 保持ポリシー(ADR-0049)

- [ ] T079 backend `domain/retention/cleaner.py` + 日次 job: 90 日(公開済み package)/ 7 日(invalidated 旧世代)/ 30 日(終端枠)のファイル削除(メタ残置 + `file_deleted_at`)、削除件数・解放容量をタイムライン記録。freezegun integration test 付き
- [ ] T080 [P] `infra/scripts/backup.sh` 更新: バックアップ対象を永続保持のみに変更(動画ファイル / 旧世代を除外、Fernet 鍵除外は継承)

### 画面残り

- [ ] T081 [P] frontend `app/(admin)/timeline/page.tsx`: 出来事ログ + 当日サマリ
- [ ] T082 [P] frontend `app/(admin)/settings/page.tsx` 統合: 自動運転レベル(T063)/ LLM provider + 月次コスト(001 移植)/ GPU health / バックアップ状態 / システム状態表示(T058)

### 品質ゲート

- [ ] T083 backend `make test-critical` を critical 8 件(fernet / llm_provider / directive_parser / acoustid / compliance_validation / state_machine + publish_invariants / artifact_invalidation / compliance_auto_stop + system_state_guards)の 100% 強制に更新
- [ ] T084 [P] `.github/workflows/ci.yml`: critical 100% + property テスト + frontend(eslint / tsc / vitest / Playwright)+ gpu_worker docker build(継承)を merge ゲート化
- [ ] T085 [P] frontend E2E(Playwright): 編成表 → 枠詳細 → 承認 → 公開表示 / 却下 → やり直し の 2 シナリオ

### ドキュメント / ADR 追従

- [ ] T086 [P] ADR-0045 の quota 記述を増分更新(R-1 の確認結果: `videos.insert` 専用バケット 100 call/日、2025-12 / 2026-06 変更。確認日と出典を明記)
- [ ] T087 [P] `README.md` のアーキ図 / 画面説明を枠中心モデルに更新
- [ ] T088 quickstart.md §7 チェックリストの実機ウォークスルー(停止ドリル + compliance ドリル含む)+ 結果を追記

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup(Phase 1)**: 依存なし、即開始可能
- **Foundational(Phase 2)**: Setup 完了 → 全 US の前提。T010〜T012(DB)→ T015〜T021(状態機械 / DAG / ガード)の順。移植(T022〜T028)は DB 完了後に並列
- **US1〜US7(Phase 3〜9)**: Foundational 完了後。US2 は US1 の `awaiting_approval` 到達が前提。US3 は US2 の publisher が前提(参照点 2 の実体)。US4 は US2 + US3(システム承認 + heartbeat 前提)。US5 は US1 + US2(承認失効)。US6 は US2 の公開実績(fixture で先行可)。US7 は Foundational のみ
- **Polish(Phase 10)**: T079 / T080 は US2 後、T083〜T085 は全 critical 完了後

### Critical Path Test 一覧(憲法 II v2.0.0 / plan.md)

| Critical Path | TEST FIRST Task |
| --- | --- |
| Fernet 暗号化 / 復号 | T022(移植) |
| LLM Pydantic validation + サブスク拒否 | T023(移植) |
| directive parser | T024(移植) |
| AcoustID プレチェック | T025(移植) |
| containsSyntheticMedia バリデーション | T025(移植) |
| 状態機械遷移ガード + 公開遷移 INV-2 | T015 + T044 |
| 成果物無効化規則(DAG + 承認失効) | T018 |
| compliance 自動停止経路 | T052 |

これら 8 件は 100% カバレッジ強制(T083 / T084 で gate)、他は best effort 60-70%。

### Parallel Opportunities

- Phase 1: T004〜T008 並列
- Phase 2: T011 / T013 / T014 並列、T015 / T018 / T020 のテスト並列、T022〜T028 の移植は全並列
- Phase 3 以降: 各 US 内の frontend タスクは backend API 完了後に並列。US6(fixture 先行)と US7 は他 US と並列可

---

## Implementation Strategy

### MVP First(US1 + US2 + US3 = L0 運用開始ライン)

1. **Phase 1 Setup** 完走(T001〜T008)
2. **Phase 2 Foundational** 完走(T010〜T030。critical T015 / T018 / T020 + 移植 5 件込み)
3. **Phase 3 US1**: 枠が `awaiting_approval` に到達
4. **Phase 4 US2**: 公開ゲート + INV-2 → 1 本公開できる
5. **Phase 5 US3**: 停止手段 + compliance 自動停止 + heartbeat → **L0 運用開始**
6. 以降 US5(やり直し)→ US4(自動運転)→ US6(分析)→ US7(移行)→ Polish

### Incremental Delivery

- **Sprint 1**: Phase 1 + 2(基盤 + 移植)
- **Sprint 2**: US1(編成表 + 制作ライン)
- **Sprint 3**: US2 + US3(公開 + 安全装置)→ L0 運用開始可能
- **Sprint 4**: US5 + US4(やり直し + 自動運転)
- **Sprint 5**: US6 + US7(分析 + 移行)
- **Sprint 6**: Polish(保持 / CI / ドキュメント)

---

## Notes

- [P] = 別ファイル / 依存なし。[移植] = `main@9c42624` の 001 実装から(テストも一緒に)
- critical path は RED → GREEN(テストを先に書いて fail させてから実装)
- コミットは ADR-0030 の `<type>(<scope>): <description>` 規約
- 各 checkpoint で動作確認、実装中の新規判断は ADR 増分(0051〜)
- 不可逆操作(公開 / private 化)の実機検証は unlisted で先行確認(001 の慣行を継承)
