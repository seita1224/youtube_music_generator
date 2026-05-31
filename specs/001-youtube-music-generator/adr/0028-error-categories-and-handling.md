# ADR-0028: エラーカテゴリ分類と挙動

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, ops, qa

## 背景

要件「失敗は Slack 通知のみ、自動リトライなし」は全失敗を同じ扱いにする運用ルールだが、実装上は不十分:

- 一過性エラー(タイムアウト・5xx)は同一ジョブ内で短いリトライをすれば回復することが多い
- LLM 構造化出力の検証失敗(ADR-0018)は同一ジョブ内リトライ規約あり
- OAuth トークン失効 / DB 接続不可 / コンプラ違反は性質が違う

「自動リトライなし」の原則は **サイクル全体のリトライをしない** という意味と解釈し、ジョブ内の短いリトライは正常な再試行として扱う。

## 決定

5 カテゴリで分類し、それぞれ挙動を定義する。

### カテゴリ定義

| カテゴリ | 例 | 挙動 |
|---------|-----|------|
| **`transient`** | ネットタイムアウト、5xx、短時間 rate limit | 同一ジョブ内で **最大 3 回リトライ**、`exponential backoff` 1s → 2s → 4s。失敗時は `recoverable` に昇格 |
| **`recoverable`** | OAuth トークン失効、ディスク容量不足、API key 無効、Codex OAuth quota 切れ | 該当ジョブのみスキップ + Slack 通知。他ジョブは継続 |
| **`fatal`** | DB 接続不可、Fernet 鍵無効、設定ファイル破損 | サイクル全体停止 + Slack 通知 |
| **`compliance`** | `containsSyntheticMedia` 未設定、AcoustID マッチ、プロンプトポリシー違反 | 該当動画の投稿停止 + Slack 通知 + 詳細ログ(構造化ログに `compliance_violation` event を記録) |
| **`quality`** | LLM 構造化リトライ N 回失敗、サムネ生成異常、コメント分析空 | 該当部分のみスキップ、デフォルト値で続行、Slack 通知 |

### 実装

- バックエンドで例外を定義:

  ```python
  class WorkflowError(Exception):
      category: Literal["transient", "recoverable", "fatal", "compliance", "quality"]
      context: dict  # job_id, video_id, step, etc.
      original: Exception | None
  ```

- 各層で raise、中央ハンドラ(APScheduler のジョブラッパ + FastAPI のミドルウェア)がカテゴリに応じて挙動分岐
- 構造化ログ(ADR-0023)に `error_category` フィールドを必ず付与
- usage_log(ADR-0024)にも失敗時の category を記録(コスト分析と紐付け)

### Slack 通知レベル

- `transient` リトライ成功: 通知なし(ログのみ)
- `transient` リトライ失敗 → `recoverable` 昇格: **WARN レベル通知**
- `recoverable`: **WARN レベル通知**
- `quality`: **INFO レベル通知**(投稿は続行できるため)
- `fatal`: **CRITICAL レベル通知**(@channel 等で確実に気づく形)
- `compliance`: **ERROR レベル通知**(BAN リスク直結)

### 「自動リトライなし」原則との整合

- 要件の「自動リトライなし」は **サイクル全体のリトライをしない**
- `transient` の同一ジョブ内リトライは「正常な再試行」(数秒程度の backoff、最大 3 回)
- ADR にこの解釈を明示し、運用ルールと整合させる

## 結果

### 良い影響

- カテゴリにより人間判断の優先度が明確
- 一過性エラーで毎回サイクルが落ちる事態を回避
- コンプラ違反を他のエラーと区別して扱える(BAN リスク管理)
- 構造化ログでエラー傾向の事後分析が容易

### 悪い影響・トレードオフ

- 各 layer で適切な category を選んで raise する規律が必要
  - 緩和: code review で確認、デフォルトは `recoverable`(安全側)
- カテゴリ分類のメンテ(新しい例外パターンが出てきたら再分類)
  - 緩和: 半年ごとにレビュー

### 受容したリスク

- カテゴリ判定の誤り(本来 `compliance` を `quality` にしてしまう等)
  - 対策: コンプラ系列は単体テスト 100%(ADR-0027)で動作を担保

## 検討した代替案

- **3 カテゴリにシンプル化(retry / skip / stop):** コンプラ違反・品質低下を区別できず雑。不採用。
- **細分化(間隔・通知レベルを別軸で持つ):** 設定が複雑、運用負荷増。不採用。
- **カテゴリなし(全部スキップ + 通知):** 一過性エラーで日常的にサイクル落ち、運用が成立しない。不採用。

## 関連

- ADR-0006: 日次/週次サイクル
- ADR-0011: APScheduler
- ADR-0018: LLM 構造化出力(`quality` カテゴリの起点)
- ADR-0020: containsSyntheticMedia(`compliance` の対象)
- ADR-0023: 観測性
- ADR-0027: テスト方針(クリティカルパス)
- ../requirements.md §失敗時の挙動
