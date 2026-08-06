# ADR インデックス

YouTube 音楽投稿自動化システムのアーキテクチャ決定記録 (ADR-0001〜0050)。
新規 ADR を追加したら本インデックスにも 1 行追記する。テンプレートは
[0000-template.md](0000-template.md)。

> **2026-07 の再設計について**: ADR-0041〜0050 が枠(Slot)中心モデルへの再設計を定義し、
> 一部の旧 ADR を置き換える。置き換え / 継承の一覧は [ADR-0050](0050-redesign-migration-strategy.md) にある。
> 以下で ⚠ が付いた項目は置き換え済み(履歴として残す)。

## 再設計 — 枠中心モデル (2026-07)

- [ADR-0041](0041-slot-as-aggregate-root.md) — 公開枠(Slot)を集約ルートとする。パターン → 物化、画面は編成表 → 枠詳細
- [ADR-0042](0042-approval-consolidated-to-publish-gate.md) — 承認を公開ゲート 1 点 + 運営判断に集約(dryrun / Plan 承認 / トラックレビューを廃止)
- [ADR-0043](0043-autonomy-levels-and-promotion-criteria.md) — 自動運転レベル L0 / L1 / L2、実績による昇格条件、取り下げ = private 化
- [ADR-0044](0044-stop-controls-and-safety-interlocks.md) — 停止手段 = 2 段階のシステム状態(CLI + Slack)、compliance 自動停止、daily heartbeat
- [ADR-0045](0045-slot-state-machine.md) — 枠の状態機械 10 状態・遷移表・不変条件、承認期限 7 日 / 遅延公開 / quota
- [ADR-0046](0046-stage-rerun-and-artifact-invalidation.md) — 工程やり直しは成果物依存 DAG で無効化を決める、承認の失効
- [ADR-0047](0047-channel-objective-function.md) — 目的関数 = 週次総視聴時間の最大化 + 維持率 80% ガードレール
- [ADR-0048](0048-viewer-comment-analysis-read-only.md) — 視聴者コメントは読み取り専用で分析に用いる
- [ADR-0049](0049-artifact-retention-policy.md) — 成果物の保持・削除ポリシー(永続 / 90 日 / 30 日 / 7 日)
- [ADR-0050](0050-redesign-migration-strategy.md) — 移行戦略 = オーケストレーション新規実装 + 工程実装の移植、旧 ADR の置き換え一覧

## 全体構成・基盤

- [ADR-0001](0001-system-architecture-python-backend-nextjs-frontend.md) — バックエンド = Python、管理UI = Next.js の2層構成
- [ADR-0009](0009-backend-framework-fastapi.md) — バックエンド Web フレームワーク = FastAPI
- [ADR-0010](0010-database-postgresql-with-pgvector.md) — DB = PostgreSQL(将来の pgvector / RAG 拡張を見据える)
- [ADR-0014](0014-python-package-manager-uv.md) — Python パッケージ管理 = uv
- [ADR-0022](0022-storage-abstraction-fsspec.md) — ストレージ抽象化 = `fsspec`
- [ADR-0029](0029-monorepo-with-directory-split.md) — モノレポ + シンプルなディレクトリ分割

## LLM

- [ADR-0002](0002-master-llm-cloud-api-with-abstraction-layer.md) — マスターLLM はクラウドAPI 前提、ローカルへの切替層は維持
- [ADR-0008](0008-llm-responsibility-scope.md) — マスターLLM の責務範囲
- [ADR-0018](0018-llm-structured-output-pydantic.md) — LLM 構造化出力 = Pydantic + LLMProvider 抽象化層
- [ADR-0019](0019-llm-provider-implementations.md) — LLM Provider 実装方針 — OpenAI / Anthropic / Ollama
- [ADR-0024](0024-llm-cost-and-usage-tracking.md) — LLM コスト・トークン使用量管理
- [ADR-0032](0032-improvement-plan-llm-schema.md) — 改善計画 LLM の出力スキーマ
- [ADR-0033](0033-initial-genres-and-planner-prompt.md) — 初期ジャンル候補と改善計画 LLM のプロンプト構造
- [ADR-0037](0037-llm-mismatch-repair.md) — LLM provider/model 不整合の GET 公開と PUT 修復
- [ADR-0040](0040-runtime-caption-includes-llm-proposal-fields.md) — 実行時プロンプトへ LLM提案フィールドを決定論連結

