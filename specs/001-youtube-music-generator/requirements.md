# YouTube 音楽投稿自動化システム 要件定義 v2

- **Status:** Draft (グリル全件反映済み、PoC 前)
- **Date:** 2026-05-26
- **Author:** @seita

> 本ドキュメントは要件の最新スナップショット。重要な決定は `adr/` に ADR として記録している。
> 矛盾が出た場合、**ADR の Decision を優先** し、本ドキュメントを追従修正する。

---

## 1. 概要

ローカル GPU マシン 1 台で AI 生成楽曲動画を継続投稿する自動化システム。
バックエンドが楽曲・サムネ・動画を生成し、AcoustID 事前チェック後 YouTube へ投稿する。
管理UI から進行状況・プロンプト介入・投稿管理・アナリティクスを操作する。

## 2. ゴール / 動機 / 成功条件

### 動機(優先順)

1. **B. YouTube アルゴリズム観察・実験データ蓄積**(ジャンル別の反応比較)
2. **C. マネタイズ**(段階的、長尺動画 + ミッドロール広告)

### 成功条件 / 撤退条件

- **暫定:** 1か月実績後に数値目標を確定する(現時点では未定義)
- **撤退条件(暫定):** チャンネル BAN 発生、または Content ID マッチ率が週 N 件超 → 規模縮小・運用ルール見直し

## 3. 前提・制約

### 実行環境

- GPU: RTX 3090 (24GB VRAM)
- OS: Ubuntu
- 開発と本番を分けず同一マシンで運用(代わりに **dryrun モード** で本番投稿の安全弁を作る、ADR-0007)

### GPU 占有制約

- VRAM 24GB の制約により、マスターLLM はクラウドAPI に逃がして VRAM を ACE-Step + SDXL 専用に確保する(ADR-0002)
- GPU 処理(音楽生成 → 画像生成)は基本シーケンシャル、同時ロード可能なら並列も検討

### 使用モデル

| 用途 | モデル | 切替 |
|------|--------|------|
| 音楽生成 | ACE-Step 1.5(Apache 2.0、商用可)| - |
| 画像生成(サムネ)| Juggernaut XL v10 デフォルト + RealVisXL/DreamShaper 等切替(ADR-0016)| 設定可能 |
| マスターLLM | OpenAI(API key / Codex OAuth)/ Anthropic(API key)/ Ollama を切替可能(ADR-0019)| 設定可能 |
| 構造化出力 | Pydantic + LLMProvider 抽象化層(ADR-0002, ADR-0018)| - |
| 指紋認識 | AcoustID + Chromaprint(無料、ADR-0005)| Phase 2 で ACRCloud 等の追加可 |

### 動画フォーマット(ADR-0003)

- 1動画 = **30分**(5分 × 6 楽曲を `ffmpeg acrossfade` で連結)
- 6 楽曲は **同一ジャンル内 6 サブテーマ + BPM/キー固定** で生成
- 映像 = 30分尺の波形ビジュアライザ + 固定サムネ(SDXL 生成)

### 投稿規模(ADR-0004)

- MVP = **1日1〜2本**(1〜2ジャンル × 各1動画 = 各30分)
- 1か月運用後に段階拡大を **人間判断** で実施

## 4. システム構成(ADR-0001, ADR-0029, ADR-0031)

### リポジトリ構成(ADR-0029)

- **モノレポ + シンプルなディレクトリ分割**: `backend/` + `frontend/` + `gpu_worker/` + `docs/` + `infra/` を 1 リポジトリに
- GitHub Private、 main + feature ブランチ + PR self-merge、 release tag なし(ADR-0030)
- コミット規約: `<type>(<scope>): <description>` 形式、 scope 推奨

### 実行方式(ADR-0031)

- backend / frontend / postgres は **docker compose**
- **GPU worker(ACE-Step + SDXL)は host 直 + systemd**、 ただし Dockerfile 用意でクラウド GPU(RunPod 等)への移行を構造的に保証
- backend ↔ GPU worker は **HTTP API + fsspec ストレージ** で疎結合
- デプロイ = `Makefile` 経由の手動(`make deploy`)、 reboot 後は API オートスタート / scheduler 手動 enable
- ロールバック = `git revert`、 マイグレーション前は自動 `pg_dump`、 `make panic-stop` でコンプラ事故時の YouTube 一括 private 化

