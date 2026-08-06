# YouTube Music Generator Constitution

> このプロジェクトの **譲れない原則** を定義する。 個別の判断は ADR(`../../specs/001-youtube-music-generator/adr/`)に記録するが、 本書の原則と矛盾する ADR は提案できない。 原則の変更には新規 ADR + 本書の改版を伴う。

## Core Principles

### I. Compliance-First (NON-NEGOTIABLE)

YouTube 規約 / 著作権 / AI 開示は **常に最優先**。 効率や速度を理由にこれを後回しにする選択は提案しない。

- 全動画投稿時に `status.containsSyntheticMedia=true` を必須化する(ADR-0020)。 投稿前バリデーション層が未設定を検知したら投稿を停止する
- 投稿前に AcoustID + Chromaprint で全 6 トラックの指紋プレチェックを行う(ADR-0005)。 NG なら該当トラックを再生成、 連続 3 回ヒットでジャンル一時停止
- 停止手段(CLI + Slack から到達できるシステム状態 `publish_paused` / `stopped`)を常備する(ADR-0044)。 各工程の開始直前と公開 API 呼び出し直前で必ず参照し、 ここを通らない実行経路を作らない
- compliance 事象では**人の操作を待たず自動で**停止する(ADR-0044): `publish_paused` + 該当動画 private 化 + 自動運転レベルの L0 降格 + 通知
- 検出されないことを「OK」とみなさない。 検出不能なグレーは事前回避する

**根拠**: 動機 C(マネタイズ)+ AI 動画規制感応度。 不可逆な BAN や法的問題は回復コストが運用全体を上回る。

### II. Test-First on Critical Paths (NON-NEGOTIABLE)

**Critical path は TDD 必須 + 100% カバレッジ**。 それ以外は best effort 60-70%(ADR-0027)。

Critical path:

- AcoustID + Chromaprint プレチェック
- `containsSyntheticMedia` 必須化バリデーション
- OAuth トークン Fernet 暗号化 / 復号
- LLM 出力 Pydantic スキーマ検証
- directive parser
- compliance 自動停止経路(`publish_paused` への遷移 + 該当動画 private 化、 ADR-0044)
- 公開遷移の不変条件(ADR-0045 INV-2: AI 開示 ∧ 指紋 CLEAR ∧ 承認記録 ∧ 公開ブロック無し ∧ システム `running`)
- 成果物の無効化規則(ADR-0046: 依存 DAG に従う破棄と承認失効)

Critical path 外: PoC コードはテスト免除可、 frontend UI は best effort、 集計クエリはスナップショットで十分。

**根拠**: 1 人運用 + 自動投稿で「壊れたことを誰も検知しない」状況を避ける。 ただし全部 80% にすると運用負担で続かない。

### III. Staged Autonomy with Reversibility (NON-NEGOTIABLE)

**投稿という不可逆アクションは、 人間の一手か、 実績で解錠された自律と取り消し手段のいずれかで守る**(ADR-0042、 ADR-0043、 ADR-0044)。

- 既定は **L0(公開前に必ず人が承認する)**。 自動運転レベルを上げるには実績条件の充足を要する(ADR-0043)。 宣言だけで上げられる経路を作らない
- 自動公開(L1 / L2)を許すのは、 **取り消し手段(YouTube private 化)と死活監視(daily heartbeat)が機能している場合に限る**(ADR-0043、 ADR-0044)
- **降格はいつでも無条件・即時**。 停止・降格に摩擦を作らない
- 人の判断は公開ゲート + 運営判断の 2 種のみとし、 形骸化する承認を増やさない(ADR-0042)
- 却下理由は次回の企画 LLM の入力に活用する

**根拠**: AI 自動生成の暴走 / LLM の hallucination による事故を構造的に止められる状態を保つ。 ただし形骸化した承認は「止められる」を提供しない。 実効性のある安全装置は、 実績に基づく自律の解錠・即時の降格・確実な取り消し手段の 3 点である。

