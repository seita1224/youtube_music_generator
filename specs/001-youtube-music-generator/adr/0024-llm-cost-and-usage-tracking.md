# ADR-0024: LLM コスト・トークン使用量管理

- **ステータス:** Accepted
- **日付:** 2026-05-25
- **決定者:** @seita
- **タグ:** backend, ml, ops, policy

## 背景

動機 C(マネタイズ)前提のシステムで、LLM コストが収益を食わない設計が必要。
プロンプト肥大化・改善計画 LLM の暴走・リトライ多発による予期せぬコスト膨張を防ぎたい。

各プロバイダ SDK は usage(トークン数)を **レスポンスに含めて返す**ため、自前 tokenize は不要。
レスポンスから取得した数値とアプリ側の単価表で推定コストを算出できる。

## 決定

### 1. 全 LLM 呼び出しを `usage_log` テーブルに記録

- `LLMProvider` 抽象化層(ADR-0002)の呼び出しラッパで自動記録
- 主要フィールド:
  - `id`, `created_at`
  - `provider`(`openai` / `anthropic` / `ollama`)
  - `auth_mode`(`api_key` / `codex_oauth`、ADR-0019)
  - `model`
  - `input_tokens`, `output_tokens`
  - `cache_creation_input_tokens`, `cache_read_input_tokens`(Anthropic 用)
  - `is_batch`(バッチ API なら半額)
  - `cost_usd_estimate`(単価表 × トークン)
  - `latency_ms`
  - `context`(JSONB: `job_id`, `video_id`, `cycle_id`, `step`, `genre`)
  - `rate_limit_remaining_tokens` / `rate_limit_remaining_requests`(Codex OAuth 用、レスポンスヘッダから)

### 2. 単価表は環境変数 or DB に外出し

- ハードコード回避(年次以上で陳腐化)
- 環境変数 `LLM_PRICING_JSON` または DB の `model_pricing` テーブルで管理
- 新モデルリリース時に運用ルールで更新

### 3. 月予算アラート

- 環境変数 `LLM_MONTHLY_BUDGET_USD` で月予算を設定(例: $20)
- 月初を起点に積算 `cost_usd_estimate` を集計
- **50% / 80% / 100%** 到達時に **Slack 通知**
- 100% 到達時のデフォルト挙動 = **警告のみ、ジョブは継続**
  - 設定 `LLM_OVER_BUDGET_BEHAVIOR=warn_only` / `pause_jobs` / `switch_to_ollama` で切替可能
  - 当面は `warn_only`(突然投稿停止すると運用が壊れるリスクが大きい)

### 4. Codex OAuth のサブスク quota 監視(別軸)

- 金額ではなく **rate limit / quota** で監視
- レスポンスヘッダ(`x-ratelimit-*`)を `usage_log` に保存
- 残量が閾値(例: 残 10%)を切ったら Slack 通知

### 5. Ollama の GPU 時間記録(別軸)

- 金銭コスト 0、代わりに `latency_ms` と VRAM 占有時間を記録
- GPU 時間が ACE-Step / SDXL の生成時間を圧迫する場合は警告

### 6. プロバイダのダッシュボード併用(二重確認)

- Anthropic Console / OpenAI Platform のダッシュボードで月次に手動確認
- アプリ集計とプロバイダ請求書が乖離する場合は単価表 or 計測ロジックを見直し

## 結果

### 良い影響

- アプリ内で全呼び出しを記録 → 後追い・改善・コスト最適化が可能
- 月予算アラートで暴走を早期検知
- Provider 切替時の比較データが蓄積する(動機 B のデータ蓄積とも整合)
- 構造化ログ(ADR-0023)と分離することで集計クエリが書きやすい

### 悪い影響・トレードオフ

- 単価表のメンテが必要(年1〜2回程度)
- `usage_log` は呼び出し回数分のレコードが増える → 1日 50〜200 呼び出し × 365日 = 年 1.8〜7 万件(無視できる規模)
- Anthropic の prompt caching を使う場合、`cache_*` カラムの単価適用が複雑化
  - 緩和: 単価表に cache 用フィールドを持たせる

### 受容したリスク

- 100% 到達時のデフォルトが `warn_only` なので、放置すると予算超過する
  - 対策: 50% / 80% アラートで気づく運用、必要なら設定で `pause_jobs` に切替

## 検討した代替案

- **プロバイダダッシュボードのみ:** 検知遅延が大きく、アプリ側で歯止め不能。不採用。
- **アプリ内記録のみ:** プロバイダ請求書との突合がなく、計測ロジックのバグに気づきにくい。両者併用が正解。
- **自前 tokenize(`tiktoken` 等で事前計測):** SDK レスポンスで取得できるので不要。事前見積もり用途には残せる(将来オプション)。

## 関連

- ADR-0002: LLMProvider 抽象化層
- ADR-0010: PostgreSQL(usage_log 保存先)
- ADR-0019: Provider 実装方針
- ADR-0023: 観測性(構造化ログ)
- ../requirements.md §使用モデル方針
