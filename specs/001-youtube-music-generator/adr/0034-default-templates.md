# ADR-0034: タイトル / 説明文 / サムネのデフォルトテンプレ

- **ステータス:** Accepted
- **日付:** 2026-05-26
- **決定者:** @seita
- **タグ:** media / ml / policy

## 背景

ADR-0017(directive parser)、 ADR-0032(改善計画 LLM スキーマ)、 ADR-0033(初期 6 ジャンル)を受けて、 各ジャンルに対応する title / description / thumbnail のデフォルトテンプレを確定する必要があった。

前提:

- 6 ジャンル: lo-fi hip-hop / chillhop / ambient / synthwave / piano solo / future garage
- 30 分動画 × 5 分 6 トラック構成(ADR-0003)
- AI 開示必須(ADR-0020、 `containsSyntheticMedia=true`)
- バイリンガル方針(日本語 + 英語、 タイトル / 説明文両方)
- SDXL によるサムネ背景生成、 ACE-Step による音楽生成

## 決定

### (1) タイトルテンプレ = 英語ジャンル先頭 + 日本語サブタイト後置

形式: `{英語ジャンル名} {duration}min | {12字以内の日本語サブタイト} {絵文字1個まで}`

- 英語ジャンル名 + 時間を先頭固定 → 海外 SEO + 日本人の作業 BGM 検索の両方をカバー
- 日本語サブタイト 12 字以内 → 表示幅が英語比 2 倍のため文字数半減
- 絵文字 1 個まで(任意)→ スパム判定回避、 mobile 文字数節約
- 合計 30 文字前後、 60 字制限の半分以下

##### ジャンル別テンプレ

```yaml
# templates/title/lo-fi-hip-hop.yaml
template: "Lo-Fi Hip Hop {{duration}}min | {{12字以内の日本語サブタイト}} {{絵文字1個まで}}"
example:  "Lo-Fi Hip Hop 30min | 雨夜のラウンジ 🌧"

# templates/title/chillhop.yaml
template: "Chillhop {{duration}}min | {{12字以内の日本語サブタイト}}"
example:  "Chillhop 30min | 朝のカフェ Jazz"

# templates/title/ambient.yaml
template: "Ambient {{duration}}min | {{12字以内の日本語サブタイト}}"
example:  "Ambient 30min | 深い眠りへの誘い"

# templates/title/synthwave.yaml
template: "Synthwave {{duration}}min | {{12字以内の日本語サブタイト}}"
example:  "Synthwave 30min | 80年代の夜景ドライブ"

# templates/title/piano-solo.yaml
template: "Piano Solo {{duration}}min | {{12字以内の日本語サブタイト}}"
example:  "Piano Solo 30min | 深夜の独白"

# templates/title/future-garage.yaml
template: "Future Garage {{duration}}min | {{12字以内の日本語サブタイト}}"
example:  "Future Garage 30min | 水中の東京"
```

### (2) 説明文テンプレ = バイリンガル + 動的シーン描写 + 動的チャプター + 静的 AI 開示

ブロック構成:

```text
{{120字以内の英語シーン描写}}
{{120字以内の日本語シーン描写}}

⏱ Chapters
00:00 {{Track1英タイトル}} / {{Track1日タイトル}}
05:00 {{Track2英タイトル}} / {{Track2日タイトル}}
10:00 {{Track3英タイトル}} / {{Track3日タイトル}}
15:00 {{Track4英タイトル}} / {{Track4日タイトル}}
20:00 {{Track5英タイトル}} / {{Track5日タイトル}}
25:00 {{Track6英タイトル}} / {{Track6日タイトル}}

🎧 About this video
This music and visuals are AI-generated using ACE-Step (Apache 2.0) and SDXL.
本動画の音楽および映像は AI(ACE-Step / SDXL)により自動生成されています。
All tracks are original generations and contain no third-party copyrighted material.
全楽曲は自動生成によるオリジナルで、 第三者の著作物は含まれません。

📺 More videos
Subscribe for daily lo-fi / chill / ambient mixes.
チャンネル登録で毎日新しい作業用 BGM をお届けします。

#{{ジャンルハッシュタグ1}} #{{ジャンルハッシュタグ2}} #{{シーンハッシュタグ}}
```

##### LLM 生成対象フィールド

