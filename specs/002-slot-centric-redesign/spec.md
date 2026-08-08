# Feature Specification: 枠中心モデルへの再設計(Slot-Centric Redesign)

**Feature Branch**: `cursor/002-slot-centric-redesign-bb9d`

**Created**: 2026-08-07

**Status**: Draft

**Input**: ADR-0041〜0050(`../001-youtube-music-generator/adr/`)+ 憲法 v2.0.0 + 画面モック(`../001-youtube-music-generator/mockups/redesign-2026-07/`)。矛盾時は ADR を優先。

> 本 feature は 001(YouTube 音楽投稿自動化システム)のオーケストレーション層を、集約ルートを Plan / dryrun から**公開枠(Slot)**へ移して作り直す再設計である(ADR-0050)。GPU・外部 API まわりの工程実装は 001 から移植する。ADR は引き続き `specs/001-youtube-music-generator/adr/` に置く(憲法 VII)。

## User Scenarios & Testing *(mandatory)*

### User Story 1 — 編成表で枠を組むと制作が自動で回ること(P1)

ユーザーは編成表(週間カレンダー)で公開枠パターン(曜日 × 時刻 JST × ジャンル指定の枠行リスト)を編集する。毎日 03:00 JST にパターン + 例外から翌日分の枠が物化され、各枠は自分の制作ライン(企画 → 音楽生成 → 検査 → パッケージング → 公開待ち → 公開 / 見送り)を自動で流れる。

**Why this priority**: 枠 = 集約ルート(ADR-0041)が本再設計の本体。これが無いと他のすべての機能に載る土台が無い。

**Independent Test**: パターンに枠行 1 本(固定ジャンル)を登録 → 物化ジョブ実行 → 枠が `empty` で生成され ID が採番される → 制作ラインが `in_production`(`current_stage` が planning → generating → inspecting → packaging と進む)→ パッケージング完了で `awaiting_approval`(L0)に達することを確認。

**Acceptance Scenarios**:

1. **Given** 枠行「月曜 07:00 Lo-Fi」を含むパターン、**When** 03:00 JST の物化ジョブが走る、**Then** 翌日分の枠が決定論的に生成され、枠 ID が採番され、以後パターンを編集しても既存枠の ID は変わらない
2. **Given** 制作着手済み(`in_production`)の枠がある状態、**When** パターンの該当枠行を変更・削除する、**Then** 着手済み枠と過去枠は変更されず、`empty` の将来枠にのみ反映される
3. **Given** 「この日だけ休む / ジャンルを変える / 臨時に 1 本足す」、**When** 枠個別の例外を登録する、**Then** パターンは変更されず、次回物化に例外が反映される
4. **Given** ジャンル指定「おまかせ」の枠、**When** 物化される、**Then** role が主力 / 拡張のジャンルのみから ADR-0047 の基準で選択され、選択理由(判断に使った数値付き)が枠詳細に表示される
5. **Given** 同日に複数枠が物化済み、**When** GPU がジョブを取り出す、**Then** 公開予定時刻の早い順(EDF、同時刻は物化順)で直列実行される
6. **Given** 指定ジャンルが一時停止中またはシステムが `stopped`、**When** 制作開始時刻に達する、**Then** 枠は `blocked` になり、前提解消後に `in_production` へ進む

---

### User Story 2 — 公開ゲート 1 点で承認 / 却下できること(P1)

L0(既定)では、パッケージング完了後に枠が `awaiting_approval` になる。ユーザーは枠詳細でプレビュー(動画・タイトル・説明文・チャプター)と検査サマリ(AI 開示・指紋・音量・形式検証)を確認し、承認または却下(理由必須)する。人の判断はこの公開ゲートと低頻度の運営判断の 2 種だけである。

**Why this priority**: 承認過多の解消(ADR-0042)が再設計の動機そのもの。US1 と合わせて「1 本公開する」最小経路が閉じる。

**Independent Test**: `awaiting_approval` の枠を 1 つ用意 → 枠詳細で承認 → `approved` になり公開予定時刻に自動公開される。別の枠で却下(理由入力)→ `rejected_pending` になり、理由が次回企画 LLM の入力に含まれることを確認。

**Acceptance Scenarios**:

