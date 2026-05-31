# Screen Specification — 管理 UI 画面仕様

> Next.js (App Router) で実装する LAN 内 admin UI(ADR-0001, ADR-0013)。 全 10 画面。 ADR / spec.md / data-model.md / contracts/backend-api.yaml と整合させる正本仕様。

## 0. 全体方針

### 対象 / 前提

- **単一ユーザー(seita)前提**(spec.md Assumptions)。 admin ロール分離やマルチユーザー UI 要素は禁止
- **LAN 内 Basic 認証**(ADR-0013)。 ヘッダー右上は「admin」固定表示 + ログアウトボタン
- **デザインシステム**: Synthetix Vibe(Neon-Noir / Glassmorphism / Electric Purple + Pulse Red / Inter + Geist mono / Deep-space dark)
- **言語**: UI ラベルは日本語(技術識別子・モデル名・ジャンル英名は英語維持)
- **解像度**: デスクトップ 1280-2560 幅、 mobile 対応は範囲外

### 画面一覧(10 画面、 全 admin)

| # | 画面 | path | 対応 User Story |
| --- | --- | --- | --- |
| 1 | ダッシュボード | `/` | 横断的(全 US の概況) |
| 2 | プラン一覧 | `/plans` | US1, US3 |
| 3 | プラン詳細 | `/plans/[id]` | US1, US3 |
| 4 | Dryrun 審査 | `/dryrun` | US2 |
| 5 | スケジューラ制御 | `/scheduler` | US1, US4 |
| 6 | 分析 | `/analytics` | US3 |
| 7 | LLM プロバイダ設定 | `/llm` | US5 |
| 8 | ジョブ進捗 | `/jobs` | US6 |
| 9 | プロンプト管理 | `/prompts` | (補助) |
| 10 | ジャンル管理 | `/genres` | US3, FR-037/038 |

**※ MVP 完了チェックは画面化しない**。 ADR-0035 の checklist は本開発作業の「投稿モード移行可否」判定基準であり、 製品機能ではない。 `GET /mvp-check` API は backend に実装するが、 結果は CLI または Dashboard の隅に内包表示する程度に留める。

## 1. 共通レイアウト

### サイドバー(canonical、 全画面共通)

固定幅 260px、 左固定。 以下の **10 項目固定**(他項目の追加禁止):

```text
[ロゴ: YMG]

▸ ダッシュボード     (path: /)
▸ プラン              (path: /plans)
▸ Dryrun 審査         (path: /dryrun)
▸ スケジューラ        (path: /scheduler)
▸ 分析                (path: /analytics)
▸ ジョブ進捗          (path: /jobs)
▸ LLM                 (path: /llm)
▸ プロンプト          (path: /prompts)
▸ ジャンル            (path: /genres)
[セパレータ]
▸ 設定                (path: /settings、 将来用、 初期は無効化)

[底部] admin / ログアウト
```

- アイコン + ラベル両表示(アイコンのみは禁止、 可読性確保)
- active 項目: 左に Electric Purple 縦バー + 白文字 / inactive: slate-400 文字
- **「投稿」「Posts」項目を含めない**(現状の各画面で混入している場合は除去)
- **「新規ジョブ作成」「サポート」「ログアウト(専用項目)」「Settings(別表記)」等の追加禁止**(現状の分析画面・ジャンル管理画面で違反あり)

### ヘッダー(canonical、 全画面共通)

- 上部固定、 高さ 64px、 backdrop-blur
- 左: 画面タイトル(`h1.headline-md`)+ サブテキスト(`body-sm` opacity 60%)
- 右: 通知ベル + 設定アイコン + 「admin」表示(アバター画像なし、 単一ユーザーなのでアバター不要)
- パンくず: 詳細画面のみ(例: 「プラン › 2026-05-27 日次」)
- **複数ユーザー名(admin_suzuki / admin_tanaka / Master Admin 等)は禁止**

### 認証 / セッション関連

