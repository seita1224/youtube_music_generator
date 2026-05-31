# Research — YouTube 音楽投稿自動化システム

> grill-me セッション(34 ADR)で確定した主要判断を Decision / Rationale / Alternatives 形式で集約する。 出典 ADR を併記。

## 1. ジャンルとモデルスタック

### Music Generation = ACE-Step 1.5

- **Decision**: ACE-Step 1.5(Apache 2.0 license)を音楽生成エンジンに採用
- **Rationale**: 商用利用可、 RTX 3090 で RTF 12.76x(高速)、 VRAM 8〜12GB、 最大 10 分生成 / 標準 5 分。 ボーカル無しのインストゥルメンタル系で品質が安定
- **Alternatives**: MusicGen (Meta, CC-BY-NC でマネタイズ不可)、 Stable Audio Open (短尺、 構造に制約)、 商用 API (Suno/Udio: マネタイズ ToS 制約 + コスト)
- **Source**: ADR(暗黙、 requirements.md §3)

### Image / Thumbnail = SDXL 派生 (Juggernaut XL v10 デフォルト)

- **Decision**: SDXL ベースの Juggernaut XL v10 をデフォルト、 ジャンル別に RealVisXL / DreamShaper / Animagine 等に切替可能
- **Rationale**: VRAM ≈ 7GB (fp16)、 ACE-Step との同時ロード余地あり。 写実 / 風景 / レトロ感など多様な mood を 1 系統でカバーできる
- **Alternatives**: FLUX-1 schnell (テキスト描画が得意だが英字でも誤字発生、 制御弱)、 Stable Cascade (品質よいが採用例少)、 動画 generative (Runway/Pika; コスト高 + 30 分尺は範囲外)
- **Source**: ADR-0016

### Master LLM = OpenAI / Anthropic / Ollama を切替

- **Decision**: 抽象化層を介して 3 Provider を切替可能、 OpenAI は api_key + Codex OAuth の 2 mode
- **Rationale**: 改善計画 LLM は VRAM を取れないため(GPU は ACE-Step + SDXL 専有)、 クラウド API が基本。 Ollama は VRAM 空き時のフォールバック
- **Alternatives**: 単一 Provider 固定(切替できず単価上昇 / quota 切れで詰む)、 ローカル LLM only(VRAM 制約で性能不足)
- **Source**: ADR-0002, ADR-0019

### Anthropic SDK サブスクリプション利用 = 禁止

- **Decision**: API key のみ、 subscription mode は起動時拒否
- **Rationale**: 2026-02-19 に Anthropic が Agent SDK のサブスク利用を公式禁止
- **Alternatives**: 無視して使う(規約違反、 アカウント停止リスク)
- **Source**: ADR-0019

### OpenAI Codex OAuth = グレーゾーン、 個人実験範囲のみ

- **Decision**: 初期は api_key、 Codex OAuth は dryrun / 個人実験範囲で動作検証してから判断
- **Rationale**: Codex OAuth は技術的には動作するがマネタイズ用途 ToS グレー。 ブロックされたら api_key にフォールバック
- **Source**: ADR-0019

## 2. 著作権 / コンプライアンス

### Content ID 事前チェック = AcoustID + Chromaprint

- **Decision**: 無料の AcoustID + Chromaprint で投稿前指紋プレチェック
- **Rationale**: AcoustID は Content ID の約 7-8% カバー、 ゼロよりはマシ。 完全に防げない前提で、 事後手動対処を組み合わせる
- **Alternatives**: ACRCloud (有料、 月 $35〜)、 何もしない(リスク高)
- **Source**: ADR-0005

### AI 開示フラグ = `status.containsSyntheticMedia=true`

- **Decision**: 全動画投稿時に必須設定、 投稿前バリデーション層でチェック
- **Rationale**: YouTube が 2024 年に必須化、 未設定で投稿すると規約違反
- **Alternatives**: 説明文だけで開示(規約上の必要条件を満たさない)
- **Source**: ADR-0020

## 3. 動画フォーマット

### 30 分 × 5 分 × 6 トラック連結

