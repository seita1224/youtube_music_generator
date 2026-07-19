"""Critical path テスト: panic-stop service 追加 (T139)。

未到達行 (95% -> 100%) を覆う追加テスト群。
対象モジュール: ``ymg_backend.domain.panic_stop.service``

未到達行:
- 124    : panic_stop で window_hours < 1 -> ValueError
- 232-233: _load_video で scalars().first() 経路
           (set_private に recent_videos 外の動画 id が指定されたケース)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from ymg_backend.domain.panic_stop.service import (
    PanicStopService,
)
from ymg_backend.infrastructure.db.models import Video
from ymg_backend.infrastructure.youtube.privacy_client import (
    YOUTUBE_VIDEOS_URL,
    VideoPrivacyUpdater,
)

pytestmark = pytest.mark.critical

_FAKE_TOKEN = "panic-stop-edge-token"
_NOW = datetime(2026, 6, 17, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# OAuth stub
# ---------------------------------------------------------------------------


class _StubOAuth:
    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


# ---------------------------------------------------------------------------
# SpySession (first() メソッドを追加した拡張版)
# ---------------------------------------------------------------------------


@dataclass
class _Recorded:
    table_name: str
    kind: str
    values: dict[str, Any]


class _ScalarResult:
    """all() と first() を持つ最小スカラー結果。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


@dataclass
class SpySession:
    """``_load_video`` の scalars().first() 経路を覆うための拡張スパイ。

    ``_load_video`` が呼ぶ ``select(Video).where(...)`` の結果を
    ``_video_by_id`` ディクショナリから解決する。
    """

    recent_videos: list[Video] = field(default_factory=list)
    recorded: list[_Recorded] = field(default_factory=list)
    flush_count: int = 0
    commit_count: int = 0
    # _load_video の select に対して返す Video (key: youtube_video_id)
    extra_videos: dict[str, Video] = field(default_factory=dict)
    # select 呼び出し回数を記録するため、recent_videos 用 select と _load_video 用を区別
    _select_call_count: int = field(default=0, init=False)

    async def execute(self, statement: Any) -> Any:
        kind = type(statement).__name__.lower()
        if "select" in kind:
            self._select_call_count += 1
            self.recorded.append(_Recorded("videos", "select", {}))
            # 1 回目の select = _recent_videos (all() で使用)
            # 2 回目以降 = _load_video (first() で使用)
            if self._select_call_count == 1:
                return _ScalarResult(list(self.recent_videos))
            else:
                # _load_video は youtube_video_id を where に渡す
                # ここでは extra_videos から全件返して first() に委ねる
                return _ScalarResult(list(self.extra_videos.values()))
        compiled = statement.compile()
        table = getattr(statement, "table", None)
        table_name = table.name if table is not None else "<unknown>"
        self.recorded.append(_Recorded(table_name, kind, dict(compiled.params)))
        return _ScalarResult([])

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:  # pragma: no cover - 呼ばれてはいけない
        self.commit_count += 1

    @property
    def app_state_upserts(self) -> list[_Recorded]:
        return [r for r in self.recorded if r.table_name == "app_state" and "insert" in r.kind]

    @property
    def audit_inserts(self) -> list[_Recorded]:
        return [r for r in self.recorded if r.table_name == "audit_log" and "insert" in r.kind]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@dataclass
class _SpyScheduler:
    disable_count: int = 0

    def disable(self) -> None:
        self.disable_count += 1


def _make_video(*, youtube_video_id: str, posted_at: datetime) -> Video:
    return Video(
        id=uuid.uuid4(),
        youtube_video_id=youtube_video_id,
        genre="lofi",
        title=f"title {youtube_video_id}",
        description="desc",
        duration_sec=120,
        privacy_status="public",
        contains_synthetic_media=True,
        posted_at=posted_at,
        thumbnail_uri="file:///thumb.jpg",
    )


def _build_updater() -> VideoPrivacyUpdater:
    return VideoPrivacyUpdater(_StubOAuth())  # type: ignore[arg-type]