### IV. Provider / Resource Abstraction

**外部依存は契約で隔離し、 切り替えコストを構造的に下げる**。

- LLM Provider 抽象化(ADR-0018、 ADR-0019): OpenAI / Anthropic / Ollama を interface 越しに切替可能
- ストレージ抽象化(ADR-0022): fsspec で `file:// / s3:// / gs://` を切替可能
- GPU worker 分離(ADR-0031): HTTP API + fsspec で疎結合、 RunPod 等へ Dockerfile を介して移行可能
- Anthropic SDK のサブスク利用は禁止(2026-02-19 公式)。 利用検出時は起動拒否

**根拠**: 1 機械 1 GPU 構成は SPOF。 1 ベンダー固定は単一障害点 + 単価上昇リスク。 切替できない設計は将来コストを払い続ける。

### V. Structured Errors with Explicit Categories

すべてのエラーは **5 カテゴリ** で分類処理する(ADR-0028)。

| カテゴリ | 例 | 対応 |
|---|---|---|
| `transient` | API timeout, GPU OOM | 自動リトライ(max 2) |
| `recoverable` | LLM validation fail, genre 辞書外 | パラメータ調整 + リトライ |
| `fatal` | OAuth invalid, disk full | scheduler 停止 + Slack 通知 |
| `compliance` | `containsSyntheticMedia` 未設定、 Content ID マッチ | **投稿停止 + 該当動画 private 化** |
| `quality` | visual_direction 短すぎ、 BPM 範囲外 | デフォルトテンプレに fallback |

「とりあえずリトライ」「黙って握りつぶす」は禁止。

**根拠**: 自動運用で「何が起きて、 何を続けるべきか」を機械が判断できるようにする。 人間が見るのは Slack 通知の集約のみ。

### VI. Structured Observability

- loguru で構造化 JSON ログ(ADR-0023)、 `video_id` / `genre` / `step` で検索可能
- 全 LLM 呼び出しを `usage_log` に provider / model / tokens / cost / context 込みで記録(ADR-0024)
- 改善計画 LLM の入力 metrics を `plan_metric_snapshot` に生で保存(ADR-0032、 再現性確保)
- プロンプトはバージョン番号付き(`prompts/planner/system_v1.md` 等)で `plans.llm_prompt_version` に記録

**根拠**: 何ヶ月後の自分(または LLM agent)が「なぜこの判断をしたか」を遡れること。 自動運用は観測なしには改善できない。

### VII. ADR-Driven Decisions

すべての非自明な判断は ADR(`../../specs/001-youtube-music-generator/adr/NNNN-*.md`)に記録する。 ADR テンプレ(`../../specs/001-youtube-music-generator/adr/0000-template.md`)に従い、 ステータス / 背景 / 決定 / 結果(良い影響 / 悪い影響 / 受容したリスク) / 検討した代替案 / 関連 を埋める。

- 推測ではなく確認済みの事実をベースに書く。 未確認は明示する
- ADR と requirements.md が矛盾したら **ADR を優先**
- 1 ADR = 1 判断、 複数の論点を 1 ADR にまとめない

**根拠**: 1 人運用で「過去の自分の考え」を未来の自分が辿れる手段は ADR だけ。 git log は変更履歴であって判断履歴ではない。

## Architectural Constraints

### Layering

- バックエンド = Python 3.13 / FastAPI / uv (ADR-0001, ADR-0009, ADR-0014)
- フロントエンド = Next.js (App Router) + shadcn/ui + tanstack/query + recharts
- DB = PostgreSQL + pgvector 拡張余地 (ADR-0010)
- スケジューラ = APScheduler in backend process (ADR-0011)
- GPU worker = host 直 + systemd、 Dockerfile 用意でクラウド移行可 (ADR-0031)

### Repository