1. **Given** `awaiting_approval` の枠、**When** 承認する、**Then** `approved` へ遷移し、公開予定時刻の到来で自動公開される(`published`)
2. **Given** `awaiting_approval` の枠、**When** 理由を添えて却下する、**Then** `rejected_pending` へ遷移し、却下理由が次回の企画 LLM への入力になる
3. **Given** 公開予定時刻を過ぎた `awaiting_approval` の枠、**When** 承認期限(公開予定時刻 + 7 日)内に承認する、**Then** その時点で即時公開される(遅延公開)
4. **Given** 承認期限を過ぎた `awaiting_approval` / `rejected_pending` / `approved` の枠、**When** 期限判定ジョブが走る、**Then** 自動で `skipped`(終端)になる
5. **Given** 検査で指紋一致が出たトラック、**When** 検査工程が完了する、**Then** 該当トラックのみ自動再生成され(人の合否判定は無い)、3 回連続一致でジャンル一時停止 + 通知
6. **Given** `containsSyntheticMedia` 未設定・指紋未 CLEAR・承認記録無し・公開ブロック有り・システム非 `running` のいずれか、**When** 公開遷移を試みる、**Then** 公開されない(INV-2)

---

### User Story 3 — 止めたいときに止まり、事故時は勝手に止まること(P1)

停止は 2 段階のシステム状態(`publish_paused` = 公開だけ停止 / `stopped` = 全ワーカー停止)として永続化され、CLI と Slack から操作できる。compliance 事象では人の操作を待たず自動で `publish_paused` + 該当動画 private 化 + L0 降格 + 通知が走る。毎朝 07:30 JST の Slack サマリが唯一の死活シグナルになる。

**Why this priority**: 憲法 I(Compliance-First)の停止手段常備。自動公開(US4)の前提となる安全装置。

**Independent Test**: CLI で `pause-publishing` → `approved` 枠が `publish_block_reason=system_paused` で待機し公開されない → `resume` → 遅延公開される。指紋一致の事後検知をシミュレート → 自動で `publish_paused` + 該当動画 private 化 + L0 降格 + Slack 通知を確認。

**Acceptance Scenarios**:

1. **Given** システム `running`、**When** CLI または Slack で `pause-publishing` する、**Then** 企画・生成・検査・パッケージングは継続し、公開だけが止まる
2. **Given** システム `stopped`、**When** 工程開始時刻に達する、**Then** 進行中の工程は完走し、新しい工程は開始されない
3. **Given** 公開後に指紋一致が判明、**When** compliance 検知が走る、**Then** 人の操作なしで `publish_paused` + 該当動画 private 化 + L0 降格 + Slack 通知
4. **Given** マシン再起動、**When** backend が起動する、**Then** スケジューラは自動再開し、`system_state` は再起動前の値を維持する(暗黙解除されない)
5. **Given** 毎朝 07:30 JST、**When** heartbeat ジョブが走る、**Then** 当日の公開予定枠・要確認件数・システム状態が Slack に届く
6. **Given** admin UI の設定画面、**When** システム状態を見る、**Then** 現在状態と停止コマンドが表示されるが、停止操作は UI からはできない

---

### User Story 4 — 実績で解錠される自動運転レベル(P2)

自動運転レベルはシステム全体で 1 つ(L0 監督 / L1 事後確認 / L2 全自動)。昇格は実績条件(L0→L1: 直近 10 枠連続無修正承認 + 検査自動不合格 0、L1→L2: 30 日以上 + 取り下げ 0 + compliance 0)を満たしたときだけ選択可能になる。降格は無条件・即時。L1 では公開通知から取り下げ(= YouTube private 化)ができる。

**Why this priority**: 運用の自動化という本来目的の到達点。ただし L0 だけでも US1〜3 で運用は成立する。

**Independent Test**: 実績が条件未達の状態で設定画面を開く → L1 が選択不可で「あと N 枠」が表示される。条件を満たす実績を投入 → L1 が選択可能になる。L1 で公開された枠を Slack から取り下げ → 動画が private 化され枠が `withdrawn`(終端)になる。

**Acceptance Scenarios**:

1. **Given** 既定状態、**When** システムを初期化する、**Then** レベルは L0 で、承認無しに公開される経路が無い
2. **Given** L1、**When** パッケージング完了 + 検査合格、**Then** `awaiting_approval` を経由せずシステムが承認記録を残して `approved` へ進み、監査ログで人 / システムの承認が区別できる
3. **Given** 昇格条件未達、**When** 設定画面でレベルを変更しようとする、**Then** 未達レベルは選択できず、未達の理由と現在の達成状況(判定に使った数値)が表示される
4. **Given** L2 で運用中、**When** 降格操作をする、**Then** 条件なしで即時 L0 に下がり、監査ログに記録される
5. **Given** L1 で公開された枠、**When** admin UI または Slack から取り下げる、**Then** YouTube 動画が private 化(削除はしない)され、枠は `withdrawn` になる。公開通知の「あと N 時間」は SLA 表示であり、期限後も取り下げ操作自体は可能
6. **Given** compliance 自動停止が発動、**When** 停止処理が完了する、**Then** レベルは L0 へ自動降格している

---

### User Story 5 — 工程単位のやり直しと安全な部分再生成(P2)

