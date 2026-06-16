"""APScheduler ベースの日次サイクル スケジューラ (T087, ADR-0011 / ADR-0031)。

責務:

1. **日次 cron 登録** (ADR-0011): JST の 2 slot (朝 / 夕) に日次サイクルを起動する
   ``cron`` ジョブを :class:`~apscheduler.schedulers.asyncio.AsyncIOScheduler` に登録する。
   各 slot のジョブは :func:`run_daily_cycle` を呼び、 その日の :class:`DailyPlan` を
   1 投稿ずつ処理する。
2. **enabled ゲート** (ADR-0031): ジョブの ``add`` は ``app_state.scheduler_enabled=true``
   のときだけ行う。 reboot 後は false 起動が既定 (``main.py`` の lifespan は
   instance 構築のみで job 登録しない)。 管理 UI の ``PUT /scheduler`` が本サービスの
   :meth:`SchedulerService.enable` / :meth:`SchedulerService.disable` を呼んで切替える。
3. **疎結合な実行本体** (ADR-0011): 実際のサイクル実行は :class:`DailyCycleOrchestrator`
   が担うが、 その合成には他チームの多数の依存 (planner / music / uploader …) が要る。
   本モジュールは「いつ起動するか」だけに責務を絞り、 実行本体は注入された
   ``CycleRunner`` (= :data:`run_daily_cycle` 互換の async callable) として受け取る。
   これにより scheduler 単体で外部依存を起動せずテストできる。

設計方針:

- ``AsyncIOScheduler`` の構築は ``main.py`` の lifespan が行い ``app.state.scheduler`` に
  保持する。 本サービスはその instance を **受け取って** ジョブを管理する (二重に scheduler
  を持たない)。
- ジョブ ID は slot ごとに安定した文字列定数 (``daily-cycle:morning`` 等) を使い、
  ``replace_existing=True`` で再登録を冪等にする。
- ジョブ本体 (:func:`_run_slot`) は自前で :class:`AsyncSession` を開く
  (``get_sessionmaker()()``)。 cron 起動は HTTP リクエスト外のため ``Depends`` は使えず、
  オーケストレータと同じ「ルート外はセッションメーカ直叩き」パターンに従う。
- ジョブ内例外は ADR-0028 の 5 区分で分類し Slack 通知して握り潰す (1 回の失敗で
  scheduler スレッドを落とさない)。 通知は副作用なので失敗しても本筋を止めない。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Final, Protocol
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ymg_backend.core.logging import bind_context
from ymg_backend.domain.dryrun.retention_job import (
    RETENTION_HOUR,
    RETENTION_JOB_ID,
    RETENTION_MINUTE,
    build_retention_runner,
)
from ymg_backend.domain.errors.errors import resolve_category
from ymg_backend.infrastructure.db.session import get_sessionmaker

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.slack.notifier import SlackNotifier

__all__ = [
    "DAILY_CYCLE_JOB_IDS",
    "CycleRunner",
    "SchedulerService",
]

# 日次サイクルを起動する JST タイムゾーン (ADR-0011: cron は JST 固定)。
_JST: Final[ZoneInfo] = ZoneInfo("Asia/Tokyo")

# 日次 cron の 2 slot (JST)。 朝 / 夕の固定時刻。 ADR-0011 は深夜帯既定を許容するが、
# 本タスク要件 (朝 / 夕 2 slot) に従う。 将来 ``app_state`` / Settings で可変化する余地を残す。
_MORNING_HOUR: Final[int] = 7
_EVENING_HOUR: Final[int] = 19
_CRON_MINUTE: Final[int] = 0

# slot ごとの安定ジョブ ID。 ``replace_existing`` で冪等に再登録できるよう固定文字列にする。
_JOB_ID_MORNING: Final[str] = "daily-cycle:morning"
_JOB_ID_EVENING: Final[str] = "daily-cycle:evening"

# 登録する全 slot のジョブ ID (api/scheduler.py からの参照・テスト用)。
DAILY_CYCLE_JOB_IDS: Final[tuple[str, ...]] = (_JOB_ID_MORNING, _JOB_ID_EVENING)

# slot 定義: (ジョブ ID, JST 時, slot 名)。 cron トリガ構築のソース。
_SLOTS: Final[tuple[tuple[str, int, str], ...]] = (
    (_JOB_ID_MORNING, _MORNING_HOUR, "morning"),
    (_JOB_ID_EVENING, _EVENING_HOUR, "evening"),
)


class CycleRunner(Protocol):
    """日次サイクル実行本体の最小 I/F (注入される async callable)。

    本番では :class:`DailyCycleOrchestrator.run` 互換のラッパ (``run_daily_cycle``) を
    渡す。 テストでは呼び出しを記録するだけの stub を渡す。 ``target_date`` のみ受け、
    ``session`` 等の重い依存は実行本体側で解決する (scheduler は起動契機だけを持つ)。
    """

    async def __call__(self, *, session: AsyncSession, target_date: date) -> None: ...


class SchedulerService:
    """既存 ``AsyncIOScheduler`` 上で日次サイクルの cron ジョブを管理するサービス。

    ``main.py`` の lifespan が構築した scheduler instance を受け取り、 enabled 状態に応じて
    日次 cron (朝 / 夕) を ``add`` / ``remove`` する。 本サービスは scheduler の所有権を持たず
    (start / shutdown は lifespan の責務)、 ジョブ定義の出し入れだけを担う。

    Args:
        scheduler: lifespan が構築済みの ``AsyncIOScheduler`` (tz=Asia/Tokyo)。
        cycle_runner: 日次サイクル実行本体 (注入)。 cron 発火時に呼ばれる。
        notifier: ジョブ内例外を通知する Slack クライアント (任意。 ``None`` で通知なし)。
    """

    __slots__ = ("_cycle_runner", "_notifier", "_scheduler")

    def __init__(
        self,
        scheduler: AsyncIOScheduler,
        *,
        cycle_runner: CycleRunner,
        notifier: SlackNotifier | None = None,
    ) -> None:
        self._scheduler: Final[AsyncIOScheduler] = scheduler
        self._cycle_runner: Final[CycleRunner] = cycle_runner
        self._notifier: Final[SlackNotifier | None] = notifier

    def add_daily_jobs(self) -> None:
        """日次サイクルの cron ジョブ (朝 / 夕) を登録する (冪等)。

        ``replace_existing=True`` で同 ID の既存ジョブを置き換えるため、 複数回呼んでも
        ジョブが重複しない。 ``app_state.scheduler_enabled=true`` を確認した呼び出し側
        (lifespan / ``PUT /scheduler``) からのみ呼ぶ。
        """
        for job_id, hour, slot in _SLOTS:
            trigger = CronTrigger(hour=hour, minute=_CRON_MINUTE, timezone=_JST)
            self._scheduler.add_job(
                self._run_slot,
                trigger=trigger,
                id=job_id,
                name=f"daily-cycle {slot} ({hour:02d}:{_CRON_MINUTE:02d} JST)",
                kwargs={"slot": slot},
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
            logger.bind(component="scheduler").info(
                "daily-cycle job registered", job_id=job_id, hour=hour, slot=slot
            )

    def add_retention_job(self) -> None:
        """dryrun リテンション (7 日 auto_expire) の日次 cron ジョブを登録する (冪等)。

        JST 00:30 に :func:`run_dryrun_retention_job` を 1 回起動する。 ジョブ本体は自前で
        ``AsyncSession`` を開き、 失敗は ADR-0028 で分類し本サービスの ``notifier`` 経由で
        通知して握り潰す (scheduler スレッドを落とさない)。 ``replace_existing=True`` で
        複数回呼んでも重複しない。
        """
        runner = build_retention_runner(notifier=self._notifier)
        trigger = CronTrigger(hour=RETENTION_HOUR, minute=RETENTION_MINUTE, timezone=_JST)
        self._scheduler.add_job(
            runner,
            trigger=trigger,
            id=RETENTION_JOB_ID,
            name=f"dryrun-retention ({RETENTION_HOUR:02d}:{RETENTION_MINUTE:02d} JST)",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        logger.bind(component="scheduler").info(
            "dryrun retention job registered", job_id=RETENTION_JOB_ID
        )

    def remove_daily_jobs(self) -> None:
        """登録済みの日次サイクル cron ジョブを全て除去する (未登録 ID は無視)。"""
        for job_id in DAILY_CYCLE_JOB_IDS:
            if self._scheduler.get_job(job_id) is not None:
                self._scheduler.remove_job(job_id)
                logger.bind(component="scheduler").info("daily-cycle job removed", job_id=job_id)

    def enable(self) -> None:
        """scheduler を起動し日次ジョブを登録する (``PUT /scheduler enabled=true`` 用)。

        scheduler が未 start なら start する (lifespan が false 起動した後の手動 enable)。
        ジョブ登録は冪等。
        """
        if not self._scheduler.running:
            self._scheduler.start()
            logger.bind(component="scheduler").info("scheduler started (enable)")
        self.add_daily_jobs()
        self.add_retention_job()  # dryrun リテンション (7 日 auto_expire) も同時に有効化

    def disable(self) -> None:
        """日次ジョブを除去する (``PUT /scheduler enabled=false`` 用)。

        scheduler instance 自体の shutdown は lifespan の責務なので止めない (再度 enable
        できるよう running は保つ)。 ジョブだけを外して発火を止める。
        """
        self.remove_daily_jobs()
        if self._scheduler.get_job(RETENTION_JOB_ID) is not None:
            self._scheduler.remove_job(RETENTION_JOB_ID)
            logger.bind(component="scheduler").info(
                "dryrun retention job removed", job_id=RETENTION_JOB_ID
            )
        logger.bind(component="scheduler").info("scheduler disabled (jobs removed)")

    def registered_job_ids(self) -> tuple[str, ...]:
        """現在登録済みの日次サイクル ジョブ ID を返す (観測・テスト用)。"""
        return tuple(
            job_id for job_id in DAILY_CYCLE_JOB_IDS if self._scheduler.get_job(job_id) is not None
        )

    async def _run_slot(self, *, slot: str) -> None:
        """cron 発火時のジョブ本体。 当日分の日次サイクルを 1 回実行する。

        cron 起動は HTTP リクエスト外のため ``Depends`` は使えない。 オーケストレータと同じ
        「ルート外はセッションメーカ直叩き」パターンで自前 ``AsyncSession`` を開く。
        ジョブ内例外は ADR-0028 の 5 区分で分類し Slack 通知して握り潰す (scheduler を
        落とさない)。 通知は副作用なので失敗しても本筋を止めない。

        Args:
            slot: 発火 slot 名 (``morning`` / ``evening``)。 ログ context 用。
        """
        target_date = datetime.now(tz=_JST).date()
        log = bind_context(
            step="scheduler.daily_cycle", cycle_id=f"{target_date.isoformat()}:{slot}"
        )
        log.info("daily-cycle job fired", slot=slot, target_date=target_date.isoformat())
        sessionmaker = get_sessionmaker()
        try:
            async with sessionmaker() as session:
                await self._cycle_runner(session=session, target_date=target_date)
        except Exception as exc:  # 1 回の失敗で scheduler スレッドを落とさない (ADR-0028)
            category = resolve_category(exc)
            log.bind(error_category=category.value).error(
                "daily-cycle job failed", slot=slot, error=str(exc)
            )
            await self._notify_failure(exc, slot=slot, target_date=target_date)

    async def _notify_failure(self, exc: BaseException, *, slot: str, target_date: date) -> None:
        """ジョブ失敗を Slack へ通知する (notifier 未設定 / 通知失敗は握り潰す)。"""
        if self._notifier is None:
            return
        try:
            await self._notifier.notify_error(
                exc, context={"slot": slot, "target_date": target_date.isoformat()}
            )
        except Exception as notify_exc:  # 通知失敗で本筋 (scheduler) を止めない
            logger.bind(component="scheduler").warning(
                "scheduler failure notification failed: {}", notify_exc
            )