```text
[ Browser ]                                  [ GPU Machine (Ubuntu) ]
    |                                                 |
    +--- HTTP / SSE ---> [ Next.js (管理UI) ] --+--- [ FastAPI ] (Python)
                                                |        |
                                                |        +-- Scheduler (日次/週次)
                                                |        +-- LLM Provider 抽象層
                                                |        +-- ACE-Step Runner (GPU)
                                                |        +-- SDXL Runner (GPU)
                                                |        +-- AcoustID Checker
                                                |        +-- FFmpeg Composer
                                                |        +-- YouTube Uploader
                                                |        +-- Slack Notifier
                                                |
                                                +--- [ PostgreSQL ] (状態・履歴)
                                                +--- [ Object Storage ] (動画・音楽・サムネ)
```

- バックエンド: **Python**(uv 管理[ADR-0014]、FastAPI[ADR-0009]、APScheduler[ADR-0011]、SQLAlchemy + Alembic、google-api-python-client、slack_sdk、pyacoustid、PyTorch 系、loguru[ADR-0023])
- フロントエンド: **Next.js (App Router)**(shadcn/ui、tanstack/query、recharts)
- 認証: バックエンド `Basic 認証`(LAN 内、ADR-0013)、OAuth2 トークンは PostgreSQL に対称鍵暗号化保存(ADR-0012)
- 型整合: OpenAPI スキーマ生成 + `openapi-typescript` で TS 型を自動生成
- DB: **PostgreSQL**(+ pgvector 拡張余地、ADR-0010)
- ストレージ: **fsspec** 抽象化で `file:// / s3:// / gs://` を切替(ADR-0022)
- 観測性: 構造化ログ(JSON Lines、loguru、ADR-0023) + LLM usage_log テーブル(ADR-0024)

## 5. 運用フロー(ADR-0006)

### 日次サイクル(毎日)

1. 改善計画 v_n を読み込み
2. ジャンル選定(マスターLLM、ADR-0008 の責務範囲内)
3. 楽曲生成 ×6(ACE-Step、サブテーマ別、BPM/キー固定)
4. AcoustID プレチェック(NG なら再生成 or スキップ、ADR-0005)
5. 楽曲連結(ffmpeg `acrossfade` 3〜5秒)
6. サムネ画像生成(SDXL)
7. 動画合成(波形ビジュアライザ + サムネ + 音声、30分)
8. タイトル・説明文生成(LLM、ディレクティブ展開)
9. YouTube 投稿(または dryrun ならスキップ、ADR-0007)
10. 失敗は Slack 通知のみ、ジャンル単位で独立

### 週次サイクル(週 1 回)

1. 過去 7 日分の動画の視聴数・コメント・retention 取得(YouTube Data API + Analytics API)
2. ジャンル別パフォーマンス分析(マスターLLM)
3. コメント傾向分析(マスターLLM)
4. 改善計画 v_(n+1) 生成
5. 管理UI で人間レビュー(承認 → 翌日以降の日次サイクルに反映)

## 6. 機能要件

### 6.1 オーケストレーション可視化

- 各ステップの進行状況・実行履歴・エラー状態を管理UI に表示
- ジャンルごとの処理状況も個別に確認可能
- SSE でリアルタイム進捗を配信

### 6.2 音楽生成方針への介入

- 改善計画用・ジャンル選択用のプロンプトを管理UI から差し替え可能
- 介入はいつでも可能、次回サイクルから反映
- 介入対象: 改善計画立案時 / ジャンル選択時

### 6.3 投稿動画の管理

- 投稿済み動画の確認・修正・デフォルト設定(サムネ・説明文等)が可能
- dryrun モードの出力を管理UI から承認 → 本番投稿

### 6.4 説明文テンプレート(ディレクティブ展開、ADR-0017)