- **Decision**: 1 動画 = 30 分 = 5 分 × 6 トラックを `ffmpeg acrossfade` で連結
- **Rationale**: mid-roll 広告(≥ 8 分必要)が確実に出せる、 作業 BGM の typical 滞在時間 30-90 分にフィット、 6 トラックなら ACE-Step の負荷 1 動画あたり ~3-5 分
- **Alternatives**: 単体 5 分(短すぎ、 mid-roll 不可)、 単体 10 分(ACE-Step 最大、 構造単調)、 60 分(GPU 時間 2 倍 + 失敗時の損失大)
- **Source**: ADR-0003

### 映像 = SDXL 背景 + `ffmpeg showwaves` overlay

- **Decision**: SDXL 静止背景 + ffmpeg showwaves で 30 分映像を作る
- **Rationale**: 動画生成モデルを使うとコスト + 時間が膨大。 lo-fi 系 BGM チャンネルの慣行に合致
- **Alternatives**: 動画生成 model(Runway 等)、 単一静止画(視聴維持↓)
- **Source**: ADR-0015

## 4. 投稿規模 / サイクル

### 投稿規模 = 1 日 1〜2 本から段階拡大

- **Decision**: MVP は 1 日 1〜2 本、 1 か月運用後に人間判断で拡大
- **Rationale**: 量で攻めると BAN リスク + 品質低下 + GPU 時間不足。 動機 B(観察)はサンプル数より分散ジャンルの方が学びが大きい
- **Alternatives**: 1 日 4〜5 本(GPU 時間が破綻)、 週 2-3 本(動機 B のサンプル不足)
- **Source**: ADR-0004

### サイクル構造 = 日次 + 週次の 2 層

- **Decision**: 日次サイクル(個別投稿生成) + 週次サイクル(改善計画立案)
- **Rationale**: 日次は実行ループ、 週次は学習ループ。 1 つに統合すると学習頻度が高すぎてノイズに振り回される
- **Alternatives**: 日次のみ(週次集約なし、 改善が遅い)、 月次のみ(改善ループが遅すぎ)
- **Source**: ADR-0006

## 5. オーケストレーション / プロンプト

### Master LLM 責務 = 計画立案のみ、 Content ID 判断は外す

- **Decision**: 改善計画 LLM はジャンル選定 + 投稿方針までで、 楽曲適法性判断は持たない(専門 API に委譲)
- **Rationale**: LLM に法律判断をさせると hallucination リスク大、 検証不能
- **Alternatives**: LLM に「侵害してそうな曲は避けて」と指示(失敗時の責任所在不明)
- **Source**: ADR-0008

### Directive Parser = `{{var}}` と `{{自由文}}` の自動判別

- **Decision**: 波括弧内が小文字 ASCII の単一識別子なら変数参照、 日本語や説明文を含むなら LLM 生成プレースホルダ
- **Rationale**: 単一の記法でテンプレを書ける、 ユーザー(自分)が記号を覚えなくて済む
- **Alternatives**: `{var}` vs `{{prompt}}` 分離(覚える記法が増える)、 全部 LLM(変数埋め込みでも余計な呼び出しコスト)
- **Source**: ADR-0017

### LLM 構造化出力 = Pydantic v2 + Provider 抽象

- **Decision**: LLM 出力は Pydantic v2 structured output で検証、 Provider 抽象層が tool calling / JSON mode の差を吸収
- **Rationale**: バリデーション失敗を確定的に検出 + リトライ可能、 統一 interface でテスト容易
- **Alternatives**: 生 JSON parse(validation 自前)、 LangChain 等の重量フレームワーク(オーバーキル)
- **Source**: ADR-0018, ADR-0032

### 改善計画 LLM 委任度 = 半分(意図 + directive)、 仕上げは別 LLM

- **Decision**: 改善計画 LLM は ジャンル / mood / visual_direction / directive、 ACE-Step / SDXL の最終プロンプトはシステム合成、 directive の `{{自由文}}` は仕上げ LLM(Haiku 級)
- **Rationale**: LLM が外しても 1 フィールドが fallback に落ちて済む。 全部任せると 1 ハズしで動画 1 本分の GPU 時間が無駄
- **Alternatives**: 全部任せる(C 案、 hallucination 時にダメージ大)、 任せない(B 案、 量産感で視聴離脱)
- **Source**: ADR-0032

### 初期 6 ジャンル + System prompt キャッシュ + few-shot 1 例

