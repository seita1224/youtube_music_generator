# ADR-0033: 初期ジャンル候補と改善計画 LLM のプロンプト構造

- **ステータス:** Accepted
- **日付:** 2026-05-26
- **決定者:** @seita
- **タグ:** ml / policy

## 背景

ADR-0032 で改善計画 LLM の出力スキーマが確定した。 これを実運用するために 2 点を決める必要があった。

1. `genres` 辞書の初期投入セット(ADR-0032 の `genre` 辞書照合の対象)
2. 改善計画 LLM に投げるプロンプトの構造

前提:

- ACE-Step はインストゥルメンタル(ボーカル無し)系で品質が安定
- ADR-0004 で月間投稿数 30〜60 本、 ジャンル数を絞らないと retention 評価のサンプルが分散する
- 動機 B(YouTube 観察) + C(マネタイズ)で、 ある程度需要のある帯を狙う必要がある
- ADR-0024 のコストトラッキング下で、 prompt caching の活用が重要

## 決定

### (1) 初期ジャンル辞書 = 6 ジャンル

`genres` テーブルに以下 6 件を初期投入する。

| genre | 想定 BPM | 特徴 | 役割 |
|---|---|---|---|
| `lo-fi hip-hop` | 70-90 | chill / nostalgic | 主力 |
| `chillhop` | 80-95 | lo-fi 隣接、 jazz 寄り | 主力 |
| `ambient` | 任意 | 構造単純、 睡眠 / 瞑想 | 主力(ACE-Step 得意) |
| `synthwave` | 80-110 | retro / 80s | 拡張枠 |
| `piano solo` | 任意 | リラックス / 勉強 | 拡張枠 |
| `future garage` | 130-140 | atmospheric | 実験枠(競合薄) |

選定ロジック:

- ACE-Step が安定して出力できる帯を中心(歌モノは除外)
- YouTube の作業用 BGM 視聴層が確立されている帯
- 6 ジャンルなら月 30〜60 本投稿で各 5〜10 本、 retention 評価が辛うじて意味を持つ
- `future garage` を 1 つ入れて experiment_slot(ADR-0032)の初期投入先とする
- 新ジャンル追加は ADR-0032 の運用フローに従う(dryrun 経由の承認)

### (2) プロンプト構造 = System キャッシュ + few-shot 1 例 + 集計サマリ

##### 構造方針

- **System prompt(prompt caching 対象 — 不変部分):**
  - 役割定義
  - 目的(retention + watch time + 探索/活用バランス)
  - ジャンル辞書(6 ジャンルの特徴説明込み)
  - directive 記法(`{{var}}` と `{{自由文}}`)の説明
  - 出力スキーマの説明(Pydantic structured output 併用)
  - 制約一覧(title 60 字以内、mood 4 字以上、visual_direction 10 字以上、 optional は None で良い、 rationale 50 字以上の具体)
  - rationale の書き方ガイド(数値や過去動画への参照を含む)
  - **手書き few-shot サンプル 1 例**

- **User prompt(都度生成 — キャッシュ対象外):**
  - target_date / target_week_start
  - 過去 N 日の analytics 集計サマリ(表形式)
  - 直近 plan 履歴(rationale 込みで N=5 程度)
  - WeeklyPlan 入力時のみ: 直前週の方針(avoid_genres / experiment_slots)

##### few-shot は初期手書き 1 例で開始

- 運用 0〜1 ヶ月は実 plan が存在しないため、 動的選別(retention 上位の plan を引っ張る)は不可
- 何もなしだと directive 形式や rationale の粒度を外しやすい
- 手書き 1 例で粒度を教える、 token コストも軽い
- **運用 1 ヶ月後**に「retention 上位の plan を自動 few-shot に切り替え」を再評価

##### analytics は集計サマリ寄り、 生データは snapshot に保存

- 入力サイズ最適化: 各動画の retention / views / 公開日 / ジャンル を表形式で渡す
- 生 JSON は plan_metric_snapshot(ADR-0032)に保存して再現性確保
- LLM が「特定動画の詳細を見たい」と判断した場合は、 別の tool call で取得できる枠組みを残す(将来拡張)

### (3) プロンプト草案(System 部分の骨格)

実装は別途プロンプトファイルで管理(`prompts/planner/system_v1.md` 等)。 ここでは骨格のみ示す。

