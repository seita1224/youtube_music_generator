# Feature Specification: YouTube 音楽投稿自動化システム

**Feature Branch**: `001-youtube-music-generator`

**Created**: 2026-05-26

**Status**: Accepted(ADR-0001〜0034 反映済み)

**Input**: requirements.md + ./adr/0001〜0035 を統合した正本仕様。 矛盾時は ADR を優先。

## Clarifications

### Session 2026-05-26

- Q: MVP → 投稿モード移行の OK 判定基準は? → A: 包括 checklist 6 項目(dryrun 3 本連続 / AcoustID 全 clear / unlisted 1 本 + ラベル目視 / panic-stop 予行 / OAuth refresh / Slack 5 カテゴリ動作)
- Q: Slack 通知のチャネル分離方針は? → A: 単一 channel に統合、 カテゴリ名 prefix で識別(`[FATAL]` / `[COMPLIANCE]` / `[TRANSIENT]` / `[RECOVERABLE]` / `[QUALITY]`)
- Q: スコープ外の範囲は? → A: 複数チャンネル運用 / Shorts 生成 / ライブ配信 / 他プラットフォーム(TikTok 等) / モバイルアプリ / 楽曲単体配信(SoundCloud 等) / コメント自動返信を Out-of-Scope。 **動画長尺バリエーション(15 / 60 / 90 min など)はスコープ内**(初期 30 分、 PoC 後に拡張)
- Q: データ保持 / 長期削除ポリシーは? → A: MVP では現状仕様(投稿動画 30 日 / dryrun 7 日 / job_history 90 日 / その他永続)で固定。 集約 / rollup / コメントテキスト削除等は **追加開発要件としてスコープ外**、 ディスク容量問題が顕在化した時点で別 ADR で設計
- Q: 新ジャンル追加の正式ワークフローは? → A: 半自動判定。 experiment_slot で 4-6 本投入(2-4 週間)→ 主力ジャンル平均 retention の **80% 以上で採用推奨 / 60% 未満で削除推奨**(間は継続)→ 管理 UI で人間承認 → `genres.role` を `experiment` → `extension` or `main` に昇格 / 削除

## User Scenarios & Testing

### User Story 1 — 日次サイクルで自動投稿が回ること(P1)

毎日、 改善計画 LLM が当日の DailyPlan を生成し、 ジャンルに従って ACE-Step + SDXL + ffmpeg で 30 分動画を生成し、 AcoustID 事前チェック + AI 開示フラグ設定を経て YouTube に投稿する。 失敗時は Slack 通知のみ。

**Why this priority**: これがチャネルの本体価値。 これが回らないと他の機能の意味がない。

**Independent Test**: 1 ジャンルでテンプレ手動設定 + dryrun = OFF + 1 本だけ投稿 → YouTube 上で動画が公開され、 説明文に AI 開示が入っていて、 30 分尺で再生できることを確認。

**Acceptance Scenarios**:

1. **Given** 当日の DailyPlan が `posts=1` で承認済み、 **When** 日次スケジューラが発火、 **Then** 30 分動画が YouTube に投稿され、 `containsSyntheticMedia=true` で公開される
2. **Given** AcoustID 事前チェックで 1 トラックが NG、 **When** 該当トラックのみ再生成、 **Then** 残り 5 トラックは保持され、 リトライ後に通常通り投稿される
3. **Given** YouTube 投稿時に `containsSyntheticMedia` 未設定、 **When** 投稿前バリデーション、 **Then** 投稿停止 + Slack 通知 + `compliance` エラーとして audit log 記録
4. **Given** 改善計画 LLM が辞書外ジャンルを返す、 **When** Pydantic validation、 **Then** リトライ後失敗で前回 plan を再利用 + rationale に明記

---

### User Story 2 — dryrun レビューワークフロー(P1)

新ジャンル投入時や運用開始初期、 投稿前にローカルで動画を確認したい。 dryrun ON で生成だけ実行、 管理 UI で承認 / 否認、 否認理由を改善計画 LLM の入力に活用する。

**Why this priority**: 動機 C(マネタイズ)+ コンプラ感応度で、 投稿前の人間確認は MVP 必須(ADR-0007)。