ユーザーは枠詳細から「この工程からやり直す」「トラック 03 だけ再生成」「文言だけ直す」を実行できる。無効化は成果物依存 DAG から導出され、トラックを差し替えたら検査と動画合成が必ず走る。`approved` の枠でやり直すと承認は失効し、再度公開ゲートを通る。

**Why this priority**: 承認集約(US2)の受容条件。「企画が的外れでも GPU 消費後にやり直せる」ことが前提(ADR-0042)。

**Independent Test**: パッケージング済みの枠でトラック 1 本を再生成 → 他 5 トラック・サムネ・文言は保持され、検査 / mix / chapters / video / package が無効化・再生成される。`approved` の枠で文言やり直し → `in_production` に戻り承認が失効することを確認。

**Acceptance Scenarios**:

1. **Given** パッケージング完了済みの枠、**When** トラック 03 だけ再生成する、**Then** 他 5 トラック / plan / thumbnail / title / description は保持され、inspection / mix / chapters / video / package が無効化される(未検査の音源が公開される経路が無い)
2. **Given** 同じ枠、**When** タイトル・説明文だけやり直す、**Then** video / thumbnail は保持され package のみ無効化される
3. **Given** `approved` の枠、**When** 任意のやり直しを行う、**Then** 枠は `in_production` に戻り(T17)、承認は失効し、再パッケージング後にあらためて公開ゲートを通る
4. **Given** `published` の枠、**When** やり直しを探す、**Then** 提供されない(取り下げ + 単発枠の追加で代替)
5. **Given** プロンプトを修正、**When** 「この枠だけに適用して再実行」する、**Then** 枠にプロンプト版のオーバーライドが記録され、成果物にどの版で作られたかが残る
6. **Given** やり直しを実行、**When** 枠タイムラインを見る、**Then** 誰が / いつ / どの成果物を / なぜ が記録され、旧世代成果物は 7 日保持後に削除される

---

### User Story 6 — 目的関数に基づく分析と企画フィードバック(P2)

分析画面は「週次総視聴時間の最大化 + ジャンル別維持率が直近実績中央値の 80% を下回らない」という目的関数を軸に KPI を表示する。おまかせ枠のジャンル選択・配分の自動縮小・昇格 / 撤退の推奨はすべてこの基準で行われ、判断に使った数値と理由が画面に出る。視聴者コメントは読み取り専用で取得・要約し、企画 LLM の定性的な補助入力にする。

**Why this priority**: 自動判断の根拠。無くても L0 の手動運用は回るが、「おまかせ」と配分自動化はこれに依存する。

**Independent Test**: 公開 7 日以上の実績データを投入 → おまかせ枠の物化でガードレール充足ジャンルのうち期待総視聴時間最大のものが選ばれ、理由が表示される。7 日未満の数値が画面で「暫定」表示されること、サンプル 3 本未満のジャンルが統計判断から除外されることを確認。

**Acceptance Scenarios**:

1. **Given** 維持率がガードレールを 2 週連続で下回ったジャンル、**When** 週次の配分更新が走る、**Then** 次週の配分が自動縮小される(撤退はしない)
2. **Given** 実験ジャンルの実績、**When** 昇格 / 撤退の推奨条件を満たす、**Then** 推奨が算出されて人に提示され、自動では実行されない(運営判断。監査ログに記録)
3. **Given** 公開から 7 日未満の動画、**When** 分析画面に数値を表示する、**Then** 「暫定」と明示され、自動判断には使われない
4. **Given** 公開済み動画にコメントが付いた、**When** 実績スナップショットを生成する、**Then** コメントのセンチメント要約が含まれ企画 LLM の入力になる。返信・削除・モデレーションの UI は存在しない

---

### User Story 7 — 旧システムからの移行(P3)

旧実装(001)から、公開済み動画の実績データ(video_id・公開日時・ジャンル・Analytics)のみを 1 回きりのスクリプトで移行する。Plan / dryrun / job_history / 未公開の生成物は引き継がない。工程実装(ACE-Step 呼び出し・指紋検査・音量検査・ffmpeg 合成・サムネ生成・YouTube クライアント・鍵管理・LLM 抽象・認証・directive parser・テンプレ)はコード移植して再利用する。

**Why this priority**: 目的関数(US6)が過去実績を必要とするため必要だが、新規運用開始だけなら無くても成立する。

**Independent Test**: 旧 DB のダンプを用意 → 移行スクリプト実行 → 公開済み動画の実績のみが新スキーマに入り、旧概念(Plan / dryrun)のデータが存在しないことを確認。

**Acceptance Scenarios**:

1. **Given** 旧 DB に公開済み動画と未公開 dryrun 出力が混在、**When** 移行スクリプトを実行する、**Then** 公開済み動画の実績のみが移行され、恒久的な互換層は作られない
2. **Given** 移植対象の工程実装、**When** 新オーケストレーションから呼び出す、**Then** 旧オーケストレーション(Plan / dryrun / Post)への依存なしに動作する

