# ADR インデックス

YouTube 音楽投稿自動化システムのアーキテクチャ決定記録 (ADR-0001〜0040)。
新規 ADR を追加したら本インデックスにも 1 行追記する。テンプレートは
[0000-template.md](0000-template.md)。

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

- [ADR-0004](0004-posting-volume-staged-from-1-2-per-day.md) — 投稿規模は 1日1〜2本 から段階的に拡大
- [ADR-0006](0006-cycle-structure-daily-and-weekly.md) — サイクル構造 = 日次 + 週次 + 音楽専用実行モード
- [ADR-0007](0007-dryrun-mode-as-mvp-requirement.md) — dryrun モードを MVP 必須機能に含める
- [ADR-0025](0025-dryrun-lifecycle-state-based.md) — dryrun ライフサイクル = 状態別 retention(承認 / 否認 / 無反応)
- [ADR-0035](0035-staged-rollout-dryrun-default.md) — MVP は全機能実装 + dryrun=ON 既定で段階移行する
- [ADR-0036](0036-job-history-trigger-text-check.md) — job_history.trigger は TEXT+CHECK (NULL 可)、ENUM は作らない
- [ADR-0011](0011-scheduler-apscheduler-in-backend-process.md) — スケジューラ = APScheduler(承認 Plan・single-flight)
- [ADR-0039](0039-evidence-backed-plan-approval-and-audio-qa.md) — 根拠付き Daily Plan 承認ゲート・仕様 hash・Audio QA

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
- [ADR-0031](0031-deploy-procedure.md) — デプロイ手順(実行方式・フロー・オートスタート・ロールバック)