**Independent Test**: 管理 UI で dryrun = ON、 1 サイクル実行、 出力動画一覧画面で再生 → 承認ボタン → YouTube に投稿される。 否認ボタン → DB に否認理由保存 + 動画削除。

**Acceptance Scenarios**:

1. **Given** dryrun = ON、 **When** 日次サイクル実行、 **Then** 動画が生成され `dryrun_outputs.state="pending"` に登録、 YouTube 投稿はスキップ
2. **Given** dryrun_outputs に pending が 7 日間滞留、 **When** retention ジョブ、 **Then** state="auto_expired" + 動画ファイル削除
3. **Given** dryrun_outputs.state="approved"、 **When** 投稿実行、 **Then** YouTube に投稿 + state="posted" + 動画ファイル削除予約

---

### User Story 3 — 週次サイクルで改善計画が更新されること(P1)

週 1 回、 過去 7 日の analytics(retention / views / 視聴時間 / トラフィックソース)を取得し、 改善計画 LLM が WeeklyPlan を生成、 管理 UI で人間承認後に翌週の日次サイクルに反映される。

**Why this priority**: 動機 B(YouTube 観察)の核。 毎週改善できないと「データ蓄積」の意味が薄れる。

**Independent Test**: 過去 1 週間分の analytics を 1 ジャンル分セット → 週次ジョブ手動実行 → WeeklyPlan が生成され rationale に retention 数値が引用されていることを確認 → 承認 → 翌日 daily に反映。

**Acceptance Scenarios**:

1. **Given** 過去 7 日に 3 ジャンル合計 10 本投稿済み、 **When** 週次ジョブ発火、 **Then** WeeklyPlan が生成 + `genre_distribution` 合計 1.0 + `referenced_metrics.window_days=7`
2. **Given** WeeklyPlan が dryrun_outputs と同じ承認フロー、 **When** 承認、 **Then** 翌日以降の DailyPlan の avoid_genres / experiment_slots に反映される

---

### User Story 4 — コンプラ事故時の緊急停止(P1)

誤投稿 / Content ID マッチ / `containsSyntheticMedia` 漏れに気づいた瞬間、 投稿停止 + 該当動画を private 化したい。

**Why this priority**: マネタイズ前提 + AI 動画規制感応度で、 不可逆事故への即応手段が必要。

**Independent Test**: `make panic-stop` を実行 → scheduler 停止 + 直近 24h の動画一覧表示 + 一括 private 化が走ることを確認。

**Acceptance Scenarios**:

1. **Given** scheduler 稼働中、 **When** `make panic-stop`、 **Then** `app_state.scheduler_enabled=false` + 直近 24h 動画リスト表示 + 確認後 privacyStatus=private 一括更新 + audit log 記録

---

### User Story 5 — マスター LLM プロバイダ切替(P2)

OpenAI API key / Codex OAuth / Anthropic API key / Ollama を切り替えたい。 Codex OAuth はサブスクリプション利用可否を実験するため。

**Why this priority**: コスト最適化と検証の柔軟性が運用継続性に直結。

**Independent Test**: `LLM_PROVIDER=anthropic LLM_AUTH_MODE=api_key` で再起動 → 同じ DailyPlan 入力で出力差分を比較。

**Acceptance Scenarios**:

1. **Given** `LLM_PROVIDER=ollama`、 **When** 改善計画リクエスト、 **Then** ローカル model で応答、 usage_log に provider="ollama" 記録
2. **Given** Anthropic SDK のサブスク利用は禁止、 **When** `LLM_AUTH_MODE=subscription`、 **Then** 起動時バリデーションで拒否

---

### User Story 6 — 管理 UI でのオーケストレーション可視化(P2)

各ステップの進捗・実行履歴・エラー状態を管理 UI で確認できる。 ジャンル単位で並行可視。

**Why this priority**: 1 人運用で何が起きているかを把握できないと改善が回らない。

**Independent Test**: 管理 UI で 1 サイクルを SSE 経由でリアルタイム監視 → 各 step が green / red で遷移するのが見える。

**Acceptance Scenarios**:

1. **Given** scheduler が job 実行中、 **When** 管理 UI を開く、 **Then** SSE で各 step の進捗が秒単位で更新
2. **Given** ジャンル A が失敗 / B が継続、 **When** Slack 通知、 **Then** A は赤、 B は緑で UI 表示

---

