"""週次サイクルの改善計画(WeeklyPlan)生成サービス(ADR-0032 / ADR-0006 / ADR-0004)。

:mod:`ymg_backend.domain.plans.planner` の :class:`~...planner.PlanGenerator`(日次)と
同じ「集計 → 履歴 → LLM 生成 → 辞書照合 → 永続化」の流れを **週次向けに踏襲**する。 daily が
1 日の投稿を決める :class:`~...schemas.DailyPlan` を出すのに対し、 本サービスは 1 週間分の
ジャンル配分 / 回避ジャンル / 実験枠を :class:`~...schemas.WeeklyPlan` として 1 回で生成する。

処理の流れ(daily と対応):

1. **集計**: ``analytics_daily`` を ``videos`` と結合し、 直近 N 日(既定 14 日)のジャンル別
   retention / views / 本数を要約する(:meth:`WeeklyPlanGenerator._aggregate_metrics`)。 併せて
   直近 M 件の週次 plan 履歴(rationale 込み)を読む。
2. **生成**: ``prompts/planner/system_v<N>.md`` を system(週次専用 system が無ければ daily の
   planner system を流用)、 集計サマリ / 履歴 / 許可ジャンル / 週次固有の指示(配分合計 1.0 /
   ``avoid_genres`` / ``experiment_slots``)を user prompt に組み、
   ``provider.generate(LlmRequest[WeeklyPlan])`` で構造化出力を得る。
3. **辞書照合**: provider は ``response_model.model_validate`` を **context なし**で呼ぶため
   (``openai_provider.py`` / ``anthropic_provider.py``)、 ジャンル辞書照合がスキップされる。
   本サービスが ``context={ALLOWED_GENRES_CONTEXT_KEY: allowed_genres}`` で **再検証**して
   ``genre_distribution`` / ``avoid_genres`` / ``experiment_slots[].genre`` の辞書外ジャンルや
   配分合計 1.0 逸脱を弾く(ADR-0032 (3)(4))。
4. **永続化**(:meth:`WeeklyPlanGenerator.create_weekly_plan`): :class:`~...models.Plan`
   (``cycle="weekly"`` / ``target_week_start`` / ``target_date=None`` / ``status="generated"`` /
   ``llm_*``)と再現性確保用の :class:`~...models.PlanMetricSnapshot` を書き、 LLM 呼び出しを
   ``usage_log`` に記録する。 commit はオーケストレータ責務で、 ここでは ``session.flush()`` まで。

エラー分類(ADR-0028):
    LLM が辞書外ジャンル / 配分合計逸脱の計画を返した場合は :class:`~...errors.QualityError`
    として送出する。 provider が投げる :class:`~ymg_backend.llm.base.LlmError`(transient /
    recoverable 等)はそのまま伝播させ、 オーケストレータのリトライ / カテゴリ分岐に委ねる。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import Integer, cast, func, select

from ymg_backend.domain.errors import QualityError
from ymg_backend.domain.plans.planner import (
    MetricSnapshot,
    _GenreMetric,
    _PlanHistoryItem,
    _resolve_provider_name,
    _round_or_none,
    _to_float,
)
from ymg_backend.domain.plans.schemas import (
    ALLOWED_GENRES_CONTEXT_KEY,
    WeeklyPlan,
)
from ymg_backend.domain.prompts.loader import PromptLoader
from ymg_backend.infrastructure.db.models import (
    AnalyticsDaily,
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

# 週次 planner の context_type(usage_log / LlmRequest.context_type、 ADR-0024)。
# daily と同じ "planner" を用いてコスト計上の文脈を揃える(集計クエリで区別しやすくする)。
_CONTEXT_TYPE: Final[str] = "planner"

# planner system prompt の領域・名前(``prompts/planner/system_v<N>.md``)。
# 週次専用 system が無ければ daily の planner system を流用し、 週次指示は user prompt 側で与える。
_PROMPT_AREA: Final[str] = "planner"
_DEFAULT_SYSTEM_NAME: Final[str] = "system"

# 集計ウィンドウ / 履歴件数の既定値。 週次は daily(7 日)より長い 14 日を見る(ADR-0032)。
_DEFAULT_METRIC_WINDOW_DAYS: Final[int] = 14
_DEFAULT_HISTORY_LIMIT: Final[int] = 5

# 計画判断は適度な多様性を持たせる(過度な決定論で量産感を出さない、 ADR-0032)。
_PLANNER_TEMPERATURE: Final[float] = 0.7
# 構造化出力(WeeklyPlan)を出し切るための上限(配分 / 実験枠 / 長文 rationale 込み)。
_PLANNER_MAX_TOKENS: Final[int] = 2048

# 集計に集めるジャンル別行の上限(プロンプト肥大化を防ぐ安全弁。 daily と同値)。
_MAX_GENRE_ROWS: Final[int] = 12

# ``supported_models`` が空の異常時に使う model フォールバック(NOT NULL 列対策)。
_UNKNOWN_MODEL: Final[str] = "unknown"


class WeeklyPlanGenerator:
    """analytics 集計 + 履歴から :class:`WeeklyPlan` を生成・永続化する週次 planner サービス。

    :class:`~ymg_backend.domain.plans.planner.PlanGenerator`(日次)と同じく **ステートレス**
    (:class:`AsyncSession` は各メソッド引数で受け渡し、 内部で生成しない)で、 不変フィールドのみ
    を保持する。 :class:`LlmProvider` と :class:`PromptLoader` を注入し、 集計ウィンドウ / 履歴件数
    は構築時に固定する。
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
        """週次 planner サービスを構築する。

        Args:
            provider: 計画生成に使う :class:`LlmProvider`(重い判断用の主力モデル)。
            prompt_loader: prompt 解決ローダ。 省略時は既定の ``backend/prompts``。
            system_prompt_name: ``planner/<name>_v<N>.md`` の名前(既定 ``system``)。
            system_prompt_version: 明示バージョン。 ``None`` なら最新版を解決する。
            metric_window_days: analytics 集計ウィンドウ日数(1 以上、 週次の既定 14)。
            history_limit: 参照する直近の週次 plan 履歴件数(0 以上)。

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

    async def generate_weekly_plan(
        self,
        *,
        session: AsyncSession | None,
        target_week_start: date,
        allowed_genres: list[str],
    ) -> tuple[WeeklyPlan, LlmUsage]:
        """集計 + 履歴から :class:`WeeklyPlan` を生成し、 ``(plan, usage)`` を返す(契約準拠)。

        永続化は行わず(:meth:`create_weekly_plan` が担う)、 LLM 生成と辞書照合のみを行う。
        ``session`` が ``None`` の場合は集計をスキップし、 空サマリで生成する(ユニットテスト /
        analytics 未収集の初期運用を許容)。

        Args:
            session: ``analytics_daily`` / ``plans`` を参照する AsyncSession(``None`` 可)。
            target_week_start: 計画対象週の月曜日(JST)。
            allowed_genres: 許可ジャンル名の一覧(``Genre.name``、 例 ``"lo-fi hip-hop"``)。
                prompt への注入と生成後の辞書照合の両方に使う。

        Returns:
            検証済み :class:`WeeklyPlan` と LLM 呼び出しの :class:`LlmUsage`。

        Raises:
            QualityError: LLM が辞書外ジャンル / 配分合計逸脱の計画を返した場合。
            ~ymg_backend.llm.base.LlmError: provider 側のエラー(transient / recoverable 等)。
        """
        snapshot = await self._aggregate_metrics(session, target_week_start)
        history = await self._load_plan_history(session)
        plan, usage = await self._invoke_llm(
            target_week_start=target_week_start,
            allowed_genres=allowed_genres,
            snapshot=snapshot,
            history=history,
        )
        logger.info(
            "weekly_planner.generate_weekly_plan produced plan",
            target_week_start=target_week_start.isoformat(),
            genres=sorted(plan.genre_distribution),
            avoid_genres=plan.avoid_genres,
            experiment_slots=len(plan.experiment_slots),
            cost_usd=usage.cost_usd,
        )
        return plan, usage

    async def create_weekly_plan(
        self,
        *,
        session: AsyncSession,
        target_week_start: date,
        allowed_genres: list[str],
    ) -> Plan:
        """:class:`WeeklyPlan` を生成し、 ``Plan`` + ``PlanMetricSnapshot`` を永続化して返す。

        :meth:`generate_weekly_plan` で計画を得たうえで、 ``plans`` 行(``cycle="weekly"`` /
        ``target_week_start`` / ``target_date=None`` / ``status="generated"`` / ``llm_*`` 記録)と
        再現性確保用の ``plan_metric_snapshot`` 行を書き、 LLM 呼び出しを ``usage_log`` に記録する。
        commit はオーケストレータ責務(ここでは ``flush`` まで)。

        Args:
            session: 永続化に使う AsyncSession(commit は呼び出し側)。
            target_week_start: 計画対象週の月曜日(JST)。
            allowed_genres: 許可ジャンル名の一覧。

        Returns:
            永続化済み(flush 済み)の :class:`Plan` レコード。 ``id`` は採番済み。

        Raises:
            QualityError: LLM が辞書外ジャンル / 配分合計逸脱の計画を返した場合。
            ~ymg_backend.llm.base.LlmError: provider 側のエラー。
        """
        snapshot = await self._aggregate_metrics(session, target_week_start)
        history = await self._load_plan_history(session)
        plan, usage = await self._invoke_llm(
            target_week_start=target_week_start,
            allowed_genres=allowed_genres,
            snapshot=snapshot,
            history=history,
        )
        plan_row = await self._persist_plan(
            session=session,
            target_week_start=target_week_start,
            plan=plan,
            usage=usage,
            snapshot=snapshot,
        )
        logger.info(
            "weekly_planner.create_weekly_plan persisted plan",
            plan_id=str(plan_row.id),
            target_week_start=target_week_start.isoformat(),
            cost_usd=usage.cost_usd,
            provider=plan_row.llm_provider,
        )
        return plan_row

    # ------------------------------------------------------------------
    # 集計 / 履歴(daily PlanGenerator の手法を週次ウィンドウで踏襲)
    # ------------------------------------------------------------------
    async def _aggregate_metrics(
        self,
        session: AsyncSession | None,
        target_week_start: date,
    ) -> MetricSnapshot:
        """直近 N 日の ``analytics_daily`` と ``videos`` を結合しジャンル別に集計する。

        ``session`` が ``None`` なら空スナップショット(サンプル 0)を返す。 集計ウィンドウは
        ``[target_week_start - window_days, target_week_start - 1]``(対象週の開始前までを見る)。
        """
        window_end = target_week_start - timedelta(days=1)
        window_start = target_week_start - timedelta(days=self._metric_window_days)
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
        """直近の **週次** plan 履歴(rationale 込み)を新しい順に取得する。"""
        if session is None or self._history_limit == 0:
            return ()
        stmt = (
            select(Plan.target_week_start, Plan.rationale)
            .where(Plan.cycle == "weekly")
            .order_by(Plan.created_at.desc())
            .limit(self._history_limit)
        )
        rows = (await session.execute(stmt)).all()
        return tuple(
            _PlanHistoryItem(target_date=row.target_week_start, rationale=row.rationale)
            for row in rows
        )

    # ------------------------------------------------------------------
    # LLM 呼び出し / 辞書照合
    # ------------------------------------------------------------------
    async def _invoke_llm(
        self,
        *,
        target_week_start: date,
        allowed_genres: list[str],
        snapshot: MetricSnapshot,
        history: tuple[_PlanHistoryItem, ...],
    ) -> tuple[WeeklyPlan, LlmUsage]:
        """system + 集計/履歴/週次指示 user prompt で LLM を呼び、 辞書照合して返す。"""
        system_text = self._load_system_prompt()
        user_text = self._build_user_prompt(
            target_week_start=target_week_start,
            allowed_genres=allowed_genres,
            snapshot=snapshot,
            history=history,
        )
        request: LlmRequest[WeeklyPlan] = LlmRequest(
            messages=[
                LlmMessage(role="system", content=system_text, cacheable=True),
                LlmMessage(role="user", content=user_text),
            ],
            response_model=WeeklyPlan,
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
        plan: WeeklyPlan,
        allowed_genres: list[str],
    ) -> WeeklyPlan:
        """provider が context なしで生成した計画をジャンル辞書付きで再検証する(ADR-0032 (4))。

        provider 実装(``openai_provider`` / ``anthropic_provider``)は
        ``response_model.model_validate`` を **context なし**で呼ぶため辞書照合がスキップされる。
        ここで ``allowed_genres`` を注入して再検証し、 ``genre_distribution`` / ``avoid_genres`` /
        ``experiment_slots[].genre`` の辞書外ジャンルや配分合計 1.0 逸脱を品質低下として弾く。
        ``allowed_genres`` が空なら辞書照合をスキップする(辞書を持たない経路を許容)が、 配分合計
        の検証(:class:`WeeklyPlan` の validator)は常に走る。
        """
        try:
            return WeeklyPlan.model_validate(
                plan.model_dump(),
                context={ALLOWED_GENRES_CONTEXT_KEY: allowed_genres},
            )
        except ValidationError as exc:
            raise QualityError(
                "weekly planner output failed genre dictionary validation",
                context={
                    "allowed_genres": list(allowed_genres),
                    "genre_distribution": sorted(plan.genre_distribution),
                    "avoid_genres": list(plan.avoid_genres),
                    "experiment_genres": [s.genre for s in plan.experiment_slots],
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
        target_week_start: date,
        plan: WeeklyPlan,
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
            cycle="weekly",
            target_date=None,
            target_week_start=target_week_start,
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

    def _build_user_prompt(
        self,
        *,
        target_week_start: date,
        allowed_genres: list[str],
        snapshot: MetricSnapshot,
        history: tuple[_PlanHistoryItem, ...],
    ) -> str:
        """集計サマリ / 履歴 / 許可ジャンル / 週次固有の指示を user prompt に組む。

        daily system prompt を流用するため、 週次特有の出力契約(``genre_distribution`` 合計 1.0 /
        ``avoid_genres`` / ``experiment_slots``)は本 user prompt 側で明示する。
        """
        genre_lines = "\n".join(f"- {g}" for g in allowed_genres) or "- (なし)"
        history_block = self._format_history(history)
        week_end = target_week_start + timedelta(days=6)
        sections = [
            "# 週次改善計画(WeeklyPlan)の作成",
            "あなたは 1 週間分のジャンル運用方針を決める。 1 日単位の投稿ではなく、",
            " 週全体のジャンル配分・回避ジャンル・実験枠を WeeklyPlan として返すこと。",
            "",
            "## 対象週(target_week_start = 月曜日, JST)",
            f"{target_week_start.isoformat()} 〜 {week_end.isoformat()}(月〜日)",
            "",
            "## 使用可能ジャンル(これ以外は禁止。 完全一致で指定)",
            genre_lines,
            "",
            f"## 過去 {snapshot.window_days} 日の analytics 集計サマリ",
            f"集計期間: {snapshot.window_start.isoformat()} 〜 {snapshot.window_end.isoformat()}"
            f"(対象本数 {snapshot.total_samples})",
            snapshot.summary_text,
            "",
            "## 直近の週次 plan 履歴(新しい順、 rationale 込み)",
            history_block,
            "",
            "## 出力契約(必ず守る)",
            "- genre_distribution: 使用可能ジャンルのみをキーにした比率。 値の合計は 1.0(±0.01)。",
            "- avoid_genres: 今週は投入を避けるジャンル(伸び悩み等)。 使用可能ジャンルから選ぶ。",
            "- experiment_slots: 新規流入を狙う実験枠(0〜数件)。 各枠に genre / rationale /",
            "  success_criteria / slot_count(1〜3)を付ける。 genre は使用可能ジャンルから選ぶ。",
            "- rationale: 上の集計サマリの数値を具体的に引用し、 配分・回避・実験の判断根拠を述べる",
            "  (50 文字以上)。",
            "- referenced_metrics: window_days / sample_size / top_metrics_summary を上の集計に合わせる。",
            "",
            "上記を踏まえ、 制約を満たす WeeklyPlan の JSON を 1 つだけ返してください。",
        ]
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
                " 保守的に主力ジャンルへ寄せ、 実験枠は最小限にとどめる。"
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
            " retention 上位ジャンルを厚めに配分、 低調ジャンルは avoid を検討。"
        )

    @staticmethod
    def _format_history(history: tuple[_PlanHistoryItem, ...]) -> str:
        """直近の週次 plan 履歴を箇条書きに整形する(空なら ``"(履歴なし)"``)。"""
        if not history:
            return "(履歴なし)"
        lines: list[str] = []
        for item in history:
            week = item.target_date.isoformat() if item.target_date else "(週開始不明)"
            lines.append(f"- 週開始 {week}: {item.rationale}")
        return "\n".join(lines)

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


__all__ = [
    "WeeklyPlanGenerator",
]