---

### Edge Cases

- 承認期限(公開予定 + 7 日)を過ぎた枠は `awaiting_approval` / `rejected_pending` / `approved` のどこにいても自動で `skipped`。`skipped` から復帰する遷移は無く、救済は保持期間(30 日)内の成果物再利用 + 単発枠の追加で行う
- YouTube quota 枯渇時、`approved` 枠は `publish_block_reason=quota_exhausted` で待機し、リセット後に古い枠から順に遅延公開される。ただし承認期限超過なら `skipped`
- 1 日の公開予定が 6 本(quota 見積り)を超えるパターンは保存できるが警告が出る
- 締切に間に合わない枠は `failed` にせず制作を続け、完成後に遅延公開として扱う
- `empty` / `blocked` / `in_production` の枠はパターン変更・例外で削除されうる(状態遷移ではなくレコード削除)。制作済み成果物は保持期間に従う
- L0→L1 の連続無修正承認カウントは、却下または工程やり直しの発生でリセットされる
- 自動リトライ上限超過 / `fatal` エラーの枠は `failed` になり、やり直し(→ `in_production`)か諦め(→ `skipped`)を人が選ぶ
- Slack 障害中は停止操作は CLI のみになる(受容済みリスク)
- `published` の枠は `withdrawn` への遷移しか持たない準終端。公開済み動画の差し替え経路は存在しない

## Requirements *(mandatory)*

### Functional Requirements

#### 枠・公開枠パターン・物化(ADR-0041)

- **FR-001**: System MUST 公開枠パターン(枠行 = 曜日 × 時刻 JST × ジャンル指定 のリスト)を編成表から編集可能とし、バージョン管理する
- **FR-002**: System MUST パターン変更を `empty` 状態(制作未着手)の将来枠にのみ反映し、制作着手済みの枠と過去枠を変更しない
- **FR-003**: System MUST 単発の変更(休む / ジャンル変更 / 臨時追加)をパターンと独立した枠個別の例外として保持する
- **FR-004**: System MUST 毎日 03:00 JST に、その時点のパターン + 例外から翌日分の枠を物化する。物化は同一入力(パターン・例外・日付)に対し決定論的でなければならない(INV-6)
- **FR-005**: System MUST 枠 ID を物化時に採番し、以後不変とする。パターン行の削除は未着手の将来枠の削除として反映する
- **FR-006**: System MUST ジャンル指定 3 種(固定 / おまかせ / 実験)をサポートする。おまかせは role が主力 / 拡張のジャンルのみを対象とし、実験ジャンルはユーザーが明示指定したときだけ編成に出る
- **FR-007**: System MUST NOT 「1 日 N 本まで」の投稿上限を独自設定として持たない。実効上限は YouTube API quota と GPU 直列処理能力の 2 つの外部制約のみとする
- **FR-008**: System MUST GPU 実行順を公開予定時刻の早い順(EDF)とし、同時刻なら物化順とする

#### 枠の状態機械(ADR-0045)

- **FR-010**: System MUST 枠の状態を 10 個(`empty` / `blocked` / `in_production` / `awaiting_approval` / `approved` / `published` / `rejected_pending` / `failed` / `skipped` / `withdrawn`)に閉じ、遷移は遷移表 T1〜T18 のみ許可する
- **FR-011**: System MUST 制作工程の進捗を状態ではなく属性 `current_stage`(planning / generating / inspecting / packaging)と成果物の有無で表す
- **FR-012**: System MUST 不変条件 INV-1〜INV-6 を全実行経路で維持する。特に INV-2(公開遷移は AI 開示設定済み ∧ 指紋 CLEAR ∧ 承認記録あり ∧ 公開ブロック無し ∧ システム `running` のときのみ)は critical path とする
- **FR-013**: System MUST 承認期限を「枠の公開予定時刻 + 7 日」とし、期限内の承認はその時点で即時公開(遅延公開)、期限超過は自動で `skipped` とする
- **FR-014**: System MUST 公開できない理由を状態ではなく属性 `publish_block_reason`(`null` / `system_paused` / `quota_exhausted`)で持ち、UI では状態と必ず併記する
- **FR-015**: System MUST 1 日の公開予定が quota 見積り上限(初期値 6 本 / 日。実装時に現行 quota 仕様を再確認)を超えるパターンの保存を許可しつつ警告を表示する
- **FR-016**: System MUST quota 枯渇時は `publish_block_reason=quota_exhausted` で待機し、リセット後に古い枠から順に公開する

#### 公開ゲート・承認(ADR-0042)

