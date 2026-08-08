# Implementation Plan: 枠中心モデルへの再設計(Slot-Centric Redesign)

**Branch**: `cursor/002-slot-centric-redesign-bb9d` | **Date**: 2026-08-07 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-slot-centric-redesign/spec.md` + ADR-0041〜0050 + 憲法 v2.0.0

## Summary

集約ルートを Plan / dryrun / Post から**公開枠(Slot)**へ移す。オーケストレーション層(枠・パターン・物化・状態機械 10 状態・スケジューリング・公開ゲート・管理 UI 全画面)は**新規実装**し、動作実績のある工程実装(ACE-Step / AcoustID / Audio QA / ffmpeg 合成 / SDXL サムネ / YouTube クライアント / Fernet 鍵管理 / LLM 抽象 / セッション認証 BFF / directive parser / テンプレ)は 001 実装から**移植**する(ADR-0050)。承認は公開ゲート 1 点に集約し(ADR-0042)、実績で解錠される自動運転レベル L0/L1/L2(ADR-0043)と 2 段階の停止状態 + compliance 自動停止(ADR-0044)で安全性を担保する。

## Technical Context

**Language/Version**: backend / gpu_worker = Python 3.13(uv 管理)、frontend = TypeScript 5 + Next.js 15(App Router)

**Primary Dependencies**: FastAPI / SQLAlchemy 2.x(async) / Alembic / Pydantic v2 / APScheduler / loguru / fsspec / pyacoustid / cryptography(Fernet) / google-api-python-client / openai / anthropic / ollama / Typer(新 CLI `ymg`) / Slack(通知 = 単一 webhook 継承、ボタン操作 = research.md R-2 参照)。frontend = shadcn/ui / @tanstack/react-query / recharts / openapi-typescript / vitest / Playwright

**Storage**: PostgreSQL(新スキーマ。旧 DB は凍結・アーカイブ、公開実績のみ 1 回きり移行)+ fsspec 抽象のローカルファイル(`file://`、ADR-0022)

**Testing**: backend = pytest + pytest-asyncio + respx + freezegun + hypothesis(状態機械 / 物化の property test)、frontend = vitest + Playwright。Critical path は TDD + 100% カバレッジ(憲法 II v2.0.0)

**Target Platform**: 自宅 Linux サーバ(RTX 3090 単機)。backend / frontend / postgres は docker compose、gpu_worker は host 直 + systemd(ADR-0031 のデプロイ手順を継承)

**Project Type**: Web application(backend + frontend + gpu_worker のモノレポ、ADR-0029 継承)

**Performance Goals**: 1 枠 = 6 トラック生成 + 合成で GPU 直列 30〜60 分程度(実測依存)。API / UI は個人利用規模(同時 1 ユーザー)で十分

**Constraints**: YouTube API quota(`videos.insert`=1600 units、10,000 units/日 → 実効 約 6 本/日。実装時に現行値を再確認)、GPU 1 台直列(EDF 順)、LAN 内運用(認証必須)、物化は決定論的(INV-6)

**Scale/Scope**: 画面 7 枚(編成表 / 枠詳細 / タイムライン / ジャンル / 分析 / プロンプト / 設定)、新規テーブル約 15、状態機械 10 状態 18 遷移、移植対象モジュール約 12

## Constitution Check

*GATE: 憲法 v2.0.0 に対して評価。*

### I. Compliance-First (NON-NEGOTIABLE) — PASS

- `containsSyntheticMedia=true` 必須 + 公開前バリデーション(FR-065、移植)
- AcoustID 指紋プレチェック(FR-061、移植)+ 3 連続ヒットでジャンル一時停止(FR-023)
- 停止手段 = `system_state`(`publish_paused` / `stopped`)を CLI + Slack から(FR-040〜042)。参照点 2 箇所固定(工程開始直前 / 公開 API 直前)を **`StageRunner` / `Publisher` の共通ガード**として実装し、これを通らない実行経路を作らない
- compliance 事象で自動停止 + private 化 + L0 降格 + 通知(FR-043)。critical path として test-first

### II. Test-First on Critical Paths (NON-NEGOTIABLE) — PASS

Critical path(TDD + 100% カバレッジ):

