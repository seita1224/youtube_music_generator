"""月次 LLM 予算の監視 / アラート (US5, T119, FR-026)。

当月の ``usage_log.cost_usd`` 合計を ``Settings.monthly_budget_usd`` と比較し、
進捗率 (``pct = total / budget * 100``) が 50% / 80% / 100% の閾値を跨いだときに
:class:`~ymg_backend.infrastructure.slack.notifier.SlackNotifier` で 1 通知を発する
(FR-026: 予算消化の段階的周知)。

設計方針:

- **集計**: 当月境界 ``[month_start, next_month_start)`` の半開区間で ``cost_usd`` を SUM する
  (``to_char`` 等の関数依存を避け、 ``created_at`` index が効く範囲条件)。
  ``usage_log.created_at`` は ``llm/usage_writer.py`` の ``usage_log_table`` では未宣言
  (writer は DB default 任せ) のため、 集計用に ``created_at`` 付きの軽量 Core Table を
  本モジュールに再宣言する (pricing / usage_writer と同じ疎結合方針)。

- **重複通知抑止**: ``app_state`` に当月で最後に通知した閾値 (``budget_alert_last_threshold``、 int)
  と対象月 (``budget_alert_month``、 ``YYYY-MM``) を保持する。 今回算出した閾値が「保存値より大きい」
  ときだけ通知し、 新しい閾値 + 当月を upsert する。 月が変われば 0 リセット。 これで同一閾値の
  再送 (cron 毎回) と降順往復 (80→50 へ戻った時) を抑える。 ``app_state_table`` は factory の
  ものを流用する (Core Table、 ORM 層非依存)。

- **副作用は業務を止めない**: 通知失敗は ``SlackNotifier`` 側が握り潰す。 本モジュールは
  dedup 状態の upsert を含むトランザクションを ``commit`` するが、 例外時は呼び出し側 (job /
  session provider) のロールバックに委ねる。

- **job からの呼出**: ``get_sessionmaker`` で独自セッションを開く :func:`run_budget_check` を
  提供し、 日次 scheduler ジョブ / LLM 呼出後フックから session を持たずに呼べるようにする。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from loguru import logger
from sqlalchemy import Column, DateTime, MetaData, Numeric, Table, func, select
from sqlalchemy.dialects.postgresql import insert

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.domain.errors.errors import NotificationLevel
from ymg_backend.infrastructure.db.session import get_sessionmaker
from ymg_backend.infrastructure.slack.notifier import SlackNotifier
from ymg_backend.llm.factory import app_state_table

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["BUDGET_THRESHOLDS_PCT", "check_budget_and_alert", "run_budget_check"]

# 予算進捗の閾値 (%)。 昇順。 跨いだ最大の閾値で 1 通知する (FR-026)。
BUDGET_THRESHOLDS_PCT: Final[tuple[int, ...]] = (50, 80, 100)

# 閾値 → Slack 通知レベル。 100%↑ は CRITICAL (mention 付与)、 80%↑ は ERROR、 50%↑ は WARN。
_THRESHOLD_LEVEL: Final[dict[int, NotificationLevel]] = {
    50: NotificationLevel.WARN,
    80: NotificationLevel.ERROR,
    100: NotificationLevel.CRITICAL,
}

# dedup 用 app_state キー。
_KEY_LAST_THRESHOLD: Final[str] = "budget_alert_last_threshold"
_KEY_ALERT_MONTH: Final[str] = "budget_alert_month"

# 当月境界の月キー書式 (YYYY-MM)。
_MONTH_FMT: Final[str] = "%Y-%m"

# 集計用 usage_log Core Table (created_at 付き)。 usage_writer 側は created_at 未宣言の
# ため、 集計に必要な cost_usd / created_at だけを別 MetaData で軽量再宣言する。
_metadata: Final[MetaData] = MetaData()

_usage_log_agg_table: Final[Table] = Table(
    "usage_log",
    _metadata,
    Column("cost_usd", Numeric(10, 6), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _month_window(now: datetime) -> tuple[datetime, datetime, str]:
    """``now`` を含む月の ``[month_start, next_month_start)`` と月キー (YYYY-MM) を返す。

    境界は ``now`` の tz を尊重する (UTC 想定だが naive でも崩れない)。 12 月は翌年 1 月へ繰上げ。
    """
    last_month_of_year = 12
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == last_month_of_year:
        next_month_start = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month_start = month_start.replace(month=month_start.month + 1)
    return month_start, next_month_start, month_start.strftime(_MONTH_FMT)


async def _sum_month_cost(
    session: AsyncSession, *, month_start: datetime, next_month_start: datetime
) -> Decimal:
    """当月の ``usage_log.cost_usd`` 合計を返す (実績なしは ``Decimal(0)``)。"""
    stmt = select(func.coalesce(func.sum(_usage_log_agg_table.c.cost_usd), 0)).where(
        _usage_log_agg_table.c.created_at >= month_start,
        _usage_log_agg_table.c.created_at < next_month_start,
    )
    total = (await session.execute(stmt)).scalar_one()
    return total if isinstance(total, Decimal) else Decimal(str(total))


def _decode_int(raw: object) -> int | None:
    """app_state.value (JSONB) を int に正規化する (型不一致 / 不在は ``None``)。

    asyncpg は JSONB を decode 済みオブジェクトでも生 JSON 文字列でも返しうるため、
    文字列なら一段 JSON デコードしてから int 判定する (factory / scheduler と同方針)。
    bool は ``int`` のサブクラスだが閾値ではないため除外する。
    """
    value: object = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _decode_str(raw: object) -> str | None:
    """app_state.value (JSONB) を str に正規化する (型不一致 / 不在は ``None``)。"""
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return raw  # 生文字列 (例: "2026-06") はそのまま採用
        return decoded if isinstance(decoded, str) else None
    return None


async def _read_dedup_state(session: AsyncSession, *, month_key: str) -> int:
    """当月で最後に通知した閾値を返す (月が異なる / 未記録は 0 = 未通知扱い)。

    ``budget_alert_month`` が当月と一致する場合のみ ``budget_alert_last_threshold`` を採用し、
    月が変わっていれば 0 を返して閾値をリセットする。
    """
    stmt = select(app_state_table.c.key, app_state_table.c.value).where(
        app_state_table.c.key.in_((_KEY_LAST_THRESHOLD, _KEY_ALERT_MONTH))
    )
    rows = (await session.execute(stmt)).all()
    stored_month: str | None = None
    stored_threshold = 0
    for row in rows:
        if row.key == _KEY_ALERT_MONTH:
            stored_month = _decode_str(row.value)
        elif row.key == _KEY_LAST_THRESHOLD:
            stored_threshold = _decode_int(row.value) or 0
    if stored_month != month_key:
        return 0
    return stored_threshold


async def _upsert_dedup_state(session: AsyncSession, *, month_key: str, threshold: int) -> None:
    """``budget_alert_last_threshold`` / ``budget_alert_month`` を同一トランザクションで upsert する。

    JSONB 列に Python ``int`` / ``str`` を渡すと SQLAlchemy が JSON エンコードして格納するため、
    読取側 (``_decode_int`` / ``_decode_str``) の一段デコードと往復一致する (flush まで)。
    """
    for key, value in ((_KEY_LAST_THRESHOLD, threshold), (_KEY_ALERT_MONTH, month_key)):
        await session.execute(
            insert(app_state_table)
            .values(key=key, value=value)
            .on_conflict_do_update(
                index_elements=[app_state_table.c.key],
                set_={"value": value},
            )
        )
    await session.flush()


def _crossed_threshold(pct: float) -> int | None:
    """``pct`` が跨いだ最大の閾値を返す (どれも未達なら ``None``)。"""
    crossed = [t for t in BUDGET_THRESHOLDS_PCT if pct >= t]
    return max(crossed) if crossed else None


def _format_message(*, threshold: int, pct: float, total: Decimal, budget: float) -> str:
    """予算アラート本文を組み立てる (閾値 / 進捗率 / 実績 / 予算)。"""
    return (
        f"月次 LLM コストが予算の {threshold}% を超過しました "
        f"(消化率 {pct:.1f}% / ${float(total):.2f} of ${budget:.2f})。"
    )


async def check_budget_and_alert(
    session: AsyncSession,
    notifier: SlackNotifier,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    """当月コストの予算消化率を評価し、 新たに跨いだ閾値があれば 1 通知する (FR-026)。

    進捗率 ``pct = total / monthly_budget_usd * 100`` を 50 / 80 / 100% と比較し、 跨いだ最大の
    閾値が「当月にこれまで通知した閾値より大きい」ときだけ通知する。 通知後は新しい閾値 + 当月を
    ``app_state`` に記録し、 同一閾値の再送と降順往復 (80→50 へ戻った時) を抑止する。
    ``monthly_budget_usd <= 0`` の場合はチェック自体をスキップする (ゼロ除算 / 無意味な常時超過回避)。

    Args:
        session: 呼び出し側が管理する AsyncSession。 dedup 状態の upsert を含めて本関数が
            ``commit`` する (通知判断と記録を 1 トランザクションに収める)。
        notifier: Slack 通知クライアント。 webhook 未設定なら no-op、 送信失敗は握り潰される。
        settings: ``monthly_budget_usd`` を参照する :class:`Settings`。
        now: 評価基準時刻 (集計対象月の決定に使う)。 省略時は現在時刻 (UTC)。

    Returns:
        None。 副作用は Slack 通知と ``app_state`` の dedup 状態更新のみ。
    """
    budget = settings.monthly_budget_usd
    if budget <= 0:
        logger.bind(component="domain.budget").debug(
            "monthly_budget_usd<=0 のため予算チェックをスキップ (budget={})", budget
        )
        return

    base = now if now is not None else datetime.now(tz=UTC)
    month_start, next_month_start, month_key = _month_window(base)

    total = await _sum_month_cost(
        session, month_start=month_start, next_month_start=next_month_start
    )
    pct = float(total) / budget * 100.0

    threshold = _crossed_threshold(pct)
    if threshold is None:
        return

    last_threshold = await _read_dedup_state(session, month_key=month_key)
    if threshold <= last_threshold:
        return  # 同一 / 降順の閾値は再通知しない

    await _upsert_dedup_state(session, month_key=month_key, threshold=threshold)
    await session.commit()

    await notifier.notify(
        level=_THRESHOLD_LEVEL[threshold],
        message=_format_message(threshold=threshold, pct=pct, total=total, budget=budget),
        context={"month": month_key, "threshold_pct": threshold},
    )


async def run_budget_check(
    *,
    settings: Settings | None = None,
    notifier: SlackNotifier | None = None,
    now: datetime | None = None,
) -> None:
    """独自セッションを開いて :func:`check_budget_and_alert` を実行する (job 用エントリ)。

    日次 scheduler ジョブ / LLM 呼出後フックが session を持たずに呼べるよう、 ``get_sessionmaker``
    でセッションを開閉する薄いラッパ。 ``settings`` / ``notifier`` 省略時はそれぞれ ``get_settings()`` /
    settings の webhook から構築する。

    Args:
        settings: 省略時は ``get_settings()``。
        notifier: 省略時は ``settings.slack_webhook_url`` から ``SlackNotifier`` を構築する。
        now: 評価基準時刻。 省略時は現在時刻 (UTC)。
    """
    resolved_settings = settings if settings is not None else get_settings()
    resolved_notifier = (
        notifier if notifier is not None else SlackNotifier(resolved_settings.slack_webhook_url)
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await check_budget_and_alert(session, resolved_notifier, resolved_settings, now=now)