- モノレポ + シンプルなディレクトリ分割 `backend/` + `frontend/` + `gpu_worker/` + `infra/`(ドキュメントは `specs/001-.../` 配下) (ADR-0029)
- GitHub Private、 main + feature ブランチ + PR self-merge、 release tag なし (ADR-0030)
- コミット規約: `<type>(<scope>): <description>` 形式、 scope 推奨

### Storage / Backup

- ストレージは fsspec 抽象化 (ADR-0022)。 保持期間は ADR-0049: 音源 / サムネ / メタデータ / ログ / プロンプトは永続、 公開済み動画は 90 日、 やり直しの旧世代は 7 日、 終端枠(見送り / 取り下げ / 却下)の成果物は 30 日
- バックアップはローカルセカンダリディスクのみ、 オフサイトなし (ADR-0026)。 対象は永続保持のもののみ。 Fernet 鍵はバックアップ対象外

### Security

- OAuth トークンは PostgreSQL に Fernet 対称鍵暗号化 (ADR-0012)
- Fernet 鍵は `.env`、 `.gitignore` で除外、 リポジトリには `.env.example` のみ
- 管理 UI はセッション認証 BFF + backend Basic (ADR-0013)。 LAN 内でも認証は必須とする(ADR-0050)
- 停止操作(`publish_paused` / `stopped`)は CLI + Slack のみに置く。 管理 UI からは状態の表示のみ (ADR-0044)

## Development Workflow

### Before Any Implementation

1. **既存パターン確認**: 同種コードがあれば踏襲、 新規発明より優先
2. **ADR 確認**: 当該領域の ADR が存在すれば従う、 矛盾するなら新 ADR を提案
3. **TDD 開始**: Critical path なら test-first 必須、 それ以外は判断

### Commit Hygiene

- secrets を commit しない(`.env` 等)、 検出時は履歴 filter + 鍵ローテ
- `<type>(<scope>): <description>` で scope を推奨(`feat(backend):` `docs(adr):` 等)
- main 直 push 禁止、 必ず PR 経由(CI gate 通過)

### Deploy Discipline

- デプロイは Makefile 経由(`make deploy`)、 自動デプロイは行わない (ADR-0031)
- マシン reboot 後の scheduler は**自動再開**する。 停止は `system_state`(`publish_paused` / `stopped`)でのみ表現し、 再起動で暗黙に解除されない (ADR-0044)
- `make migrate` 前に自動 `pg_dump`、 down migration は書かない
- ロールバックは `git revert` + `make deploy`、 release tag は使わない

## Quality Gates

PR merge 前に以下が green になること:

- `make lint` (backend ruff + frontend eslint + 型チェック)
- `make test` (critical path 100% + その他 best effort)
- `make build` (docker compose build / gpu_worker Dockerfile build)
- secrets スキャン(`.env` 系の混入なし)

## Governance

- 本憲法は ADR-0001 〜 0050 を礎に成立する
- 原則(I 〜 VII)を覆す提案は新規 ADR + 本書改版を伴う。 ADR 単独で原則を上書きできない
- アーキテクチャ制約 / 開発ワークフロー / 品質ゲートは ADR 経由で進化可能、 個別 ADR で更新する
- 本書と個別 ADR が矛盾した場合、 原則(NON-NEGOTIABLE 含む)が上、 アーキテクチャ制約以下は個別 ADR が上

### 改版履歴

- **2.0.0** (2026-07-25): 枠中心の再設計 (ADR-0041〜0050) に伴う改版。 Principle III を「dryrun-First」から「Staged Autonomy with Reversibility」へ全面改訂(自動公開 L1 / L2 の許容と、 その条件の明文化)。 Principle I の停止手段を `make panic-stop` から 2 段階のシステム状態 + compliance 自動停止へ置換。 Principle II の critical path に公開遷移の不変条件と成果物無効化規則を追加。 Storage / Deploy / Security の各制約を更新

**Version**: 2.0.0 | **Ratified**: 2026-05-26 | **Last Amended**: 2026-07-25