| # | Critical path | 新規 / 移植 |
|---|---|---|
| 1 | AcoustID + Chromaprint プレチェック | 移植(テストも移植) |
| 2 | `containsSyntheticMedia` バリデーション | 移植(テストも移植) |
| 3 | OAuth トークン Fernet 暗号化 / 復号 | 移植(テストも移植) |
| 4 | LLM 出力 Pydantic 検証(+ Anthropic サブスク起動拒否) | 移植(テストも移植) |
| 5 | directive parser | 移植(テストも移植) |
| 6 | compliance 自動停止経路(`publish_paused` 遷移 + private 化 + L0 降格) | **新規 test-first** |
| 7 | 公開遷移の不変条件 INV-2(AI 開示 ∧ 指紋 CLEAR ∧ 承認記録 ∧ ブロック無し ∧ `running`) | **新規 test-first** |
| 8 | 成果物の無効化規則(依存 DAG からの導出、承認失効 INV-5) | **新規 test-first** |

状態機械の遷移表 T1〜T18 と物化の決定論(INV-6)は hypothesis による property test で担保する(遷移ガードは #7 の一部として 100%、物化は best effort 高め)。

### III. Staged Autonomy with Reversibility (NON-NEGOTIABLE) — PASS

- 既定 L0、昇格は実績条件の自動判定のみ(FR-030〜033)。宣言で上げる経路なし
- L1 / L2 の前提 = 取り下げ(private 化)+ daily heartbeat(FR-035 / FR-045)
- 降格は無条件・即時(FR-034)。人の判断は公開ゲート + 運営判断のみ(FR-020)
- 却下理由は企画 LLM 入力へ(FR-022)

### IV. Provider / Resource Abstraction — PASS

LLM Provider 抽象 / fsspec / GPU worker HTTP 分離をすべて移植で継承(FR-066 / FR-103)。Anthropic サブスク起動拒否も継承。

### V. Structured Errors with Explicit Categories — PASS

5 分類を継承し、新モデルの挙動対応(FR-044)。`fatal`→`stopped`、`compliance`→FR-043、`quality`→自動再生成 / 上限で `failed`。

### VI. Structured Observability — PASS

構造化 JSON ログ(`slot_id` / `genre` / `stage` バインド)+ SSE(FR-104)、usage_log(FR-066)、実績スナップショット保存(FR-068)、プロンプト版数記録(FR-056)。

### VII. ADR-Driven Decisions — PASS

設計判断は ADR-0041〜0050 で確定済み。実装中の新規判断は ADR 追加(`specs/001-youtube-music-generator/adr/` に連番継続)。

## Project Structure

### Documentation (this feature)

```text
specs/002-slot-centric-redesign/
├── spec.md              # 正本仕様(ADR-0041〜0050 を統合)
├── plan.md              # 本ファイル
├── research.md          # Phase 0: 実装判断の確認事項(R-1〜R-6)
├── data-model.md        # Phase 1: 新スキーマ / 状態機械 / 成果物 DAG
├── quickstart.md        # Phase 1: セットアップ + 1 枠ウォークスルー
├── contracts/
│   ├── backend-api.yaml            # 新 API(OpenAPI)
│   ├── gpu-worker-api.yaml         # 001 から継承(コピー、変更なし)
│   └── llm-provider-interface.md   # 001 から継承(コピー、変更なし)
├── checklists/requirements.md
└── tasks.md             # Phase 2(/speckit-tasks)
```

### Source Code (repository root) — モノレポ(ADR-0029 継承)

旧オーケストレーション(`domain/plans` / `domain/dryrun` / `domain/panic_stop` / `domain/mvp_check` / 旧 `api/*` / 旧 frontend 画面)は**削除**し、以下に置き換える。移植モジュールは「移植」と明記。旧実装の参照は git 履歴(`main@9c42624` 以前)で行う(ADR-0050 の凍結)。

```text
backend/
├── src/ymg_backend/
│   ├── main.py                      # FastAPI app factory(認証 / lifespan / scheduler 自動再開)
│   ├── cli.py                       # ymg CLI(stop / pause-publishing / resume / migrate-legacy)
│   ├── core/
│   │   ├── config.py                # 移植(env 整理)
│   │   ├── logging.py               # 移植(bind: slot_id / genre / stage)
│   │   └── security.py              # 移植(Fernet + セッション認証、critical #3)
│   ├── domain/
│   │   ├── slots/
│   │   │   ├── state_machine.py     # 10 状態 / T1〜T18 / INV ガード(critical #7)
│   │   │   ├── materializer.py      # 03:00 JST 物化(決定論、INV-6)
│   │   │   ├── patterns.py          # 公開枠パターン(バージョン管理)+ 例外
│   │   │   └── deadlines.py         # 承認期限 7 日 / 遅延公開 / 自動 skipped
│   │   ├── artifacts/
│   │   │   ├── dag.py               # 成果物依存 DAG の単一定義(critical #8)
│   │   │   ├── invalidation.py      # 推移的無効化 + 承認失効(critical #8)
│   │   │   └── store.py             # 版 / 来歴の記録、fsspec URI 管理
│   │   ├── production/
│   │   │   ├── line.py              # 制作ライン(planning→generating→inspecting→packaging)
│   │   │   ├── stage_runner.py      # 工程開始前の system_state / blocked ガード(参照点 1)
│   │   │   ├── edf_queue.py         # GPU 実行順 = 公開予定の早い順
│   │   │   └── stages/              # 各工程(移植コードの呼び出し側)
│   │   │       ├── planning.py      #   企画 LLM(旧 planner を読み替え移植)
│   │   │       ├── generating.py    #   音楽生成 dispatch(移植: music_jobs)
│   │   │       ├── inspecting.py    #   指紋 + Audio QA(移植: acoustid / audio_qa)
│   │   │       └── packaging.py     #   サムネ / 動画合成 / 文言(移植: image_jobs, thumbnail_overlay, video_compose, render/*)
│   │   ├── publish/
│   │   │   ├── gate.py              # 公開ゲート(承認 / 却下、L0)+ システム承認(L1/L2)
│   │   │   ├── publisher.py         # 公開実行(参照点 2 + INV-2 ガード、critical #7)
│   │   │   └── quota.py             # quota 見積り / 枯渇待機 / 警告
│   │   ├── autonomy/
│   │   │   ├── levels.py            # L0/L1/L2、昇格判定(実績カウント)、降格
│   │   │   └── withdraw.py          # 取り下げ = private 化 → withdrawn
│   │   ├── safety/
│   │   │   ├── system_state.py      # running / publish_paused / stopped(1 行)
│   │   │   ├── auto_stop.py         # compliance 自動停止(critical #6)
│   │   │   └── heartbeat.py         # 07:30 JST 当日サマリ
│   │   ├── objective/
│   │   │   ├── function.py          # 総視聴時間 + 維持率 80% ガードレール
│   │   │   ├── genre_select.py      # おまかせ選択 + 理由生成
│   │   │   └── recommend.py         # 配分縮小 / 昇格・撤退推奨
│   │   ├── retention/
│   │   │   └── cleaner.py           # 保持ポリシー削除ジョブ(90/30/7 日)
│   │   ├── compliance/              # 移植(acoustid.py / validators.py、critical #1 #2)
│   │   ├── directive/               # 移植(parser.py、critical #5)
│   │   ├── prompts/ templates/      # 移植(loader + prompt/template 資産)
│   │   └── errors/                  # 移植(5 分類)
│   │   ├── analytics/               # 移植改修(summary → 実績スナップショット)
│   ├── llm/                         # 移植(base / openai / anthropic / ollama / pricing / usage_writer / factory、critical #4)
│   ├── infrastructure/
│   │   ├── db/                      # 新スキーマ ORM(session.py は移植)
│   │   ├── youtube/                 # 移植(uploader / oauth / analytics_client / comments_client / compliance_gate)
│   │   ├── slack/                   # 移植 + 拡張(notifier + コマンド/ボタン受信、research R-2)
│   │   ├── storage/                 # 移植(fsspec_wrapper)
│   │   ├── gpu_worker_client.py     # 移植
│   │   ├── scheduler.py             # 新規(物化 / 期限 / heartbeat / 削除 / analytics jobs、自動再開)
│   │   ├── event_bus.py             # 移植(SSE 用)
│   │   └── audit.py                 # 移植(audit_log writer)
│   └── api/                         # 新 API(contracts/backend-api.yaml 準拠)
│       ├── schedule.py              # 編成表 / パターン / 例外
│       ├── slots.py                 # 枠詳細 / 承認 / 却下 / やり直し / 取り下げ / 単発枠
│       ├── system.py                # system_state(表示用 GET + CLI/Slack 用 PUT)/ autonomy / health
│       ├── genres.py analytics.py prompts.py llm.py timeline.py sse.py auth.py
├── prompts/ templates/              # 移植(資産。planner → 企画 LLM に読み替え)
├── alembic/versions/                # 新チェーン(002 ベースライン)
└── tests/
    ├── critical/                    # critical #1〜#8(100%)
    ├── integration/                 # 枠ライフサイクル E2E / 移行スクリプト
    └── property/                    # hypothesis: 状態機械 / 物化決定論

frontend/
├── app/(admin)/
│   ├── page.tsx                     # 編成表(ホーム、週間カレンダー + パターン編集)
│   ├── slots/[id]/page.tsx          # 枠詳細(工程 IN/OUT、やり直し、公開ゲート)
│   ├── timeline/page.tsx            # タイムライン
│   ├── genres/page.tsx              # ジャンル(ポートフォリオ / role 遷移)
│   ├── analytics/page.tsx           # 分析(目的カード / 視聴者の声)
│   ├── prompts/page.tsx             # プロンプト(工程 1:1 + 試しに 1 件生成)
│   └── settings/page.tsx            # 設定(自動運転レベル / LLM / GPU / バックアップ / システム状態表示)
├── app/api/                         # 移植(セッション認証 BFF: auth/ + backend proxy)
├── app/login/                       # 移植
└── lib/                             # 移植改修(fetch wrapper / openapi-typescript / SSE)

gpu_worker/                          # 変更なし(契約維持、ADR-0031)
infra/
├── systemd/                         # 継承(ymg-stack / ymg-gpu-worker / ymg-backup)
└── scripts/                         # backup.sh 継承、panic-stop.sh 削除(ADR-0044 置換)、migrate-legacy.sh 新規
```

**Structure Decision**: モノレポ内での in-place 置き換え。旧オーケストレーションはこの feature ブランチ上で削除し、「参照用凍結」は git 履歴(main の 9c42624 以前)と旧ブランチ `001-youtube-music-generator` で担保する。並行稼働はしない(ADR-0050)。

## Phase Plan

### Phase 0: Outline & Research ✅(research.md)

設計判断は ADR で確定済みのため、Phase 0 は「実装時に再確認が必要な外部事実」の洗い出しに限定する(R-1: YouTube quota 現行値、R-2: Slack ボタン受信方式、R-3: hypothesis の導入、R-4: 旧 DB 移行の対象範囲、R-5: Typer CLI、R-6: alembic チェーンのリセット)。

### Phase 1: Design & Contracts ✅(data-model.md + contracts/ + quickstart.md)

- data-model.md: 新テーブル群(slot_patterns / pattern_rows / slot_exceptions / slots / artifacts / approval_records / system_state / autonomy_state / genres / videos / analytics_daily / comments / metrics_snapshots / slot_timeline / audit_log / usage_log / model_pricing / oauth_credentials / prompt_versions)+ 状態機械 + DAG
- contracts/backend-api.yaml: 新 API。gpu-worker-api.yaml / llm-provider-interface.md は 001 から無変更コピー(契約維持の宣言)
- quickstart.md: env / make / 移行スクリプト / 1 枠ウォークスルー

### Phase 2: Task generation(/speckit-tasks → tasks.md)

User Story 単位(US1〜US7)+ Setup / Foundational / Polish。critical #6〜#8 は test-first を明記。

## Post-Phase 1 Constitution Check Re-eval

Phase 1 の設計後も違反なし。特記:

- INV-2 ガードを `publisher.py` の単一経路に集約し、API / スケジューラ / 遅延公開 / quota 復帰のすべてがここを通る設計とした(憲法 I の「ここを通らない実行経路を作らない」)
- 成果物 DAG は `artifacts/dag.py` の単一定義から導出(ADR-0046 の受容リスク対策)

## Complexity Tracking

> 憲法違反なし。ただし旧設計からの複雑性増を記録する。

| 複雑性 | 必要性 | 単純な代替を退けた理由 |
|---|---|---|
| 成果物ごとの版 / 来歴記録 | ADR-0046 の部分やり直し + 承認失効 | 「常に全部作り直し」は GPU 時間を浪費(ADR-0046 で不採用済み) |
| 保持期間 4 系統の削除ジョブ | ADR-0049 | 全永続はディスクが枯渇(ADR-0049 で不採用済み) |
| Slack 受信系の追加(通知のみ→操作) | ADR-0043 / 0044 が Slack からの取り下げ / 停止を要求 | UI のみでは外出時に停止できない(ADR-0044 で不採用済み) |

## Next: Phase 2

`/speckit-tasks` で tasks.md を生成する。

## References

- ADR-0041〜0050(正本)、憲法 v2.0.0
- モック: `specs/001-youtube-music-generator/mockups/redesign-2026-07/slot-centric-mock.html`
- 旧実装(移植元): git `main@9c42624` の `backend/` / `frontend/` / `gpu_worker/`