- **FR-020**: System MUST 人の判断を公開ゲート(枠ごと、L0 時)と運営判断(ジャンル昇格 / 撤退 / 新規追加、自動運転レベル変更)の 2 種のみとし、これ以外に人の承認を要求する工程を作らない
- **FR-021**: System MUST L0 でパッケージング完了後に枠を `awaiting_approval` とし、枠詳細でプレビュー(動画・タイトル・説明文・チャプター)と検査サマリ(AI 開示・指紋・音量・形式検証)を提示する
- **FR-022**: System MUST 却下に理由(自由記述)を必須とし、却下理由を次回の企画 LLM への入力に含める
- **FR-023**: System MUST 自動検査(指紋照合・AI 開示フラグ・音量 / 尺 / 無音率)を検査工程として実行し、人の承認対象から外す。指紋一致は該当トラックのみ自動再生成(3 回連続一致でジャンル一時停止 + 通知)、品質不合格は自動再生成、リトライ上限超過で枠を `failed` にして通知する
- **FR-024**: System MUST 仕様確定(compile)と検証(validate)を人の承認なしの内部処理として自動実行する(ADR-0039 の compile / 仕様 hash / 自動 QA の技術部分を継承)
- **FR-025**: System MUST 運営判断を監査ログに記録する

#### 自動運転レベル(ADR-0043)

- **FR-030**: System MUST 自動運転レベル(L0 監督 / L1 事後確認 / L2 全自動)をシステム全体で 1 つ持ち、既定を L0 とする。枠ごと・ジャンルごとのレベルは持たない
- **FR-031**: System MUST L1 / L2 で枠が `awaiting_approval` を経由せず、パッケージング完了時にシステムが承認記録を残して `approved` へ進むこととし、監査ログ上で人 / システムの承認を区別する
- **FR-032**: System MUST 昇格条件を自動判定する — L0→L1: 直近 10 枠が連続で無修正承認(却下も工程やり直しも無い)かつその 10 枠で検査の自動不合格が 0。L1→L2: L1 での運用 30 日以上かつその期間の取り下げ 0・compliance エラー 0
- **FR-033**: System MUST 条件未達のレベルを設定画面で選択不可とし、未達の理由と現在の達成状況(判定に使った数値)を表示する
- **FR-034**: System MUST 降格をいつでも無条件・即時で受け付け、レベル変更(昇格・降格とも)を監査ログに記録する
- **FR-035**: System MUST 取り下げ = YouTube 動画の private 化(削除しない)とし、取り下げた枠を `withdrawn`(終端)とする。「24 時間以内」は通知上の SLA 表示であり、操作自体は期限後も可能とする
- **FR-036**: System MUST 取り下げ操作を admin UI と Slack の両方に置く

#### 停止手段・安全装置(ADR-0044)

- **FR-040**: System MUST システム状態(`running` / `publish_paused` / `stopped`)を DB に 1 行で永続化する
- **FR-041**: System MUST 状態の参照点を「各工程の開始直前」(`stopped` なら開始しない)と「公開 API 呼び出し直前」(`publish_paused` / `stopped` なら公開しない)の 2 箇所に固定し、ここを通らない実行経路を作らない
- **FR-042**: System MUST 停止操作を CLI(`ymg stop` / `ymg pause-publishing` / `ymg resume`)と Slack に置き、admin UI には現在状態と停止コマンドの表示のみ置く(UI からの停止操作は不可)
- **FR-043**: System MUST compliance 事象(公開後の指紋一致判明 / YouTube からのポリシー・著作権通知 / `containsSyntheticMedia` 未設定投稿の検知)で、人の操作を待たず自動で `publish_paused` + 該当動画 private 化 + L0 降格 + Slack 通知を行う
- **FR-044**: System MUST エラー 5 分類(ADR-0028)を継承し、新モデルの挙動(transient=リトライ max 2 / recoverable=調整 + リトライ、上限超過で `failed` / fatal=`stopped` + 通知 / compliance=FR-043 / quality=自動再生成、上限超過で `failed`)に従う
- **FR-045**: System MUST 毎朝 07:30 JST に当日サマリ(公開予定枠・要確認件数・システム状態)を Slack に送信し(daily heartbeat)、heartbeat の運用を L1 以上の前提条件とする
- **FR-046**: System MUST マシン再起動後にスケジューラを自動再開し、停止は `system_state` でのみ表現する(再起動で暗黙解除されない)

#### 工程やり直し・成果物無効化(ADR-0046)