### User Story 7 — クラウド GPU(RunPod 等)への移行(P3)

ローカル GPU が故障 / 増強したいとき、 backend のコードを変えずに GPU worker をクラウドに移行できる。

**Why this priority**: 将来オプション、 ただし設計時に担保しないと後から実現困難。

**Independent Test**: GPU worker の Dockerfile を `docker build` + RunPod Pod にデプロイ → backend の `GPU_WORKER_BASE_URL` を変更 → 同じ動画生成が走ることを確認。

**Acceptance Scenarios**:

1. **Given** GPU worker を RunPod Pod に展開、 **When** backend の env を `GPU_WORKER_BASE_URL=https://<pod>.proxy.runpod.net` に変更、 **Then** backend と frontend のコード変更なしで動画生成が継続

---

### Edge Cases

- **AcoustID API ダウン**:`transient` エラーカテゴリでリトライ、 max 2 回後は scheduler 停止 + Slack(`fatal` に昇格)
- **YouTube quota 超過**(daily 10,000 units): 投稿停止 + 翌日に持ち越し(`recoverable`)
- **GPU OOM during ACE-Step**: `torch.cuda.empty_cache()` 後リトライ、 失敗なら該当 1 本を skip して他は継続
- **改善計画 LLM が DailyPlan ではなく WeeklyPlan を返す**: cycle discriminator 検証で reject、 リトライ
- **directive parser が `{{var}}` を未定義変数として検出**: テンプレ側のバグ、 build CI でテンプレ検証する
- **管理 UI から scheduler を ON にしたまま `make down`**:再起動時 scheduler は OFF 起動(ADR-0031)、 明示再 ON が必要
- **PostgreSQL dump 失敗(disk full 等)**: `fatal` カテゴリ、 マイグレーション中断
- **dryrun_outputs が 7 日待っても誰も触らず大量滞留**: retention ジョブが auto_expired にしてディスク回収
- **同一ジャンル × 同一サブテーマで連日生成 → 視聴者が同じ動画と誤認**: WeeklyPlan の avoid_genres と experiment_slots でジャンル分散制御
- **AcoustID で生成楽曲がたまたまヒット**: 該当トラックのみ再生成、 連続 3 回ヒットならジャンル一時停止
- **YouTube 投稿後の Content ID マッチ通知**: webhook なし、 週次 analytics 取得時に状態確認、 `compliance` 扱いで該当動画を private 化判断

## Requirements

### Functional Requirements

#### コア投稿フロー

- **FR-001**: System MUST 日次スケジューラで毎日 1 〜 2 本(ADR-0004)の動画を生成し、 dryrun=OFF なら YouTube に投稿する
- **FR-002**: System MUST 1 本の動画を 5 分 × 6 トラック連結で 30 分尺に組み立てる(ADR-0003、 `ffmpeg acrossfade 3〜5秒`)
- **FR-003**: System MUST ACE-Step 1.5(Apache 2.0)で音楽を生成する
- **FR-004**: System MUST SDXL 派生モデル(デフォルト Juggernaut XL v10、 ADR-0016)でサムネ背景を生成する
- **FR-005**: System MUST `ffmpeg showwaves` overlay + SDXL 背景で動画を合成する(ADR-0015)
- **FR-006**: System MUST 全動画投稿時に `status.containsSyntheticMedia=true` を設定する(ADR-0020)
- **FR-007**: System MUST 投稿前バリデーション層で `containsSyntheticMedia` が true でない場合は投稿を停止する

#### コンプラ事前チェック

- **FR-010**: System MUST 投稿前に AcoustID + Chromaprint で 6 トラックすべての指紋プレチェックを行う(ADR-0005)
- **FR-011**: System MUST AcoustID NG トラックを単独で再生成する(他トラックは保持)
- **FR-012**: System MUST AcoustID 連続 3 回ヒット時に該当ジャンルを一時停止する

#### LLM プロバイダ