- **Decision**: 初期辞書 = lo-fi hip-hop / chillhop / ambient / synthwave / piano solo / future garage の 6 件。 Anthropic prompt caching に乗る形で system block に役割 / 辞書 / 制約 / few-shot を配置、 User block は analytics + 履歴のみ
- **Rationale**: 6 ジャンルなら月 30〜60 本投稿で各 5〜10 本、 retention 評価がギリギリ意味を持つ。 caching で約 90% コスト削減
- **Alternatives**: lo-fi only 2-3 ジャンル(視聴者層拡大不可)、 広く 10 ジャンル(サンプル分散しすぎ)、 caching 無し(コスト線形増)、 few-shot 無し(directive 形式を外す)
- **Source**: ADR-0033

## 6. テンプレ / 出力ポリシー

### タイトル = 英語ジャンル先頭 + 日本語 12 字サブタイト

- **Decision**: 形式 `{英語ジャンル} {duration}min | {12字以内の日本語サブタイト} {絵文字1個まで}`
- **Rationale**: 海外 lo-fi 視聴層 + 日本語作業 BGM 検索の両方をカバー。 日本語サブタイトは表示幅 2 倍を考慮して 12 字
- **Alternatives**: 英語のみ(国内検索捨てる)、 日本語先頭(国際検索捨てる)、 シリーズ化 (`Vol.X`、 初動の検索流入を切る)
- **Source**: ADR-0034

### 説明文 = バイリンガル動的描写 + 動的チャプター + 静的 AI 開示

- **Decision**: 冒頭 200-300 字を英語 + 日本語シーン描写、 チャプター 6 件は LLM 生成、 AI 開示と Content ID 免責は二か国語固定文
- **Rationale**: SEO は冒頭 100-150 字に効く、 チャプターは視聴維持↑、 AI 開示は言い回しブレ防止で固定
- **Alternatives**: 英語のみ(タイトル日本語と不整合)、 チャプター静的(視聴喚起弱)
- **Source**: ADR-0034

### サムネ = SDXL 背景 + Pillow オーバーレイ

- **Decision**: SDXL 出力に Pillow でテキストオーバーレイ、 ジャンル別フォント / 配色を YAML 管理
- **Rationale**: テキストなしサムネはクリック率↓。 SDXL の英字描画は不安定。 Pillow なら完璧
- **Alternatives**: SDXL 単独(誤字多)、 FLUX-1 一発生成(制御弱)、 動画フレーム抽出(showwaves が映る)
- **Source**: ADR-0034

## 7. バックエンド基盤

### FastAPI / uv / APScheduler / PostgreSQL(+ pgvector)

- **Decision**: バックエンド = Python + FastAPI、 uv パッケージ管理、 APScheduler を backend プロセス内、 DB = PostgreSQL + pgvector 拡張余地
- **Rationale**: 1 人運用で OPS シンプル化、 SQLite だと将来 RAG 拡張で詰む、 pgvector は最初から入れる
- **Alternatives**: Django (重い)、 Flask (依存薄いが OpenAPI 連動弱)、 別プロセス scheduler (運用箱増)、 SQLite (拡張性問題)
- **Source**: ADR-0009, ADR-0010, ADR-0011, ADR-0014

### Next.js (App Router) 管理 UI、 Basic 認証(LAN 内)

- **Decision**: フロントエンドは Next.js、 LAN 内 Basic 認証で保護
- **Rationale**: ADR-0001 で「画面要件があるのでフロントは Next.js が自然」と確定。 Basic 認証は LAN 信頼前提で OK
- **Source**: ADR-0001, ADR-0013

### OAuth トークン = Fernet 暗号化 in PostgreSQL

- **Decision**: トークンを Fernet 対称鍵で暗号化して DB 保存、 鍵は `.env`、 バックアップ対象外
- **Rationale**: DB ダンプ流出時の防衛線、 ダンプ + 鍵を同じ場所に置かない原則
- **Source**: ADR-0012, ADR-0026

### ストレージ = fsspec 抽象化

- **Decision**: `file:// / s3:// / gs://` を fsspec で統一、 backend / GPU worker 両方が利用
- **Rationale**: 将来クラウドストレージ移行(RunPod 連動 / 容量問題)を構造的に担保
- **Source**: ADR-0022

### 構造化ログ = loguru JSON

- **Decision**: loguru で JSON Lines、 `video_id` / `genre` / `step` でフィルタ可能
- **Rationale**: 標準 logging より設定楽、 JSON Lines は管理 UI 検索と相性◎
- **Source**: ADR-0023