- **FR-050**: System MUST 成果物依存 DAG(plan → track[1..6] / thumbnail / title / description、track → inspection / mix / chapters、mix + thumbnail → video、video + title + description + inspection → package)をコード上の単一定義とし、無効化を必ずその定義から導出する(手書きの分岐を書かない)
- **FR-051**: System MUST 成果物 X の再生成時に X に推移的に依存する成果物をすべて無効化し、X に依存しない成果物を保持する
- **FR-052**: System MUST トラック単体の再生成を提供し、その際 inspection / mix / chapters / video / package が必ず無効化されることを保証する(未検査音源の公開経路を閉じる)
- **FR-053**: System MUST 文言(title / description)だけのやり直しを提供し、その際 video / thumbnail を保持する
- **FR-054**: System MUST 無効化された成果物を持つ枠が `awaiting_approval` / `approved` / `published` を取れないこと(INV-5)を保証し、`approved` でのやり直しは `in_production` に戻して承認を失効させる
- **FR-055**: System MUST NOT `published` の枠に対するやり直しを提供しない(取り下げ + 単発枠の追加で代替)
- **FR-056**: System MUST 各成果物にバージョンと来歴(プロンプト版・モデル・上流成果物の版)を記録する
- **FR-057**: System MUST プロンプト修正後の再実行を「新バージョンとして保存し以後の既定にする」「この枠だけのオーバーライドとして試す」の両方で提供する
- **FR-058**: System MUST やり直しを枠のタイムラインに記録し(誰が / いつ / どの成果物を / なぜ)、旧世代成果物を即時削除しない(FR-081 の保持期間に従う)

#### 制作パイプライン(001 から移植、ADR-0050)

- **FR-060**: System MUST ACE-Step 1.5 で 5 分 × 6 トラックを生成し、30 分尺の動画に組み立てる(ADR-0003 / ADR-0031 継承)
- **FR-061**: System MUST 公開前に AcoustID + Chromaprint で全トラックの指紋検査を行う(ADR-0005 継承)
- **FR-062**: System MUST 音量正規化・尺・無音率の自動検査を行う(ADR-0039 の Audio QA 技術部分を継承)
- **FR-063**: System MUST ffmpeg `showwaves` overlay + SDXL 背景で動画を合成する(ADR-0015 継承)
- **FR-064**: System MUST SDXL 派生モデル(デフォルト Juggernaut XL v10)でサムネを生成する(ADR-0016 継承)
- **FR-065**: System MUST 全公開時に `status.containsSyntheticMedia=true` を設定し、公開前バリデーションで未設定を検知したら公開を停止する(ADR-0020 継承)
- **FR-066**: System MUST 企画 LLM / 仕上げ LLM を LLM Provider 抽象(OpenAI / Anthropic / Ollama)+ Pydantic 構造化出力で呼び出し、全呼び出しを usage 記録する(ADR-0018 / 0019 / 0024 継承。Anthropic サブスク利用は起動時拒否)
- **FR-067**: System MUST directive parser とジャンル別デフォルトテンプレ(タイトル / 説明文 / サムネ)を継承する(ADR-0017 / 0034)
- **FR-068**: System MUST 旧「改善計画 LLM」を企画 LLM と実績スナップショットに読み替えて継承する(スキーマ・プロンプト構造の方針は ADR-0032 / 0033 を踏襲)

#### 目的関数・分析・コメント(ADR-0047 / ADR-0048)

- **FR-070**: System MUST 自動判断の主目的を「週次総視聴時間(estimatedMinutesWatched)の最大化」、ガードレールを「ジャンル別平均視聴維持率がそのジャンルの直近実績(中央値)の 80% 以上」とする
- **FR-071**: System MUST おまかせ枠のジャンル選択を「ガードレール充足ジャンルのうち直近の 1 本あたり総視聴時間期待値が最大のもの(同点は配分が目標から遅れているジャンル優先)」で行う
- **FR-072**: System MUST ガードレールを 2 週連続で下回ったジャンルの次週配分を自動縮小する(撤退はしない)
- **FR-073**: System MUST ジャンルの昇格 / 撤退の推奨を算出して人に提示し、自動では実行しない
- **FR-074**: System MUST すべての自動判断について、判断に使った数値と理由を画面に表示する
- **FR-075**: System MUST 自動判断に公開から 7 日以上経過した確定値のみを使い、7 日未満の数値は「暫定」と明示し、サンプル 3 本未満のジャンルを統計判断(配分縮小・撤退推奨)の対象外とする
- **FR-076**: System MUST 視聴者コメントの取得とセンチメント要約を行い、実績スナップショットに含めて企画 LLM の定性的な補助入力とする。返信・削除・モデレーションの UI は作らず、書き込み系 API を使わない
- **FR-077**: System MUST 分析画面に「視聴者の声」セクション(件数・傾向・代表コメント、読み取り専用と明示)を置く

#### 成果物の保持・削除(ADR-0049)

