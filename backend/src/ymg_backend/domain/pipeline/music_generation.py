"""音楽専用オーケストレータ (ADR-0006 ``music_generation``)。

承認済み Daily Plan の各 Post について ``MusicJobRunner`` で 6 トラックを生成し、
``AudioTrack`` / WAV を確定する。 AcoustID・画像・ffmpeg・YouTube・Slack は組み立てない。

状態遷移:

- Plan: ``approved → executing → music_generated | failed``
- Post: ``pending → generating → music_generated | failed``

進捗は :class:`~ymg_backend.infrastructure.job_progress.JobProgressRecorder` 経由で
``job_step_events`` + EventBus に ``cycle`` / ``post`` / ``music`` を対で記録する。
予約開始後の例外 (``SchedulerHaltError`` / Post 作成失敗を含む) では Plan と開いている
工程を必ず終端してから再送出する。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ymg_backend.core.logging import bind_context
from ymg_backend.domain.errors.errors import (
    FatalError,
    SchedulerHaltError,
    YmgError,
    resolve_category,
)
from ymg_backend.domain.plans.schemas import ALLOWED_GENRES_CONTEXT_KEY, DailyPlan, DailyPost
from ymg_backend.infrastructure.db.models import Genre, Plan, Post

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.domain.pipeline.music_jobs import MusicJobRunner
    from ymg_backend.infrastructure.job_progress import JobProgressRecorder

_TRACK_COUNT: Final[int] = 6

__all__ = ["MusicGenerationOrchestrator", "MusicGenerationResult"]


@dataclass(frozen=True)
class MusicGenerationResult:
    """音楽専用実行のサマリ (不変)。"""

    plan_id: str
    post_ids: list[str]
    succeeded_count: int
    failed_count: int


class MusicGenerationOrchestrator:
    """承認済み Plan の音楽生成だけを駆動するオーケストレータ。

    ``job_history(status=running)`` の予約は呼び出し側 (:class:`MusicRunService`) の責務。
    本クラスは Plan/Post 状態遷移・MusicJobRunner・進捗記録を担う。
    """

    __slots__ = ("_music", "_progress")

    def __init__(
        self,
        *,
        music: MusicJobRunner,
        progress: JobProgressRecorder,
    ) -> None:
        self._music: Final[MusicJobRunner] = music
        self._progress: Final[JobProgressRecorder] = progress

    async def run(
        self,
        *,
        session: AsyncSession,
        plan_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> MusicGenerationResult:
        """予約済み ``run_id`` で ``plan_id`` の音楽生成を実行する。

        Args:
            session: 永続化セッション (本メソッドが commit する)。
            plan_id: 対象 Daily Plan (呼び出し時点で ``approved`` 想定)。
            run_id: 予約済み ``job_history.id``。

        Returns:
            :class:`MusicGenerationResult`。

        Raises:
            FatalError: Plan 不在・payload 不正など継続不能。
            SchedulerHaltError: DB 不達などインフラ級 fatal。
        """
        log = bind_context(step="music_generation", cycle_id=str(plan_id))
        plan_row = await session.get(Plan, plan_id)
        if plan_row is None:
            raise FatalError(
                "音楽生成対象の Plan が見つかりません",
                context={"plan_id": str(plan_id), "run_id": str(run_id)},
            )

        allowed_genres = await self._load_enabled_genres(session)
        plan = self._validate_plan_payload(plan_row, allowed_genres)
        plan_id_str = str(plan_row.id)
        log.info("music generation started", plan_id=plan_id_str, posts=len(plan.posts))

        plan_row.status = "executing"
        await session.commit()

        cycle_started = False
        active_post: Post | None = None
        post_failure_recorded = False

        try:
            await self._progress.record(
                run_id=run_id,
                step="cycle",
                status="running",
                context_type="plan",
                context_id=plan_row.id,
            )
            cycle_started = True

            post_ids: list[str] = []
            succeeded_count = 0
            failed_count = 0
            had_fatal = False

            for position, daily_post in enumerate(plan.posts):
                active_post = None
                post_failure_recorded = False
                try:
                    active_post = await self._create_post(
                        session=session,
                        plan_row=plan_row,
                        position=position,
                        daily_post=daily_post,
                    )
                except SQLAlchemyError as exc:
                    raise SchedulerHaltError(
                        "DB 操作に失敗しました (scheduler 停止)", original=exc
                    ) from exc

                post_ids.append(str(active_post.id))
                try:
                    await self._process_post(
                        session=session,
                        post=active_post,
                        daily_post=daily_post,
                        run_id=run_id,
                    )
                except SchedulerHaltError as exc:
                    await self._record_post_failure(
                        session, active_post, exc, run_id=run_id
                    )
                    post_failure_recorded = True
                    log.error("music generation halted scheduler", post_id=str(active_post.id))
                    raise
                except FatalError as exc:
                    await self._record_post_failure(
                        session, active_post, exc, run_id=run_id
                    )
                    post_failure_recorded = True
                    failed_count += 1
                    had_fatal = True
                    log.error(
                        "music generation aborted by fatal error",
                        post_id=str(active_post.id),
                    )
                    break
                except SQLAlchemyError as exc:
                    await self._record_post_failure(
                        session, active_post, exc, run_id=run_id
                    )
                    post_failure_recorded = True
                    raise SchedulerHaltError(
                        "DB 操作に失敗しました (scheduler 停止)", original=exc
                    ) from exc
                except YmgError as exc:
                    await self._record_post_failure(
                        session, active_post, exc, run_id=run_id
                    )
                    post_failure_recorded = True
                    failed_count += 1
                    continue
                except Exception as exc:
                    await self._record_post_failure(
                        session, active_post, exc, run_id=run_id
                    )
                    post_failure_recorded = True
                    failed_count += 1
                    continue
                succeeded_count += 1

            # 全 post 成功時のみ music_generated。 一部失敗は Plan failed (再承認が必要)。
            plan_failed = had_fatal or failed_count > 0
            plan_row.status = "failed" if plan_failed else "music_generated"
            await session.commit()
            await self._progress.record(
                run_id=run_id,
                step="cycle",
                status="failed" if plan_failed else "succeeded",
                context_type="plan",
                context_id=plan_row.id,
                error_category=(
                    "fatal" if had_fatal else ("recoverable" if failed_count > 0 else None)
                ),
                error_message=(
                    f"{failed_count} post(s) failed" if failed_count > 0 else None
                ),
            )
            cycle_started = False
            log.info(
                "music generation finished",
                plan_id=plan_id_str,
                succeeded=succeeded_count,
                failed=failed_count,
            )
            return MusicGenerationResult(
                plan_id=plan_id_str,
                post_ids=post_ids,
                succeeded_count=succeeded_count,
                failed_count=failed_count,
            )
        except BaseException as exc:
            await self._terminalize_aborted_run(
                session,
                plan_row=plan_row,
                run_id=run_id,
                exc=exc,
                cycle_started=cycle_started,
                post=active_post,
                post_failure_recorded=post_failure_recorded,
            )
            raise

    async def _terminalize_aborted_run(
        self,
        session: AsyncSession,
        *,
        plan_row: Plan,
        run_id: uuid.UUID,
        exc: BaseException,
        cycle_started: bool,
        post: Post | None,
        post_failure_recorded: bool,
    ) -> None:
        """予約開始後の例外で Plan / 開いている工程を failed に確定する。"""
        if post is not None and not post_failure_recorded and post.status in (
            "pending",
            "generating",
        ):
            await self._record_post_failure(session, post, exc, run_id=run_id)

        if plan_row.status == "executing":
            plan_row.status = "failed"
            try:
                await session.commit()
            except SQLAlchemyError:
                await session.rollback()

        if cycle_started:
            category = resolve_category(exc)
            await self._progress.record(
                run_id=run_id,
                step="cycle",
                status="failed",
                context_type="plan",
                context_id=plan_row.id,
                error_category=category.value,
                error_message=str(exc),
            )

    async def _load_enabled_genres(self, session: AsyncSession) -> list[str]:
        rows = (
            (await session.execute(select(Genre.name).where(Genre.enabled.is_(True))))
            .scalars()
            .all()
        )
        return list(rows)

    @staticmethod
    def _validate_plan_payload(plan_row: Plan, allowed_genres: list[str]) -> DailyPlan:
        context = {ALLOWED_GENRES_CONTEXT_KEY: allowed_genres} if allowed_genres else {}
        try:
            return DailyPlan.model_validate(plan_row.payload, context=context)
        except Exception as exc:
            raise FatalError(
                "Plan.payload を DailyPlan として検証できませんでした",
                context={"plan_id": str(plan_row.id)},
                original=exc,
            ) from exc

    async def _create_post(
        self,
        *,
        session: AsyncSession,
        plan_row: Plan,
        position: int,
        daily_post: DailyPost,
    ) -> Post:
        post = Post(
            id=uuid.uuid4(),
            plan_id=plan_row.id,
            position=position,
            genre=daily_post.genre,
            payload=daily_post.model_dump(mode="json"),
            status="pending",
        )
        session.add(post)
        await session.commit()
        return post

    async def _process_post(
        self,
        *,
        session: AsyncSession,
        post: Post,
        daily_post: DailyPost,
        run_id: uuid.UUID,
    ) -> None:
        log = bind_context(step="music_generation.post", genre=post.genre, job_id=str(post.id))
        post.status = "generating"
        await session.commit()
        await self._progress.record(
            run_id=run_id,
            step="post",
            status="running",
            genre=post.genre,
            context_type="post",
            context_id=post.id,
        )
        await self._progress.record(
            run_id=run_id,
            step="music",
            status="running",
            genre=post.genre,
            context_type="post",
            context_id=post.id,
        )

        tracks = await self._music.submit_and_wait(
            session=session,
            post=post,
            daily_post=daily_post,
            track_count=_TRACK_COUNT,
        )
        if len(tracks) != _TRACK_COUNT:
            raise FatalError(
                f"音楽トラック数が {_TRACK_COUNT} 件ではありません",
                context={"post_id": str(post.id), "count": len(tracks)},
            )
        post.status = "music_generated"
        await session.commit()

        await self._progress.record(
            run_id=run_id,
            step="music",
            status="succeeded",
            genre=post.genre,
            context_type="post",
            context_id=post.id,
        )
        await self._progress.record(
            run_id=run_id,
            step="post",
            status="succeeded",
            genre=post.genre,
            context_type="post",
            context_id=post.id,
        )
        log.info("post music generated", tracks=len(tracks))

    async def _record_post_failure(
        self,
        session: AsyncSession,
        post: Post,
        exc: BaseException,
        *,
        run_id: uuid.UUID,
    ) -> None:
        category = resolve_category(exc)
        post.status = "failed"
        post.error_category = category.value
        post.error_message = str(exc)
        try:
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
        # music / post が running のまま残らないよう対で failed を記録する。
        await self._progress.record(
            run_id=run_id,
            step="music",
            status="failed",
            genre=post.genre,
            context_type="post",
            context_id=post.id,
            error_category=category.value,
            error_message=str(exc),
        )
        await self._progress.record(
            run_id=run_id,
            step="post",
            status="failed",
            genre=post.genre,
            context_type="post",
            context_id=post.id,
            error_category=category.value,
            error_message=str(exc),
        )