def _route_videos_update() -> respx.Route:
    return respx.put(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"id": "x", "status": {"privacyStatus": "private"}})
    )


# ---------------------------------------------------------------------------
# 124: window_hours < 1 -> ValueError
# ---------------------------------------------------------------------------


@respx.mock
async def test_panic_stop_raises_value_error_when_window_hours_zero() -> None:
    """window_hours=0 は ValueError を送出する (境界値)。"""
    session = SpySession()
    service = PanicStopService(privacy_updater=_build_updater())

    with pytest.raises(ValueError, match="window_hours must be >= 1"):
        await service.panic_stop(
            session=session,  # type: ignore[arg-type]
            scheduler_service=_SpyScheduler(),  # type: ignore[arg-type]
            window_hours=0,
        )


@respx.mock
async def test_panic_stop_raises_value_error_when_window_hours_negative() -> None:
    """window_hours=-1 も ValueError を送出する。"""
    session = SpySession()
    service = PanicStopService(privacy_updater=_build_updater())

    with pytest.raises(ValueError, match="window_hours must be >= 1"):
        await service.panic_stop(
            session=session,  # type: ignore[arg-type]
            scheduler_service=None,
            window_hours=-5,
        )


@respx.mock
async def test_panic_stop_window_hours_one_is_valid() -> None:
    """window_hours=1 は ValueError にならない (境界値: 最小有効値)。"""
    _route_videos_update()
    session = SpySession(recent_videos=[])
    service = PanicStopService(privacy_updater=_build_updater())

    result = await service.panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=_SpyScheduler(),  # type: ignore[arg-type]
        window_hours=1,
    )

    assert result.scheduler_enabled is False


# ---------------------------------------------------------------------------
# 232-233: _load_video の scalars().first() 経路
# (set_private に recent_videos 外の動画 id が指定された場合)
# ---------------------------------------------------------------------------


@respx.mock
async def test_panic_stop_set_private_video_not_in_recent_loads_from_db() -> None:
    """set_private に recent_videos に含まれない id を指定すると _load_video が呼ばれる。

    _load_video は select(Video).where(...) -> scalars().first() で 1 件取得する。
    取得できた場合は privacy_status を 'private' に更新する。
    """
    _route_videos_update()

    # recent_videos には入っていない動画
    extra_vid_id = "vid_OUTSIDE_WINDOW"
    extra_vid = _make_video(
        youtube_video_id=extra_vid_id,
        posted_at=_NOW - timedelta(hours=48),  # window 外
    )

    session = SpySession(
        recent_videos=[],  # 直近 window に動画なし
        extra_videos={extra_vid_id: extra_vid},  # _load_video の返却値
    )
    service = PanicStopService(privacy_updater=_build_updater())

    result = await service.panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=_SpyScheduler(),  # type: ignore[arg-type]
        window_hours=24,
        set_private=[extra_vid_id],
    )

    # _load_video 経路で Video を取得し private 化した
    assert result.updated_count == 1
    assert extra_vid.privacy_status == "private"
    # audit には updated_count が記録される
    assert len(session.audit_inserts) == 1
    assert session.audit_inserts[0].values.get("payload", {}).get("updated_count") == 1


@respx.mock
async def test_panic_stop_set_private_video_not_in_db_still_counts() -> None:
    """set_private の動画が DB にも存在しない場合でも updated_count は増える。

    _load_video が None を返す場合 (DB に行なし)。 YouTube 側の更新は成功 (mock) であり、
    DB の Video 行更新はスキップ (None チェック) される。
    """
    _route_videos_update()

    session = SpySession(
        recent_videos=[],
        extra_videos={},  # _load_video が None を返す (first() = None)
    )
    service = PanicStopService(privacy_updater=_build_updater())

    result = await service.panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=_SpyScheduler(),  # type: ignore[arg-type]
        window_hours=24,
        set_private=["vid_GHOST"],
    )

    # YouTube 側は private 化し updated_count はカウントする (DB 行なしでも)
    assert result.updated_count == 1
    assert result.scheduler_enabled is False