- 固定テキスト部分 + LLM 生成部分(`{{...}}` 形式)を組み合わせ可能
- 自動判別: `{{単一識別子}}` は変数参照、`{{自由文}}` は LLM 生成
- LLM は曲のメタデータ(ジャンル・BPM・キー・サブテーマ)と前後文脈で生成
- 1テンプレ内の複数 LLM ディレクティブは 1回の LLM 呼び出しで JSON 構造化展開(ADR-0018)
- 例:

  ```text
  【新曲】{{この曲の雰囲気を1文で}}
  ジャンル: {{genre}}
  {{曲調や聴きどころを2〜3行で説明}}
  ── チャンネル登録お願いします ──
  #lofi #chill #studymusic
  ```

### 6.5 タイトル・サムネイル自動生成

- 動画タイトルもディレクティブ形式のテンプレート(ADR-0017)で指定可能
- サムネイル画像は SDXL 派生モデル(デフォルト Juggernaut XL v10、ADR-0016)で自動生成
- ジャンル別の代替モデル(RealVisXL/DreamShaper/Animagine 等)を設定可能
- 動画は SDXL サムネ + ffmpeg `showwaves` overlay(ADR-0015)

### 6.6 LLM モデルの個別制御(ADR-0019)

- マスターLLM・音楽生成モデル・画像生成モデルそれぞれを個別に切替可能
- マスターLLM の Provider 切替: OpenAI(API key / Codex OAuth)/ Anthropic(API key)/ Ollama(ローカル)
- 環境変数 `LLM_PROVIDER` + `LLM_AUTH_MODE` で選択、管理UI からも変更可能

### 6.7 dryrun モード(ADR-0007, ADR-0025)

- フラグ or 管理UI トグルで切替
- 投稿以外は本番と同一の経路で実行(AcoustID プレチェック・AI 開示フラグ検証も含む)
- 出力動画をローカル再生 or 管理UI でレビュー可能
- ライフサイクル: 承認 → 本番投稿、否認/無反応7日 → 自動削除
- 否認理由は改善計画 LLM の入力に活用

### 6.8 全動画のアナリティクス確認(優先度: 低)

- 投稿済み動画全体の分析データを表示
- ジャンル別の比較分析が可能

## 7. 非機能要件

### 7.1 GPU リソース管理(ADR-0002, ADR-0019)

- マスターLLM はクラウドAPI 経路をデフォルト、Ollama 経路も維持(VRAM 制約時の代替)
- ACE-Step (8〜12GB) + SDXL 派生 (約7GB fp16) の同時ロード可否を PoC で確認
- 不可ならロード/アンロード切替で運用

### 7.2 ストレージライフサイクル(ADR-0022, ADR-0025)

- 抽象化: `fsspec` で `file:// / s3:// / gs://` を統一
- 楽曲ファイル・サムネ画像: 永続保存
- 動画ファイル(投稿用): 投稿後 N 日でローカル削除、必要に応じて安価ストレージへ移送
- 動画ファイル(dryrun): 承認→即削除、否認→即削除、無反応→7日後削除
- 改善計画・分析結果・コメント分析・usage_log: DB に永続保存

### 7.3 失敗時の挙動 / 観測性(ADR-0023)

- 失敗理由を Slack に通知のみ、自動リトライなし
- ジャンル単位で独立して処理、1ジャンル失敗で他は継続
- 構造化ログ(JSON Lines、loguru)で後追い検索が可能
- 管理UI からログ閲覧(`video_id` / `genre` / `step` フィルタ)

### 7.4 認証・トークン管理(ADR-0012, ADR-0013)

- YouTube OAuth2 + リフレッシュトークン: PostgreSQL に **`Fernet` 対称鍵暗号化** で保存、鍵は環境変数
- OAuth スコープ: `youtube.upload` + `youtube` + `yt-analytics.readonly`(ADR-0021)
- AI 生成コンテンツの開示フラグ: `status.containsSyntheticMedia=true` を全投稿で設定(ADR-0020)
- 管理UI: Basic 認証(LAN 内、`.env` で資格情報管理)

### 7.5 バックアップ(ADR-0026)

- ローカルセカンダリディスクのみ、 オフサイトなし(全損リスクを受容)
- `pg_dump` + 動画/音楽メタデータ + `.env`(除く Fernet 鍵)を毎日 cron で別ディスクにコピー
- マイグレーション前は自動 `pg_dump`(ADR-0031 と統合)

### 7.6 テスト戦略(ADR-0027)

