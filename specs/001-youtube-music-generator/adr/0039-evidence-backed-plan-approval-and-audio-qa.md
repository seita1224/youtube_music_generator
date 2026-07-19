# ADR-0039: 根拠付き Daily Plan 承認ゲート・仕様 hash・Audio QA

- **ステータス:** Accepted
- **日付:** 2026-07-14
- **決定者:** @seita
- **タグ:** backend / frontend / ml / ops / policy

## 背景

Daily Plan の承認が「生成済みならワンクリック」だと、Analytics 不足や LLM の数値誤引用のまま GPU 実行に進む。また実行時にプロンプトを再合成すると、画面で見た仕様と実際の生成が乖離する。

管理UI の正本は `/plans/[id]` とし、根拠 → LLMによる音楽企画 → トラック生成レシピ → 検証 → 承認 → 生成 → トラック QA を一画面で完結させる必要がある。

## 決定

### (1) Plan 状態機械

`plans.status` の語彙:

```text
generated → compiled → blocked | approved
generated | compiled | blocked → rejected
rejected → superseded (regenerate で新 Plan 生成時)
compiled → approved (validation_pass)
blocked → approved (audited_force のみ)
approved → executing → music_generated | failed
music_generated → qa_complete (全トラック review_state=accepted)
```

| 内部値 | UI ラベル |
|---|---|
| `generated` | 生成済 |
| `compiled` | **仕様確定済** |
| `blocked` | ブロック |
| `approved` | 承認済 |
| `executing` | 実行中 |
| `music_generated` | 音楽生成済 |
| `qa_complete` | QA完了 |
| `rejected` | 却下 |
| `superseded` | 置換済 |
| `failed` | 失敗 |

旧 `completed` はフル日次(動画・投稿)完了用に残すが、音楽専用フローの終端は `qa_complete` とする。一覧フィルタは新語彙を含む。

### (2) Evidence 凍結と検証ゲート

- Plan 生成時に Evidence(`plan_evidence_snapshots`)を凍結する。後から Analytics が増えても変更しない。最新根拠が必要なら理由付き再生成で新 Plan を作り、旧 Plan を `superseded` にする
- Analytics 正本は `plan_metric_snapshot`。LLM 引用は typed `metric_claims`(ADR-0032)
- 検証(`plan_validation_runs`): claim↔snapshot 一致、sample size、target_date、Post 数、genre、desired→final BPM/key、directive、6本・300秒・seed/subtheme 一意性
- **sparse gate:** Analytics 行 0 または sample size < 3 は block
- **通常承認:** validation pass のみ
- **強制承認:** 理由 **min 20 文字** + findings snapshot 付き `audit_log`(`plan_force_approved`)必須。`blocked` から `approved` へ
- **却下:** 理由 **min 4 文字**、`audit_log`(`plan_rejected`)。dryrun 却下とは別語彙(ADR-0025)
- **再生成:** 新 Plan を作り旧を `superseded`

### (3) 生成仕様の確定(compile)と仕様 hash

- UI 操作名: **「生成仕様を確定」**(`POST .../compile`)
- 決定論 compiler が Post position ごとに `music_compilations`(実行用音楽生成仕様 / CompiledMusicBatch)を永続化
- 各トラックは LLM提案層とシステム確定層の二層(ADR-0032)。実行はシステム確定のみ
- 実行時プロンプト(`caption`)は genre / subtheme / instruments / arrangement / texture(非空時) / BPM / key を決定論連結する。`visual_direction` は混ぜない(詳細は ADR-0040)
- 承認時に `plans.active_compilation_hash` を固定。run-now / cron / GPU submit で照合(ADR-0011)
- 承認後の仕様改変は拒否。実行中の再 compile も拒否

### (4) Audio QA と人間レビューの分離

自動 QA(`qa_result`)と人間レビュー(`review_state`)は別語彙:

| フィールド | 値 | UI |
|---|---|---|
| `qa_result` | `pending` / `pass` / `warn` / `fail` | QA PASS / WARN / FAIL / 計測中 |
| `review_state` | `pending` / `accepted` / `rejected` / `regenerating` | 未審査 / 採用 / 却下 / 再生成中 |

