# Data Model: 枠中心モデルへの再設計

**Input**: ADR-0041 / 0045 / 0046 / 0049 / 0050、spec.md Key Entities

新スキーマは新 DB + 新 Alembic ベースラインで開始する(research.md R-6)。旧スキーマからの引き継ぎは R-4 の 5 テーブルのみ。

## ER 構造(概念図)

```text
slot_patterns 1─* slot_pattern_rows ─(物化)→ slots ←─ slot_exceptions
                                              │
                    slots 1─* artifacts(plan / track×6 / inspection / mix /
                                        chapters / thumbnail / title /
                                        description / video / package)
                    slots 1─* approval_records
                    slots 1─* slot_timeline
                    slots 0..1─1 videos ─* analytics_daily
                                        └─* comments
genres 1─* slots / videos / genre_weekly_allocations
metrics_snapshots(企画 LLM 入力の生コピー)
system_state(1 行) / autonomy_state(1 行)
audit_log / usage_log / model_pricing / oauth_credentials / llm_provider_secrets(継承)
```

## Enums(PostgreSQL native ENUM)

| Enum | 値 | 出典 |
| --- | --- | --- |
| `slot_state` | `empty` `blocked` `in_production` `awaiting_approval` `approved` `published` `rejected_pending` `failed` `skipped` `withdrawn` | ADR-0045 (1) |
| `production_stage` | `planning` `generating` `inspecting` `packaging` | ADR-0045 (1) |
| `publish_block_reason` | `system_paused` `quota_exhausted`(列は nullable、`null`=公開可能) | ADR-0045 (4) |
| `slot_origin` | `pattern`(物化) `adhoc`(単発枠) | ADR-0041 (2) |
| `genre_mode` | `fixed` `auto` `experiment` | ADR-0041 (3) |
| `genre_role` | `main` `extension` `experiment` | 001 継承 |
| `exception_kind` | `skip` `genre_override` `adhoc_add` | ADR-0041 (2) |
| `system_state_kind` | `running` `publish_paused` `stopped` | ADR-0044 (1) |
| `autonomy_level` | `l0` `l1` `l2` | ADR-0043 (1) |
| `artifact_kind` | `plan` `track` `inspection` `mix` `chapters` `thumbnail` `title` `description` `video` `package` | ADR-0046 (1) |
| `artifact_status` | `valid` `invalidated` `file_deleted` | ADR-0046 / 0049 |
| `approval_actor` | `human` `system` | ADR-0043 (1) |
| `approval_action` | `approve` `reject` | ADR-0042 (4) |
| `error_category` | `transient` `recoverable` `fatal` `compliance` `quality` | ADR-0028 継承 |
| `llm_provider` / `llm_auth_mode` | 001 継承 | ADR-0019 |

`slot_timeline.event_type` は TEXT(開いた語彙)。旧 `job_history.trigger` の TEXT+CHECK 方式(ADR-0036)は job_history 作り直しに伴い失効済み。

## Tables

### `slot_patterns` — 公開枠パターンのバージョン (ADR-0041)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| version | INT | UNIQUE, NOT NULL | 単調増加。最大 version が現行 |
| note | TEXT | | 変更メモ |
| created_at | TIMESTAMPTZ | NOT NULL | |

パターン編集 = 新 version の行 + rows 一式を作成(イミュータブル)。物化済み枠は `slots.pattern_version` で由来を保持。

### `slot_pattern_rows` — 枠行(曜日 × 時刻 × ジャンル指定)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| pattern_id | BIGINT | FK slot_patterns, NOT NULL | |
| weekday | SMALLINT | CHECK 0..6, NOT NULL | 月=0 |
| publish_time_jst | TIME | NOT NULL | |
| genre_mode | genre_mode | NOT NULL | |
| genre_id | BIGINT | FK genres, `fixed` / `experiment` 時 NOT NULL(CHECK) | `auto` 時 NULL |
| position | INT | NOT NULL | 表示順 |

UNIQUE(pattern_id, weekday, publish_time_jst)。

### `slot_exceptions` — 単発の例外 (ADR-0041)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| target_date | DATE | NOT NULL | JST |
| kind | exception_kind | NOT NULL | |
| target_time_jst | TIME | `skip` / `genre_override` 時 NOT NULL | 対象枠行の時刻 |
| publish_time_jst | TIME | `adhoc_add` 時 NOT NULL | 追加枠の時刻 |
| genre_mode | genre_mode | `genre_override` / `adhoc_add` 時 NOT NULL | |
| genre_id | BIGINT | FK genres, nullable | |
| reuse_from_slot_id | BIGINT | FK slots, nullable | 見送り成果物の再利用元(ADR-0045 (3)) |
| note | TEXT | | |
| created_at | TIMESTAMPTZ | NOT NULL | |