- ログイン UI は不要(Basic 認証は HTTP ヘッダーで完結)
- セッション失効時は backend が 401 返却 → frontend は full reload
- 「Active Session」「ACTIVE SESSION」等の表示は不要

### 仕様外要素(全画面共通で禁止)

- ❌ オーディオプレイヤー UI(楽曲配信プロダクトではない)
- ❌ チーム / 組織 / ワークスペース概念
- ❌ サブスクリプション / 課金 / プラン(PRO PLAN 等)バッジ
- ❌ Cluster / Node 識別子(単一マシン構成)
- ❌ 「メッセージ」「チャット」「コメント返信」等のソーシャル UI
- ❌ 公開ページ的なマーケティングセクション

## 2. 画面別仕様

### ① ダッシュボード(`/`)

**目的**: 横断的な状態確認のホーム画面。

#### 必須要素

- 上段 KPI カード 4 枚(横並び):
  - スケジューラ状態(`停止中` / `稼働中` バッジ + Enable トグル + 次回発火時刻)
  - LLM プロバイダ(`provider` / `model` / `auth_mode` + 「切替」リンク → `/llm`)
  - 月予算(円形プログレス `$使用 / $予算`、 50/80/100% 閾値ドット)
  - GPU ワーカー(ステータスドット + VRAM 使用率バー + ロード済モデル pills)
- 中段 2 カラム:
  - 「直近のジョブ」リスト 5 件(ジャンルカラーチップ + ステップ名(日本語: 楽曲生成 / AcoustID 検査 / 画像生成 / 動画合成 / アップロード)+ 状態 + 経過時間)
  - 「直近の投稿」グリッド 4 件(サムネ + 日本語サブタイト + ジャンル英名バッジ + retention% + 経過時間)
- 下段 MVP 完了状況の **小さなインジケーター**(`x / 6 完了` のサマリのみ)→ 詳細は `/mvp-check`(本仕様外、 内部 endpoint)に遷移

#### 除外

- ❌ オーディオプレイヤー
- ❌ 大型バナー / ヒーローセクション

---

### ② プラン一覧(`/plans`)

**目的**: DailyPlan / WeeklyPlan の生成・承認・状態管理。

#### 必須要素

- ヘッダーボタン: 「+ プランを生成」(Electric Purple 塗り)
- フィルタータブ: 「全て / 日次 / 週次」
- ステータス chip フィルター: `生成済 / 承認済 / 実行中 / 完了 / 失敗`
- 検索ボックス: 「プラン ID やジャンルで検索」
- プランカード(縦並びリスト):
  - 左に cycle ラベル `日次` / `週次` + 対象日
  - 中央にタイトル(`ジャンル × サブタイト` 概要)+ rationale 1 行抜粋
  - 右に model 名 + cost + アクションボタン(`詳細を見る / 承認 / 再実行`)
- ページネーション or 「過去のプランを追加で読み込む」

#### ルール

- 失敗カードのエラー表示は **日本語化必須**: `Generation Terminated` → `生成失敗(再実行可)` 等
- カードの cycle カラー: 日次=Cyan、 週次=Electric Purple

---

### ③ プラン詳細(`/plans/[id]`)

**目的**: 個別 DailyPlan / WeeklyPlan の内訳確認 + 承認。

#### 必須要素

- パンくず: `プラン › {target_date} {cycle}`
- ヘッダー: 「{target_date} {cycle}プラン」(例: 「2026-05-27 日次プラン」)+ ステータスバッジ + 「再生成 / 承認」ボタン
- **ヘッダーサブテキストも日本語化**: 例「YouTube 視聴維持率の最大化を狙う日次自動投稿計画」(英語 boilerplate 禁止)
- メタ情報グリッド(3 列): `サイクル / 対象日(or 対象週) / LLM プロバイダ / モデル / プロンプトバージョン / コスト / 生成日時 / 承認日時`
- 「立案理由(Rationale)」セクション(全文表示、 引用ブロック)
- 「投稿(Posts)」セクション:
  - DailyPlan の場合: 1〜2 ポストカード
  - 各ポストカード: サムネ + 最終タイトル + ジャンルバッジ + ステータス + 投稿予定時刻 + `mood / bpm_range / visual_direction / title_directive(コード形式) / description_directive`
