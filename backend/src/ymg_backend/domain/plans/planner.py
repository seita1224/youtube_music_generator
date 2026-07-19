"""改善計画(DailyPlan)生成サービス(ADR-0032 / ADR-0006 / ADR-0004)。

「次に何を作るか」を決める重い LLM 呼び出し(planner)を担う。 :mod:`finisher` が
directive の ``{{自由文}}`` を展開する軽い仕上げ層だったのに対し、 本サービスは過去の
analytics 集計と直近の plan 履歴を踏まえてジャンル / ムード / 視覚意図 / directive を
:class:`~ymg_backend.domain.plans.schemas.DailyPlan` として 1 回で生成する。

処理の流れ:

1. **集計**: ``analytics_daily`` を ``videos`` と結合し、 直近 N 日のジャンル別 retention /
   views / 本数を要約する(:meth:`PlanGenerator._aggregate_metrics`)。 併せて直近 M 件の
   plan 履歴(rationale 込み)を読む。
2. **生成**: ``prompts/planner/system_v<N>.md`` を system、 few-shot(``few_shot_v<N>.json``)と
   集計サマリ / 履歴 / 許可ジャンルを user prompt に組み、
   ``provider.generate(LlmRequest[DailyPlan])`` で構造化出力を得る。
3. **辞書照合**: provider は ``response_model.model_validate`` を **context なし**で呼ぶため
   (``openai_provider.py`` / ``anthropic_provider.py``)、 ジャンル辞書照合がスキップされる。
   本サービスが ``context={ALLOWED_GENRES_CONTEXT_KEY: allowed_genres}`` で **再検証**して
   辞書外ジャンルを弾く(ADR-0032 (4))。
4. **永続化**(:meth:`PlanGenerator.create_daily_plan`): :class:`~...models.Plan`
   (``status="generated"`` / ``llm_provider`` / ``llm_model`` / ``llm_prompt_version`` /
   ``llm_cost_usd``)と再現性確保用の :class:`~...models.PlanMetricSnapshot` を書き、
   LLM 呼び出しを ``usage_log`` に記録する。 commit はオーケストレータ責務で、 ここでは
   ``session.flush()`` まで(``write_audit_log`` / ``write_usage_log`` と同方針)。

エラー分類(ADR-0028):
    LLM が辞書外ジャンルや制約違反の計画を返した場合は
    :class:`~ymg_backend.domain.errors.QualityError`(該当部分のみスキップしデフォルトで
    続行できる品質低下)として送出する。 provider が投げる
    :class:`~ymg_backend.llm.base.LlmError`(transient / recoverable 等)はそのまま伝播させ、
    オーケストレータのリトライ / カテゴリ分岐に委ねる。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import Integer, cast, func, select

from ymg_backend.domain.errors import QualityError
from ymg_backend.domain.plans.schemas import (
    ALLOWED_GENRES_CONTEXT_KEY,
    DailyPlan,
)
from ymg_backend.domain.prompts.loader import PromptLoader, PromptNotFoundError
from ymg_backend.infrastructure.db.models import (
    AnalyticsDaily,
    DryrunOutput,
    Plan,
    PlanMetricSnapshot,
    Video,
)
from ymg_backend.llm.base import (
    LlmMessage,
    LlmProvider,
    LlmProviderName,
    LlmRequest,
    LlmUsage,
)
from ymg_backend.llm.usage_writer import write_usage_log

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# planner の context_type(usage_log / LlmRequest.context_type、 ADR-0024)。
_CONTEXT_TYPE: Final[str] = "planner"

# planner system prompt / few-shot の領域・名前(``prompts/planner/system_v1.md`` 等)。
_PROMPT_AREA: Final[str] = "planner"
_DEFAULT_SYSTEM_NAME: Final[str] = "system"
_FEW_SHOT_NAME: Final[str] = "few_shot"

# 集計ウィンドウ / 履歴件数の既定値(ADR-0032 「過去 N 日」「直近 N=5」)。
_DEFAULT_METRIC_WINDOW_DAYS: Final[int] = 7
_DEFAULT_HISTORY_LIMIT: Final[int] = 5

# session 渡しで否認理由を自動集約する際に DB から拾う直近 rejected の件数上限(US2 契約 (d))。
# プロンプト肥大化を抑えつつ「直近に避けるべき理由」を十分カバーする件数。
_DEFAULT_REJECTED_REASONS_LIMIT: Final[int] = 10

# 計画判断は適度な多様性を持たせる(過度な決定論で量産感を出さない、 ADR-0032)。
_PLANNER_TEMPERATURE: Final[float] = 0.7
# 構造化出力(DailyPlan)を出し切るための上限。 directive 込みでも収まる余裕を持つ。
_PLANNER_MAX_TOKENS: Final[int] = 2048

# 集計に集めるジャンル別行の上限(プロンプト肥大化を防ぐ安全弁)。
_MAX_GENRE_ROWS: Final[int] = 12


@dataclass(frozen=True, slots=True)
class _GenreMetric:
    """1 ジャンル分の集計指標(集計ウィンドウ内)。"""

    genre: str
    sample_size: int
    avg_retention_pct: float | None
    avg_views: float | None


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """planner に渡した analytics 集計の不変スナップショット(再現性確保用)。

    ``metrics`` は :class:`PlanMetricSnapshot` の JSONB へそのまま格納できる形(集計ウィンドウ /
    ジャンル別指標 / サンプル総数)。 ``summary_text`` は user prompt と
    :class:`~...schemas.ReferencedMetrics` の ``top_metrics_summary`` に転用する人間可読サマリ。
    """

    window_start: date
    window_end: date
    window_days: int
    total_samples: int
    genre_metrics: tuple[_GenreMetric, ...]
    summary_text: str

    def to_metrics_json(self) -> dict[str, Any]:
        """``plan_metric_snapshot.metrics`` 用の JSON シリアライズ可能 dict を返す。"""
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "window_days": self.window_days,
            "total_samples": self.total_samples,
            "genres": [
                {
                    "genre": m.genre,
                    "sample_size": m.sample_size,
                    "avg_retention_pct": m.avg_retention_pct,
                    "avg_views": m.avg_views,
                }
                for m in self.genre_metrics
            ],
            "summary_text": self.summary_text,
        }


@dataclass(frozen=True, slots=True)
class _PlanHistoryItem:
    """直近 plan 履歴の 1 件(rationale 込み、 重複回避の参考用)。"""

    target_date: date | None
    rationale: str


def _to_float(value: Decimal | float | None) -> float | None:
    """``Decimal`` / ``float`` / ``None`` を ``float | None`` に正規化する(JSON 用)。"""
    if value is None:
        return None
    return float(value)


def _round_or_none(value: float | None, *, digits: int = 1) -> float | None:
    """``None`` を保ちつつ丸める(サマリ表示の桁数を揃える)。"""
    return None if value is None else round(value, digits)


class PlanGenerator:
    """analytics 集計 + 履歴から :class:`DailyPlan` を生成・永続化する planner サービス。

    ステートレス(:class:`AsyncSession` は各メソッド引数で受け渡し、 内部で生成しない)で、
    不変フィールドのみを保持する。 :class:`LlmProvider` と :class:`PromptLoader` を注入し、
    集計ウィンドウ / 履歴件数は構築時に固定する。
    """

    def __init__(
        self,
        provider: LlmProvider,
        *,
        prompt_loader: PromptLoader | None = None,
        system_prompt_name: str = _DEFAULT_SYSTEM_NAME,
        system_prompt_version: int | None = None,
        metric_window_days: int = _DEFAULT_METRIC_WINDOW_DAYS,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
    ) -> None:
        """planner サービスを構築する。

        Args:
            provider: 計画生成に使う :class:`LlmProvider`(重い判断用の主力モデル)。
            prompt_loader: prompt 解決ローダ。 省略時は既定の ``backend/prompts``。
            system_prompt_name: ``planner/<name>_v<N>.md`` の名前(既定 ``system``)。
            system_prompt_version: 明示バージョン。 ``None`` なら最新版を解決する。
            metric_window_days: analytics 集計ウィンドウ日数(1 以上)。
            history_limit: 参照する直近 plan 履歴件数(0 以上)。

        Raises:
            ValueError: ``metric_window_days < 1`` または ``history_limit < 0`` の場合。
        """
        if metric_window_days < 1:
            raise ValueError("metric_window_days must be >= 1")
        if history_limit < 0:
            raise ValueError("history_limit must be >= 0")
        self._provider: Final[LlmProvider] = provider
        self._prompt_loader: Final[PromptLoader] = prompt_loader or PromptLoader()
        self._system_prompt_name: Final[str] = system_prompt_name
        self._system_prompt_version: Final[int | None] = system_prompt_version
        self._metric_window_days: Final[int] = metric_window_days
        self._history_limit: Final[int] = history_limit

    async def generate_daily_plan(
        self,
        *,
        session: AsyncSession | None,
        target_date: date,
        allowed_genres: list[str],
        rejected_reasons: list[str] | None = None,
    ) -> tuple[DailyPlan, LlmUsage]:
        """集計 + 履歴から :class:`DailyPlan` を生成し、 ``(plan, usage)`` を返す(契約準拠)。

        永続化は行わず(:meth:`create_daily_plan` が担う)、 LLM 生成と辞書照合のみを行う。
        ``session`` が ``None`` の場合は集計をスキップし、 空サマリで生成する(ユニットテスト /
        analytics 未収集の初期運用を許容)。

        Args:
            session: ``analytics_daily`` / ``plans`` を参照する AsyncSession(``None`` 可)。
            target_date: 計画対象日(JST)。
            allowed_genres: 許可ジャンル名の一覧(``Genre.name``、 例 ``"lo-fi hip-hop"``)。
                prompt への注入と生成後の辞書照合の両方に使う。
            rejected_reasons: 直近の否認理由(新しい順)を user prompt の「避ける理由」節へ
                注入する(US2 契約 (d))。 ``None`` で ``session`` がある場合は直近の
                ``DryrunOutput.reject_reason`` を自動集約する。 空 / 不在なら注入をスキップ。

        Returns:
            検証済み :class:`DailyPlan` と LLM 呼び出しの :class:`LlmUsage`。

        Raises:
            QualityError: LLM が辞書外ジャンル / 制約違反の計画を返した場合。
            ~ymg_backend.llm.base.LlmError: provider 側のエラー(transient / recoverable 等)。
        """
        snapshot = await self._aggregate_metrics(session, target_date)
        history = await self._load_plan_history(session)
        reasons = await self._resolve_rejected_reasons(session, rejected_reasons)
        plan, usage = await self._invoke_llm(
            target_date=target_date,
            allowed_genres=allowed_genres,
            snapshot=snapshot,
            history=history,
            rejected_reasons=reasons,
        )
        logger.info(
            "planner.generate_daily_plan produced plan",
            target_date=target_date.isoformat(),
            posts=len(plan.posts),
            genres=[p.genre for p in plan.posts],
            cost_usd=usage.cost_usd,
        )
        return plan, usage

    async def create_daily_plan(
        self,
        *,
        session: AsyncSession,
        target_date: date,
        allowed_genres: list[str],
        rejected_reasons: list[str] | None = None,
    ) -> Plan:
        """:class:`DailyPlan` を生成し、 ``Plan`` + ``PlanMetricSnapshot`` を永続化して返す。

        :meth:`generate_daily_plan` で計画を得たうえで、 ``plans`` 行(``status="generated"`` /
        ``llm_*`` 記録)と再現性確保用の ``plan_metric_snapshot`` 行を書き、 LLM 呼び出しを
        ``usage_log`` に記録する。 commit はオーケストレータ責務(ここでは ``flush`` まで)。

        Args:
            session: 永続化に使う AsyncSession(commit は呼び出し側)。
            target_date: 計画対象日(JST)。
            allowed_genres: 許可ジャンル名の一覧。
            rejected_reasons: 直近の否認理由(新しい順)。 ``None`` の場合は直近の
                ``DryrunOutput.reject_reason`` を自動集約して prompt へ注入する(US2 契約 (d))。

        Returns:
            永続化済み(flush 済み)の :class:`Plan` レコード。 ``id`` は採番済み。

        Raises:
            QualityError: LLM が辞書外ジャンル / 制約違反の計画を返した場合。
            ~ymg_backend.llm.base.LlmError: provider 側のエラー。
        """
        snapshot = await self._aggregate_metrics(session, target_date)
        history = await self._load_plan_history(session)
        reasons = await self._resolve_rejected_reasons(session, rejected_reasons)
        plan, usage = await self._invoke_llm(
            target_date=target_date,
            allowed_genres=allowed_genres,
            snapshot=snapshot,
            history=history,
            rejected_reasons=reasons,
        )
        plan_row = await self._persist_plan(
            session=session,
            plan=plan,
            usage=usage,
            snapshot=snapshot,
        )
        logger.info(
            "planner.create_daily_plan persisted plan",
            plan_id=str(plan_row.id),
            target_date=target_date.isoformat(),
            cost_usd=usage.cost_usd,
            provider=plan_row.llm_provider,
        )
        return plan_row

    # ------------------------------------------------------------------
    # 集計 / 履歴
    # ------------------------------------------------------------------
    async def _aggregate_metrics(
        self,
        session: AsyncSession | None,
        target_date: date,
    ) -> MetricSnapshot:
        """直近 N 日の ``analytics_daily`` と ``videos`` を結合しジャンル別に集計する。

        ``session`` が ``None`` なら空スナップショット(サンプル 0)を返す。 集計ウィンドウは
        ``[target_date - window_days, target_date - 1]``(対象日当日はまだ計測前のため除外)。
        """
        window_end = target_date - timedelta(days=1)
        window_start = target_date - timedelta(days=self._metric_window_days)
        if session is None:
            return MetricSnapshot(
                window_start=window_start,
                window_end=window_end,
                window_days=self._metric_window_days,
                total_samples=0,
                genre_metrics=(),
                summary_text="analytics 未取得(集計対象なし)。 保守的に主力ジャンルへ寄せる。",
            )

        stmt = (
            select(
                Video.genre.label("genre"),
                func.count().label("sample_size"),
                func.avg(AnalyticsDaily.retention_pct).label("avg_retention_pct"),
                func.avg(cast(AnalyticsDaily.views, Integer)).label("avg_views"),
            )
            .select_from(AnalyticsDaily)
            .join(Video, Video.youtube_video_id == AnalyticsDaily.youtube_video_id)
            .where(AnalyticsDaily.metric_date >= window_start)
            .where(AnalyticsDaily.metric_date <= window_end)
            .group_by(Video.genre)
            .order_by(func.avg(AnalyticsDaily.retention_pct).desc().nullslast())
            .limit(_MAX_GENRE_ROWS)
        )
        rows = (await session.execute(stmt)).all()
        genre_metrics = tuple(
            _GenreMetric(
                genre=row.genre,
                sample_size=int(row.sample_size),
                avg_retention_pct=_round_or_none(_to_float(row.avg_retention_pct)),
                avg_views=_round_or_none(_to_float(row.avg_views), digits=0),
            )
            for row in rows
        )
        total_samples = sum(m.sample_size for m in genre_metrics)
        summary_text = self._format_metric_summary(
            genre_metrics, window_days=self._metric_window_days, total_samples=total_samples
        )
        return MetricSnapshot(
            window_start=window_start,
            window_end=window_end,
            window_days=self._metric_window_days,
            total_samples=total_samples,
            genre_metrics=genre_metrics,
            summary_text=summary_text,
        )

    async def _load_plan_history(
        self,
        session: AsyncSession | None,
    ) -> tuple[_PlanHistoryItem, ...]:
        """直近の daily plan 履歴(rationale 込み)を新しい順に取得する。"""
        if session is None or self._history_limit == 0:
            return ()
        stmt = (
            select(Plan.target_date, Plan.rationale)
            .where(Plan.cycle == "daily")
            .order_by(Plan.created_at.desc())
            .limit(self._history_limit)
        )
        rows = (await session.execute(stmt)).all()
        return tuple(
            _PlanHistoryItem(target_date=row.target_date, rationale=row.rationale) for row in rows
        )

    async def _resolve_rejected_reasons(
        self,
        session: AsyncSession | None,
        explicit: list[str] | None,
    ) -> tuple[str, ...]:
        """user prompt の「避ける理由」節へ注入する否認理由を解決する(US2 契約 (d))。

        ``explicit`` が渡された場合はそれを優先する(空文字 / 空白のみは除去)。 ``None`` で
        ``session`` がある場合は直近の ``DryrunOutput.reject_reason``(``state == "rejected"``)を
        新しい順に最大 :data:`_DEFAULT_REJECTED_REASONS_LIMIT` 件まで自動集約する。 ``session``
        も無く ``explicit`` も無ければ空タプル(注入スキップ)。
        """
        if explicit is not None:
            return tuple(r.strip() for r in explicit if r and r.strip())
        if session is None:
            return ()
        stmt = (
            select(DryrunOutput.reject_reason)
            .where(DryrunOutput.state == "rejected")
            .where(DryrunOutput.reject_reason.is_not(None))
            .order_by(DryrunOutput.reviewed_at.desc().nullslast())
            .limit(_DEFAULT_REJECTED_REASONS_LIMIT)
        )
        rows = (await session.execute(stmt)).all()
        stripped = (row.reject_reason.strip() for row in rows if row.reject_reason)
        return tuple(reason for reason in stripped if reason)

    # ------------------------------------------------------------------
    # LLM 呼び出し / 辞書照合
    # ------------------------------------------------------------------
    async def _invoke_llm(
        self,
        *,
        target_date: date,
        allowed_genres: list[str],
        snapshot: MetricSnapshot,
        history: tuple[_PlanHistoryItem, ...],
        rejected_reasons: tuple[str, ...] = (),
    ) -> tuple[DailyPlan, LlmUsage]:
        """system + few-shot + 集計/履歴 user prompt で LLM を呼び、 辞書照合して返す。"""
        system_text = self._load_system_prompt()
        few_shot_text = self._load_few_shot_text()
        user_text = self._build_user_prompt(
            target_date=target_date,
            allowed_genres=allowed_genres,
            snapshot=snapshot,
            history=history,
            few_shot_text=few_shot_text,
            rejected_reasons=rejected_reasons,
        )
        request: LlmRequest[DailyPlan] = LlmRequest(
            messages=[
                LlmMessage(role="system", content=system_text, cacheable=True),
                LlmMessage(role="user", content=user_text),
            ],
            response_model=DailyPlan,
            temperature=_PLANNER_TEMPERATURE,
            max_tokens=_PLANNER_MAX_TOKENS,
            context_type=_CONTEXT_TYPE,
            context_id=None,
            prompt_version=self._system_prompt_ref(),
        )
        response = await self._provider.generate(request)
        plan = self._enforce_genre_dictionary(response.parsed, allowed_genres)
        return plan, response.usage

    def _enforce_genre_dictionary(
        self,
        plan: DailyPlan,
        allowed_genres: list[str],
    ) -> DailyPlan:
        """provider が context なしで生成した計画をジャンル辞書付きで再検証する(ADR-0032 (4))。

        provider 実装(``openai_provider`` / ``anthropic_provider``)は
        ``response_model.model_validate`` を **context なし**で呼ぶため辞書照合がスキップされる。
        ここで ``allowed_genres`` を注入して再検証し、 辞書外ジャンルや制約違反を品質低下として弾く。
        ``allowed_genres`` が空なら照合をスキップする(辞書を持たない経路を許容)。
        """
        try:
            return DailyPlan.model_validate(
                plan.model_dump(),
                context={ALLOWED_GENRES_CONTEXT_KEY: allowed_genres},
            )
        except ValidationError as exc:
            raise QualityError(
                "planner output failed genre dictionary validation",
                context={
                    "allowed_genres": list(allowed_genres),
                    "genres": [p.genre for p in plan.posts],
                },
                original=exc,
            ) from exc

    # ------------------------------------------------------------------
    # 永続化
    # ------------------------------------------------------------------
    async def _persist_plan(
        self,
        *,
        session: AsyncSession,
        plan: DailyPlan,
        usage: LlmUsage,
        snapshot: MetricSnapshot,
    ) -> Plan:
        """``plans`` / ``plan_metric_snapshot`` / ``usage_log`` を書き、 flush して ``Plan`` を返す。"""
        plan_id = uuid.uuid4()
        provider_name = self._provider_name()
        model = self._provider_model()
        prompt_ref = self._system_prompt_ref()
        plan_row = Plan(
            id=plan_id,
            cycle="daily",
            target_date=plan.target_date,
            target_week_start=None,
            payload=plan.model_dump(mode="json"),
            rationale=plan.rationale,
            status="generated",
            llm_provider=provider_name,
            llm_model=model,
            llm_prompt_version=prompt_ref,
            llm_cost_usd=Decimal(str(usage.cost_usd)),
        )
        session.add(plan_row)
        session.add(
            PlanMetricSnapshot(
                plan_id=plan_id,
                metric_window_start=snapshot.window_start,
                metric_window_end=snapshot.window_end,
                metrics=snapshot.to_metrics_json(),
            )
        )
        await session.flush()
        await write_usage_log(
            session,
            provider=provider_name,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            cached_tokens=usage.cached_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=Decimal(str(usage.cost_usd)),
            duration_ms=usage.duration_ms,
            context_type=_CONTEXT_TYPE,
            context_id=plan_id,
            prompt_version=prompt_ref,
        )
        return plan_row

    # ------------------------------------------------------------------
    # prompt 組み立て
    # ------------------------------------------------------------------
    def _load_system_prompt(self) -> str:
        """planner system prompt(``planner/<name>_v<N>.md``)を解決する。"""
        resolved = self._prompt_loader.load(
            _PROMPT_AREA, self._system_prompt_name, version=self._system_prompt_version
        )
        return resolved.text

    def _system_prompt_ref(self) -> str:
        """``plans.llm_prompt_version`` / ``LlmRequest.prompt_version`` 用の参照 ID。"""
        resolved = self._prompt_loader.load(
            _PROMPT_AREA, self._system_prompt_name, version=self._system_prompt_version
        )
        return resolved.ref

    def _load_few_shot_text(self) -> str:
        """few-shot(``planner/few_shot_v<N>.json``)を整形テキストにして返す。

        ファイル欠落(:class:`PromptNotFoundError`)は致命ではないため空文字にフォールバックし、
        few-shot なしでも生成を続行する(system prompt 末尾で「1 例提供」と述べるが任意)。
        """
        try:
            data = self._prompt_loader.load_json(_PROMPT_AREA, _FEW_SHOT_NAME)
        except PromptNotFoundError:
            logger.debug("planner few-shot not found; proceeding without examples")
            return ""
        return json.dumps(data, ensure_ascii=False, indent=2)

    def _build_user_prompt(
        self,
        *,
        target_date: date,
        allowed_genres: list[str],
        snapshot: MetricSnapshot,
        history: tuple[_PlanHistoryItem, ...],
        few_shot_text: str,
        rejected_reasons: tuple[str, ...] = (),
    ) -> str:
        """集計サマリ / 履歴 / 許可ジャンル / few-shot / 否認理由を user prompt に組む。"""
        genre_lines = "\n".join(f"- {g}" for g in allowed_genres) or "- (なし)"
        history_block = self._format_history(history)
        sections = [
            "## 対象日(target_date, JST)",
            target_date.isoformat(),
            "",
            "## 使用可能ジャンル(これ以外は禁止。 genre には完全一致で指定)",
            genre_lines,
            "",
            f"## 過去 {snapshot.window_days} 日の analytics 集計サマリ",
            f"集計期間: {snapshot.window_start.isoformat()} 〜 {snapshot.window_end.isoformat()}"
            f"(対象本数 {snapshot.total_samples})",
            snapshot.summary_text,
            "",
            "## 直近の plan 履歴(新しい順、 rationale 込み)",
            history_block,
        ]
        if rejected_reasons:
            sections.extend(
                [
                    "",
                    "## 直近の却下理由(避ける。 同様の方向性は繰り返さない)",
                    self._format_rejected_reasons(rejected_reasons),
                ]
            )
        if few_shot_text:
            sections.extend(
                [
                    "",
                    "## 良い例(few-shot、 粒度の手本。 内容は盲目的に複製しない)",
                    few_shot_text,
                ]
            )
        sections.extend(
            [
                "",
                "上記を踏まえ、 制約を満たす DailyPlan の JSON を 1 つだけ返してください。"
                " rationale には上の集計サマリの数値を具体的に引用してください。",
            ]
        )
        return "\n".join(sections)

    @staticmethod
    def _format_metric_summary(
        genre_metrics: tuple[_GenreMetric, ...],
        *,
        window_days: int,
        total_samples: int,
    ) -> str:
        """ジャンル別集計を人間可読の 1 段落サマリに整形する。

        ``ReferencedMetrics.top_metrics_summary``(``min_length=20``)にも転用できるよう、
        サンプルが無い場合でも 20 文字以上の保守的サマリを返す。
        """
        if not genre_metrics:
            return (
                f"過去 {window_days} 日の集計対象が 0 本。 サンプル不足のため"
                " 保守的に主力ジャンルへ寄せ、 探索は最小限にとどめる。"
            )
        parts: list[str] = []
        for m in genre_metrics:
            retention = "未取得" if m.avg_retention_pct is None else f"{m.avg_retention_pct}%"
            views = "未取得" if m.avg_views is None else f"{int(m.avg_views)}"
            parts.append(
                f"{m.genre}(本数{m.sample_size}, retention {retention}, 平均 views {views})"
            )
        return (
            f"過去 {window_days} 日 {total_samples} 本: " + " / ".join(parts) + "。"
            " retention 上位ジャンルを exploitation、 低調ジャンルは見送りを検討。"
        )

    @staticmethod
    def _format_history(history: tuple[_PlanHistoryItem, ...]) -> str:
        """直近 plan 履歴を箇条書きに整形する(空なら ``"(履歴なし)"``)。"""
        if not history:
            return "(履歴なし)"
        lines: list[str] = []
        for item in history:
            day = item.target_date.isoformat() if item.target_date else "(日付不明)"
            lines.append(f"- {day}: {item.rationale}")
        return "\n".join(lines)

    @staticmethod
    def _format_rejected_reasons(rejected_reasons: tuple[str, ...]) -> str:
        """直近の否認理由を箇条書きに整形する(新しい順、 呼び出し側で非空保証)。"""
        return "\n".join(f"- {reason}" for reason in rejected_reasons)

    # ------------------------------------------------------------------
    # provider メタ情報
    # ------------------------------------------------------------------
    def _provider_name(self) -> LlmProviderName:
        """``plans.llm_provider`` / ``usage_log.provider``(ENUM)用の provider 名を解決する。"""
        return _resolve_provider_name(self._provider)

    def _provider_model(self) -> str:
        """``plans.llm_model`` 用のモデル ID を解決する(``supported_models`` 先頭)。"""
        models = self._provider.supported_models()
        return models[0] if models else _UNKNOWN_MODEL


# --- provider メタ抽出ヘルパ(provider 実装に依存しすぎない緩い解決) ----------------
# クラス名 → ``llm_provider`` ENUM 値。 provider 実装(``llm/*_provider.py``)のクラス名に対応。
_PROVIDER_NAME_BY_CLASS: Final[dict[str, LlmProviderName]] = {
    "OpenAIProvider": "openai",
    "AnthropicProvider": "anthropic",
    "OllamaProvider": "ollama",
}

# ``supported_models`` が空の異常時に使う model フォールバック(NOT NULL 列対策)。
_UNKNOWN_MODEL: Final[str] = "unknown"


def _resolve_provider_name(provider: LlmProvider) -> LlmProviderName:
    """provider インスタンスから ``llm_provider`` ENUM 値を解決する。

    クラス名→ENUM 値の対応表で解決し、 未知のクラスは安全側で ``"openai"`` にフォールバック
    する(``plans.llm_provider`` / ``usage_log.provider`` は ENUM 制約があるため有効値が必要)。
    """
    return _PROVIDER_NAME_BY_CLASS.get(type(provider).__name__, "openai")


__all__ = [
    "MetricSnapshot",
    "PlanGenerator",
]