### `slots` — 枠(集約ルート、ADR-0041 / 0045)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | 物化時に採番、不変 |
| publish_at | TIMESTAMPTZ | NOT NULL | 公開予定(JST 意味論) |
| origin | slot_origin | NOT NULL | pattern / adhoc |
| pattern_version | INT | origin=pattern 時 NOT NULL | 物化元 |
| exception_id | BIGINT | FK slot_exceptions, nullable | 適用された例外 |
| genre_mode | genre_mode | NOT NULL | |
| genre_id | BIGINT | FK genres, NOT NULL | `auto` は物化時に確定 |
| genre_reason | TEXT | `auto` 時 NOT NULL | おまかせ選択理由 + 判断数値(FR-074) |
| state | slot_state | NOT NULL, default `empty` | |
| current_stage | production_stage | nullable | `in_production` 時のみ非 NULL(CHECK) |
| publish_block_reason | publish_block_reason | nullable | `approved` 時のみ非 NULL 可(CHECK) |
| approval_deadline_at | TIMESTAMPTZ | NOT NULL | = publish_at + 7 日(物化時に確定) |
| failed_category | error_category | nullable | `failed` 時の分類 |
| prompt_overrides | JSONB | default '{}' | 枠限定のプロンプト版(ADR-0046 (4)) |
| materialized_at | TIMESTAMPTZ | NOT NULL | |
| state_changed_at | TIMESTAMPTZ | NOT NULL | 遷移時刻(タイムラインにも記録) |

物化の決定論(INV-6)のため、物化 UPSERT キーは `(publish_at, origin, pattern_version, exception_id)`。`empty` / `blocked` / `in_production` の枠はパターン変更・例外で**レコード削除**されうる(遷移ではない)。

### `artifacts` — 枠配下の成果物(版・来歴つき、ADR-0046)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| slot_id | BIGINT | FK slots, NOT NULL | |
| kind | artifact_kind | NOT NULL | |
| track_index | SMALLINT | CHECK 1..6、kind=track 時のみ非 NULL(CHECK) | |
| version | INT | NOT NULL | 枠 × kind × track_index 内で単調増加 |
| status | artifact_status | NOT NULL, default `valid` | |
| uri | TEXT | nullable | fsspec URI(ファイル実体を持つ kind) |
| content | JSONB | nullable | plan / title / description / chapters / inspection の中身 |
| provenance | JSONB | NOT NULL | prompt_version / model / 上流成果物の (kind, track_index, version) |
| created_at | TIMESTAMPTZ | NOT NULL | |
| invalidated_at | TIMESTAMPTZ | nullable | 無効化時刻(7 日保持の起点) |
| file_deleted_at | TIMESTAMPTZ | nullable | 保持期間超過削除(メタは残す、FR-082) |

UNIQUE(slot_id, kind, track_index, version)。「現行版」= 各 (slot_id, kind, track_index) の `status='valid'` 最大 version。

**依存 DAG(コード上の単一定義 `artifacts/dag.py` と一致させる)**:

```text
plan → track[i], thumbnail, title, description
track[i] → inspection, mix, chapters
mix, thumbnail → video
video, title, description, inspection → package
```

無効化 = 再生成対象の後続(推移的閉包)の `valid` 行を `invalidated` に更新。

### `approval_records` — 公開ゲートの判断 (ADR-0042 / 0043)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| slot_id | BIGINT | FK slots, NOT NULL | |
| actor | approval_actor | NOT NULL | human / system(L1・L2) |
| action | approval_action | NOT NULL | |
| reason | TEXT | action=reject 時 NOT NULL(CHECK) | 却下理由 → 企画 LLM 入力 |
| autonomy_level | autonomy_level | NOT NULL | 判断時のレベル |
| package_artifact_id | BIGINT | FK artifacts, NOT NULL | どの package に対する判断か |
| invalidated_at | TIMESTAMPTZ | nullable | 承認失効(T17、INV-5) |
| created_at | TIMESTAMPTZ | NOT NULL | |

INV-2 の「承認記録が存在する」= 対象枠の現行 `package` を指す `action=approve` かつ `invalidated_at IS NULL` の行が存在すること。

### `system_state` — システム状態(1 行、ADR-0044)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | SMALLINT | PK, CHECK (id = 1) | シングルトン |
| state | system_state_kind | NOT NULL, default `running` | |
| changed_via | TEXT | NOT NULL | cli / slack / auto(compliance) |
| changed_by | TEXT | NOT NULL | 操作者 or 検知元 |
| reason | TEXT | | |
| updated_at | TIMESTAMPTZ | NOT NULL | |

