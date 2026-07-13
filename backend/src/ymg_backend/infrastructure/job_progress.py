"""工程イベントの永続化 + EventBus 配信 (ADR-0023 ``JobProgressRecorder``)。

``job_step_events`` を短い独立トランザクションで書いた直後に、 既存
:class:`~ymg_backend.infrastructure.event_bus.EventBus` へ publish する。
正本は DB、 SSE は差分配信のみ (ADR-0023)。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final

from loguru import logger

from ymg_backend.infrastructure.db.models import MUSIC_GENERATION_JOB_NAME, JobStepEvent
from ymg_backend.infrastructure.event_bus import EventBus, JobEvent, JobStatus, event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["JobProgressRecorder"]


class JobProgressRecorder:
    """``job_step_events`` へ 1 行書き、 成功後に EventBus へ fan-out する。

    オーケストレータの長寿命セッションとは独立した短い TX を使うため、 進捗記録の失敗が
    本処理のロールバックに巻き込まれない (逆も同様)。 publish は EventBus 契約どおり
    例外を漏らさない。
    """

    __slots__ = ("_bus", "_job_name", "_session_factory")

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        bus: EventBus | None = None,
        job_name: str = MUSIC_GENERATION_JOB_NAME,
    ) -> None:
        self._session_factory: Final[async_sessionmaker[AsyncSession]] = session_factory
        self._bus: Final[EventBus] = bus if bus is not None else event_bus
        self._job_name: Final[str] = job_name

    async def record(
        self,
        *,
        run_id: uuid.UUID,
        step: str,
        status: JobStatus,
        genre: str | None = None,
        context_type: str | None = None,
        context_id: uuid.UUID | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """工程イベントを DB に確定し、 続けて EventBus へ publish する。

        DB 書き込みに失敗した場合はログして握り潰す (進捗欠落 < 本処理停止)。
        EventBus 側は例外を漏らさない契約。
        """
        event_id = uuid.uuid4()
        try:
            async with self._session_factory() as session:
                session.add(
                    JobStepEvent(
                        id=event_id,
                        run_id=run_id,
                        step=step,
                        status=status,
                        genre=genre,
                        context_type=context_type,
                        context_id=context_id,
                        error_category=error_category,
                        error_message=error_message,
                    )
                )
                await session.commit()
        except Exception as exc:  # 進捗欠落は許容、 本処理は止めない
            logger.bind(component="job_progress", run_id=str(run_id), step=step).warning(
                "job_step_events write failed: {}", exc
            )
            return

        self._bus.publish(
            JobEvent(
                timestamp="",
                job_name=self._job_name,
                step=step,
                status=status,
                run_id=str(run_id),
                genre=genre,
                context_type=context_type,
                context_id=str(context_id) if context_id is not None else None,
                error_category=error_category,  # type: ignore[arg-type]
                message=error_message,
            )
        )
