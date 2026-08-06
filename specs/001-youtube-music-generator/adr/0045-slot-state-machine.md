# ADR-0045: 枠の状態機械(承認期限・遅延公開・quota・失敗系を含む)

- **ステータス:** Accepted
- **日付:** 2026-07-25
- **決定者:** @seita
- **タグ:** backend / frontend / policy

## 背景

ADR-0041 で枠を集約ルートにしたが、枠が取りうる状態を閉じた形で列挙しなければ、次が定義できない。

- 却下したあと枠がどうなるのか(モック上で記述が矛盾していた)
- 公開予定時刻を過ぎても承認されなかった枠(**L0 では構造的に必ず発生する**)
- 生成失敗・API 障害・quota 超過に落ちた枠
- L1 で取り下げた枠

旧設計にはこれらに対応する状態が無く、Plan の状態機械(ADR-0039)は制作工程の進捗を表すもので、公開の可否や失敗を表現していなかった。本 ADR は Lean による形式化の主対象である。

## 決定

### (1) 状態は 10 個。工程の進捗は状態ではなく属性で持つ

制作工程(企画 / 音楽生成 / 検査 / パッケージング)を状態として展開すると状態数が増え、やり直し(ADR-0046)で工程間を逆行するたびに遷移が増える。**制作中は 1 状態 `in_production` とし、どの工程にいるかは属性 `current_stage` と成果物の有無で表す**。

| 状態 | 意味 | 終端 |
|---|---|---|
| `empty` | 枠は存在するが制作未着手 | |
| `blocked` | 前提未達で制作を開始できない(ジャンル一時停止中 / システム `stopped`) | |
| `in_production` | 制作中(`current_stage` = planning / generating / inspecting / packaging) | |
| `awaiting_approval` | 全工程完了、人の承認待ち(L0 のみ) | |
| `approved` | 承認済み。公開予定時刻を待つ | |
| `published` | 公開済み | |
| `rejected_pending` | 却下済み、処置待ち | |
| `failed` | 工程が失敗し、自動リトライ上限を超えた | |
| `skipped` | 見送り | ✓ |
| `withdrawn` | 公開後に取り下げた(private 化済み) | ✓ |

`published` は準終端とする。`published → withdrawn` のみを許し、それ以外の遷移を持たない。

### (2) 遷移表

| # | From | To | 条件・契機 |
|---|---|---|---|
| T1 | (なし) | `empty` | 物化(前日 03:00 JST) |
| T2 | `empty` | `in_production` | 企画開始(前日 03:00 の枠駆動生成 / 手動開始) |
| T3 | `empty` | `blocked` | 指定ジャンルが一時停止中、またはシステム `stopped` |
| T4 | `blocked` | `in_production` | 前提解消 |
| T5 | `in_production` | `awaiting_approval` | 全成果物が揃い、検査合格、かつ L0 |
| T6 | `in_production` | `approved` | 全成果物が揃い、検査合格、かつ L1 / L2(システム承認を記録) |
| T7 | `in_production` | `failed` | 自動リトライ上限超過 / `fatal` エラー |
| T8 | `failed` | `in_production` | やり直し(ADR-0046) |
| T9 | `failed` | `skipped` | 諦める |
| T10 | `awaiting_approval` | `approved` | 人が承認 |
| T11 | `awaiting_approval` | `rejected_pending` | 人が却下(理由必須) |
| T12 | `awaiting_approval` | `skipped` | **承認期限超過**(自動) |
| T13 | `rejected_pending` | `in_production` | 任意の工程からやり直し |
| T14 | `rejected_pending` | `skipped` | 見送りを選択、または承認期限超過(自動) |
| T15 | `approved` | `published` | 公開実行が成功 |
| T16 | `approved` | `skipped` | 承認期限超過(自動) |
| T17 | `approved` | `in_production` | **承認の失効**(成果物が無効化された場合。ADR-0046) |
| T18 | `published` | `withdrawn` | L1 の取り下げ、または compliance 自動 private 化 |

`empty` / `blocked` / `in_production` の枠は、パターン変更や単発例外で削除されうる(ADR-0041)。削除は状態遷移ではなくレコード削除として扱う。制作済みの成果物は保持期間(ADR-0049)に従う。

### (3) 承認期限と遅延公開