- **FR-020**: System MUST 改善計画 LLM の Provider を OpenAI / Anthropic / Ollama で切替可能とする(ADR-0019)
- **FR-021**: System MUST OpenAI 利用時に api_key と Codex OAuth の 2 モードを切替可能とする
- **FR-022**: System MUST Anthropic SDK のサブスクリプション利用を起動時に拒否する(2026-02-19 公式禁止)
- **FR-023**: System MUST LLM 出力を Pydantic v2 で構造化検証する(ADR-0018、 ADR-0032)
- **FR-024**: System MUST Pydantic validation 失敗時に最大 2 回まで temperature を下げてリトライする(ADR-0028 `recoverable`)
- **FR-025**: System MUST すべての LLM 呼び出しを `usage_log` に provider / model / tokens / cost / context 込みで記録する(ADR-0024)
- **FR-026**: System MUST 月予算 50% / 80% / 100% で Slack 通知を発行する
- **FR-027**: System MUST System prompt を prompt caching が効く形で構成する(ADR-0033)

#### 改善計画 LLM

- **FR-030**: System MUST 日次サイクル開始前に DailyPlan を Pydantic スキーマで生成する(ADR-0032)
- **FR-031**: System MUST 週次サイクルで WeeklyPlan を生成する
- **FR-032**: System MUST `genre` を `genres` テーブルとの辞書照合で validate する
- **FR-033**: System MUST DailyPlan の `posts` を 1 〜 2 件に制限する(ADR-0004)
- **FR-034**: System MUST WeeklyPlan の `genre_distribution` の合計が 1.0(±0.01)であることを validate する
- **FR-035**: System MUST 改善計画 LLM の入力 metrics を `plan_metric_snapshot` に保存し再現性を確保する
- **FR-036**: System MUST 改善計画 LLM のプロンプトをバージョン番号付きで管理し、 `plans.llm_prompt_version` に記録する
- **FR-037**: System MUST `experiment_slot` で投入した新ジャンルが 4 本以上投稿 + 14 日以上経過した時点で、 平均 retention を主力ジャンル(`genres.role='main'`)平均と比較し、 `>=80%` で「採用推奨」、 `<60%` で「削除推奨」、 中間は継続として WeeklyPlan の rationale に明記する
- **FR-038**: System MUST 採用推奨 / 削除推奨ジャンルの最終承認を管理 UI 経由で受け、 承認時に `genres.role` を遷移(`experiment` → `extension`/`main` または disabled)させ、 audit_log に記録する

#### Directive parser と仕上げ LLM

- **FR-040**: System MUST directive 形式(`{{var}}` と `{{自由文}}`)を自動判別する(ADR-0017)
- **FR-041**: System MUST `{{var}}` を context から埋め込み、 `{{自由文}}` を仕上げ LLM で生成する
- **FR-042**: System MUST 仕上げ LLM 呼び出しに Haiku 級安価 model を使用可能とする

#### テンプレ

- **FR-050**: System MUST ジャンルごとのタイトル / 説明文 / サムネテンプレを `templates/` に保有する(ADR-0034)
- **FR-051**: System MUST タイトルを `{英語ジャンル} {duration}min | {日本語サブタイト} {絵文字1個まで}` 形式で生成する
- **FR-052**: System MUST 説明文を「冒頭 200-300 字バイリンガル描写 + チャプター + AI 開示固定文 + ハッシュタグ 3 個」構造で生成する
- **FR-053**: System MUST AI 開示文を二か国語固定文(LLM 非生成)で挿入する
- **FR-054**: System MUST サムネ画像を「SDXL 背景 + Pillow オーバーレイ」で合成する
- **FR-055**: System MUST サムネのフォント / 配色をジャンル別 YAML テンプレで切り替える

#### dryrun

- **FR-060**: System MUST dryrun フラグで「投稿以外を本番経路で実行」する(ADR-0007、 ADR-0025)
- **FR-061**: System MUST dryrun_outputs に state(pending / approved / rejected / auto_expired / posted)を持たせる
- **FR-062**: System MUST dryrun_outputs.state="pending" を 7 日後に "auto_expired" + 動画ファイル削除する
- **FR-063**: System MUST 否認理由を改善計画 LLM の入力に活用する

#### スケジューラ

- **FR-070**: System MUST APScheduler を backend プロセス内で実行する(ADR-0011)
- **FR-071**: System MUST `app_state.scheduler_enabled` フラグで scheduler の有効化を制御する
- **FR-072**: System MUST マシン reboot 後は `scheduler_enabled=false` で起動する(ADR-0031)
- **FR-073**: System MUST 管理 UI から scheduler ON / OFF できる

#### バックエンド基盤

