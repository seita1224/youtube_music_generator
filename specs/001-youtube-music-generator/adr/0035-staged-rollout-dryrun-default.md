# ADR-0035: MVP は全機能実装 + dryrun=ON 既定で段階移行する

- **ステータス:** Accepted
- **日付:** 2026-05-26
- **決定者:** @seita
- **タグ:** ops / policy

## 背景

要件定義完了時点(ADR-0001 〜 0034)で、 YouTube uploader / OAuth / analytics / panic-stop まで含む全機能が spec / plan / contracts に組み込まれている。 一方で、 動画品質や AcoustID プレチェックの実機妥当性、 `containsSyntheticMedia` の YouTube 側挙動を確認する前に本投稿を始めるのはリスクが高い。

論点は「MVP のスコープをどこで切るか」。 3 通り検討した。

- A. 全機能実装 + dryrun=ON 既定で運用、 準備完了後にスイッチ
- B. YouTube 統合を MVP 範囲外、 手動アップロード
- C. uploader 実装するが `videos.insert` 直前で TODO

## 決定

### A を採用 — 全機能実装 + dryrun=ON 既定で段階移行

具体的な実装 / 運用ルール:

#### 1. `.env` 既定値

- `DRYRUN_DEFAULT=true` を初期値とする(quickstart.md 既定と整合)
- `app_state.dryrun_enabled` の seed も `true`

#### 2. 投稿モードへの切替は audit_log 必須

- 管理 UI から `dryrun_enabled` を `true → false` に切替えた瞬間に `audit_log` に記録:
  - `action="dryrun_disabled"`
  - `payload={"actor": "<basic_auth_user>", "from": true, "to": false}`
- 逆方向(`false → true`)も同様に記録

#### 3. 最初の本投稿は手動オペレーション 1 本

- scheduler に任せず、 管理 UI の `POST /posts/{post_id}/retry` 経路で 1 本だけ実行
- YouTube Studio で以下を目視確認してから定常運用に移行:
  - 公開状態 / `containsSyntheticMedia` ラベル
  - サムネレンダリング(Pillow オーバーレイの破綻なし)
  - 説明文の AI 開示固定文 + チャプター
  - 30 分尺の音声 / 映像同期

#### 4. 投稿モード切替後 最初の 1 週間は posting レート制限

- `max_daily_posts=1` を強制(ADR-0004 の上限 2 本にせず 1 本固定)
- 1 週間経過 + 異常なしの確認後に 2 本 / 日へ昇格
- 制限値は `app_state.daily_post_limit_override` jsonb で管理

#### 5. panic-stop の予行演習を MVP 完了条件に含める

- dryrun モード中に unlisted で 1 本だけ投稿、 `make panic-stop` で private 化できることを確認
- 上記が完了するまで `dryrun_enabled=false` への切替を技術的にブロックする 必要はないが、 完了チェックリストに含める

### 削除しないもの

選択肢 B / C では削れる範囲だが、 A 採用により以下は **全て初期 MVP に含める**:

- `backend/src/.../infrastructure/youtube/` 全モジュール
- `oauth_credentials` テーブル + Fernet 暗号化 / 復号
- `videos` テーブル + `containsSyntheticMedia` CHECK constraint
- panic-stop の YouTube private 化フロー
- analytics_daily 取得 (週次サイクル LLM 入力)

## 結果

### 良い影響

- spec.md / data-model.md / contracts と運用が完全に整合する。 後でリファクタする必要がない
- panic-stop の動作確認が MVP 完了条件に入るため、 マネタイズ移行前に安全弁が実機検証される
- 動機 B(YouTube アルゴリズム観察)に必要な analytics 取得が初期から動く
- 「投稿するかしないか」がランタイム判断になるため、 投稿準備が整った瞬間に切替えられる(コード変更不要)
- audit_log を介して dryrun 切替が記録されるため、 不可逆判断のトレーサビリティが残る

### 悪い影響・トレードオフ

- 初期実装工数が B / C より大きい(YouTube OAuth / Data API / Analytics API / panic-stop)
- Google Cloud project セットアップが MVP セットアップ手順に入る(quickstart.md §8)
- 「投稿ボタンを押さない期間」が長引くと、 OAuth トークンが refresh されず期限切れる可能性 → 週次 cron で refresh する処理は必須

### 受容したリスク

- 投稿準備が整う前に誤って `dryrun_enabled=false` に切替えてしまう人為ミス: audit_log に記録、 切替時に管理 UI で確認ダイアログを出す(将来実装)
- OAuth トークン期限切れ: 週次の analytics 取得ジョブ自体が refresh を起こすため自然回復、 ただし最初の 1 週間以内に少なくとも 1 回 analytics を叩く必要がある(技術的には初投稿前でも空 analytics 取得は可能)

## 検討した代替案

### 代替案 B: YouTube 統合を MVP 範囲外

- スコープ最小化メリットは大きいが、 spec を一度削って後で書き直すコスト + ADR との整合性管理が破綻しがち
- 動機 B(YouTube 観察)を回すには結局 OAuth + analytics 実装が必要、 後送りの利得が薄い
- panic-stop が後出しになり、 マネタイズ前の安全弁が運用開始時に動作未確認

### 代替案 C: uploader 実装するが `videos.insert` だけ TODO

- A と B の中間、 工数削減は中程度
- 「実装したけど使われない」コードが残る、 1 人運用で「半実装の境界線管理」が認知負荷
- `containsSyntheticMedia` 等のバリデーションを本物の YouTube API で確認できない期間がある

## 関連

- ADR-0004: 投稿規模 1日1〜2本
- ADR-0007: dryrun モード MVP 必須
- ADR-0020: `containsSyntheticMedia=true` 必須
- ADR-0021: Data API + Analytics API
- ADR-0025: dryrun ライフサイクル
- ADR-0028: 5 種類のエラーカテゴリ(audit_log と panic-stop 関連)
- ADR-0031: デプロイ手順(panic-stop)
- specs/001-youtube-music-generator/spec.md (User Story 4 / FR-006, FR-102)