初期ポリシー **`audio_qa_v1`**:

- **FAIL:** decode 不可、実測尺が 300±2 秒外、無音率 20% 超
- **WARN:** true peak が -0.1 dBTP 超、integrated LUFS が -24〜-8 外、最終 BPM からの偏差 15% 超、曲間類似度 0.90 超
- FAIL/WARN 表示は「指標名・実測値・閾値・推奨アクション」を必須
- QA FAIL ヘルプ: 「音声ファイルは生成済みだが自動品質基準未達。未採用・未投稿。要レビュー/却下/再生成」
- トラック却下理由 min 4 文字。部分再生成は attempt 履歴を保持し、当該 position のみ新 seed

### (5) API 契約(実装は後続 U)

| Method | Path | 用途 |
|---|---|---|
| GET | `/plans/{id}/workspace` | 根拠・企画・レシピ・検証・QA の正本ペイロード |
| POST | `/plans/{id}/compile` | 生成仕様を確定 → `compiled` |
| POST | `/plans/{id}/validate` | 検証実行 |
| POST | `/plans/{id}/approve` | 通常承認(body に `force` + reason 可) |
| POST | `/plans/{id}/reject` | 却下(reason min 4) |
| POST | `/plans/{id}/regenerate` | 理由付き再生成 |
| GET/PUT | `/plans/feature-gate` | `PLAN_APPROVAL_V2` mode (`off` / `readonly` / `on`) |
| GET/POST | `/posts/{id}/tracks/{position}/qa` 系 | QA 取得・accept/reject/regenerate |

feature gate 名: `PLAN_APPROVAL_V2`(段階移行 U9)。

| mode | workspace / compile / validate | approve・reject・regenerate | run-now / cron |
|---|---|---|---|
| `off` | 可 | 可(ゲート未適用) | 可 |
| `readonly` | 可 | **拒否 (403)** | **拒否 / cron skip** |
| `on` | 可 | 可 | 可 |

- 段階移行は `readonly` で開始(migration `009_plan_approval_v2_gate`)。backfill 後に `on` へ切替え、approve と execute を同時に解禁する
- rollback は mode を `readonly` / `off` に戻すだけ。新テーブル・監査履歴は保持する
- backfill: `generated` → compile+validate(自動承認しない)。`approved` 未実行 → re-validate、fail なら `blocked`。実行済み run / WAV の hash は書き換えない

### (6) UI 用語(正)

| UI用語 | 技術名 |
|---|---|
| LLMによる音楽企画 | LLM music planning |
| トラック生成レシピ | MusicCompilation / track plan |
| 実行用音楽生成仕様 | CompiledMusicBatch |
| 実行時プロンプト | caption |
| 生成仕様を確定 | compile |
| LLM提案 / システム確定 | proposal / system-locked fields |

インフラ語(GPU要求 / caption / compile)をユーザー向け主語にしない。

## 結果

### 良い影響

- Analytics 不足・誤引用のまま実行する経路が閉じる
- 画面で見た仕様と GPU payload が hash で一致する
- 自動 QA と人間判断の用語衝突が解消する

### 悪い影響・トレードオフ

- 状態数・API・テーブルが増え、legacy Plan の backfill が必要
- 強制承認の運用ミスで sparse 根拠を通せる(監査必須で緩和)

### 受容したリスク

- `audio_qa_v1` 閾値は初期値。運用後に version bump(`audio_qa_v2`)で更新する

## 検討した代替案

- **一覧ワンクリック承認の継続:** 根拠確認が形骸化する。不採用
- **実行時に毎回プロンプト再合成:** preview と実生成が乖離する。不採用
- **QA FAIL = 自動却下:** オペレータ判断余地を奪う。自動判定と人間レビューを分離

## 関連

- ADR-0003: 300秒 × 6
- ADR-0006 / ADR-0011: 承認後実行・hash 照合
- ADR-0025: Plan 却下 vs dryrun 却下
- ADR-0032: tracks / metric_claims / 二層フィールド
- ADR-0033: few-shot プレースホルダ・prompt provenance
- contracts/backend-api.yaml / data-model.md / screen-spec.md