- **FR-080**: System MUST バックエンドを Python + FastAPI(uv 管理)で実装する(ADR-0001、 ADR-0009、 ADR-0014)
- **FR-081**: System MUST フロントエンドを Next.js (App Router) で実装する
- **FR-082**: System MUST DB を PostgreSQL + pgvector 拡張余地で構成する(ADR-0010)
- **FR-083**: System MUST OAuth2 トークンを Fernet 対称鍵暗号化で `oauth_credentials` テーブルに保存する(ADR-0012)
- **FR-084**: System MUST Fernet 鍵を `.env` で管理し、 `.gitignore` で除外する
- **FR-085**: System MUST 管理 UI を Basic 認証で保護する(ADR-0013、 LAN 内前提)
- **FR-086**: System MUST OpenAPI スキーマ生成 + `openapi-typescript` で frontend の型を生成する
- **FR-087**: System MUST ストレージを fsspec 抽象化で扱う(ADR-0022)

#### GPU worker

- **FR-090**: System MUST ACE-Step + SDXL 実行を独立プロセス(GPU worker)に分離する(ADR-0031)
- **FR-091**: System MUST backend と GPU worker を HTTP API + fsspec ストレージで疎結合する
- **FR-092**: System MUST GPU worker の `GPU_WORKER_BASE_URL` を env で切替可能とする
- **FR-093**: System MUST GPU worker の Dockerfile を repo に保有し、 CI で `docker build` を通す
- **FR-094**: System MUST GPU worker を host 直 + systemd で運用しつつ、 RunPod 等のコンテナ環境に移行可能な契約を維持する

#### YouTube 統合

- **FR-100**: System MUST `youtube.upload` + `youtube` + `yt-analytics.readonly` の OAuth スコープで動作する(ADR-0021)
- **FR-101**: System MUST 週次サイクルで YouTube Data API + Analytics API から retention / views / 視聴時間 / トラフィックソースを取得する
- **FR-102**: System MUST `make panic-stop` で直近 24 時間の投稿動画を一括 private 化できる(ADR-0031)

#### 観測性 / エラー

- **FR-110**: System MUST loguru で構造化 JSON ログを出力する(ADR-0023)
- **FR-111**: System MUST エラーを 5 カテゴリ(transient / recoverable / fatal / compliance / quality)で分類処理する(ADR-0028)
- **FR-112**: System MUST `compliance` カテゴリ発生時に投稿停止 + 該当動画 private 化を実行する
- **FR-113**: System MUST `fatal` カテゴリ発生時に scheduler 停止 + Slack 通知する
- **FR-114**: System MUST Slack 通知を単一 webhook(`SLACK_WEBHOOK_URL`)に集約し、 メッセージ冒頭にカテゴリ prefix(`[FATAL]` / `[COMPLIANCE]` / `[TRANSIENT]` / `[RECOVERABLE]` / `[QUALITY]`)を付与する
- **FR-115**: System MUST `fatal` および `compliance` カテゴリ通知に `<!channel>` mention を含め、 即応必要性を視覚的に強調する

#### バックアップ / デプロイ

- **FR-120**: System MUST 毎日 cron で PostgreSQL `pg_dump` + 動画 / 音楽メタデータをローカルセカンダリディスクにコピーする(ADR-0026)
- **FR-121**: System MUST `make migrate` 実行前に自動で `pg_dump` を取る(ADR-0031)
- **FR-122**: System MUST Fernet 鍵をバックアップ対象から除外する(ダンプ流出時の防衛線)
- **FR-123**: System MUST デプロイを Makefile 経由の手動実行とする(`make deploy` 等)

### Key Entities

ADR-0032 + requirements.md §11.1 の DB スキーマと整合。 詳細は data-model.md。

