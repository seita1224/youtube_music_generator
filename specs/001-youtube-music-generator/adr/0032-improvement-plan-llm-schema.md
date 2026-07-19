# ADR-0032: 改善計画 LLM の出力スキーマ

- **ステータス:** Accepted
- **日付:** 2026-05-26 (改訂: 2026-07-14 — DailyPost トラック層・metric_claims・LLM提案/システム確定分離)
- **決定者:** @seita
- **タグ:** ml / backend

## 背景

ADR-0002(マスター LLM クラウド + 抽象化)、ADR-0008(マスター LLM の責務は「次に何を作るか」の計画、Content ID 判断は外す)、ADR-0017(directive parser)、ADR-0018(Pydantic structured output)、ADR-0024(LLM コストトラッキング)を踏まえ、 改善計画 LLM の出力スキーマを確定する。

中心論点は **「LLM にどこまで任せるか」**。 3 つの極を比較した。

| | LLM が決めること | リスク |
|---|---|---|
| 任せない | ジャンルと一言メモだけ | 量産感が出て視聴者離脱 |
| 半分任せる | ジャンル + ムード + 視覚意図 + タイトル指示(directive) | 中間、 fallback が効く |
| 全部任せる | ACE-Step / SDXL の最終プロンプトまで | LLM が外すと 1 回数分の GPU 時間を捨てる |

2026-07-14 改訂で、音楽側はさらに **LLM提案(企画)とシステム確定(実行用音楽生成仕様)を二層化**し、Analytics 数値は LLM 自由文ではなく typed `metric_claims` で snapshot に紐付ける(ADR-0039)。

## 決定

### (1) 委任レベル = **半分任せる**

LLM は「ジャンル / ムード / 視覚意図 / directive 形式の指示」と、各トラックの **LLM提案フィールド** を返す。 ACE-Step や SDXL に渡す最終プロンプト(実行時プロンプト / caption)と最終 BPM / music key は、システム側の music compiler と directive parser(ADR-0017)で合成・固定する。 directive 内の `{{自由文}}` 部分は、 仕上げ用の小さな LLM(Haiku 級)で別途生成する。

設計理由:

- **失敗の局所化**: LLM が雑な出力を返しても 1 フィールドがデフォルトに落ちて済む。 全部任せる構成では 1 回の hallucination が動画生成全体を無駄にする
- **コスト分離**: 改善計画は Sonnet/Opus 級、 仕上げは Haiku 級と使い分け、 ADR-0024 のコストトラッキングと整合
- **量産感の回避**: 完全テンプレ運用は 1〜2 ヶ月で視聴者が見飽きる構造
- **実行再現性**: 承認後は実行用音楽生成仕様を再合成せず、hash 固定で exact replay(ADR-0039)

### (2) スキーマは Daily と Weekly で分離

ADR-0006 の日次/週次サイクル分離に合わせ、 別 Pydantic クラスとする。 統合 discriminator にすると LLM が「埋めるべきフィールド」を毎回判定する必要が生じ、 失敗率が上がる。

### (3) Pydantic v2 スキーマ確定版(抜粋 + 2026-07-14 拡張)

