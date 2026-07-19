"""Composition root: 音楽専用実行 / (互換) フル日次オーケストレータの組み立て。

本番の即時・定時実行は :class:`MusicRunService` (ADR-0006 / ADR-0011) を注入する。
フル日次 :class:`DailyCycleOrchestrator` は統合テスト・将来の投稿完了経路用に残す。
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Final

from loguru import logger

from ymg_backend.core.security import build_cipher_from_settings
from ymg_backend.domain.pipeline.acoustid import AcoustidChecker
from ymg_backend.domain.pipeline.daily_cycle import DailyCycleOrchestrator
from ymg_backend.domain.pipeline.image_jobs import ImageJobRunner
from ymg_backend.domain.pipeline.music_generation import MusicGenerationOrchestrator
from ymg_backend.domain.pipeline.music_jobs import MusicJobRunner
from ymg_backend.domain.pipeline.music_run import MusicRunService
from ymg_backend.domain.pipeline.planner import Planner
from ymg_backend.domain.plans.finisher import LlmFinisher
from ymg_backend.domain.prompts.loader import PromptLoader
from ymg_backend.domain.templates.loader import TemplateLoader
from ymg_backend.infrastructure.gpu_worker_client import GpuWorkerClient
from ymg_backend.infrastructure.job_progress import JobProgressRecorder
from ymg_backend.infrastructure.scheduler import CycleRunner, SchedulerService
from ymg_backend.infrastructure.slack.notifier import SlackNotifier
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter
from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth
from ymg_backend.infrastructure.youtube.uploader import YouTubeUploader
from ymg_backend.llm.factory import create_llm_provider

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ymg_backend.core.config import Settings
    from ymg_backend.llm.base import LlmProvider

__all__ = [
    "build_daily_cycle_orchestrator",
    "build_daily_cycle_orchestrator_with_provider",
    "build_music_run_service",
    "build_scheduler_service",
    "make_cycle_runner",
    "make_music_cron_runner",
]


def build_daily_cycle_orchestrator_with_provider(
    settings: Settings,
    provider: LlmProvider,
) -> DailyCycleOrchestrator:
    """注入済み LLM provider から ``DailyCycleOrchestrator`` を同期組み立てする。

    テストは stub provider を渡して本関数を直接呼べる。 AcoustID は常に本番
    ``AcoustidChecker`` (fingerprinter 省略 = ``default_fingerprinter``)。

    Args:
        settings: 環境設定。
        provider: planner / finisher が共有する LLM provider。

    Returns:
        依存注入済みの :class:`DailyCycleOrchestrator`。
    """
    storage = StorageAdapter(settings.storage_base_uri)
    notifier = SlackNotifier(settings.slack_webhook_url)
    gpu_client = GpuWorkerClient(settings.gpu_worker_base_url)
    music = MusicJobRunner(gpu_client, storage)
    image = ImageJobRunner(gpu_client, storage)
    prompt_loader = PromptLoader()
    planner = Planner(provider, prompt_loader)
    finisher = LlmFinisher(provider, prompt_loader=prompt_loader)
    acoustid = AcoustidChecker(settings.acoustid_api_key, storage, notifier)
    cipher = build_cipher_from_settings(settings)
    oauth = YouTubeOAuth(settings, cipher)
    uploader = YouTubeUploader(oauth, storage, settings)
    return DailyCycleOrchestrator(
        settings=settings,
        planner=planner,
        finisher=finisher,
        music=music,
        # AcoustidChecker は consecutive_hits 必須だが AcoustidLike Protocol は
        # stub 互換のため省略。 実行時はシグネチャ検査で吸収する (daily_cycle)。
        acoustid=acoustid,  # type: ignore[arg-type]
        image=image,
        uploader=uploader,
        notifier=notifier,
        storage=storage,
        template_loader=TemplateLoader(),
    )


async def build_daily_cycle_orchestrator(
    settings: Settings,
    *,
    session: AsyncSession | None = None,
) -> DailyCycleOrchestrator:
    """settings (+ 任意で app_state) から本番 ``DailyCycleOrchestrator`` を組み立てる。"""
    provider = await create_llm_provider(settings, session=session)
    orchestrator = build_daily_cycle_orchestrator_with_provider(settings, provider)
    logger.bind(component="composition").info(
        "DailyCycleOrchestrator assembled",
        gpu_worker=settings.gpu_worker_base_url,
        dryrun_default=settings.dryrun_default,
    )
    return orchestrator


def build_music_run_service(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> MusicRunService:
    """音楽専用実行サービスを組み立てる (AcoustID / OAuth / Slack 不要)。

    Args:
        settings: 環境設定 (storage / GPU worker URL)。
        session_factory: 進捗記録・finalize・execute 用の独立セッション工場。

    Returns:
        cron / run-now 共有の :class:`MusicRunService`。
    """
    storage = StorageAdapter(settings.storage_base_uri)
    gpu_client = GpuWorkerClient(settings.gpu_worker_base_url)
    music = MusicJobRunner(gpu_client, storage)
    progress = JobProgressRecorder(session_factory)
    orchestrator = MusicGenerationOrchestrator(music=music, progress=progress)
    service = MusicRunService(orchestrator=orchestrator, session_factory=session_factory)
    logger.bind(component="composition").info(
        "MusicRunService assembled",
        gpu_worker=settings.gpu_worker_base_url,
    )
    return service


def make_music_cron_runner(service: MusicRunService) -> CycleRunner:
    """``MusicRunService.run_cron`` を ``CycleRunner`` Protocol 互換に包む。

    予約と実行のセッション分離は :meth:`MusicRunService.run_cron` 側の責務。
    """

    async def cycle_runner(*, target_date: date) -> None:
        await service.run_cron(target_date=target_date)

    return cycle_runner


def make_cycle_runner(orchestrator: DailyCycleOrchestrator) -> CycleRunner:
    """``orchestrator.run`` を ``CycleRunner`` Protocol 互換の callable に包む (フル日次用)。

    フル日次は実行本体が session を要求するため、 ここで自前の session を開く。
    """
    from ymg_backend.infrastructure.db.session import get_sessionmaker

    async def cycle_runner(*, target_date: date) -> None:
        async with get_sessionmaker()() as session:
            await orchestrator.run(session=session, target_date=target_date)

    return cycle_runner


def build_scheduler_service(
    scheduler: AsyncIOScheduler,
    *,
    cycle_runner: CycleRunner,
    notifier: SlackNotifier | None = None,
) -> SchedulerService:
    """既存 ``AsyncIOScheduler`` 上に ``SchedulerService`` を構築する。

    weekly / analytics runner は未配線 (``None``)。 日次スロットは音楽生成 cron
    (:meth:`MusicRunService.run_cron`) を ``cycle_runner`` として受け取る。
    """
    service: Final[SchedulerService] = SchedulerService(
        scheduler,
        cycle_runner=cycle_runner,
        notifier=notifier,
    )
    return service
