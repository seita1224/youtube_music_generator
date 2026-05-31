# ADR-0032: 改善計画 LLM の出力スキーマ

- **ステータス:** Accepted
- **日付:** 2026-05-26
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

## 決定

### (1) 委任レベル = **半分任せる**

LLM は「ジャンル / ムード / 視覚意図 / directive 形式の指示」を返す。 ACE-Step や SDXL に渡す最終プロンプトは、 システム側のテンプレと directive parser(ADR-0017)で合成する。 directive 内の `{{自由文}}` 部分は、 仕上げ用の小さな LLM(Haiku 級)で別途生成する。

設計理由:

- **失敗の局所化**: LLM が雑な出力を返しても 1 フィールドがデフォルトに落ちて済む。 全部任せる構成では 1 回の hallucination が動画生成全体を無駄にする
- **コスト分離**: 改善計画は Sonnet/Opus 級、 仕上げは Haiku 級と使い分け、 ADR-0024 のコストトラッキングと整合
- **量産感の回避**: 完全テンプレ運用は 1〜2 ヶ月で視聴者が見飽きる構造

### (2) スキーマは Daily と Weekly で分離

ADR-0006 の日次/週次サイクル分離に合わせ、 別 Pydantic クラスとする。 統合 discriminator にすると LLM が「埋めるべきフィールド」を毎回判定する必要が生じ、 失敗率が上がる。

### (3) Pydantic v2 スキーマ確定版

```python
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator, ValidationInfo


class ReferencedMetrics(BaseModel):
    window_days: int = Field(ge=1, le=180)        # 何日分を見たか
    sample_size: int = Field(ge=0)                 # 対象動画本数
    top_metrics_summary: str = Field(min_length=20)


class ExpectedKpi(BaseModel):
    retention_pct: float | None = Field(default=None, ge=0, le=100)
    expected_views_24h: int | None = Field(default=None, ge=0)


class DailyPost(BaseModel):
    genre: str                                     # 辞書照合(下記 validator)
    mood: str = Field(min_length=4)
    bpm_range: tuple[int, int] | None = None
    visual_direction: str = Field(min_length=10)   # SDXL 用の意図テキスト
    title_directive: str = Field(min_length=8)     # ADR-0017 parser 用
    description_directive: str = Field(min_length=20)
    thumbnail_directive: str | None = None
    schedule_jst: datetime | None = None

    @field_validator("genre")
    @classmethod
    def validate_genre(cls, v: str, info: ValidationInfo) -> str:
        allowed = (info.context or {}).get("allowed_genres", set())
        if allowed and v not in allowed:
            raise ValueError(f"genre {v!r} not in allowed list")
        return v


class DailyPlan(BaseModel):
    cycle: Literal["daily"] = "daily"
    plan_id: str                                   # システム発行 UUID v7
    target_date: date                              # JST
    posts: list[DailyPost] = Field(min_length=1, max_length=2)  # ADR-0004
    rationale: str = Field(min_length=20)
    referenced_metrics: ReferencedMetrics
    expected_kpi: ExpectedKpi | None = None        # 初期は optional


class ExperimentSlot(BaseModel):
    genre: str
    rationale: str = Field(min_length=20)
    success_criteria: str = Field(min_length=10)
    slot_count: int = Field(default=1, ge=1, le=3)


class WeeklyPlan(BaseModel):
    cycle: Literal["weekly"] = "weekly"
    plan_id: str
    target_week_start: date                        # 月曜日 (JST)
    genre_distribution: dict[str, float]           # 合計 1.0
    avoid_genres: list[str] = []
    experiment_slots: list[ExperimentSlot] = []
    rationale: str = Field(min_length=50)
    referenced_metrics: ReferencedMetrics

    @field_validator("genre_distribution")
    @classmethod
    def validate_distribution(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"genre_distribution must sum to 1.0, got {total}")
        return v
```

### (4) 値の取り回し方針