- `{{120字以内の英語シーン描写}}`
- `{{120字以内の日本語シーン描写}}`
- `{{Track1〜6 英タイトル}}` (6 個、 各 20 字程度)
- `{{Track1〜6 日タイトル}}` (6 個、 各 10 字程度)
- `{{ジャンルハッシュタグ1〜2}}` (ジャンル辞書 + 派生)
- `{{シーンハッシュタグ}}` (LLM 生成、 mood ベース)

##### 設計のポイント

- 冒頭 200-300 字に SEO が効く → 英語 + 日本語のシーン描写を 2 文に
- チャプターは LLM 生成 + 5 分間隔(YouTube 自動チャプター対応、 視聴維持↑)
- AI 開示は **二か国語の固定文**(LLM に書かせない、 言い回しブレ防止、 訴訟リスク対応)
- Content ID 免責文を AI 開示と合体
- ハッシュタグ 3 個(ジャンル 2 + シーン 1)で title 直下表示を確保

### (3) サムネテンプレ = SDXL + Pillow オーバーレイ

##### 共通仕様

- 解像度 1280×720 (16:9 YouTube 標準)
- 背景 SDXL 生成 (`visual_direction` から)
- フォーマット JPEG quality 90 (YouTube 2MB 制限内)

##### レイアウト(全ジャンル共通)

```text
┌─────────────────────────────────────┐
│ [GENRE BADGE]                       │  左上: ジャンルバッジ (小, 半透明)
│                                     │
│         [SDXL 背景]                 │
│                                     │
│  ┌────────────────────────────┐    │
│  │ {{日本語サブタイト}}        │    │  中央下: 大見出し
│  │ {{ジャンル · 30 MIN}}       │    │  サブテキスト
│  └────────────────────────────┘    │
│                            30 MIN   │  右下: 時間
└─────────────────────────────────────┘
```

##### ジャンル別フォント / 配色

```yaml
# templates/thumbnail/lo-fi-hip-hop.yaml
font_primary: "Bebas Neue"
font_secondary: "Noto Sans JP"
color_text: "#FFFFFF"
color_outline: "#000000"
outline_width: 4
text_position: "center-bottom"
genre_badge_color: "#1A1A1A"

# templates/thumbnail/chillhop.yaml
font_primary: "Bebas Neue"
color_text: "#FFDC8A"
color_outline: "#3D2817"

# templates/thumbnail/ambient.yaml
font_primary: "Cormorant Garamond"
color_text: "#E8E8E8"
color_outline: "#1A1A2E"
text_position: "center"

# templates/thumbnail/synthwave.yaml
font_primary: "VT323"
color_text: "#FF006E"
color_outline: "#3A0CA3"
outline_width: 6

# templates/thumbnail/piano-solo.yaml
font_primary: "Playfair Display"
color_text: "#F8F8F0"
color_outline: "#2C2C2C"

# templates/thumbnail/future-garage.yaml
font_primary: "Space Grotesk"
color_text: "#A0D8EF"
color_outline: "#0A1929"
```

##### サムネテキスト

- メイン = タイトルと同じ日本語サブタイト(別呼び出しせず再利用)
- サブ = `{ジャンル英名} · 30 MIN`
- バッジ = ジャンル英名のみ(左上、 4-12 字)

##### 採用理由

- テキストなしサムネはクリック率が落ちる(作業用 BGM サムネはほぼ全部テキスト入り)
- SDXL は英字でもテキスト描画が不安定(`Lo-Fi` → `Lof-i` 等)
- Pillow で後付けすれば文字は完璧、 フォント / 色 / 位置を完全制御
- ジャンル別フォント / 配色でブランド統一感を作る

### (4) 仕上げ LLM コスト試算

1 動画あたり directive 評価 16 回(英描写 1 + 日描写 1 + トラック英 6 + トラック日 6 + ハッシュタグ 2)+ タイトル 1 = 17 回。

- Haiku 1 回 ≈ $0.0005
- 1 動画 ≈ $0.0085
- 月 30〜60 本 → 月 $0.25-0.50

ADR-0024 のコスト追跡で吸収可能、 budget alert の閾値設定対象外で良い。

### (5) テンプレファイル配置