再起動で暗黙に変わらない(FR-046)。参照点は StageRunner(工程開始直前)と Publisher(公開 API 直前)の 2 箇所のみ。

### `autonomy_state` — 自動運転レベル(1 行、ADR-0043)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | SMALLINT | PK, CHECK (id = 1) | シングルトン |
| level | autonomy_level | NOT NULL, default `l0` | |
| level_since | TIMESTAMPTZ | NOT NULL | L1→L2 の 30 日判定起点 |
| changed_by | TEXT | NOT NULL | |
| change_reason | TEXT | | 昇格判定数値 / 降格理由 / auto-demotion |
| updated_at | TIMESTAMPTZ | NOT NULL | |

昇格条件のカウント(直近 10 枠の無修正承認、取り下げ 0、compliance 0)はこのテーブルに持たず、approval_records / slot_timeline / audit_log から**毎回導出**する(カウンタの二重管理を避ける。判定に使った数値は画面と change_reason に出す)。

### `genres` — ジャンルポートフォリオ(001 継承 + role 運用)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| name | TEXT | UNIQUE, NOT NULL | 英語名(プロンプト用) |
| display_name_ja | TEXT | NOT NULL | |
| role | genre_role | NOT NULL | 昇格 / 撤退は運営判断(FR-025) |
| target_share | NUMERIC(4,3) | CHECK 0..1 | 目標配分 |
| bpm_min / bpm_max | SMALLINT | | 001 継承 |
| prompt_hints | JSONB | | 001 継承 |
| is_paused | BOOLEAN | NOT NULL default false | 指紋 3 連続ヒット等の一時停止 |
| paused_reason | TEXT | | |
| enabled | BOOLEAN | NOT NULL default true | 撤退で false |
| created_at / updated_at | TIMESTAMPTZ | | |

### `genre_weekly_allocations` — 配分の週次確定値 (ADR-0047)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| week_start | DATE | PK 複合 | 月曜 |
| genre_id | BIGINT | PK 複合, FK genres | |
| share | NUMERIC(4,3) | NOT NULL | |
| reason | TEXT | NOT NULL | 自動縮小等の判断数値と理由(FR-074) |
| created_at | TIMESTAMPTZ | | |

### `videos` — 公開済み動画(新規 + legacy 移行)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| slot_id | BIGINT | FK slots, UNIQUE, nullable | legacy 行は NULL |
| youtube_video_id | TEXT | UNIQUE, NOT NULL | |
| title | TEXT | NOT NULL | |
| genre_id | BIGINT | FK genres, NOT NULL | |
| contains_synthetic_media | BOOLEAN | NOT NULL, CHECK (= TRUE) | 憲法 I |
| privacy_status | TEXT | NOT NULL | public / unlisted / private |
| published_at | TIMESTAMPTZ | NOT NULL | |
| withdrawn_at | TIMESTAMPTZ | nullable | 取り下げ / compliance private 化 |
| source | TEXT | NOT NULL | slot / legacy |

### `analytics_daily` / `comments` — 001 継承(移行対象)

001 の列構成を継承。`analytics_daily` に `is_final BOOLEAN`(公開 7 日以上経過の確定値か、ADR-0047 (3))を追加。`comments` に `sentiment JSONB`(要約結果、ADR-0048)を追加。

### `metrics_snapshots` — 企画 LLM 入力の生コピー(旧 plan_metric_snapshot の読み替え、ADR-0032 方針継承)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| slot_id | BIGINT | FK slots, NOT NULL | どの枠の企画に使ったか |
| payload | JSONB | NOT NULL | 実績サマリ + コメント要約 + 却下理由 |
| prompt_version | TEXT | NOT NULL | |
| created_at | TIMESTAMPTZ | NOT NULL | |

### `slot_timeline` — 枠タイムライン(永続、ADR-0046 (5) / 0049)

| 列 | 型 | 制約 | 意味 |
| --- | --- | --- | --- |
| id | BIGSERIAL | PK | |
| slot_id | BIGINT | FK slots, nullable | NULL = システム全体の出来事(削除ジョブ実績等) |
| event_type | TEXT | NOT NULL | state_transition / rerun / retention_cleanup / heartbeat … |
| actor | TEXT | NOT NULL | human / system / scheduler |
| payload | JSONB | NOT NULL | 遷移(from/to)、やり直し(対象成果物・理由)、削除(件数・容量) |
| created_at | TIMESTAMPTZ | NOT NULL | |

