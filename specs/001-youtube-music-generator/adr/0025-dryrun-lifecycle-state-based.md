# ADR-0025: dryrun ライフサイクル = 状態別 retention(承認 / 否認 / 無反応)

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** ops, backend

## 背景

ADR-0007 で dryrun モードを MVP 必須機能に決定。retention は「例: 48時間」と暫定で記載していたが、実運用では3つの状態を扱う必要がある:

1. **未確認(pending)**: 生成済み、管理UI で確認されていない
2. **承認済み(approved)**: 管理UI で確認・本番投稿に移行
3. **否認(rejected)**: 管理UI で却下、または無反応で retention 経過

一律 retention にすると承認・否認のタイミングと噛み合わず、ストレージ膨張・運用負担が出る。

## 決定

### dryrun レコードに状態を持たせる

```sql
CREATE TABLE dryrun_outputs (
  id UUID PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL,
  cycle_id UUID NOT NULL,
  genre TEXT NOT NULL,
  video_path TEXT NOT NULL,           -- fsspec の URL(ADR-0022)
  metadata JSONB NOT NULL,            -- 投稿予定タイトル・説明・タグ
  status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
  status_updated_at TIMESTAMPTZ,
  rejection_reason TEXT,              -- 否認時の理由
  approved_video_id TEXT              -- 承認後の YouTube video ID
);
```

### 状態遷移と retention ルール

| 状態 | 遷移トリガ | ファイル削除タイミング |
|------|-----------|--------------------|
| `pending` | 生成完了時 | — |
| `approved` | 管理UI で承認 → 本番投稿パスに移行 | 投稿完了直後にファイル削除(ストレージ抽象化層経由) |
| `rejected` | 管理UI で却下、または無反応で 7日経過 | 状態遷移直後にファイル削除 |

- 無反応(pending のまま 7日経過)は自動で `rejected` 扱いに遷移
- `rejection_reason` は管理UI から記録(自由テキスト)、無反応 rejected の場合は `auto_expired` を入れる

### スケジューラジョブ

- APScheduler(ADR-0011)で **日次の retention ジョブ** を実行
- 過去 7日以上 `pending` のレコードを `rejected` に遷移、ファイル削除
- 承認済みの一時ファイルが削除漏れしている場合のクリーンアップも併用

### 否認理由の活用

- `rejection_reason` は改善計画 LLM(週次サイクル、ADR-0006)の入力に含める
- 「なぜ承認に至らなかったか」を改善計画に反映できる
- 動機 B(観察・実験データ)とも整合

## 結果

### 良い影響

- 状態が明示化され、運用ルールが機械可読
- ストレージ消費が最適(承認は即時、否認も即時、無反応のみ 7日待機)
- 否認理由が改善計画にフィードバックされる(品質向上ループ)
- dryrun が「永遠に投稿されない罠」になりにくい(7日後に自動 rejected で気づける)

### 悪い影響・トレードオフ

- 状態遷移ロジック + 日次ジョブの実装コスト(数百行程度、軽微)
- 7日無反応で rejected になるが、本人がレビュー余裕を作れなかった日はやり直しが必要
  - 緩和: 重要なテスト時は管理UI から retention を延長できる("pin" 機能、将来オプション)

### 受容したリスク

- 承認直後のファイル削除でディスクから消えるため、投稿後に「やっぱり差し替えたい」の柔軟性は下がる
  - YouTube 側に投稿済みのため、本来そこから引き戻すのは別の問題

## 検討した代替案

- **一律 48時間自動削除:** 旅行・忙しい日にレビューできないと消える、ストレージは最小だが運用が脆い。不採用。
- **一律 7日自動削除:** 承認後もファイルが残るのは冗長、状態が分からない。不採用。
- **手動削除のみ:** ストレージ膨張、運用ルールとして弱い。不採用。

## 関連

- ADR-0006: 週次サイクル(改善計画入力)
- ADR-0007: dryrun モード
- ADR-0010: PostgreSQL(dryrun_outputs テーブル)
- ADR-0011: APScheduler(retention ジョブ)
- ADR-0022: ストレージ抽象化(ファイル削除)
- ../requirements.md §dryrun
