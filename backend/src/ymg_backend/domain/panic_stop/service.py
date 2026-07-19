"""コンプライアンス緊急停止 (panic-stop) サービス (US4 T112, ADR-0031 / ADR-0035)。

運用者が「直ちに投稿を止め、 直近の投稿を非公開化したい」場面 (コンプラ事故 / BAN リスク)
のための一括操作を束ねる。 1 回の ``panic_stop`` で以下を順に行う:

1. scheduler を停止する (:meth:`SchedulerService.disable`)。 未配線なら skip。
2. ``app_state.scheduler_enabled=false`` を永続化する (``disable()`` はフラグを書かないため、
   ``PUT /scheduler`` と同じ upsert をここで行う)。
3. 直近 ``window_hours`` 時間に投稿された動画 (:class:`Video`) を列挙する (private 化候補)。
4. ``set_private`` に指定された ``youtube_video_id`` の各動画を YouTube 側で private 化し
   (:class:`VideoPrivacyUpdater`)、 対応する ``Video.privacy_status`` を ``private`` に更新する。
5. ``audit_log`` に ``panic_stop`` を記録する (ADR-0031: 不可逆判断のトレーサビリティ)。

設計方針:

- scheduler instance は HTTP リクエスト外で構築される (``main.py`` の lifespan が
  ``app.state.scheduler_service`` に格納) ため ``Depends`` で解決できない。 API が
  ``request.app.state`` から取り出し、 本 service に引数で渡す (``api/scheduler.py`` の
  ``_apply_scheduler_jobs`` と同方針)。
- DB 書き込みは ``flush`` まで。 commit は呼び出し側 (API) の責務 (既存の境界に従う)。
- ``app_state`` 参照は ORM 層に依存せず SQLAlchemy Core の軽量 Table を本モジュールに閉じて
  持つ (``api/scheduler.py`` / ``infrastructure/audit.py`` と同じ疎結合方針)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from loguru import logger
from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import JSONB, insert

from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import Video

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.scheduler import SchedulerService
    from ymg_backend.infrastructure.youtube.privacy_client import VideoPrivacyUpdater

# panic-stop の既定 window (直近 24 時間の投稿を private 化候補とする, contracts L350)。
DEFAULT_WINDOW_HOURS: Final[int] = 24

# private 化後に設定する公開範囲 (settable enum, contracts L520-534)。
_PRIVACY_PRIVATE: Final[str] = "private"

# app_state のキー (``api/scheduler.py`` と同じ。 disable() はフラグを書かないため本 service が upsert)。
_KEY_SCHEDULER_ENABLED: Final[str] = "scheduler_enabled"

# audit_log の action 名 (ADR-0031 panic-stop)。
_ACTION_PANIC_STOP: Final[str] = "panic_stop"

# app_state.value (JSONB) への疎結合参照用 Core Table (ORM 層非依存, api/scheduler.py と同形)。
_metadata: Final[MetaData] = MetaData()

app_state_table: Final[Table] = Table(
    "app_state",
    _metadata,
    Column("key", String, primary_key=True, nullable=False),
    Column("value", JSONB, nullable=False),
)


@dataclass(frozen=True)
class PanicStopResult:
    """``panic_stop`` の実行結果 (contracts ``PanicStopResponse`` と整合)。

    Attributes:
        scheduler_enabled: 停止後の scheduler 有効フラグ (常に ``False``)。
        recent_videos: 直近 ``window_hours`` 内に投稿された ``Video`` 行 (private 化候補)。
        updated_count: 実際に private 化した動画本数。
    """

    scheduler_enabled: bool
    recent_videos: list[Video]
    updated_count: int


class PanicStopService:
    """scheduler 停止 + 直近動画 private 化 + audit を束ねる service (US4 T112)。

    Args:
        privacy_updater: YouTube 側の動画公開範囲を変更する :class:`VideoPrivacyUpdater`。
    """

    __slots__ = ("_privacy_updater",)

    def __init__(self, *, privacy_updater: VideoPrivacyUpdater) -> None:
        self._privacy_updater: Final[VideoPrivacyUpdater] = privacy_updater

    async def panic_stop(
        self,
        *,
        session: AsyncSession,
        scheduler_service: SchedulerService | None,
        window_hours: int = DEFAULT_WINDOW_HOURS,
        set_private: list[str] | None = None,
        actor: str = "seita",
    ) -> PanicStopResult:
        """投稿を緊急停止し、 直近動画を列挙 / private 化する (flush まで)。

        Args:
            session: ``app_state`` / ``videos`` / ``audit_log`` 書き込み用 ``AsyncSession``
                (commit は呼び出し側)。
            scheduler_service: 停止対象の scheduler。 ``None`` (未配線) の場合は
                ``disable()`` を skip し、 フラグ永続化と audit / private 化のみ行う。
            window_hours: 直近何時間の投稿を private 化候補とするか (>=1)。
            set_private: private 化する ``youtube_video_id`` 配列。 ``None`` / 空なら
                候補列挙のみで private 化は行わない。
            actor: audit_log の操作主体 (Basic 認証ユーザー名)。

        Returns:
            :class:`PanicStopResult` (停止後フラグ / 候補動画 / private 化件数)。

        Raises:
            ValueError: ``window_hours`` が 1 未満の場合 (境界での入力検証)。
            RecoverableError: YouTube 側 privacy 更新が 4xx 拒否の場合 (ADR-0028)。
            TransientError: YouTube 側 privacy 更新が 5xx / 通信失敗の場合 (ADR-0028)。
        """
        if window_hours < 1:
            raise ValueError("window_hours must be >= 1")

        # 1) scheduler 停止 (未配線なら skip。 副作用は best-effort, api/scheduler.py と同方針)。
        if scheduler_service is not None:
            scheduler_service.disable()
        else:
            logger.bind(component="panic_stop").warning(
                "scheduler_service not wired; flag persisted but jobs unchanged"
            )

        # 2) app_state.scheduler_enabled=false を永続化 (disable() はフラグを書かない)。
        await self._disable_scheduler_flag(session)

        # 3) 直近 window の動画を列挙 (private 化候補)。
        recent_videos = await self._recent_videos(session, window_hours=window_hours)

        # 4) set_private に指定された動画を private 化 (YouTube + DB)。
        targets = set(set_private or [])
        updated_count = await self._set_videos_private(
            session, recent_videos=recent_videos, targets=targets
        )

        # 5) audit_log に panic_stop を記録 (ADR-0031)。
        await write_audit_log(
            session,
            action=_ACTION_PANIC_STOP,
            actor=actor,
            target_type="scheduler",
            target_id=_KEY_SCHEDULER_ENABLED,
            payload={
                "actor": actor,
                "window_hours": window_hours,
                "set_private": sorted(targets),
                "updated_count": updated_count,
            },
        )

        logger.bind(component="panic_stop").info(
            "panic-stop 実行完了 (actor={}, window_hours={}, updated_count={})",
            actor,
            window_hours,
            updated_count,
        )
        return PanicStopResult(
            scheduler_enabled=False,
            recent_videos=recent_videos,
            updated_count=updated_count,
        )

    async def _disable_scheduler_flag(self, session: AsyncSession) -> None:
        """``app_state.scheduler_enabled`` を ``false`` に upsert する (flush まで)。"""
        stmt = (
            insert(app_state_table)
            .values(key=_KEY_SCHEDULER_ENABLED, value=False)
            .on_conflict_do_update(
                index_elements=[app_state_table.c.key],
                set_={"value": False},
            )
        )
        await session.execute(stmt)
        await session.flush()

    @staticmethod
    async def _recent_videos(session: AsyncSession, *, window_hours: int) -> list[Video]:
        """直近 ``window_hours`` 時間に投稿された動画を新しい順で返す。"""
        threshold = datetime.now(UTC) - timedelta(hours=window_hours)
        stmt = select(Video).where(Video.posted_at >= threshold).order_by(Video.posted_at.desc())
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    async def _set_videos_private(
        self,
        session: AsyncSession,
        *,
        recent_videos: list[Video],
        targets: set[str],
    ) -> int:
        """``targets`` の各動画を YouTube + DB で private 化し件数を返す (flush まで)。

        候補 (``recent_videos``) に含まれる動画は ORM 行を直接更新する。 候補外の
        ``youtube_video_id`` が指定された場合は DB から個別取得して更新する (運用者が
        window 外を明示指定したケースに対応)。
        """
        if not targets:
            return 0

        by_video_id = {v.youtube_video_id: v for v in recent_videos}
        updated = 0
        for youtube_video_id in targets:
            # YouTube 側を先に private 化し (失敗時は DB を巻き戻さず例外送出)、 成功後に DB 反映。
            await self._privacy_updater.set_privacy(
                session=session,
                youtube_video_id=youtube_video_id,
                privacy_status=_PRIVACY_PRIVATE,
            )
            video = by_video_id.get(youtube_video_id) or await self._load_video(
                session, youtube_video_id
            )
            if video is not None:
                video.privacy_status = _PRIVACY_PRIVATE
            updated += 1

        await session.flush()
        return updated

    @staticmethod
    async def _load_video(session: AsyncSession, youtube_video_id: str) -> Video | None:
        """``youtube_video_id`` で ``Video`` 行を 1 件取得する (候補外指定の補完用)。"""
        stmt = select(Video).where(Video.youtube_video_id == youtube_video_id)
        return (await session.execute(stmt)).scalars().first()


__all__ = ["DEFAULT_WINDOW_HOURS", "PanicStopResult", "PanicStopService", "app_state_table"]