### 継承テーブル(構成変更なし or 軽微)

- `audit_log`(運営判断・レベル変更・承認の記録。001 継承)
- `usage_log`(LLM 呼び出し。context に `slot_id` / `stage` を持たせる。ADR-0024)
- `model_pricing` / `oauth_credentials` / `llm_provider_secrets`(001 継承、R-4 で移行)

プロンプトはテーブル化しない(001 のファイルベース版管理 `prompts/<area>/<name>_v<N>.md` を継承。枠限定オーバーライドは `slots.prompt_overrides`)。

## 状態機械(ADR-0045 が正本)

遷移 T1〜T18・不変条件 INV-1〜INV-6 は ADR-0045 の表に従う。実装上の対応:

| 規則 | 実装位置 | 検証 |
| --- | --- | --- |
| 遷移ガード(T1〜T18 以外を拒否) | `domain/slots/state_machine.py`(唯一の遷移入口) | critical #7 + hypothesis |
| INV-2(公開の唯一の入口) | `domain/publish/publisher.py` | critical #7(100%) |
| INV-5(無効化成果物を持つ枠の格下げ) | `domain/artifacts/invalidation.py` → state_machine T17 | critical #8(100%) |
| INV-6(物化の決定論) | `domain/slots/materializer.py`(純関数 + UPSERT) | hypothesis property |
| 承認期限(T12 / T14 / T16) | `domain/slots/deadlines.py`(日次 + 毎時ジョブ) | integration + freezegun |

## Validation Rules

| ルール | DB | アプリ層 |
| --- | --- | --- |
| `videos.contains_synthetic_media = TRUE` | CHECK | 公開前バリデータ(critical #2) |
| 却下に理由必須 | CHECK (action='reject' → reason IS NOT NULL) | API 422 |
| `current_stage` は `in_production` のみ | CHECK | state_machine |
| `publish_block_reason` は `approved` のみ | CHECK | state_machine |
| `track_index` は kind=track のみ | CHECK | artifacts store |
| system_state / autonomy_state 1 行 | CHECK (id=1) | — |
| pattern 行の genre_id 必須(fixed / experiment) | CHECK | API |
| 物化の一意性 | UNIQUE(publish_at, origin, pattern_version, exception_id) | materializer |
| おまかせ理由必須 | — | materializer(genre_mode=auto → genre_reason) |
| 1 日公開予定 > `PUBLISH_WARN_PER_DAY` で警告 | — | パターン保存 API(保存は許可、FR-015) |

## Alembic マイグレーション戦略(R-6)

- 新チェーン `0001_slot_baseline`(全 ENUM + 全テーブル + seed: genres 6 件 / system_state running / autonomy_state l0 / model_pricing)
- 旧 DB は凍結アーカイブ。`ymg migrate-legacy` が読み取り専用接続で R-4 の 5 テーブルを移行
- down migration は書かない(憲法 Deploy Discipline)。`make migrate` 前の自動 pg_dump を継承

## インデックス戦略

- `slots(publish_at)`, `slots(state)`, `slots(state, approval_deadline_at)`(期限ジョブ), `slots(publish_at) WHERE state='approved'`(公開ジョブ / EDF)
- `artifacts(slot_id, kind, track_index) WHERE status='valid'`, `artifacts(invalidated_at) WHERE status='invalidated'`(7 日削除), `artifacts(file_deleted_at)`
- `approval_records(slot_id, created_at)`, `slot_timeline(slot_id, created_at)`, `slot_timeline(event_type, created_at)`
- `analytics_daily(video_id, date)` UNIQUE, `videos(youtube_video_id)` UNIQUE, `comments(video_id, published_at)`

## Retention / Storage(ADR-0049 の写像)

| データ | 保存先 | 寿命 | 実装 |
| --- | --- | --- | --- |
| track / mix 音源、thumbnail | ファイル(fsspec)+ artifacts メタ | 永続 | バックアップ対象 |
| plan / title / description / chapters / inspection | artifacts.content(DB) | 永続 | バックアップ対象 |
| audit_log / slot_timeline / prompts 全版 | DB / ファイル | 永続 | バックアップ対象 |
| package(公開済み最終 mp4) | ファイル | 公開確認後 90 日 | `retention/cleaner.py` 日次 |
| 無効化された旧世代成果物 | ファイル | invalidated_at + 7 日 | 同上 |
| 終端枠(skipped / withdrawn / rejected_pending)の成果物 | ファイル | 終端到達 + 30 日 | 同上 |

削除はファイルのみ(`file_deleted_at` を記録、メタは残す)。削除件数・解放容量は slot_timeline に記録(FR-083)。
