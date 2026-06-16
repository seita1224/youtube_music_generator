# Performance Notes (T143 / T144)

> 分析メモ。 **実装変更は不要**。 現状コードの並列化余地と prompt caching の計測方法をまとめる。
> 出典は repo 内の実コード。 数値前提は「RTX 3090 24GB 単一 GPU / 1 投稿 = 音楽 6 トラック」。

## T143: パイプラインで並列化できる箇所

### 現状 (直列)

1 投稿の処理は `daily_cycle.py` の `_process_post` が **直列** に駆動する
(`backend/src/ymg_backend/domain/pipeline/daily_cycle.py`):

```
music (6 トラック) → acoustid → image → render(title/desc/thumbnail/video)
```

- 音楽 6 トラックは `MusicJobRunner.submit_and_wait` 内で
  `for position in range(track_count):` の **逐次ループ** (music_jobs.py:173 付近)。
  1 トラックずつ GPU worker に投げて完了を待ってから次を投げる。
- 投稿間 (1 サイクル = 1〜2 投稿) も `run` の `for ... in plan.posts` で直列。

### 並列化の余地と注意点

| 区間 | 並列化余地 | 制約 / 注意 |
|---|---|---|
| **音楽 6 トラック** | 大。 各トラックは独立 (相互依存なし)。`asyncio.gather` で複数ジョブを同時投入できれば壁時計時間を短縮 | **GPU は単一**。 worker 側がジョブを直列実行するなら投入を並列化しても効果は出ない。 効くのは worker がバッチ/キュー並列を持つ場合のみ。 VRAM が許す範囲で worker 内 batch size を上げる方が現実的 |
| **SDXL 画像生成 ↔ AcoustID チェック** | 中。 画像生成 (GPU) と AcoustID 著作権チェック (外部 HTTP API、 GPU 非依存) は **入力が独立**。 現状は acoustid → image の順だが、 画像生成を acoustid と並走させられる | acoustid が `genre_suspended` を返すと post を中断する (FR-012)。 先に画像を作ると中断時に画像が無駄になる。 トレードオフ: 中断は稀 (連続 3 hit) なので、 平常時の時短を優先するなら並走が有利 |
| **投稿間 (post 単位)** | 中。 post 同士は独立 (失敗も隔離済み) | GPU 競合。 2 投稿を並走させると VRAM/スループットを取り合う。 1 GPU では逐次が無難 |
| **render 内 (title/description)** | 小。 title と description の LLM 呼び出しは独立 | LLM API 並列呼び出しは可能だが、 1 投稿あたり数秒オーダーで支配項ではない (音楽生成が支配的) |

### 結論 (優先度)

1. **GPU 単一がボトルネック**。 投入を `asyncio.gather` で並列化しても、 worker が直列実行なら
   壁時計時間は縮まない。 まず worker 側のバッチ/同時実行能力を確認するのが先。
2. GPU を消費しない **AcoustID チェックを画像生成と並走** させるのが、 1 GPU 環境で
   コード変更が最小かつ確実に効く並列化 (acoustid は外部 HTTP I/O 待ちが主)。
3. post 間・トラック間の並列化は GPU を増やす (例: RunPod で worker 複数) まで保留。
   RunPod 移行 (`infra/runbooks/runpod-migration.md`) で worker をスケールアウトできれば
   post 並列が初めて意味を持つ。

## T144: Anthropic prompt caching の hit 率計測

### 計測の仕組み (既存)

`usage_log` テーブルに 1 LLM 呼び出しごとの token 内訳が記録される
(`backend/src/ymg_backend/infrastructure/db/models/usage_log.py`):

- `prompt_tokens` — 入力トークン総数
- `cached_tokens` — キャッシュから読まれた入力トークン数
- `completion_tokens` — 出力トークン数

Anthropic provider は API レスポンスの `usage.cache_read_input_tokens` を
`cached_tokens` に書き込む (`llm/anthropic_provider.py:149`)。
caching は system block / tool 定義に `cache_control: {type: "ephemeral"}` を付けて有効化している
(ADR-0033)。

### hit 率の定義と SQL

**cache hit 率 = cached_tokens / (prompt_tokens) 相当** で測る。
provider=anthropic に絞って集計する:

```sql
-- 直近 7 日の Anthropic prompt caching hit 率
SELECT
  date_trunc('day', created_at) AS day,
  sum(cached_tokens)            AS cached,
  sum(prompt_tokens)            AS prompt_total,
  round(100.0 * sum(cached_tokens) / nullif(sum(prompt_tokens), 0), 1) AS hit_pct
FROM usage_log
WHERE provider = 'anthropic'
  AND created_at >= now() - interval '7 days'
GROUP BY 1
ORDER BY 1 DESC;
```

> 注: Anthropic の `prompt_tokens` (= API の `input_tokens`) は **キャッシュ未ヒット分のみ** を
> 数える実装もあるため、 厳密な hit 率は `cached_tokens / (cached_tokens + prompt_tokens)` で
> 取る方が安全。 上の SQL の分母を `sum(cached_tokens + prompt_tokens)` に変えれば
> 「全入力トークンに対するキャッシュ充足率」になる。 運用ではどちらか一方に固定して継続観測する。

context (planner / weekly_planner / finisher 等) 別に見たいときは `context_type` で絞る:

```sql
SELECT context_type,
       round(100.0 * sum(cached_tokens) / nullif(sum(cached_tokens + prompt_tokens), 0), 1) AS hit_pct
FROM usage_log
WHERE provider = 'anthropic' AND created_at >= now() - interval '30 days'
GROUP BY 1 ORDER BY hit_pct;
```

### 90% 未満時の対処方針

prompt caching は「キャッシュ可能な前置きが安定して再利用されること」で効く。
hit 率が **90% 未満** に落ちている場合の切り分け:

1. **キャッシュ TTL 切れ (ephemeral は約 5 分)**: 呼び出し間隔が空くと前置きが期限切れになる。
   → バッチ的にまとめて叩く / 呼び出し頻度の低い経路は caching を期待しない。
2. **前置きが毎回変わっている**: system block や few-shot に可変要素 (日付・動的選別の例) が
   混ざると prefix が一致せずキャッシュが効かない。 → 可変部分を **プロンプト末尾** に移し、
   `cache_control` を付ける固定前置きを安定させる (ADR-0033 の few-shot 選別が動的化したら要注意)。
3. **最小キャッシュ長未満**: 前置きが短すぎるとキャッシュ対象にならない。
   → 固定前置き (テンプレ/スキーマ説明) を `cacheable=True` ブロックに集約して長さを確保。
4. **provider 切替の影響**: `LLM_PROVIDER` が openai/ollama のときは Anthropic caching は無関係。
   集計は必ず `provider = 'anthropic'` で絞る。

対処後は上の SQL を日次で再観測し、 hit 率が 90% 台へ戻るかを確認する。
コスト面では `cost_usd` 列で実額も追えるため、 hit 率改善が請求額に効いているかを併せて見る。

## 関連

- ADR-0024 LLM コスト/usage トラッキング (`usage_log`)
- ADR-0033 改善計画 LLM スキーマ + few-shot / prompt caching
- ADR-0031 デプロイ (GPU worker スケールアウト = RunPod)
- `backend/src/ymg_backend/domain/pipeline/daily_cycle.py` (直列パイプライン)
- `backend/src/ymg_backend/domain/pipeline/music_jobs.py` (6 トラック逐次ループ)
- `backend/src/ymg_backend/llm/anthropic_provider.py` (cached_tokens 記録)