```text
templates/
├── title/
│   ├── lo-fi-hip-hop.yaml
│   ├── chillhop.yaml
│   ├── ambient.yaml
│   ├── synthwave.yaml
│   ├── piano-solo.yaml
│   └── future-garage.yaml
├── description/
│   ├── _shared/
│   │   ├── ai_disclosure.txt          # 二か国語固定文
│   │   └── channel_promo.txt
│   └── default.yaml                    # ジャンル共通の基本構造
└── thumbnail/
    ├── _shared/
    │   ├── layout.json                 # ジャンル共通レイアウト
    │   └── badge.json                  # バッジ仕様
    ├── lo-fi-hip-hop.yaml
    ├── chillhop.yaml
    ├── ambient.yaml
    ├── synthwave.yaml
    ├── piano-solo.yaml
    └── future-garage.yaml
```

### (6) フォントライセンス

選定したフォントは全て商用利用可・改変不要のもの:

| フォント | ライセンス | 用途 |
|---|---|---|
| Bebas Neue | SIL OFL | lo-fi / chillhop |
| Cormorant Garamond | SIL OFL | ambient |
| VT323 | SIL OFL | synthwave |
| Playfair Display | SIL OFL | piano solo |
| Space Grotesk | SIL OFL | future garage |
| Noto Sans JP | SIL OFL | 全ジャンル日本語 |

全 SIL OFL ライセンス、 リポジトリに同梱して問題なし。

## 結果

### 良い影響

- 6 ジャンル全てにテンプレが揃い、 投稿パイプラインを自動実行可能
- バイリンガル化で日本語検索 + 国際 lo-fi 視聴層の両方にリーチ
- AI 開示が固定文 → 言い回しブレ防止、 訴訟リスク対応
- サムネのジャンル別フォント / 配色でブランド統一感を構築
- ハッシュタグ 3 個構成で YouTube の title 直下表示を活用
- Pillow オーバーレイで SDXL の弱点(テキスト描画)を完全に補填

### 悪い影響・トレードオフ

- ジャンル別テンプレが 18 ファイル(title 6 + thumbnail 6 + description shared)、 メンテナンス対象が増える
- 仕上げ LLM 呼び出しが 1 動画 17 回 → レイテンシ + 軽微なコスト
- Pillow フォント描画は OS のフォントレンダリングと差分が出る可能性 → 出力サンプルで初期確認が必要
- バイリンガル説明文は冗長感が出る可能性、 視聴者からのフィードバックで A/B 検討

### 受容したリスク

- バイリンガル説明文の機械翻訳感: 仕上げ LLM のプロンプトに NG 例 few-shot で抑制
- 同じ「作業用 BGM」語彙が並んで量産感が出るリスク: 過去 N 件の context を仕上げ LLM に渡し、 語彙バリエーション確保
- フォントレンダリングの細部 OS 差分: 初期に Ubuntu(host)で出力したサンプルを基準とする
- ジャンル追加時にサムネテンプレも追加する必要(運用フロー組み込み)

## 検討した代替案

### タイトル

- **B. 日本語ジャンル先頭:** 国内 SEO 最重視、 ただし国際 lo-fi 視聴層を切る。 不採用
- **シーン提示型のみ:** 「ジャンル + 時間 + シーン」の組み合わせより検索流入が弱い。 不採用
- **シリーズ化 (Vol.X 形式):** ファン獲得には強いが初動検索を切る。 不採用

### 説明文

- **英語のみ:** タイトルがバイリンガルなので、 説明文だけ英語は不自然。 不採用
- **チャプターを静的(`Track 1` 固定):** YouTube 自動チャプター化はされるが、 視聴者の興味喚起が弱い。 不採用

### サムネ

- **SDXL のみ(オーバーレイなし):** テキストなしサムネはクリック率が落ちる。 不採用
- **別 model (FLUX-1) でテキスト込み一発生成:** 英字でも誤字発生、 制御弱い。 不採用
- **動画フレーム抽出 + オーバーレイ:** showwaves が映ると微妙、 SDXL 背景の方が制御しやすい。 不採用

## 関連

- ADR-0003: 動画フォーマット 30分(5分×6)
- ADR-0015: showwaves + SDXL 背景
- ADR-0016: Juggernaut XL + ジャンル別代替モデル
- ADR-0017: directive parser
- ADR-0020: AI disclosure (`containsSyntheticMedia=true`)
- ADR-0024: LLM コストトラッキング
- ADR-0032: 改善計画 LLM スキーマ
- ADR-0033: 初期ジャンル + planner プロンプト