- **critical path 100%**: AcoustID + Chromaprint、 `containsSyntheticMedia` 必須化バリデーション、 OAuth トークン暗号化、 LLM 出力 Pydantic スキーマ検証、 directive parser
- それ以外は best effort 60-70% カバレッジ
- pytest + httpx で backend、 vitest + Playwright で frontend、 PoC コードはテスト免除

### 7.7 エラーハンドリング(ADR-0028)

5 種類のエラーカテゴリで一元管理:

| カテゴリ | 例 | 対応 |
|---|---|---|
| `transient` | API timeout, GPU OOM | 自動リトライ(max 2) |
| `recoverable` | LLM validation fail, genre 辞書外 | パラメータ調整 + リトライ |
| `fatal` | OAuth invalid, disk full | scheduler 停止 + Slack 通知 |
| `compliance` | `containsSyntheticMedia` 未設定、 ContentID マッチ | **投稿停止 + 該当動画 private 化(`make panic-stop`)** |
| `quality` | visual_direction 短すぎ、 BPM 範囲外 | デフォルトテンプレに fallback |

### 7.8 LLM コスト管理(ADR-0024)

- 全 LLM 呼び出しを `usage_log` テーブルに記録(provider/model/tokens/cost/context)
- 月予算 50%/80%/100% で Slack 通知、100% 時のデフォルト挙動 = 警告のみ
- Codex OAuth は rate limit ヘッダで quota 残量追跡(別軸)
- Ollama は GPU 時間・VRAM 占有時間で別軸記録

## 8. 著作権・コンプライアンス方針

- ジャンル選定 LLM には **プロンプトポリシー** を渡す(ADR-0008)
  - 特定アーティスト名禁止、現代ヒット曲名禁止、固有名詞原則禁止
- 投稿前に **AcoustID + Chromaprint** で指紋プレチェック(ADR-0005)
- AcoustID で漏れた Content ID マッチは **事後手動対処**
- ACRCloud 等の有料サービスは interface だけ用意、Phase 2 で評価
- AI 生成コンテンツの開示: 全投稿で `status.containsSyntheticMedia=true`(ADR-0020)
  - 投稿前バリデーション層で必須設定をチェック、未設定なら投稿停止 + Slack 通知

## 9. リスクと受容

| リスク | 対策 / 受容理由 |
|--------|----------------|
| YouTube AI生成大量投稿でのチャンネル BAN | ADR-0004(規模縮小)、ADR-0007(dryrun)で緩和 |
| AcoustID で漏れた Content ID マッチ | 事後手動対処、週 N 件超なら自動停止判断 |
| マスターLLM のクラウドコスト増 | ADR-0024(月予算アラート)、Ollama 切替の選択肢を維持 |
| Codex OAuth の ToS グレー | 初期は API key 利用、Codex OAuth は実験段階のみ、ブロック時は API key フォールバック |
| 6 楽曲連結時の主観品質劣化 | PoC で実測、NG なら代替構成(同プロンプト + 異 seed 等)に切替 |
| 改善計画 LLM のバグで翌日全滅 | dryrun + 人間レビュー枠(ADR-0006, ADR-0025) |
| 動画ファイルのストレージ膨張 | ADR-0022(fsspec)+ ADR-0025(dryrun 状態別 retention) |
| OAuth トークン漏洩 | ADR-0012(Fernet 暗号化、鍵は別管理) |
| LAN 内 MitM | LAN 信頼前提、必要時に Tailscale/HTTPS 追加(ADR-0013) |
| 単価表の陳腐化 | 環境変数で外出し、年次レビュー(ADR-0024) |

## 10. ADR 一覧

### コア構成・運用

| ID | タイトル | Status |
|----|---------|--------|
| [0001](adr/0001-system-architecture-python-backend-nextjs-frontend.md) | バックエンド = Python、管理UI = Next.js | Accepted |
| [0002](adr/0002-master-llm-cloud-api-with-abstraction-layer.md) | マスターLLM はクラウドAPI 前提、切替層維持 | Accepted |
| [0003](adr/0003-video-format-30min-via-six-track-stitching.md) | 動画フォーマット = 30分(5分×6本連結) | Accepted |
| [0004](adr/0004-posting-volume-staged-from-1-2-per-day.md) | 投稿規模は 1日1〜2本 から段階拡大 | Accepted |
| [0006](adr/0006-cycle-structure-daily-and-weekly.md) | サイクル構造 = 日次 + 週次 の2層 | Accepted |
| [0007](adr/0007-dryrun-mode-as-mvp-requirement.md) | dryrun モードを MVP 必須機能に | Accepted |
| [0025](adr/0025-dryrun-lifecycle-state-based.md) | dryrun ライフサイクル(状態別 retention) | Accepted |

