"""dryrun リテンション (7 日 auto_expire) の日次ジョブ本体 (US2, ADR-0025 / ADR-0028)。

責務:

``pending`` のまま ``created_at`` から 7 日 (``RETENTION_DAYS``) 経過した
:class:`DryrunOutput` を ``auto_expired`` へ一括遷移させ、 対応する動画を
``StorageAdapter.delete(missing_ok=True)`` で削除し、 ``write_audit_log`` に
``dryrun_auto_expired`` を 1 行ずつ残す。 retention は dryrun レビュー期限切れ
(= 運用者が 7 日放置した提案) の自動失効であり、 ここを過ぎた動画はストレージから
消えて配信不可になる (api 側 video エンドポイントは ``auto_expired`` を 409 に写像)。

設計方針 (scheduler.py の日次サイクル ジョブと同方針):

- **ルート外はセッションメーカ直叩き** (ADR / 内部契約 (a2)): cron 起動は HTTP リクエスト
  外で ``Depends`` を使えないため、 :func:`get_sessionmaker` から自前で ``AsyncSession``
  を開く。 1 トランザクション内で全失効行を処理し、 ジョブ側でまとめて ``commit`` する
  (service 層の flush 規約とは別に、 ここがトランザクション境界の所有者)。
- **ジョブ内例外は握り潰す** (ADR-0028): 1 回の失敗で APScheduler スレッド (日次サイクル)
  を落とさない。 例外は 5 区分 (transient / recoverable / fatal / compliance / quality) で
  ``resolve_category`` 分類し Slack 通知して飲み込む。 通知は副作用なので失敗しても
  本筋を止めない。
- **冪等性**: 動画削除は ``missing_ok=True`` で多重実行に耐える。 状態遷移済み
  (``state != "pending"``) の行は ``where`` で除外されるため二重失効しない。
- **不変志向**: ジョブ関数はステートレス。 依存 (sessionmaker / settings / notifier) は
  引数で受け取り、 既定はモジュールの合成 (``get_sessionmaker`` / ``get_settings``) に委ねる。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ymg_backend.core.config import get_settings
from ymg_backend.core.logging import bind_context
from ymg_backend.domain.errors.errors import resolve_category
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import DryrunOutput
from ymg_backend.infrastructure.db.session import get_sessionmaker
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ymg_backend.core.config import Settings
    from ymg_backend.infrastructure.slack.notifier import SlackNotifier

__all__ = [
    "RETENTION_DAYS",
    "RETENTION_HOUR",
    "RETENTION_JOB_ID",
    "RETENTION_MINUTE",
    "build_retention_runner",
    "run_dryrun_retention_job",
]

# dryrun レビュー保持日数 (ADR-0025: pending を 7 日で auto_expire)。
RETENTION_DAYS: Final[int] = 7

# scheduler への登録で使う安定ジョブ ID。 ``replace_existing`` で冪等に再登録する。
RETENTION_JOB_ID: Final[str] = "dryrun-retention:daily"

# 失効ジョブの実行時刻 (JST 00:30)。 日次サイクル slot (07 / 19 時) と衝突しない深夜帯。
RETENTION_HOUR: Final[int] = 0
RETENTION_MINUTE: Final[int] = 30

# retention ジョブ本体が踏む状態と監査アクション (再発明防止のため定数化)。
_PENDING_STATE: Final[str] = "pending"
_AUTO_EXPIRED_STATE: Final[str] = "auto_expired"
_AUDIT_ACTION: Final[str] = "dryrun_auto_expired"
_AUDIT_TARGET_TYPE: Final[str] = "dryrun_output"

# 失効判定の基準 TZ。 created_at は tz-aware (UTC 保存) なので比較は aware 同士で行う。
_JST: Final[ZoneInfo] = ZoneInfo("Asia/Tokyo")


async def _auto_expire_in_session(
    session: AsyncSession,
    *,
    storage: StorageAdapter,
    now: datetime,
) -> int:
    """単一セッション内で失効処理を行い、 失効件数を返す (commit は呼び出し側)。

    ``pending`` かつ ``created_at < now - RETENTION_DAYS`` の :class:`DryrunOutput` を
    ``auto_expired`` へ遷移させ (``auto_expired_at`` を ``now`` に設定)、 動画を削除し、
    監査ログを 1 行ずつ残す。 各行の動画削除は ``missing_ok=True`` で冪等。

    Args:
        session: 呼び出し側が所有する ``AsyncSession`` (トランザクション境界は呼び出し側)。
        storage: 動画削除に使う ``StorageAdapter``。
        now: 失効基準時刻 (tz-aware)。 ``auto_expired_at`` にも採用する。

    Returns:
        失効させた件数。
    """
    cutoff = now - timedelta(days=RETENTION_DAYS)
    stmt = select(DryrunOutput).where(
        DryrunOutput.state == _PENDING_STATE,
        DryrunOutput.created_at < cutoff,
    )
    rows = (await session.execute(stmt)).scalars().all()

    for output in rows:
        output.state = _AUTO_EXPIRED_STATE
        output.auto_expired_at = now
        # 動画削除は冪等 (既に消えていても落とさない)。 失敗はジョブ全体の except で握る。
        storage.delete(output.video_uri, missing_ok=True)
        await write_audit_log(
            session,
            action=_AUDIT_ACTION,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(output.id),
            payload={"video_uri": output.video_uri, "retention_days": RETENTION_DAYS},
        )

    return len(rows)


async def run_dryrun_retention_job(
    *,
    now: datetime | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
    storage: StorageAdapter | None = None,
    notifier: SlackNotifier | None = None,
) -> int:
    """dryrun リテンションの日次ジョブ本体 (cron から呼ばれる)。

    自前で ``AsyncSession`` を開き、 7 日経過した ``pending`` の dryrun を一括失効
    (``auto_expired``) + 動画削除 + 監査ログ記録し、 1 トランザクションで ``commit`` する。
    ジョブ内例外は ADR-0028 の 5 区分で分類し Slack 通知して握り潰す (日次サイクルの
    APScheduler スレッドを落とさない)。 通知失敗も本筋を止めない。

    Args:
        now: 失効基準時刻 (省略時は JST 現在時刻)。 テストで固定するために注入可能。
        sessionmaker: セッションメーカ (省略時は ``get_sessionmaker()``)。 cron はルート外
            のため ``Depends`` を使えず自前で開く (内部契約 (a2))。
        settings: 設定 (省略時は ``get_settings()``)。 ``storage_base_uri`` の参照に使う。
        storage: 動画削除アダプタ (省略時は ``settings.storage_base_uri`` から構築)。
        notifier: 失敗通知先 (省略時は通知なし)。

    Returns:
        失効させた件数 (失敗時は 0)。
    """
    resolved_now = now if now is not None else datetime.now(tz=_JST)
    log = bind_context(
        step="dryrun.retention",
        job_id=RETENTION_JOB_ID,
        cycle_id=resolved_now.date().isoformat(),
    )
    log.info("dryrun retention job fired", cutoff_days=RETENTION_DAYS)

    resolved_settings = settings if settings is not None else get_settings()
    resolved_storage = (
        storage if storage is not None else StorageAdapter(resolved_settings.storage_base_uri)
    )
    resolved_maker = sessionmaker if sessionmaker is not None else get_sessionmaker()

    try:
        async with resolved_maker() as session:
            expired = await _auto_expire_in_session(
                session, storage=resolved_storage, now=resolved_now
            )
            await session.commit()
    except Exception as exc:  # 1 回の失敗で scheduler スレッドを落とさない (ADR-0028)
        category = resolve_category(exc)
        log.bind(error_category=category.value).error("dryrun retention job failed", error=str(exc))
        await _notify_failure(notifier, exc, now=resolved_now)
        return 0

    log.info("dryrun retention job done", expired_count=expired)
    return expired


async def _notify_failure(
    notifier: SlackNotifier | None,
    exc: BaseException,
    *,
    now: datetime,
) -> None:
    """ジョブ失敗を Slack へ通知する (notifier 未設定 / 通知失敗は握り潰す)。"""
    if notifier is None:
        return
    try:
        await notifier.notify_error(
            exc, context={"job": RETENTION_JOB_ID, "date": now.date().isoformat()}
        )
    except Exception as notify_exc:  # 通知失敗で本筋を止めない
        bind_context(step="dryrun.retention", job_id=RETENTION_JOB_ID).warning(
            "dryrun retention failure notification failed: {}", notify_exc
        )


def build_retention_runner(
    *,
    settings: Settings | None = None,
    storage: StorageAdapter | None = None,
    notifier: SlackNotifier | None = None,
) -> Callable[[], object]:
    """cron に渡す引数なし async callable を返す (依存を束縛したジョブ起点)。

    APScheduler の ``add_job`` には引数なしで呼べる callable を渡すのが扱いやすいので、
    依存 (settings / storage / notifier) をクロージャに束ねる。 実行のたびに自前セッションを
    開くため sessionmaker は束ねない (プロセス単位 lru_cache なジョブ実行時解決に委ねる)。

    Args:
        settings: 設定 (省略時は実行時 ``get_settings()``)。
        storage: 動画削除アダプタ (省略時は実行時に構築)。
        notifier: 失敗通知先。

    Returns:
        引数なしで ``await`` 可能なジョブ callable。
    """

    async def _runner() -> int:
        return await run_dryrun_retention_job(settings=settings, storage=storage, notifier=notifier)

    return _runner