```python
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator, ValidationInfo


class ReferencedMetrics(BaseModel):
    window_days: int = Field(ge=1, le=180)        # LLM申告。正本は snapshot
    sample_size: int = Field(ge=0)                 # LLM申告。正本は snapshot
    top_metrics_summary: str = Field(min_length=20)


class MetricClaim(BaseModel):
    """LLM が rationale 等で引用する数値。snapshot path へ紐付ける。"""
    claim_id: str
    metric_path: str
    claimed_value: float | int | str
    unit: str | None = None


class ExpectedKpi(BaseModel):
    retention_pct: float | None = Field(default=None, ge=0, le=100)
    expected_views_24h: int | None = Field(default=None, ge=0)


class TrackLlmProposal(BaseModel):
    """LLM提案層(希望値。実行値ではない)。"""
    position: int = Field(ge=0, le=5)
    subtheme: str = Field(min_length=2)
    instruments: str = Field(min_length=2)
    arrangement: str = Field(min_length=4)
    texture: str = Field(min_length=4)
    prompt_ingredients: list[str] = Field(min_length=1)
    desired_bpm: int = Field(ge=50, le=200)
    desired_music_key: str = Field(min_length=2)


class DailyPost(BaseModel):
    genre: str
    mood: str = Field(min_length=4)
    bpm_range: tuple[int, int] | None = None
    visual_direction: str = Field(min_length=10)   # SDXL 用。実行時プロンプトへ混ぜない
    title_directive: str = Field(min_length=8)
    description_directive: str = Field(min_length=20)
    thumbnail_directive: str | None = None
    schedule_jst: datetime | None = None
    tracks: list[TrackLlmProposal] = Field(min_length=6, max_length=6)

    @field_validator("genre")
    @classmethod
    def validate_genre(cls, v: str, info: ValidationInfo) -> str:
        allowed = (info.context or {}).get("allowed_genres", set())
        if allowed and v not in allowed:
            raise ValueError(f"genre {v!r} not in allowed list")
        return v


class DailyPlan(BaseModel):
    cycle: Literal["daily"] = "daily"
    # plan_id / target_date はシステム確定。LLM 出力からは除外しサーバーが注入
    posts: list[DailyPost] = Field(min_length=1, max_length=2)
    rationale: str = Field(min_length=20)
    referenced_metrics: ReferencedMetrics
    metric_claims: list[MetricClaim] = []
    expected_kpi: ExpectedKpi | None = None
```

システム側が永続化する **システム確定層**(実行用音楽生成仕様のトラック単位)は LLM 出力ではない:

| フィールド | 意味 |
|---|---|
| `final_bpm` / `music_key` | desired を検証・必要なら BPM clamp 後に固定。無効 key は暗黙補正せず検証 fail |
| `caption`(実行時プロンプト) | 最終 BPM / music key / subtheme に加え、LLM提案の `instruments` / `arrangement` / `texture`(非空時)を決定論連結。`visual_direction` は混ぜない(ADR-0040) |
| `duration_sec` | 常に 300 |
| `model` | `acestep-1.5` |
| `seed` | 決定論(position / plan / compiler version 由来) |
| `output_position` | 0..5 |
| `compiler_version` / `compilation_hash` | 仕様再現用 |

WeeklyPlan スキーマ(ExperimentSlot 等)は従来どおり(本改訂の対象外)。元の WeeklyPlan / ExperimentSlot 定義は変更しない。

### (4) 値の取り回し方針

- **`plan_id`・`target_date`・metrics の実値はシステム発行 / snapshot 正本**(LLM には生成・改変させない)。UUID v7 で時系列ソート可能
- **`genre` は辞書照合**。 context 注入で辞書外は ValidationError
- **`rationale` の min_length** は形式的ガードレール。数値引用は `metric_claims` 経由で snapshot と照合(ADR-0039)
- **`expected_kpi` は初期 optional**
- **`schedule_jst` は初期 None 推奨**
- **legacy Plan**(tracks 欠落): versioned genre template から決定論的に 6 subtheme を展開してから compile

### (5) 仕上げ LLM(Haiku 級)の責務

directive parser が `{{自由文}}` トークンを検出したら、 仕上げ LLM を別呼びする。

入力:

- 自由文の指示(parser 抽出)
- 当該 DailyPost のコンテキスト(genre / mood / visual_direction)

出力:

- 短い文字列1個(タイトルのサブ部分 / 説明文の概要等)

これにより:

- 改善計画 LLM は重い判断だけ、 仕上げ LLM は軽い文章生成
- 仕上げ部分だけ別 model に切り替え可能(コスト最適化)