- 「参照メトリクス(Referenced Metrics)」: `window_days / sample_size / top_metrics_summary`(spec ADR-0032 の `ReferencedMetrics` と整合)+「スナップショットを表示」リンク

#### 禁止

- ❌ 独自指標(`Retent-Score` 等)。 ADR-0032 の Pydantic フィールド(`retention_pct` / `expected_views_24h` 等)のみ表示
- ❌ 英語サブテキスト(`Daily automation blueprint...` 等)

---

### ④ Dryrun 審査(`/dryrun`)

**目的**: dryrun_outputs の承認 / 却下ワークフロー。

#### 必須要素

- ヘッダー: 「Dryrun 審査」 + 状態タブ「保留中(N) / 承認済 / 却下 / 自動失効 / 投稿済」
- `dryrun_enabled` トグル + 注意文「投稿モード切替には audit_log 記録 + MVP 完了基準クリアが必要」
- 左カラム(320px): pending 一覧 3〜N 件、 active カードは Electric Purple ボーダー
  - 各カード: サムネ + 日本語タイトル + ジャンルバッジ + 「N 分前作成」「残り N 日 N 時間で失効」
- 右カラム(残り幅): 動画プレイヤー(16:9 大型)+ 波形 visualizer + 経過時間 mono
  - チャプター(6 件、 **英 / 日併記必須**、 ADR-0034 と整合): `00:00 Lo-Fi Beats / 雨の静寂`
  - システムメタデータ: `post_id / plan_id / VRAM ピーク / 指紋ハッシュ`
  - 「音声トラック(6 件全 ✓ CLEAR)」緑チェック
  - 下端: 「× 却下」(赤 ghost)+ 「✓ 承認して投稿」(Electric Purple 塗り)
  - 却下時 reason テキストエリア(min 4 字、 改善計画 LLM 入力に活用)

#### 禁止

- ❌ Chapter 名が英語のみ → **必ず英 / 日併記**
- ❌ 投稿モード切替操作はこの画面ではなく `/scheduler` へ誘導

---

### ⑤ スケジューラ制御(`/scheduler`)

**目的**: scheduler ON/OFF + panic-stop。

#### 必須要素

- 上段大トグル: 「スケジューラ」+ 「停止中 / 稼働中」状態バッジ + Enable トグル + 「マシン再起動後は手動有効化が必要(ADR-0031)」+ 次回発火時刻
- 中段「実行中のジョブ」: 進行中ジョブリスト + 「全て一時停止 / 全て再開」
- システムステータス: 稼働率 / ヘルスチェック
- 下段「緊急停止(Panic Stop)」赤ボーダーゾーン:
  - 警告文 + 「期間(時間)」入力(デフォルト 24)
  - 直近 N 時間の動画プレビュー(サムネ + youtube_video_id + 現在の privacy_status + チェックボックス)
  - 「Panic Stop を実行」赤ボタン → 確認モーダル
- 「実行履歴(Audit Log)」表: `実行日時 / 実行者 / 対象件数 / 状態`

#### ルール

- 実行者は常に「seita」固定(`admin_suzuki / admin_tanaka / admin_root / system_auto_quota` 等は禁止)
- 実行者列は実質情報量がないため、 単一ユーザー前提なら列削除でも可

---

### ⑥ 分析(`/analytics`)

**目的**: YouTube Analytics 取得結果の可視化。

#### 必須要素

- ヘッダー: 「分析」 + 期間セレクタ「過去 14日 / 30日 / 90日」+ ジャンルフィルター chip 6 つ(英ジャンル名)
- KPI 4 カード: 総再生回数 / 平均視聴維持率 / 投稿動画数 / 合計視聴時間(全て前期比表示)
- 中段:
  - 視聴維持率の推移(ジャンル別折れ線、 凡例 6 ジャンル)
  - 再生回数上位 5 動画リスト