### コンプライアンス・著作権

| ID | タイトル | Status |
|----|---------|--------|
| [0005](adr/0005-content-id-pre-check-with-acoustid.md) | Content ID 事前チェック = AcoustID + Chromaprint | Accepted |
| [0008](adr/0008-llm-responsibility-scope.md) | マスターLLM の責務範囲 | Accepted |
| [0020](adr/0020-ai-disclosure-via-contains-synthetic-media.md) | AI 開示フラグ = `containsSyntheticMedia=true` | Accepted |

### ML・LLM

| ID | タイトル | Status |
|----|---------|--------|
| [0016](adr/0016-thumbnail-sdxl-model-strategy.md) | サムネ生成 = Juggernaut XL v10 + ジャンル別切替 | Accepted |
| [0017](adr/0017-directive-parser-auto-detect.md) | ディレクティブパーサ(自動判別方式) | Accepted |
| [0018](adr/0018-llm-structured-output-pydantic.md) | LLM 構造化出力 = Pydantic + 抽象化層 | Accepted |
| [0019](adr/0019-llm-provider-implementations.md) | LLM Provider 実装(OpenAI / Anthropic / Ollama)| Accepted |
| [0024](adr/0024-llm-cost-and-usage-tracking.md) | LLM コスト・トークン使用量管理 | Accepted |

### バックエンド基盤

| ID | タイトル | Status |
|----|---------|--------|
| [0009](adr/0009-backend-framework-fastapi.md) | バックエンド Web フレームワーク = FastAPI | Accepted |
| [0010](adr/0010-database-postgresql-with-pgvector.md) | DB = PostgreSQL(+ pgvector 拡張余地) | Accepted |
| [0011](adr/0011-scheduler-apscheduler-in-backend-process.md) | スケジューラ = APScheduler(常駐内) | Accepted |
| [0014](adr/0014-python-package-manager-uv.md) | Python パッケージ管理 = uv | Accepted |
| [0022](adr/0022-storage-abstraction-fsspec.md) | ストレージ抽象化 = fsspec | Accepted |

### メディア・動画

| ID | タイトル | Status |
|----|---------|--------|
| [0015](adr/0015-video-visualizer-showwaves-overlay.md) | 動画ビジュアライザ = SDXL + `showwaves` overlay | Accepted |
| [0021](adr/0021-analytics-data-api-and-analytics-api.md) | アナリティクス取得 = Data API + Analytics API | Accepted |

### セキュリティ・認証

| ID | タイトル | Status |
|----|---------|--------|
| [0012](adr/0012-oauth-token-encrypted-in-postgres.md) | OAuth トークン = PostgreSQL に対称鍵暗号化保存 | Accepted |
| [0013](adr/0013-admin-ui-auth-basic.md) | 管理UI 認証 = Basic 認証(LAN 内)| Accepted |

### 運用・観測性

| ID | タイトル | Status |
|----|---------|--------|
| [0023](adr/0023-observability-structured-logging.md) | 観測性 = 構造化ログ(loguru)+ 管理UI 閲覧 | Accepted |
| [0026](adr/0026-backup-local-secondary-disk.md) | バックアップ = ローカルセカンダリディスクのみ | Accepted |
| [0027](adr/0027-testing-strategy-emphasis-on-critical-paths.md) | テスト戦略(critical path 100% + その他 best effort) | Accepted |
| [0028](adr/0028-error-categories-and-handling.md) | エラーカテゴリ 5 分類 | Accepted |
| [0029](adr/0029-monorepo-with-directory-split.md) | モノレポ + シンプルなディレクトリ分割 | Accepted |
| [0030](adr/0030-git-operations.md) | git 運用ポリシー(Private + main+PR + scope 推奨) | Accepted |
| [0031](adr/0031-deploy-procedure.md) | デプロイ手順(ハイブリッド + Makefile + panic-stop) | Accepted |