- **FR-080**: System MUST マスター音源(トラック個別 + mix)・サムネイル・メタデータ(企画 JSON・文言・チャプター・検査結果)・監査ログ / 枠タイムライン・プロンプト全バージョンを永続保持する
- **FR-081**: System MUST 公開済み最終動画(mp4)を公開確認後 90 日、やり直しで無効化された旧世代成果物を 7 日、終端枠(`skipped` / `withdrawn` / `rejected_pending`)の成果物を 30 日の保持後に自動削除する
- **FR-082**: System MUST 削除でメタデータを残しファイルのみ削除し、枠詳細に「保持期間を過ぎたため削除済み」と表示する
- **FR-083**: System MUST 削除ジョブを日次実行し、削除件数と解放容量をタイムラインに記録する
- **FR-084**: System MUST バックアップ(ローカル別ディスク、ADR-0026 継承)の対象を永続保持のもののみとし、Fernet 鍵を対象外とする

#### 画面構成(ADR-0041、モック `redesign-2026-07/`)

- **FR-090**: System MUST 編成表(ホーム)= 週間カレンダーを提供する。枠の色 = 制作ラインの現在地、公開枠パターンの編集もここで行う
- **FR-091**: System MUST 枠詳細(ドリルダウン)を提供する。各工程の「使った(入力)/ できた(出力)」、工程単位のやり直し、公開ゲートを含む
- **FR-092**: System MUST タイムライン(出来事ログ + 当日サマリ)、ジャンル(ポートフォリオと role 遷移)、分析(KPI と企画フィードバック)、プロンプト(工程 1:1 の管理 + 枠に影響しない「試しに 1 件生成」)、設定(自動運転レベル / LLM / GPU / バックアップ / システム状態表示)の各画面を提供する
- **FR-093**: System MUST NOT 旧画面(全ジョブ横断の制作ラインボード・受信箱・公開キュー・運営方針)を作らない

#### 基盤・セキュリティ(継承)

- **FR-100**: System MUST backend を Python 3.13 + FastAPI + uv、frontend を Next.js(App Router)、DB を PostgreSQL で構成する(ADR-0001 / 0009 / 0010 / 0014 継承)
- **FR-101**: System MUST 管理 UI の認証をセッション認証 BFF(ADR-0013)で継承し、LAN 内でも認証を必須とする(ADR-0050)。単一ユーザー・パスワードのみ・2FA なし
- **FR-102**: System MUST OAuth トークンを Fernet 対称鍵暗号化で DB に保存し、鍵を `.env` 管理・バックアップ対象外とする(ADR-0012 継承)
- **FR-103**: System MUST GPU worker を独立プロセス + HTTP API + fsspec ストレージ抽象で疎結合に保つ(ADR-0022 / 0031 継承)
- **FR-104**: System MUST 構造化 JSON ログ(枠 ID / ジャンル / 工程で検索可能)と実行進捗の SSE 配信を提供する(ADR-0023 継承)
- **FR-105**: System MUST スケジューラを APScheduler(backend プロセス内、single-flight)で実行する(ADR-0011 の採用部分を継承。「承認 Plan のみ実行」前提は状態機械に置換)

#### 移行(ADR-0050)

- **FR-110**: System MUST オーケストレーション層(枠 / パターン / 物化 / 状態機械 / スケジューリング / 承認 / 管理 UI 全画面)を新規実装とし、旧 Plan / dryrun / Post 系のテーブル・API・画面を改修しない
- **FR-111**: System MUST 工程実装(FR-060〜FR-068 の対象)を 001 からコード移植して再利用し、移植時に旧オーケストレーションへの依存が見つかれば個別に切り出す
- **FR-112**: System MUST 旧 DB から公開済み動画の実績データ(video_id・公開日時・ジャンル・Analytics)のみを 1 回きりのスクリプトで移行し、恒久的な互換層を作らない

### Key Entities