- **plans**: DailyPlan / WeeklyPlan の保存。 `cycle` discriminator、 `payload` jsonb、 `llm_model` / `llm_cost_usd` / `llm_prompt_version` を持つ
- **posts**: DailyPlan.posts の個別レコード。 `music_job_id` / `image_job_id` / `youtube_video_id` / `final_title` / `final_description` / `posted_at` / `retention_24h`
- **plan_metric_snapshot**: 改善計画 LLM への入力 analytics の生コピー(再現性確保)
- **genres**: 辞書照合用ジャンルマスタ。 初期 6 件(ADR-0033)
- **videos**: 投稿済み動画メタ(`youtube_video_id`, `genre`, `posted_at`, `privacy_status`)
- **audio_tracks**: 6 トラックの個別メタ(`bpm`, `key`, `acoustid_status`, `fingerprint`)
- **gpu_jobs**: GPU worker への job キュー(`job_type`, `status`, `output_uri`, `vram_peak_mb`)
- **dryrun_outputs**: dryrun 状態(`state`: pending/approved/rejected/auto_expired/posted)
- **oauth_credentials**: Fernet 暗号化 OAuth トークン
- **usage_log**: 全 LLM 呼び出し記録(`provider`, `model`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `context_id`)
- **model_pricing**: LLM 単価表(`provider`, `model`, `input_per_1m`, `output_per_1m`)
- **job_history**: 日次 / 週次 / dryrun retention ジョブの実行履歴
- **app_state**: グローバル設定(`scheduler_enabled` bool 等)
- **comments**: YouTube コメント取得結果(週次分析対象)
- **analytics_***: 動画 × 日次の指標(retention / views / impressions / ctr)

## Success Criteria

### Measurable Outcomes(暫定 — 1か月運用後に数値確定、 ADR で更新予定)

- **SC-001**: 日次サイクルが連続 7 日エラー無し完走(`fatal` カテゴリ発生ゼロ)
- **SC-002**: AcoustID 事前チェックで Content ID 事後マッチ率を週 1 件未満に抑える
- **SC-003**: `containsSyntheticMedia=true` 設定漏れがゼロ(投稿前バリデーション 100% 通過)
- **SC-004**: 改善計画 LLM の Pydantic validation 失敗率 5% 未満
- **SC-005**: dryrun レビュー UI で 1 動画の確認が 60 秒以内に完了(再生 + 承認ボタン押下)
- **SC-006**: マシン reboot から `make up` 完了 + healthcheck 全 green までが 60 秒以内
- **SC-007**: ロールバック(`git revert` + `make deploy`)が 5 分以内に完了
- **SC-008**: `make panic-stop` 実行から該当動画 private 化完了まで 60 秒以内(24 本想定)
- **SC-009**: LLM 月予算アラート(50% / 80% / 100%)が Slack に届く
- **SC-010**: 投稿動画の retention(平均)を 1 か月運用後に確定する数値目標として再定義する

### 将来確定(運用 1 か月後)

- 投稿動画の平均 retention 数値目標
- 撤退条件の数値化(BAN 1 件で停止 / Content ID マッチ週 N 件超で規模縮小)
- 月間収益目標(動機 C のマネタイズ閾値)

## Out-of-Scope

以下は本仕様の範囲外。 将来要望が出た場合は別 feature(`002-*` 等)として独立した spec を立てる:

- **複数チャンネル運用**(YouTube アカウント / channel ID は単一)
- **YouTube Shorts 自動生成**(60 秒以下の縦動画フォーマット)
- **ライブ配信**(YouTube Live, 24/7 lo-fi stream 形式を含む)
- **他プラットフォーム配信**(TikTok / Instagram Reels / X / Twitch / SoundCloud 等)
- **モバイルアプリ**(管理 UI は LAN 内 Web のみ)
- **楽曲単体配信**(Spotify / Apple Music / SoundCloud 等の音楽配信プラットフォーム)
- **コメント自動返信 / 自動 like**(コメント取得は分析目的のみ、 返信は手動)
- **複数ユーザー運用 / RBAC**(単一管理者前提、 Basic 認証)
- **多言語 UI**(管理 UI は日本語のみ。 投稿動画はバイリンガル英 + 日でユーザー向けに対応)

### In-Scope だが MVP 初期は固定値で運用

以下は仕様上スコープ内、 ただし MVP 初期は固定値運用、 PoC 後に拡張:

- **動画長尺バリエーション**(15 / 60 / 90 min など): 初期は 30 分(ADR-0003、 5 分 × 6 トラック連結)固定、 ACE-Step + ffmpeg + 改善計画 LLM 周辺が長尺対応できるよう **抽象化して実装** する。 別長尺の投入時は別 ADR で運用ルールを更新

### 追加開発要件(MVP 完了後の拡張、 別 ADR で設計)