### 計画 LLM / テンプレ

| ID | タイトル | Status |
|----|---------|--------|
| [0032](adr/0032-improvement-plan-llm-schema.md) | 改善計画 LLM の出力スキーマ(DailyPlan / WeeklyPlan) | Accepted |
| [0033](adr/0033-initial-genres-and-planner-prompt.md) | 初期 6 ジャンル + planner プロンプト構造 | Accepted |
| [0034](adr/0034-default-templates.md) | タイトル / 説明文 / サムネのデフォルトテンプレ | Accepted |

## 11. 次のアクション

### 設計フェーズ(残作業)

1. **DB スキーマ設計**(ER 図 + DDL + Alembic マイグレーション):
   - `videos`, `audio_tracks`, `genres`, `plans`, `posts`, `plan_metric_snapshot`, `usage_log`, `oauth_credentials`, `dryrun_outputs`, `analytics_*`, `comments`, `job_history`, `gpu_jobs`, `app_state`(`scheduler_enabled` flag、 ADR-0031)
   - ADR-0032 の `plans` / `posts` / `plan_metric_snapshot` を含む
2. **プロンプトファイルの実体化**(ADR-0033 の構造に従う):
   - `prompts/planner/system_v1.md`(改善計画 LLM の system prompt)
   - `prompts/planner/few_shot_v1.json`(手書き DailyPlan サンプル 1 件)
   - `prompts/finisher/title_v1.md` / `description_v1.md`(仕上げ LLM 用)
3. **テンプレファイルの実体化**(ADR-0034 の構造):
   - `templates/title/*.yaml` × 6
   - `templates/description/default.yaml` + `_shared/`
   - `templates/thumbnail/*.yaml` × 6 + `_shared/`
   - フォント同梱(SIL OFL ライセンス全 6 種 + Noto Sans JP)
4. **単価表の初期データ**(`LLM_PRICING_JSON` / `model_pricing` テーブル、 ADR-0024)
5. **Makefile 作成**(ADR-0031: `make up` / `make deploy` / `make migrate` / `make panic-stop` 等)
6. **systemd unit 作成**(`ymg-stack.service` / `ymg-gpu-worker.service`)

### 実装フェーズ

7. **ACE-Step PoC**(2〜3日): 5分単発の主観品質 + 6本連結時の音楽的整合性確認
8. AcoustID + Chromaprint プレチェックの Python 実装(`pyacoustid`)
9. Python プロジェクト初期化(uv + FastAPI スケルトン)
10. Next.js プロジェクト初期化(管理UI スケルトン)
11. **GPU worker プロジェクト初期化**(FastAPI、 host 直 + Dockerfile、 ADR-0031)
12. LLMProvider 抽象化層の実装(`openai` + `anthropic` + `ollama` の3 provider、 ADR-0019)
13. ストレージ抽象化層(`fsspec`)実装(ADR-0022)
14. APScheduler ジョブ実装(日次・週次・retention)
15. directive parser 実装(ADR-0017)
16. Pillow サムネオーバーレイ実装(ADR-0034)

### 検証済み(完了)

- YouTube AI 開示 API フィールド = `status.containsSyntheticMedia`(ADR-0020 で調査済み)
- ACE-Step の生成長さ仕様(5分は標準内、最大10分まで)
- SDXL 派生モデルのライセンス(主要モデルは商用利用可)
- 選定フォント 6 種すべて SIL OFL ライセンス(ADR-0034 で確認済み)

### 将来 ADR

17. 1か月運用後、成功条件・撤退条件の **数値目標** を確定する追加 ADR
18. 投稿規模拡大判断ルール(ADR-0004 後継、1か月後)
19. few-shot を「retention 上位 plan の自動選別」に切り替える ADR(ADR-0033 の B3 昇格、 運用 1 ヶ月後)
20. `expected_kpi` の必須化判断 ADR(ADR-0032、 運用 1〜2 ヶ月後)
21. RunPod 等への GPU 切り替え時の運用 ADR(ADR-0031 の準備は完了、 実切替時の詳細)