- **承認期限 = 枠の公開予定時刻 + 7 日**
- 期限内に承認されれば、**その時点で即時公開する**(遅延公開)。公開予定時刻を過ぎていても公開する
- 期限を過ぎたら自動で `skipped`(T12 / T14 / T16)
- `skipped` は終端。惜しい成果物を救済したい場合は、保持期間内(ADR-0049)に**単発枠を追加して成果物を再利用**する。状態機械に復帰遷移は持たせない

### (4) 公開できない理由は状態ではなく属性で持つ

`approved` の枠が公開されない理由は複数あるが、状態を増やさず `publish_block_reason` で表す。

| 値 | 意味 |
|---|---|
| `null` | 公開可能(時刻到来で公開する) |
| `system_paused` | `publish_paused` / `stopped`(ADR-0044) |
| `quota_exhausted` | YouTube API quota 枯渇 |

いずれも解消後に遅延公開として処理する。ただし承認期限((3))を過ぎていれば `skipped` になる。

### (5) YouTube quota

- `videos.insert` は **1600 units**、既定の割当は **10,000 units/日**。したがって公開の実効上限は **1 日およそ 6 本**
  ⚠ 出典は旧 research.md の確認値(2026-05 時点)。**実装時に現行の quota 仕様を再確認すること**
- 編成表で 1 日の公開予定が 6 本を超えるパターンを組んだ場合、保存はできるが**警告を表示する**
- quota 枯渇時は `publish_block_reason = quota_exhausted` で待機し、リセット後に古い枠から順に公開する

### (6) 不変条件(Lean 形式化の対象)

- **INV-1**: 枠は常にちょうど 1 つの状態を持つ
- **INV-2**: `published` に入る遷移(T15)は、次をすべて満たすときのみ許される — AI 開示フラグ設定済み ∧ 指紋検査 CLEAR ∧ 承認記録が存在する(L0 なら人、L1 / L2 ならシステム) ∧ `publish_block_reason = null` ∧ システム状態が `running`
- **INV-3**: `skipped` と `withdrawn` から出る遷移は存在しない
- **INV-4**: `withdrawn` に入る遷移は `published` からのみ
- **INV-5**: 成果物が 1 つでも無効化されている枠は `awaiting_approval` / `approved` / `published` を取れない(T17 で `in_production` へ戻る)
- **INV-6**: 物化は決定論的 — 同じパターン・例外・日付から、同じ枠集合が生成される

## 結果

### 良い影響

- 「承認が間に合わなかった」「生成に失敗した」「quota が尽きた」がすべて明示的な状態・属性になり、画面に出せる
- 状態が 10 個に閉じたため、Lean での網羅的な形式化が可能になる
- INV-2 が公開の唯一の入口を規定するため、コンプライアンス要件を 1 箇所で保証できる

### 悪い影響・トレードオフ

- 遅延公開を認めるため、「月曜 07:00 の枠」が水曜に公開されることがある。編成の意味が弱まるが、承認忘れで丸ごと捨てるよりは良いと判断した
- `publish_block_reason` を属性にしたことで、状態だけを見ても「なぜ公開されないか」が分からない。UI では必ず併記する

### 受容したリスク

- 承認期限 7 日は運用実感による初期値。長すぎれば古い企画が公開され、短すぎれば旅行中に全滅する
- quota 6 本/日 は旧 research の値であり、Google の割当変更で変動しうる

## 検討した代替案

- **工程ごとに状態を持つ(planning / generating / …):** 状態数が増え、やり直しで逆行遷移が組み合わせ的に増える。`current_stage` 属性を採用
- **公開時刻を過ぎたら即 `skipped`:** L0 では夜間・外出で必ず取りこぼす。遅延公開を採用
- **`quota_deferred` を独立した状態にする:** `approved` と実質同じ(待っているだけ)。属性で表現
- **`skipped` からの復帰遷移を持つ:** 終端の意味が消え、形式化が難しくなる。単発枠の追加で代替

## 関連

- ADR-0041: 枠と物化
- ADR-0042: 公開ゲート(T10 / T11)
- ADR-0043: L1 / L2 の T6、取り下げの T18
- ADR-0044: `publish_block_reason = system_paused`、`failed` に落とすエラー分類
- ADR-0046: 成果物の無効化と T17(承認の失効)
- ADR-0049: `skipped` / `withdrawn` 後の成果物保持
- supersedes: ADR-0039 の Plan 状態機械