- 下段:
  - ジャンル別パフォーマンス棒グラフ(6 ジャンル × 平均 retention)
  - 実験スロットの状態カード(`Future Garage` 採用推奨 / `Vaporwave` 削除推奨 等、 FR-037/038 と整合)
- 最下段「トラフィックソース」円グラフ + 横棒

#### 禁止

- ❌ サイドバー追加項目(「+ 新規ジョブ作成」「サポート」「ログアウト」)
- ❌ 凡例から `Lo-Fi` 等 6 ジャンル中いずれかが欠落することは禁止(常に全 6 表示)

---

### ⑦ LLM プロバイダ設定(`/llm`)

**目的**: provider 切替 + 月次コスト + キャッシュ統計。

#### 必須要素

- 上段「現在のプロバイダ」カード(大):
  - Provider 名 + ロゴ + `モデル / 認証モード / 接続中` ステータス + ヘルスチェック時刻
- 「プロバイダ切替」フォーム:
  - Provider radio(OpenAI / Anthropic / Ollama)
  - 認証モード select(api_key / codex_oauth、 Anthropic 選択時は subscription を起動時拒否注意)
  - モデル select
  - 「適用」ボタン + 「切替は audit_log に記録されます」注意
- 「利用可能なプロバイダ」3 カード(現在 active 除く):
  - 各カードに provider 説明 + 注意バッジ(Anthropic sub 禁止 / Codex OAuth グレー)+ 「切替」ボタン
- 下段「月次利用量(YYYY-MM)」: 円形プログレス + 閾値 + プロバイダ別棒グラフ + 直近 7 日 token sparkline + 「詳細な usage_log を表示」リンク
- 「プロンプトキャッシュ」: Anthropic ヒット率 + OpenAI Responses 自動キャッシュ率 + 過去推移 sparkline

#### **必須除外(Critical 違反現存)**

- ❌ **オーディオプレイヤー UI(「Synthetix Drift」等)を絶対に表示しない**。 LLM 設定画面に音楽プレイヤーは不要

---

### ⑧ ジョブ進捗(`/jobs`)

**目的**: SSE で各ステップの実行状況をリアルタイム可視化。

#### 必須要素

- ヘッダー: 「ジョブ進捗」+ LIVE インジケータ(緑脈動)+ 「接続中: SSE /jobs/stream」mono + 「全て展開 / 折り畳む」
- グリッド: ジャンル列 × ステップ行
  - 列: 稼働中ジャンル(最大 6 列)
  - 行: `楽曲生成 / AcoustID 検査 / 画像生成 / サムネ合成 / 動画合成 / タイトル整形 / 投稿前検証 / アップロード`
- 各セルは小ステータスカード: 待機 / 実行中(脈動 + 進捗バー)/ 完了(緑チェック + 所要時間)/ 失敗(赤バツ + エラーカテゴリ pill)
- 右ドロワー(選択中ジョブの詳細): コンテキスト / ステータス / VRAM 使用 / ログテール(コードブロック)/ 関連リソースリンク / 「ジョブをキャンセル」赤
- フッターバー: 「今日完走: N / 失敗: N / 平均所要: M 分 S 秒」

#### 禁止

- ❌ ヘッダーに `SYSTEM NODE: CLUSTER-A-TYO-02` 等のクラスタ識別子(単一マシン構成のため不要)
- ❌ マルチノード前提の集計表示

---

### ⑨ プロンプト管理(`/prompts`)

**目的**: バージョン管理されたプロンプトの編集 / A/B 試行 / ロールバック。

#### 必須要素

