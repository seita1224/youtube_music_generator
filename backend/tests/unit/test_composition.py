"""composition root: 音楽専用サービス組み立ての単体テスト。"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from ymg_backend.core.config import Settings
from ymg_backend.domain.pipeline.acoustid import AcoustidChecker
from ymg_backend.domain.pipeline.daily_cycle import DailyCycleOrchestrator
from ymg_backend.domain.pipeline.music_run import MusicRunService
from ymg_backend.infrastructure.composition import (
    build_daily_cycle_orchestrator_with_provider,
    build_music_run_service,
    make_cycle_runner,
    make_music_cron_runner,
)

_VALID_FERNET_KEY = "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg="


class _StubProvider:
    async def generate(self, req: Any) -> Any:
        raise AssertionError("LLM must not be called in composition smoke test")


def _settings() -> Settings:
    return Settings(
        fernet_key=SecretStr(_VALID_FERNET_KEY),
        storage_base_uri="file:///tmp/ymg-composition-test",
        gpu_worker_base_url="http://gpu-worker.test",
        acoustid_api_key=SecretStr("test-acoustid-key"),
        slack_webhook_url=SecretStr(""),
        dryrun_default=True,
        youtube_client_id="cid.apps.googleusercontent.com",
        youtube_client_secret=SecretStr("client-secret"),
        youtube_channel_id="UC_test",
    )


def test_build_orchestrator_wires_real_acoustid_checker() -> None:
    """フル日次経路では本番 AcoustidChecker が注入される。"""
    orch = build_daily_cycle_orchestrator_with_provider(_settings(), _StubProvider())  # type: ignore[arg-type]

    assert isinstance(orch, DailyCycleOrchestrator)
    assert isinstance(orch._acoustid, AcoustidChecker)


def test_build_music_run_service_has_no_acoustid_dependency() -> None:
    """音楽専用サービスは MusicRunService を返し、 AcoustID を組み立てない。"""
    service = build_music_run_service(_settings(), session_factory=MagicMock())
    assert isinstance(service, MusicRunService)


@pytest.mark.asyncio
async def test_make_cycle_runner_delegates_to_orchestrator_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """フル日次 CycleRunner ラッパが orchestrator.run を呼ぶ。"""
    orch = MagicMock(spec=DailyCycleOrchestrator)
    orch.run = AsyncMock(return_value=MagicMock())
    session = MagicMock()

    class _SessCtx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        "ymg_backend.infrastructure.db.session.get_sessionmaker",
        lambda: (lambda: _SessCtx()),
    )
    runner = make_cycle_runner(orch)
    target = date(2026, 7, 11)

    await runner(target_date=target)

    orch.run.assert_awaited_once_with(session=session, target_date=target)


@pytest.mark.asyncio
async def test_make_music_cron_runner_delegates_to_run_cron() -> None:
    """音楽 cron ラッパが MusicRunService.run_cron を呼ぶ (session 引数なし)。"""
    service = MagicMock(spec=MusicRunService)
    service.run_cron = AsyncMock()
    runner = make_music_cron_runner(service)
    target = date(2026, 7, 11)

    await runner(target_date=target)

    service.run_cron.assert_awaited_once_with(target_date=target)