```text
あなたは YouTube 作業用 BGM チャンネルの改善計画担当です。
目的は retention と watch time の最大化、 およびジャンル探索と活用のバランスです。

## 使用可能ジャンル(辞書外は禁止)
- lo-fi hip-hop: BPM 70-90、 chill / nostalgic、 主力枠
- chillhop: BPM 80-95、 lo-fi 隣接、 jazz 寄り
- ambient: BPM 任意、 構造単純、 睡眠 / 瞑想用
- synthwave: BPM 80-110、 retro / 80s
- piano solo: BPM 任意、 リラックス / 勉強用
- future garage: BPM 130-140、 atmospheric、 実験枠

## 出力形式
DailyPlan または WeeklyPlan(JSON)。 cycle フィールドで区別。
directive フィールドでは以下の記法を使う:
- {{genre}} {{duration}}: システム変数の埋め込み(波括弧内が小文字 ASCII のみは変数)
- {{8語以内の雨夜サブタイトル}}: 自由文の生成指示(日本語 or 説明文を含む場合)

## 制約
- DailyPost.title_directive: 最終生成タイトルが 60 字以内に収まる構造にする
- DailyPost.mood: 4 文字以上
- DailyPost.visual_direction: 10 文字以上、 SDXL に通る具体性
- DailyPost.bpm_range: ジャンルの想定範囲内
- 不明な optional フィールドは None / 省略を返す(架空の数字を埋めない)
- rationale: 数値や過去動画への参照を含む 50 字以上の具体的な分析

## 良い例
[手書きの DailyPlan サンプル 1 件、 directive 記法と rationale 粒度を示す]
```

### (4) プロバイダ別のキャッシュ実装

- **Anthropic API**: `cache_control: { type: "ephemeral" }` を system block に付与
- **OpenAI Responses API**: prompt caching は自動(prefix 1024 token 以上で発火)
- **Ollama**: キャッシュは効かない、 ローカル運用は dryrun 限定とする

ADR-0019(LLMProvider 抽象化)の interface で provider 差を吸収する。

### (5) プロンプトのバージョン管理

- `prompts/planner/system_v1.md` / `prompts/planner/system_v2.md` のようにバージョン番号付きで保存
- 使用バージョンを `plans.llm_prompt_version` カラムに記録
- 後で「v1 と v2 でどちらが retention 高い plan を出したか」を比較可能にする

## 結果

### 良い影響

- 初期 6 ジャンルに絞ることで、 retention 評価のサンプル数が確保できる
- prompt caching で改善計画 LLM のコスト(unit cost)を約 90% 削減(Anthropic 場合)
- few-shot 1 例で directive 記法 / rationale 粒度を効率的に教えられる
- プロンプトのバージョン管理により、 後で系統的な A/B 比較が可能
- ジャンル辞書外を validator で拒否することで、 LLM の hallucination による「謎ジャンル投稿」を完全に防げる
- experiment_slot 経由で新ジャンルの探索フローが構造的に確保される

### 悪い影響・トレードオフ

- 6 ジャンルしか選べないため、 初期の表現幅が制限される
- プロンプトを丁寧に書く分、 system token 数が増える(キャッシュ前提なら問題なし)
- few-shot 1 例の書き方が悪いと、 LLM がそれを盲目的に真似てくる(リスク低だが、 たまに確認する)
- プロンプトバージョン管理のメンテナンスコストが小さく発生

### 受容したリスク

- 初期 6 ジャンルの選定は YouTube アルゴリズムや市場動向で陳腐化する。 WeeklyPlan の experiment_slot を実運用してジャンル追加を促す
- 集計サマリ方式は LLM が「特定動画の詳細を見たい」場合に情報不足になる可能性。 必要が見えたら tool call 拡張を行う
- few-shot 1 例の影響は token 化バイアスを生む。 数ヶ月後に few-shot ありなしの比較実験を行う

## 検討した代替案

### ジャンル数

- **代替案 B: lo-fi 系のみ 2〜3 ジャンル:** retention 評価のサンプル数は確保できるが、 視聴者層拡大とジャンル探索の余地がなくなる。 不採用
- **代替案 C: 広く 10 ジャンル全部:** サンプル数が分散しすぎて月 30〜60 本では何も分からない。 不採用

### プロンプト構造

- **System に全部詰める + User は空に近い:** prompt caching が効くが、 User 側の analytics 反映が困難。 不採用
- **System 最小 + User に全部:** prompt caching が効かず、 token コストが線形に増える。 不採用

### few-shot

- **B1: few-shot なし:** directive 記法を外す失敗が増える。 不採用
- **B3: 動的選別:** 初期は plan が存在しないため不可。 運用 1 ヶ月後の選択肢として残す

### analytics 渡し方

- **C1: 生 JSON:** token 浪費 + LLM の認知資源を数字読みに取られる。 不採用
- **C3: ハイブリッド:** 初期にはオーバーキル、 必要が見えたら拡張

## 関連

- ADR-0002: マスター LLM クラウド + 抽象化
- ADR-0004: 1日1〜2本投稿
- ADR-0017: directive parser
- ADR-0018: Pydantic structured output
- ADR-0019: LLMProvider 抽象化
- ADR-0024: LLM コストトラッキング
- ADR-0032: 改善計画 LLM の出力スキーマ