## コンテンツ生成・動画

- [ADR-0003](0003-video-format-30min-via-six-track-stitching.md) — 動画フォーマット = 30分(300秒 × 6本連結)

- [ADR-0015](0015-video-visualizer-showwaves-overlay.md) — 動画ビジュアライザ = SDXL 生成サムネ + ffmpeg `showwaves` overlay
- [ADR-0016](0016-thumbnail-sdxl-model-strategy.md) — サムネ生成 = Juggernaut XL v10 デフォルト + ジャンル別モデル切替
- [ADR-0017](0017-directive-parser-auto-detect.md) — ディレクティブパーサ = 独自実装(自動判別方式)
- [ADR-0034](0034-default-templates.md) — タイトル / 説明文 / サムネのデフォルトテンプレ

## サイクル・運用フロー

- ⚠ [ADR-0004](0004-posting-volume-staged-from-1-2-per-day.md) — 投稿規模は 1日1〜2本 から段階的に拡大 → ADR-0041
- ⚠ [ADR-0006](0006-cycle-structure-daily-and-weekly.md) — サイクル構造 = 日次 + 週次 + 音楽専用実行モード → ADR-0041
- ⚠ [ADR-0007](0007-dryrun-mode-as-mvp-requirement.md) — dryrun モードを MVP 必須機能に含める → ADR-0042
- ⚠ [ADR-0025](0025-dryrun-lifecycle-state-based.md) — dryrun ライフサイクル = 状態別 retention → ADR-0042 / ADR-0049
- ⚠ [ADR-0035](0035-staged-rollout-dryrun-default.md) — MVP は全機能実装 + dryrun=ON 既定で段階移行する → ADR-0043
- ⚠ [ADR-0036](0036-job-history-trigger-text-check.md) — job_history.trigger は TEXT+CHECK → job_history 作り直しにより失効
- [ADR-0011](0011-scheduler-apscheduler-in-backend-process.md) — スケジューラ = APScheduler(single-flight)。※「承認 Plan のみ実行」前提は ADR-0045 に置換
- ⚠ [ADR-0039](0039-evidence-backed-plan-approval-and-audio-qa.md) — 根拠付き Daily Plan 承認ゲート・仕様 hash・Audio QA → 承認は ADR-0042、状態機械は ADR-0045(compile / 自動 QA の技術部分は継承)

## コンプライアンス・セキュリティ

- [ADR-0005](0005-content-id-pre-check-with-acoustid.md) — Content ID 事前チェック = AcoustID + Chromaprint
- [ADR-0012](0012-oauth-token-encrypted-in-postgres.md) — OAuth2 リフレッシュトークン = PostgreSQL に対称鍵暗号化で保存
- [ADR-0013](0013-admin-ui-auth-basic.md) — 管理UI 認証 = frontend セッション + backend Basic(LAN 内)
- [ADR-0020](0020-ai-disclosure-via-contains-synthetic-media.md) — AI 開示フラグ = `status.containsSyntheticMedia=true`
- [ADR-0026](0026-backup-local-secondary-disk.md) — バックアップ = ローカル別ディスクのみ(段階的アプローチ)
- [ADR-0038](0038-validation-error-sensitive-input-redaction.md) — RequestValidationError の機密 `input` 除去

## アナリティクス・観測性・品質

- [ADR-0021](0021-analytics-data-api-and-analytics-api.md) — アナリティクス取得 = YouTube Data API + YouTube Analytics API
- [ADR-0023](0023-observability-structured-logging.md) — 観測性 = 構造化ログ + 実行進捗永続化(SSE)
- [ADR-0027](0027-testing-strategy-emphasis-on-critical-paths.md) — テスト方針 = メリハリ型(クリティカルパス厳格、その他 best effort)
- [ADR-0028](0028-error-categories-and-handling.md) — エラーカテゴリ分類と挙動

## デプロイ・git 運用

- [ADR-0030](0030-git-operations.md) — git 運用ポリシー(公開設定・ブランチ・コミット規約)
- [ADR-0031](0031-deploy-procedure.md) — デプロイ手順(実行方式・フロー・オートスタート・ロールバック)。※ `make panic-stop` と scheduler 手動 enable は ADR-0044 に置換
