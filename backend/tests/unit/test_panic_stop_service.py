"""``PanicStopService`` の単体テスト (domain/panic_stop/service.py, US4 T112)。

実 DB / 実 YouTube / 実 scheduler を一切起動せず、 in-memory の :class:`_FakeSession`、
``disable`` 呼び出しを記録する :class:`_FakeScheduler`、 ``set_privacy`` を記録する
:class:`_FakePrivacyUpdater` で panic-stop の挙動を検証する。

検証観点:

- scheduler 配線時: ``disable()`` を 1 回呼び、 ``app_state.scheduler_enabled=false`` を upsert、
  直近動画を列挙し、 ``set_private`` 指定動画を YouTube + DB で private 化、 audit を記録。
- scheduler 未配線 (``None``) でも例外を出さず、 フラグ永続化 / 列挙 / private 化 / audit を行う。
- ``set_private`` が ``None`` / 空なら private 化はせず候補列挙のみ (``updated_count == 0``)。
- ``window_hours`` 境界: 0 以下は ``ValueError``。
- YouTube 側 4xx (RecoverableError) はそのまま送出される (API が写像する想定)。
- 戻り値 ``PanicStopResult`` は ``scheduler_enabled=False`` / ``recent_videos`` / ``updated_count``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from ymg_backend.domain.errors.errors import RecoverableError
from ymg_backend.domain.panic_stop.service import (
    DEFAULT_WINDOW_HOURS,
    PanicStopResult,
    PanicStopService,
)

pytestmark = pytest.mark.unit


# --- in-memory fakes --------------------------------------------------------------


@dataclass
class _FakeVideo:
    """ORM ``Video`` の最小スタンドイン (privacy_status を可変で持つ)。"""

    youtube_video_id: str
    privacy_status: str = "public"
    posted_at: datetime = field(default_factory=lambda: datetime(2026, 6, 16, tzinfo=UTC))


class _ScalarsResult:
    """``execute(...).scalars().all()/first()`` を満たす最小結果。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


@dataclass
class _FakeSession:
    """``execute`` / ``flush`` のみ持つ in-memory セッション。

    ``execute`` のディスパッチ:
    - app_state への ``Insert`` (upsert) / audit_log への insert → 空結果 (戻り不要)。
    - 直近動画 ``select(Video).where(posted_at >= ...)`` → ``recent`` を返す。
    - 単体 ``select(Video).where(youtube_video_id = ...)`` → ``by_id`` から 1 件。
    """

    recent: list[_FakeVideo] = field(default_factory=list)
    by_id: dict[str, _FakeVideo] = field(default_factory=dict)
    flushes: int = 0
    upserted_flag: bool | None = None

    async def execute(self, statement: Any) -> Any:
        rendered = str(statement)
        cls = statement.__class__.__name__.lower()
        if cls.startswith("insert"):
            # app_state.scheduler_enabled の upsert を観測 (audit insert は無視)。
            compiled = statement.compile().params
            if compiled.get("key") == "scheduler_enabled":
                self.upserted_flag = bool(compiled.get("value"))
            return _ScalarsResult([])
        if "posted_at >=" in rendered:
            return _ScalarsResult(list(self.recent))
        if "youtube_video_id =" in rendered:
            # bind 値が取れないため by_id 全体から first を返す簡易実装 (テストでは 1 件のみ登録)。
            return _ScalarsResult(list(self.by_id.values()))
        return _ScalarsResult([])

    async def flush(self) -> None:
        self.flushes += 1


@dataclass
class _FakeScheduler:
    """``disable()`` の呼び出し回数を記録する scheduler スタンドイン。"""

    disabled: int = 0

    def disable(self) -> None:
        self.disabled += 1


@dataclass
class _FakePrivacyUpdater:
    """``set_privacy`` の呼び出しを記録する updater (任意で例外送出)。"""

    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def set_privacy(
        self, *, session: Any, youtube_video_id: str, privacy_status: str
    ) -> None:
        del session
        self.calls.append({"youtube_video_id": youtube_video_id, "privacy_status": privacy_status})
        if self.error is not None:
            raise self.error


def _service(updater: _FakePrivacyUpdater) -> PanicStopService:
    return PanicStopService(privacy_updater=updater)  # type: ignore[arg-type]