- ヘッダー: 「プロンプト管理」 + 「+ 新規バージョン作成」
- カテゴリタブ: `立案 LLM(planner) / 仕上げ LLM(finisher) / タイトル(title) / 説明文(description)`
- 左カラム: バージョン履歴(`system_v3(下書き) / system_v2(本番) / system_v1`)
- 中央カラム: コードエディタ(Markdown + プレビュー切替)、 directive `{{var}}` を cyan pill、 `{{自由文}}` を pink pill にハイライト
- 右カラム: ステータス(使用中 / ロールバック / 下書きを試す)+ 最終更新(seita)+ 使用 plan 数 / 平均 cost / retention 相関 + 「実 plan で試行」+ A/B テスト枠 + 「Diff with v1」
- 下段「プロンプト差分(Diff)」unified diff(+/- 色付き)

#### ルール

- 更新者は常に「seita」(System Administrator / Admin User 等は禁止)

---

### ⑩ ジャンル管理(`/genres`)

**目的**: 6 ジャンル(主力 3 / 拡張 2 / 実験 1)の運用 + 新ジャンル候補管理。

#### 必須要素

- ヘッダー: 「ジャンル管理」 + サブテキスト「主力 / 拡張 / 実験ジャンルの運用ステータス」+ 「+ 新規ジャンル追加(dryrun 経由)」
- 状態タブ: 「全 6 件 / 主力(3) / 拡張(2) / 実験(1) / 無効」 + 検索ボックス
- テーブル: `ジャンル / role / BPM 範囲 / 投稿数 / 平均 retention / 主力比 / 推奨 / アクション`
  - 各行: Lo-Fi Hip Hop(主力) / Chillhop(主力) / Ambient(主力) / Synthwave(拡張) / Piano Solo(拡張) / Future Garage(実験 + 採用推奨バッジ + 「主力に昇格」)
- 下段 2 カラム:
  - 「新規ジャンル候補(LLM 提案)」: Confidence 付き 3 件、 各カードに「dryrun に投入」ボタン
  - 「ジャンル role 遷移履歴」タイムライン + 「audit_log 全件を表示」リンク

#### 禁止

- ❌ サイドバーに `Settings` 項目追加(canonical サイドバー §1 と整合)
- ❌ role pill 配色のジャンル混同(主力=Electric Purple、 拡張=Cyan、 実験=Pulse Red 強調)

---

## 3. データ命名規則(全画面共通)

| 概念 | 表記 |
| --- | --- |
| 投稿者 / 実行者 | `seita`(単一ユーザー、 admin 表記は UI ヘッダー右上のみ) |
| youtube_video_id | 11 字 Base64 形式(実物 ID)、 デモなら `dQw4w9WgXcQ` 形式 |
| post_id / plan_id / gpu_job_id | UUID v7 形式(モックは略形でも可) |
| ジャンル名 | 英語表記(辞書照合と整合): `Lo-Fi Hip Hop / Chillhop / Ambient / Synthwave / Piano Solo / Future Garage` |
| 日本語サブタイト | カタカナ + 漢字 + ひらがな + 半角絵文字 1 個まで、 12 字以内(ADR-0034) |
| 時間表示(経過) | `N 分前 / N 時間前 / N 日前` |
| 時刻(JST) | `YYYY-MM-DD HH:MM JST` |
| 通貨 | USD($)+ 小数 2 桁、 円表記なし |
| 視聴維持率 | `XX.X%` 形式、 「視聴維持率」または「retention」併記可 |

## 4. アクセス制御 / 認証

- 全画面 Basic 認証必須(`/health` のみ非認証)
- セッション失効時の動作: 401 → frontend full reload(ログイン画面なし、 ブラウザ標準 Basic auth ダイアログ表示)
- 「ログアウト」リンクはサイドバー底部、 クリックで Basic auth セッション破棄(ブラウザ動作依存、 ベストエフォート)
- audit_log への記録対象操作: scheduler ON/OFF、 dryrun_enabled 切替、 plan 承認 / 拒否、 動画 privacy 変更、 ジャンル role 変更、 panic-stop 実行、 prompt version 切替、 LLM provider 切替