- **公開枠パターン(SlotPattern)**: 枠行(曜日 × 時刻 JST × ジャンル指定)のリスト。バージョン管理され、変更は将来の `empty` 枠にのみ反映される
- **枠例外(SlotException)**: 特定日の休止 / ジャンル変更 / 臨時追加。パターンから独立して保持され、物化時に適用される
- **枠(Slot)**: 集約ルート。公開日時(JST)× ジャンル指定。状態(10 種)+ `current_stage` + `publish_block_reason` + 承認期限を持つ。物化時に採番される不変 ID
- **成果物(Artifact)**: plan / track[1..6] / inspection / mix / chapters / thumbnail / title / description / video / package。各々バージョンと来歴(プロンプト版・モデル・上流の版)を持ち、依存 DAG 上で無効化が決まる
- **承認記録(ApprovalRecord)**: 公開ゲートの判断。行為者(人 / システム)・時刻・却下理由を持つ。INV-2 の必要条件
- **システム状態(SystemState)**: `running` / `publish_paused` / `stopped` の 1 行。全ワーカーの参照点
- **自動運転レベル(AutonomyLevel)**: L0 / L1 / L2 のシステム全体設定 + 昇格判定に使う実績カウンタ
- **ジャンル(Genre)**: role(主力 / 拡張 / 実験)と一時停止状態を持つポートフォリオ。運営判断で遷移
- **実績スナップショット(MetricsSnapshot)**: 企画 LLM 入力の生データ(視聴時間・維持率・コメント要約)。再現性のため保存(ADR-0032 の方針継承)
- **枠タイムライン(SlotTimeline)**: 枠ごとの出来事ログ(やり直し・状態遷移・削除ジョブ実績)。永続
- **監査ログ(AuditLog)**: 運営判断・レベル変更・承認(人 / システム)の記録。永続

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: L0 において 1 本の動画を公開するまでに必要な人の操作が 1 回(公開ゲート)以下になる(旧設計は直列 3 回)。L1 / L2 では 0 回
- **SC-002**: 「いつ何が公開されるか」「各枠がどの工程にいるか」への回答が編成表 1 画面で得られる(旧設計は Plan / dryrun / Post の 3 画面横断)
- **SC-003**: compliance 事象の検知から、人の操作なしで公開停止 + 該当動画 private 化 + L0 降格 + 通知が完了する
- **SC-004**: 承認されないまま承認期限(公開予定 + 7 日)を過ぎた枠が 100% 自動で `skipped` になり、承認記録なしで公開される枠が 0 件である(INV-2)
- **SC-005**: トラック差し替え後に検査を経ずに公開へ到達する経路が存在しない(無効化規則がテストで網羅される)
- **SC-006**: 同一のパターン・例外・日付から物化した枠集合が常に同一である(決定論、INV-6)
- **SC-007**: すべての自動判断(おまかせ選択・配分縮小・昇格 / 撤退推奨・システム承認)に、判断に使った数値と理由が画面表示される
- **SC-008**: daily heartbeat が毎朝届き、届かない日はシステム停止と判断できる(死活監視の唯一のシグナルとして機能する)

## Out-of-Scope

- コメントへの返信・削除・モデレーション(読み取り専用。ADR-0048)
- 複数チャンネル運用 / Shorts / ライブ配信 / 他プラットフォーム / モバイルアプリ(001 から継承)
- 枠ごと・ジャンルごとの自動運転レベル(システム全体で 1 つ。ADR-0043)
- 外形監視サービス(healthchecks.io 等)の導入(daily heartbeat で開始。将来課題)
- 公開済み動画の差し替え(取り下げ + 単発枠で代替。ADR-0046)
- admin UI からの停止操作(表示のみ。ADR-0044)
- 動画尺の可変化(30 分固定を継承。可変化時は目的関数の見直しが必要。ADR-0047)
- 旧システムとの並行稼働(移行期間中は旧システムを停止。ADR-0050)
- Lean による状態機械の形式検証の**実施**(ADR-0045 は形式化対象と明記するが、本 feature では property-based テストで不変条件を担保し、Lean 化は別作業とする)

## Assumptions

- 単一 YouTube チャンネル・単一ユーザー(seita)・RTX 3090 単機のローカル運用(001 から継承)
- 動画は 30 分(5 分 × 6 トラック連結)固定(ADR-0003 継承)
- YouTube API quota は 10,000 units / 日、`videos.insert` = 1600 units(2026-05 時点の確認値。**実装時に現行仕様を再確認する**。ADR-0045)
- 初期ジャンルは 001 の `genres` 6 件(ADR-0033)を role 付きで引き継ぐ
- 昇格条件の数値(10 枠 / 30 日)、ガードレール(80% / 2 週 / 3 本)、保持期間(7 / 30 / 90 日)、承認期限(7 日)は運用実感による初期値であり、運用しながら ADR で更新する
- 旧リポジトリ(001 実装)は参照用に凍結し、削除しない(ADR-0050)。本 feature の新実装は同一モノレポ内で旧コードを置き換える形で行い、置き換え完了までは旧コードをビルド対象から外して残す
- Slack は単一 workspace / 単一 webhook + ボタン操作(通知は 001 の 5 カテゴリ prefix 方針を継承)

## ADR References

- 正本: ADR-0041 / 0042 / 0043 / 0044 / 0045 / 0046 / 0047 / 0048 / 0049 / 0050
- 継承: ADR-0001 / 0002 / 0003 / 0005 / 0008 / 0009 / 0010 / 0012 / 0013 / 0014 / 0015 / 0016 / 0017 / 0018 / 0019 / 0020 / 0021 / 0022 / 0023 / 0024 / 0026 / 0027 / 0028 / 0029 / 0030 / 0032 / 0033 / 0034 / 0037 / 0038 / 0040
- 置き換え済み(履歴): ADR-0004 / 0006 / 0007 / 0025 / 0035 / 0036 / 0039、ADR-0011 / 0031 の一部
- 憲法: v2.0.0(Principle I / II / III 改版)