### (6) 失敗時の挙動(ADR-0028 への割り当て)

- **Pydantic validation 失敗** → `recoverable`:max 2 回リトライ(temperature 低下) → 前回成功 plan を再利用 + rationale に明記 → なお失敗で scheduler 停止 + 通知
- **`genre` 辞書外** → `recoverable`:辞書を明示して再要求 → なお外なら dryrun 行き
- **`visual_direction` が短すぎる** → `quality`:min_length validator で reject、 retry 後はジャンル別デフォルトテンプレに fallback
- **`genre_distribution` の合計が 1.0 でない** → `recoverable`:再要求
- **`metric_claims` と snapshot 不一致 / sparse** → Plan は `blocked`(通常承認不可)。強制承認は ADR-0039
- **無効 music key / トラック差分化不足** → compile / validate fail、仕様を確定しない

### (7) DB スキーマとの対応(概略)

```text
plans
  id (uuid v7)
  cycle (enum: daily, weekly)
  target_date / target_week_start (date)
  payload (jsonb)            -- Pydantic model_dump(LLM提案含む)
  rationale (text)
  llm_model (text)
  llm_cost_usd (numeric)
  status (plan_status — ADR-0039)
  active_compilation_hash (text, nullable)
  created_at (timestamptz)

posts
  id (uuid v7)
  plan_id (fk → plans.id)
  position (smallint)
  payload (jsonb)            -- DailyPost dump(tracks LLM提案含む)
  ...

plan_metric_snapshot         -- Analytics 正本(LLM 申告値ではない)
plan_evidence_snapshots      -- 生成時点凍結(ADR-0039 / U2)
music_compilations           -- 実行用音楽生成仕様(Post position 単位、ADR-0039)
plan_validation_runs         -- 検証履歴
```

## 結果

### 良い影響

- LLM の hallucination ダメージが 1 フィールド単位に局所化する
- desired / final の分離で「希望値を実行してしまった」事故を防げる
- `metric_claims` により Analytics 不一致を機械検証できる
- directive parser を介すことで「テンプレ差し替えだけ」で見た目を一斉変更できる
- Daily / Weekly が別構造体のため、LLM の埋めるべきフィールドが明確

### 悪い影響・トレードオフ

- LLM 呼び出しが 2 段になる(改善計画 + 仕上げ)ため、 単純な round-trip が増える
- スキーマ管理コストが Daily/Weekly で 2 倍 + tracks / claims が増える
- legacy Plan の template 展開パスが必要

### 受容したリスク

- LLM が「directive 形式を理解できる」前提に立つ。 安価な model(Haiku 等)が directive を解釈できない可能性 → 改善計画は Sonnet 以上を最低ラインとする
- `expected_kpi` を初期 optional にしたため、 LLM の予測精度を初期から計測できない期間がある

## 検討した代替案

- **任せない(B):** ジャンル + 一言メモのみ。不採用
- **全部任せる(C):** ACE-Step / SDXL の最終プロンプトまで LLM 生成。不採用
- **Daily / Weekly を統合スキーマ:** 不採用
- **desired BPM/key をそのまま実行:** 検証・補正・再現性が崩れる。不採用
- **`expected_kpi` を最初から必須:** 初期はノイズが多すぎる。運用後に再評価

## 関連

- ADR-0002: マスター LLM クラウド + 抽象化
- ADR-0003: 300秒 × 6 トラック
- ADR-0004: 投稿規模 1日1〜2本
- ADR-0006: 日次/週次サイクル分離
- ADR-0007: dryrun モード MVP 必須
- ADR-0008: マスター LLM の責務範囲
- ADR-0017: directive parser
- ADR-0018: Pydantic structured output
- ADR-0024: LLM コストトラッキング
- ADR-0028: 5 種類のエラーカテゴリ
- ADR-0033: few-shot / prompt provenance
- ADR-0039: 根拠付き承認・compile hash・QA