以下は将来必要になった時点で別 feature / 別 ADR として設計する。 MVP の DB スキーマや保存方針は **現状仕様(全永続 + 投稿動画 30 日 / dryrun 7 日 / job_history 90 日)で固定** する:

- **長期データ保持 / rollup 戦略**: `analytics_daily` の月次集約、 `comments` の本文削除(sentiment / topic_tags のみ保持)、 `usage_log` の月次圧縮等。 ディスク容量問題が顕在化した時点で別 ADR で設計

## Rollout Policy(ADR-0035)

MVP は全機能(YouTube uploader / OAuth / analytics / panic-stop 含む)を実装するが、 初期は **dryrun=ON 既定** で運用し、 投稿開始は人間判断で段階移行する。

- `.env` 既定: `DRYRUN_DEFAULT=true` / `app_state.dryrun_enabled=true`
- 投稿モードへの切替(`dryrun_enabled: true → false`)は audit_log に必須記録
- **最初の本投稿は手動オペレーション 1 本**(scheduler に任せず、 管理 UI から `POST /posts/{id}/retry` で発火)
- YouTube Studio で公開状態 / `containsSyntheticMedia` ラベル / サムネ / 説明文 / 30 分尺を目視確認してから定常運用に移行
- 投稿モード切替後 **最初の 1 週間は `max_daily_posts=1` 強制**(ADR-0004 の上限 2 本にしない)、 異常なしで 2 本 / 日へ昇格
- **MVP → 投稿モード移行 checklist(6 項目すべて green で `dryrun_enabled=false` に切替可)**:
  1. dryrun モードで 3 本連続の動画生成が成功(エラー無し、 healthcheck 全 green)
  2. AcoustID + Chromaprint プレチェックで全 18 トラック(3 本 × 6)が `clear` ステータス
  3. unlisted privacy で 1 本投稿し、 YouTube Studio で `containsSyntheticMedia` ラベル / サムネ / 説明文 / 30 分尺を目視確認
  4. `make panic-stop` 予行を実施し、 当該 unlisted 動画が `privacyStatus=private` に変更される(audit_log 記録あり)
  5. YouTube OAuth refresh が週次 cron 経由で 1 回成功(token 期限切れ運用検証)
  6. Slack 通知が 5 エラーカテゴリ(transient / recoverable / fatal / compliance / quality)すべてで疎通確認済み(疎通テスト用エンドポイント or 意図的な発火)
- OAuth トークン期限切れ防止のため、 投稿モード切替前でも週次 analytics 取得は走らせる(空でも refresh は起きる)

## Assumptions

- **実行環境**: 単一マシン(RTX 3090 / Ubuntu)、 LAN 内、 自宅電源
- **運用者**: 1 人(複数ユーザー対応は将来課題)
- **インターネット**: 安定接続前提、 短時間切断は `transient` で吸収
- **YouTube API quota**: 日次 10,000 units の標準枠で十分(1 本投稿 ≈ 1,600 units、 1 日 1〜2 本)
- **AcoustID API**: 無料枠で十分(月 30〜60 本 × 6 トラック = 月 180〜360 リクエスト)
- **ジャンル拡張**: 初期 6 ジャンル、 新ジャンル追加は dryrun 経由の承認フロー
- **OpenAI Codex OAuth**: ToS グレーゾーンと認識、 初期は API key 利用、 OAuth は dryrun / 個人実験範囲で評価
- **Anthropic API**: subscription 利用は禁止(2026-02-19 公式)、 API key のみ
- **公開設定**: GitHub Private repo、 ポートフォリオ価値より競争優位の囲い込み優先(動機 C)
- **バックアップ全損リスク**: オフサイトバックアップなし、 ローカルセカンダリディスクのみ、 ディスク同時死は受容(ADR-0026)
- **GPU 切替可能性**: RunPod / Lambda Labs 等のコンテナ GPU への移行を構造的に担保、 実切替時の詳細は別 ADR
- **動画フォーマット**: 30 分 = 5 分 × 6 トラック前提、 PoC で品質確認後に他長尺バリエーション検討

## ADR References

主要 ADR は requirements.md §10 参照。 個別 ADR: `./adr/0001-0034`。

Plan 詳細は `plan.md` 参照、 DB スキーマは `data-model.md`、 API 契約は `contracts/`、 セットアップ手順は `quickstart.md`。
