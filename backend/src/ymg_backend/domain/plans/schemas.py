"""改善計画 LLM の出力スキーマ(ADR-0032)。

マスター LLM は「ジャンル / ムード / 視覚意図 / directive 形式の指示」を返す
(委任レベル = 半分任せる)。 ACE-Step / SDXL に渡す最終プロンプトは、
システム側のテンプレと directive parser(ADR-0017)で合成する。

Daily と Weekly はスキーマを分離する(ADR-0032 (2))。 統合 discriminator にすると
LLM が毎回「埋めるべきフィールド」を判定する必要が生じ、 失敗率が上がるため。

`genre` の辞書照合は context 注入で行う(ADR-0032 (4))::

    DailyPlan.model_validate(data, context={ALLOWED_GENRES_CONTEXT_KEY: db_genres})

context に `allowed_genres` が無い場合(空)は照合をスキップする。 これにより
辞書を持たないユニットテストや部分検証でもスキーマ自体は流せる。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

# `model_validate(..., context={...})` に渡す許可ジャンル集合のキー(ADR-0032 (4))。
ALLOWED_GENRES_CONTEXT_KEY = "allowed_genres"

# 直近の否認理由リスト(`list[str]`)を planner に受け渡す際のキー(US2 契約 (d))。
# `ALLOWED_GENRES_CONTEXT_KEY` と異なり `model_validate` の context ではなく、
# user prompt 文面への注入用(`PlanGenerator._build_user_prompt` の「避ける理由」節)。
# 空 / None の場合は注入をスキップする。
REJECTED_REASONS_CONTEXT_KEY = "rejected_reasons"

# genre_distribution 合計の許容誤差(浮動小数誤差を吸収、 ADR-0032 (3))。
_DISTRIBUTION_SUM_TOLERANCE = 0.01


def _resolve_allowed_genres(info: ValidationInfo) -> frozenset[str]:
    """ValidationInfo.context から許可ジャンル集合を取り出す。

    context 未指定 / キー無しの場合は空集合を返し、 照合をスキップさせる。
    """
    context = info.context or {}
    allowed = context.get(ALLOWED_GENRES_CONTEXT_KEY)
    if not allowed:
        return frozenset()
    return frozenset(allowed)


class ReferencedMetrics(BaseModel):
    """改善計画 LLM が参照した analytics の要約(再現性確保用)。"""

    model_config = ConfigDict(frozen=True)

    window_days: int = Field(ge=1, le=180)  # 何日分を見たか
    sample_size: int = Field(ge=0)  # 対象動画本数
    top_metrics_summary: str = Field(min_length=20)


class ExpectedKpi(BaseModel):
    """改善計画に対する予測 KPI(初期は optional、 ADR-0032 (4))。"""

    model_config = ConfigDict(frozen=True)

    retention_pct: float | None = Field(default=None, ge=0, le=100)
    expected_views_24h: int | None = Field(default=None, ge=0)


class DailyPost(BaseModel):
    """DailyPlan 内の 1 投稿分の指示(ADR-0032)。"""

    model_config = ConfigDict(frozen=True)

    genre: str  # 辞書照合(下記 validator、 context 注入)
    mood: str = Field(min_length=4)
    bpm_range: tuple[int, int] | None = None
    visual_direction: str = Field(min_length=10)  # SDXL 用の意図テキスト
    title_directive: str = Field(min_length=8)  # ADR-0017 parser 用
    description_directive: str = Field(min_length=20)
    thumbnail_directive: str | None = None
    schedule_jst: datetime | None = None

    @field_validator("genre")
    @classmethod
    def validate_genre(cls, v: str, info: ValidationInfo) -> str:
        allowed = _resolve_allowed_genres(info)
        if allowed and v not in allowed:
            raise ValueError(f"genre {v!r} not in allowed list")
        return v


class DailyPlan(BaseModel):
    """日次サイクルの改善計画(ADR-0006 / ADR-0032)。"""

    model_config = ConfigDict(frozen=True)

    cycle: Literal["daily"] = "daily"
    plan_id: str  # システム発行 UUID v7
    target_date: date  # JST
    posts: list[DailyPost] = Field(min_length=1, max_length=2)  # ADR-0004
    rationale: str = Field(min_length=20)
    referenced_metrics: ReferencedMetrics
    expected_kpi: ExpectedKpi | None = None  # 初期は optional


class ExperimentSlot(BaseModel):
    """週次計画で投入する実験ジャンル枠(ADR-0032)。"""

    model_config = ConfigDict(frozen=True)

    genre: str
    rationale: str = Field(min_length=20)
    success_criteria: str = Field(min_length=10)
    slot_count: int = Field(default=1, ge=1, le=3)

    @field_validator("genre")
    @classmethod
    def validate_genre(cls, v: str, info: ValidationInfo) -> str:
        allowed = _resolve_allowed_genres(info)
        if allowed and v not in allowed:
            raise ValueError(f"genre {v!r} not in allowed list")
        return v


class WeeklyPlan(BaseModel):
    """週次サイクルの改善計画(ADR-0006 / ADR-0032)。"""

    model_config = ConfigDict(frozen=True)

    cycle: Literal["weekly"] = "weekly"
    plan_id: str
    target_week_start: date  # 月曜日 (JST)
    genre_distribution: dict[str, float]  # 合計 1.0
    avoid_genres: list[str] = Field(default_factory=list)
    experiment_slots: list[ExperimentSlot] = Field(default_factory=list)
    rationale: str = Field(min_length=50)
    referenced_metrics: ReferencedMetrics

    @field_validator("genre_distribution")
    @classmethod
    def validate_distribution(cls, v: dict[str, float], info: ValidationInfo) -> dict[str, float]:
        if not v:
            raise ValueError("genre_distribution must not be empty")
        allowed = _resolve_allowed_genres(info)
        if allowed:
            unknown = {g for g in v if g not in allowed}
            if unknown:
                raise ValueError(f"genre_distribution contains unknown genres: {sorted(unknown)}")
        total = sum(v.values())
        if abs(total - 1.0) > _DISTRIBUTION_SUM_TOLERANCE:
            raise ValueError(f"genre_distribution must sum to 1.0, got {total}")
        return v
