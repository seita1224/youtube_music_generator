# ADR-0021: アナリティクス取得 = YouTube Data API + YouTube Analytics API

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, ml, ops

## 背景

ADR-0006 の週次サイクルで投稿済み動画のデータを取得し、LLM で改善計画を立案する。
データ取得方式を決める必要がある。

YouTube が提供する API は 2 系統:

- **Data API v3**: 動画メタ・視聴数(累計)・いいね数・コメント数・コメント本文・検索
- **Analytics API**: retention、watch time、視聴者属性、流入元、デバイス、地域別、日別時系列など詳細指標
- **Reporting API**: バルク CSV ダウンロード型(数千動画規模向け)

動機 B(アルゴリズム観察)・C(マネタイズ)の両方で、retention・流入元・watch time といった指標は重要。
Data API のみでは表面的な数値しか取れず、改善計画 LLM への入力として弱い。

## 決定

- **Data API v3** と **Analytics API** を併用する
- Reporting API は本規模(1日1〜2本、ADR-0004)にはオーバースペックのため当面採用しない

### Data API v3 の用途

- 動画メタ(タイトル・説明・タグ・カテゴリ)の取得・更新
- 累計の視聴数・いいね数・コメント数
- コメント本文の取得(LLM 分析の入力)
- 投稿(`videos.insert`、ADR-0020)

### Analytics API の用途(週次サイクルの入力)

- `views`, `estimatedMinutesWatched`, `averageViewDuration`, `averageViewPercentage`(視聴維持率)
- `audienceWatchRatio`(秒単位の維持率カーブ、視聴者がどこで離脱したかを特定)
- `trafficSourceType`(流入元 — Browse / Search / Suggested / External 等)
- `viewerAgeGroup`, `viewerGender`(視聴者属性、しきい値次第で取得可)
- `deviceType`, `country`(必要に応じて)

### 認証スコープ

OAuth スコープに以下を追加(ADR-0012 のトークン保管に統合):

- `https://www.googleapis.com/auth/youtube.upload`(投稿用、既存)
- `https://www.googleapis.com/auth/youtube`(動画管理)
- `https://www.googleapis.com/auth/yt-analytics.readonly`(Analytics API、追加)

### 取得タイミング

- 週次サイクル(ADR-0006)で過去 7 日分の集計を取得
- Analytics API は **データ準備遅延が数時間〜1日** あるため、当日のデータは含めない設計(D-1 までを対象とする等)
- 取得結果は PostgreSQL の専用テーブル(時系列、`video_id × date` を主キー)に保存

### ライブラリ

- `google-api-python-client`(Data API、Analytics API 両方サポート)
- `google-auth` / `google-auth-oauthlib`(OAuth フロー)

## 結果

### 良い影響

- retention カーブ・流入元・watch time を LLM 入力に渡せる → 改善計画の精度が上がる
- 動機 B(観察データ蓄積)・動機 C(マネタイズ最適化)の両方を満たす
- Data API と同じ Python クライアントで完結、追加ランタイム不要

### 悪い影響・トレードオフ

- OAuth スコープが増える(初回認可時にユーザー同意ダイアログが拡張)
  - 影響軽微: 1人運用で初回のみ
- Analytics API は別 quota 管理が必要
  - 緩和: 週1回のバッチ取得なので余裕
- データ準備遅延を考慮した日付指定が必要
  - 緩和: D-1 までを対象とする実装ルールを明文化

### 受容したリスク

- 動画数が少ない初期は視聴者属性等の集計が **しきい値で抑制** されて取得できない場合がある
  - 対策: 取得失敗を許容するロジック、null/empty で改善計画 LLM に渡し、推論側でハンドリング

## 検討した代替案

- **Data API のみ:** 視聴数・コメント本文止まりで改善計画が表面的になる。不採用。
- **Data API + Analytics + Reporting API:** Reporting API はバルク CSV、本規模にはオーバースペック。将来規模拡大時に検討。
- **WebFetch / スクレイピング:** ToS 違反、不安定、保守不可能。不採用。

## 関連

- ADR-0006: 日次/週次サイクル
- ADR-0010: PostgreSQL(集計テーブル保存先)
- ADR-0012: OAuth トークン保管
- ADR-0020: 投稿(Data API videos.insert)
- ../requirements.md §運用フロー §機能要件 6.8
- YouTube Analytics API: <https://developers.google.com/youtube/analytics>