### LLM コストトラッキング = `usage_log` テーブル

- **Decision**: 全 LLM 呼び出しを provider / model / tokens / cost / context で記録、 月予算 50%/80%/100% で Slack 通知
- **Rationale**: 動機 C(マネタイズ)の損益管理に必須、 Codex OAuth 利用時の quota 残量も別軸で記録
- **Source**: ADR-0024

## 8. 運用

### dryrun MVP 必須 + 状態別ライフサイクル

- **Decision**: dryrun MVP 必須機能、 state = pending/approved/rejected/auto_expired/posted、 7 日無反応で自動削除
- **Rationale**: 投稿という不可逆アクションに人間の一手を挟む、 ストレージ膨張も防ぐ
- **Source**: ADR-0007, ADR-0025

### バックアップ = ローカルセカンダリディスクのみ

- **Decision**: オフサイトなし、 ローカル 2nd ディスクのみ、 全損リスクを受容
- **Rationale**: 個人プロジェクトの運用コスト最適化、 失っても再構築可能な情報(投稿動画は YouTube に残る)
- **Source**: ADR-0026

### テスト戦略 = critical path 100% + その他 best effort

- **Decision**: AcoustID/containsSyntheticMedia/OAuth crypto/Pydantic validation/directive parser を 100%、 他は 60-70%、 PoC コードは免除
- **Rationale**: 全部 80% は 1 人運用で持続不可、 critical だけ厳しくしてリスク高い部分を守る
- **Source**: ADR-0027

### エラーカテゴリ = 5 分類

- **Decision**: transient / recoverable / fatal / compliance / quality で対応動作を確定
- **Rationale**: 「とりあえずリトライ」「黙って握りつぶす」を構造的に禁止、 何をするか各 type ごとに事前定義
- **Source**: ADR-0028

### モノレポ + ハイブリッド実行 + Makefile + panic-stop

- **Decision**:
  - リポジトリ = モノレポ(backend/frontend/gpu_worker/docs/infra)、 Private、 main + PR
  - 実行 = backend/frontend/postgres は docker compose、 GPU worker は host 直 systemd(Dockerfile 用意で RunPod 移行可)
  - デプロイ = Makefile 経由手動、 マイグレ前自動 pg_dump、 reboot 後 scheduler 手動 enable
  - 緊急停止 = `make panic-stop` で 24h 内動画を private 化
- **Rationale**: 1 人運用の認知負荷最小化、 切替可能性を構造的に担保、 コンプラ事故への即応手段
- **Source**: ADR-0029, ADR-0030, ADR-0031

### 改善計画 LLM 入力 = 集計サマリ + 生 snapshot

- **Decision**: LLM への入力は集計サマリ(表形式)、 生 JSON は `plan_metric_snapshot` に保存して再現性確保
- **Rationale**: token 浪費抑制、 数字読みに LLM の認知資源を取らせない、 ただし後追い検証のため生データは保存
- **Source**: ADR-0032, ADR-0033

## 9. 検証済み事実(Phase 0 完了時点)

- YouTube AI 開示 API フィールド = `status.containsSyntheticMedia`(確認済)
- ACE-Step の生成長さ仕様 = 5 分は標準内、 最大 10 分まで(確認済)
- SDXL 派生モデルのライセンス = 主要モデル(Juggernaut/RealVis/DreamShaper)は商用利用可(確認済)
- 選定フォント 6 種(Bebas Neue / Cormorant Garamond / VT323 / Playfair Display / Space Grotesk / Noto Sans JP)= 全 SIL OFL ライセンス、 同梱可(確認済)
- Anthropic SDK サブスク利用 = 2026-02-19 公式禁止(確認済)
- Vercel Postgres / KV は廃止 = ADR-0010(self-host PostgreSQL)の妥当性を補強(確認済)

## 10. NEEDS CLARIFICATION(残)

設計フェーズ完了時点では NEEDS CLARIFICATION なし。 以下は **将来 ADR** として requirements.md §11 に列挙済み:

- 1 か月運用後の成功 / 撤退条件の数値化
- few-shot を retention 上位 plan の自動選別へ昇格(B3、 ADR-0033)
- `expected_kpi` の必須化(ADR-0032)
- RunPod 等への GPU 実切替時の運用詳細(ADR-0031 の preparation は完了)
- 投稿規模拡大判断ルール(ADR-0004 後継)