## 5. レスポンシブ対応(範囲外)

- 初期はデスクトップ 1280px 以上のみ対応
- mobile / tablet 対応はスコープ外(spec.md Out-of-Scope と整合)

## 6. モックアップの位置づけと既知の差分(2026-06-01 確定)

### 決定: モックは「ビジュアル ideation」で凍結、 本仕様が唯一の正典

stitch のモックアップは Synthetix Vibe の視覚言語とレイアウトを確立する目的を果たしたため、 **2026-06-01 時点で「ビジュアル ideation 完了」として凍結**する。 以後の実装(Next.js)は **本 `screen-spec.md` の §1〜§5 を唯一の正(source of truth)** とし、 stitch モックの画素・文言をそのまま写経しない。

理由: stitch `edit_screens` の dom_operations は API 上「適用済」を返すが、 **保存先 HTML に永続化されない**ことが検証で確定(2026-06-01、 編集 6 日後の新鮮な HTML + screenshot 目視で確認)。 逐次編集での修正は信頼できないため、 モックを追従修正する投資は打ち切る。

### 検証で確定した「モックに残る差分」(実装時は本仕様 §2 に従い、 以下はモックを信用しないこと)

| 画面 | モックに残存する誤り | 実装時の正(本仕様の該当節) |
| --- | --- | --- |
| ② プラン一覧 | 失敗カードに「Generation Terminated」 | 「生成失敗(再実行可)」(§2 ②ルール) |
| ③ プラン詳細 | 英語サブテキスト / 独自指標 `Retent-Score` | 日本語サブ + ADR-0032 フィールド(§2 ③禁止) |
| ④ Dryrun 審査 | Chapter 名が英語のみ(Lo-Fi Beats 等) | 英 / 日併記(§2 ④必須) |
| ⑤ スケジューラ | `admin_suzuki / admin_tanaka` 複数管理者名 | `seita` 単一(§2 ⑤ルール) |
| ⑥ 分析 | サイドバー「新規ジョブ作成 / サポート」 | canonical サイドバー(§1) |
| ⑧ ジョブ進捗 | (該当文言「単一ノード」未挿入) | クラスタ識別子なし・単一マシン前提(§2 ⑧禁止) |
| ⑨ プロンプト管理 | 更新者「System Administrator / Admin User」 | `seita`(§2 ⑨ルール) |
| ⑩ ジャンル管理 | サイドバーに `Settings` 項目 | canonical サイドバー(§1) |
| 全画面 | サイドバー見出しが画面ごとに不統一(「YMG Automation V3.0」「YMG / AI Music Engine」)/ 「MVP チェック」項目残存 | canonical サイドバー(§1)、 「MVP チェック」は画面化しない(§0) |

### 唯一クリーンなモック画面(参考実装に使える)

| 画面 | screen ID | 状態 |
| --- | --- | --- |
| ⑦ LLM プロバイダ設定(プレイヤー削除版) | `d7268abe0998489ebfc0a44e6abaac16` | ✅ オーディオプレイヤー除去済 / 3 provider カード正。 ただしサイドバーの「MVP チェック」残存は §0 に従い実装しない |

### モック側の手動クリーンアップ(任意、 実装には影響しない)

- 旧 LLM 版(プレイヤーあり)`e9db218057b24c39afb2e1fb69799600` と MVP チェック画面 `af5aeb8264d54d359603833f51cdf2d8` は stitch UI 上に残存。 MCP に削除 API がないため手動削除は任意。 実装は本仕様準拠で行うため放置しても支障なし。

## 7. 関連

- [spec.md](./spec.md) - User Stories と Functional Requirements
- [plan.md](./plan.md) - Project Structure と Phase Plan
- [data-model.md](./data-model.md) - DB スキーマ
- [contracts/backend-api.yaml](./contracts/backend-api.yaml) - REST API
- [adr/](./adr/) - 全 35 ADR
- stitch project: `14646950244569127795`(Synthetix Vibe design system 適用)