- **`plan_id` はシステム発行**(LLM には生成させない)。 UUID v7 で時系列ソート可能にする
- **`genre` は辞書照合**。 LLM 呼び出し時に `model_validate(..., context={"allowed_genres": db_genres})` で context 注入し、 辞書外は ValidationError とする
- **`rationale` の min_length** は形式的ガードレール。 「20文字以上を書け」と要求するだけで、 内容空っぽな出力を抑制できる(GPT-4 / Claude で確認済の挙動)
- **`expected_kpi` は初期 optional**、 運用 1〜2 ヶ月で「予測と実測の相関」が取れたら必須化を再評価
- **`schedule_jst` は初期 None 推奨**、 システム側で固定時刻運用(YouTube アルゴリズム的に初動 > 投稿時刻)

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

### (7) DB スキーマとの対応(概略)

```text
plans
  id (uuid v7)
  cycle (enum: daily, weekly)
  target_date / target_week_start (date)
  payload (jsonb)            -- Pydantic model_dump
  rationale (text)
  llm_model (text)
  llm_cost_usd (numeric)
  status (enum: generated, approved, executing, completed, failed)
  created_at (timestamptz)

posts
  id (uuid v7)
  plan_id (fk → plans.id)
  position (smallint)        -- daily の posts[] index
  payload (jsonb)            -- DailyPost dump
  music_job_id (fk → gpu_jobs.id, nullable)
  image_job_id (fk → gpu_jobs.id, nullable)
  youtube_video_id (text, nullable)
  final_title (text, nullable)         -- directive parse 後の最終タイトル
  final_description (text, nullable)
  posted_at (timestamptz, nullable)
  retention_24h (numeric, nullable)    -- analytics で後追い更新

plan_metric_snapshot
  plan_id (fk)
  metric_window_start / end (date)
  metrics_jsonb              -- LLM に渡した analytics の生コピー(再現性確保)
```

`plan_metric_snapshot` を残すことで、 同じ入力を別 model に投げて出力差分を比較する後追い検証が可能になる。

## 結果

### 良い影響

- LLM の hallucination ダメージが 1 フィールド単位に局所化する
- コスト最適化(改善計画 = 高品質、 仕上げ = 安い)が自然に効く
- directive parser を介すことで「テンプレ差し替えだけ」で見た目を一斉変更できる
- Daily / Weekly が別構造体のため、LLM の埋めるべきフィールドが明確
- `plan_metric_snapshot` により、 後で「同じ判断材料で別 model 試行」が可能

### 悪い影響・トレードオフ

- LLM 呼び出しが 2 段になる(改善計画 + 仕上げ)ため、 単純な round-trip が増える
- スキーマ管理コストが Daily/Weekly で 2 倍
- `genre` 辞書のメンテナンスが必要(新ジャンル追加に承認ワークフロー)
- directive parser のテンプレ管理が必要(`templates/*.yaml`)

### 受容したリスク

- LLM が「directive 形式を理解できる」前提に立つ。 安価な model(Haiku 等)が directive を解釈できない可能性 → 改善計画は Sonnet 以上を最低ラインとする
- `expected_kpi` を初期 optional にしたため、 LLM の予測精度を初期から計測できない期間がある

## 検討した代替案

- **任せない(B):** ジャンル + 一言メモのみ。 同じテンプレで量産することになり、 1〜2 ヶ月で視聴者が見飽きるリスク。不採用
- **全部任せる(C):** ACE-Step / SDXL の最終プロンプトまで LLM 生成。 hallucination 時に 1 動画分の GPU 時間が無駄になり、 fallback も効きにくい。不採用
- **Daily / Weekly を統合スキーマ:** discriminator で分岐しても、 LLM が「埋めるべきフィールド」を毎回判定する負荷が増え、 失敗率が上がる。不採用
- **`expected_kpi` を最初から必須:** 初期はノイズが多すぎ、 必須化のメリットが薄い。 運用 1〜2 ヶ月後に再評価

## 関連

- ADR-0002: マスター LLM クラウド + 抽象化
- ADR-0004: 投稿規模 1日1〜2本
- ADR-0006: 日次/週次サイクル分離
- ADR-0007: dryrun モード MVP 必須
- ADR-0008: マスター LLM の責務範囲
- ADR-0017: directive parser
- ADR-0018: Pydantic structured output
- ADR-0024: LLM コストトラッキング
- ADR-0028: 5 種類のエラーカテゴリ