# --- happy path -------------------------------------------------------------------


async def test_panic_stop_disables_and_privatizes_targets() -> None:
    """scheduler を止め、 指定動画を YouTube + DB で private 化し audit する。"""
    v1 = _FakeVideo("vid_1", privacy_status="public")
    v2 = _FakeVideo("vid_2", privacy_status="public")
    session = _FakeSession(recent=[v1, v2])
    scheduler = _FakeScheduler()
    updater = _FakePrivacyUpdater()

    result = await _service(updater).panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=scheduler,  # type: ignore[arg-type]
        window_hours=12,
        set_private=["vid_1"],
        actor="admin",
    )

    assert isinstance(result, PanicStopResult)
    assert result.scheduler_enabled is False
    assert result.updated_count == 1
    assert [v.youtube_video_id for v in result.recent_videos] == ["vid_1", "vid_2"]
    # scheduler.disable() を 1 回呼ぶ。
    assert scheduler.disabled == 1
    # app_state.scheduler_enabled=false を永続化。
    assert session.upserted_flag is False
    # YouTube 側 private 化は対象動画のみ。
    assert updater.calls == [{"youtube_video_id": "vid_1", "privacy_status": "private"}]
    # DB 行も private に更新 (候補内の v1 のみ、 v2 は据え置き)。
    assert v1.privacy_status == "private"
    assert v2.privacy_status == "public"


async def test_no_targets_enumerates_only() -> None:
    """``set_private`` 省略時は private 化せず候補列挙のみ (updated_count == 0)。"""
    v1 = _FakeVideo("vid_1")
    session = _FakeSession(recent=[v1])
    updater = _FakePrivacyUpdater()

    result = await _service(updater).panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=_FakeScheduler(),  # type: ignore[arg-type]
    )

    assert result.updated_count == 0
    assert updater.calls == []
    assert v1.privacy_status == "public"
    # 既定 window で列挙だけは行う。
    assert [v.youtube_video_id for v in result.recent_videos] == ["vid_1"]


async def test_default_window_hours() -> None:
    """``window_hours`` 省略時は ``DEFAULT_WINDOW_HOURS`` (= 24)。"""
    assert DEFAULT_WINDOW_HOURS == 24
    session = _FakeSession(recent=[])
    result = await _service(_FakePrivacyUpdater()).panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=_FakeScheduler(),  # type: ignore[arg-type]
    )
    assert result.recent_videos == []


# --- scheduler 未配線 -------------------------------------------------------------


async def test_scheduler_not_wired_skips_disable() -> None:
    """``scheduler_service=None`` でも例外を出さず、 フラグ永続化 / 列挙 / private 化を行う。"""
    v1 = _FakeVideo("vid_1", privacy_status="public")
    session = _FakeSession(recent=[v1])
    updater = _FakePrivacyUpdater()

    result = await _service(updater).panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=None,
        set_private=["vid_1"],
    )

    assert result.scheduler_enabled is False
    assert result.updated_count == 1
    assert session.upserted_flag is False
    assert v1.privacy_status == "private"


# --- 入力検証 ---------------------------------------------------------------------


@pytest.mark.parametrize("bad_window", [0, -1, -24])
async def test_window_hours_below_one_raises(bad_window: int) -> None:
    """``window_hours`` が 1 未満なら ``ValueError`` (境界検証)。"""
    with pytest.raises(ValueError, match="window_hours"):
        await _service(_FakePrivacyUpdater()).panic_stop(
            session=_FakeSession(),  # type: ignore[arg-type]
            scheduler_service=_FakeScheduler(),  # type: ignore[arg-type]
            window_hours=bad_window,
        )


# --- YouTube エラー伝播 -----------------------------------------------------------


async def test_youtube_error_propagates() -> None:
    """YouTube 側 4xx (RecoverableError) はそのまま送出される (API が写像する)。"""
    v1 = _FakeVideo("vid_1", privacy_status="public")
    session = _FakeSession(recent=[v1])
    updater = _FakePrivacyUpdater(error=RecoverableError("youtube rejected"))

    with pytest.raises(RecoverableError):
        await _service(updater).panic_stop(
            session=session,  # type: ignore[arg-type]
            scheduler_service=_FakeScheduler(),  # type: ignore[arg-type]
            set_private=["vid_1"],
        )
