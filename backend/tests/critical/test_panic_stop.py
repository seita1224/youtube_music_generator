"""Critical path テスト: コンプラ緊急停止 (panic-stop) service (T111 / US4)。

ADR-0031 (緊急停止) を 100% カバーする。対象は US4 内部契約の
``ymg_backend.domain.panic_stop.service`` :

    DEFAULT_WINDOW_HOURS: Final[int] = 24

    @dataclass(frozen=True)
    class PanicStopResult:
        scheduler_enabled: bool          # 停止後 = False
        recent_videos: list[Video]       # 直近 window 内の Video 行
        updated_count: int               # 実際に private 化した本数

    class PanicStopService:
        def __init__(self, *, privacy_updater: VideoPrivacyUpdater) -> None: ...
        async def panic_stop(self, *, session, scheduler_service, window_hours=24,
                             set_private=None, actor="seita") -> PanicStopResult: ...

検証観点 (panic_stop の全分岐を 100% カバー):

1. **scheduler 停止 (disable 呼び出し)**: ``scheduler_service.disable()`` を 1 回呼ぶ。
2. **app_state.scheduler_enabled=false の永続化**: ``app_state`` テーブルへ
   ``scheduler_enabled=false`` の upsert (insert + on_conflict) を発行する。
   結果 ``scheduler_enabled`` は False。
3. **直近 N 時間の Video リスト取得**: ``posted_at >= now - window_hours`` で
   ``videos`` を引き、 ``recent_videos`` に載せる。
4. **YouTube videos.update(privacyStatus=private) 呼び出し (respx mock)**:
   ``set_private`` の各 youtube_video_id について ``PUT .../v3/videos?part=status``
   を打つ。 body は ``{"id":..., "status":{"privacyStatus":"private"}}``。
5. **Video.privacy_status="private" 更新**: 対応する ``Video`` ORM 行の
   ``privacy_status`` を ``private`` に書き換える (flush まで)。
6. **audit_log 記録**: ``action="panic_stop"`` で ``audit_log`` へ 1 行 insert する。
   ``payload`` に ``window_hours`` / ``set_private`` / ``updated_count`` を含む。
7. **commit は呼ばない (flush まで)**: トランザクション境界は API 側の責務。
8. **scheduler_service=None なら disable を skip** しフラグ永続化 + audit は行う。

OAuth / scheduler は stub / mock。 実 YouTube / 実 refresh は一切打たない。
HTTP は respx で ``videos.update`` (PUT) を mock する。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

# 実装が公開すべき内部契約 (US4)。service は ``domain/panic_stop/service.py``、
# privacy 更新 client は ``infrastructure/youtube/privacy_client.py`` に置く。
from ymg_backend.domain.panic_stop.service import (
    DEFAULT_WINDOW_HOURS,
    PanicStopResult,
    PanicStopService,
)
from ymg_backend.infrastructure.db.models import Video
from ymg_backend.infrastructure.youtube.privacy_client import (
    YOUTUBE_VIDEOS_URL,
    VideoPrivacyUpdater,
)

pytestmark = pytest.mark.critical

_FAKE_TOKEN = "panic-stop-token"
_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


# --- OAuth stub ---------------------------------------------------------------------


class _StubOAuth:
    """``YouTubeOAuth`` の代替 (固定トークンを返す duck-typed stub)。

    ``YouTubeOAuth`` は ``__slots__`` で ``get_access_token`` を上書きできないため、
    必要な I/F (``get_access_token``) のみを持つ stub を ``VideoPrivacyUpdater`` に渡す。
    実 HTTP / 実 refresh / 実 cipher は一切打たない。
    """

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


# --- scheduler stub -----------------------------------------------------------------


@dataclass
class _SpyScheduler:
    """``SchedulerService`` の最小スパイ (disable 呼び出し回数を記録)。"""

    disable_count: int = 0

    def disable(self) -> None:
        self.disable_count += 1


# --- session スパイ -----------------------------------------------------------------


@dataclass
class _Recorded:
    """記録した execute() 呼び出し (table 名 / 種別 / values)。"""

    table_name: str
    kind: str
    values: dict[str, Any]


class _ScalarResult:
    """``execute(select(...)).scalars().all()`` の戻り。 Video 行を供給する。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


@dataclass
class SpySession:
    """``AsyncSession`` の最小スパイ。

    - ``execute(select(...))``: 直近 window の ``Video`` 行 (``recent_videos``) を返す。
    - ``execute(insert(...))``: app_state / audit_log への書き込みを記録する。
    - ``flush``: 回数を記録 (commit は呼ばれてはいけない)。
    """

    recent_videos: list[Video] = field(default_factory=list)
    recorded: list[_Recorded] = field(default_factory=list)
    flush_count: int = 0
    commit_count: int = 0

    async def execute(self, statement: Any) -> Any:
        kind = type(statement).__name__.lower()
        if "select" in kind:
            self.recorded.append(_Recorded("videos", "select", {}))
            return _ScalarResult(list(self.recent_videos))
        compiled = statement.compile()
        table = getattr(statement, "table", None)
        table_name = table.name if table is not None else "<unknown>"
        self.recorded.append(_Recorded(table_name, kind, dict(compiled.params)))
        return _ScalarResult([])

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:  # pragma: no cover - 呼ばれてはいけない
        self.commit_count += 1

    # --- 観測ヘルパ -----------------------------------------------------------------

    @property
    def app_state_upserts(self) -> list[_Recorded]:
        return [r for r in self.recorded if r.table_name == "app_state" and "insert" in r.kind]

    @property
    def audit_inserts(self) -> list[_Recorded]:
        return [r for r in self.recorded if r.table_name == "audit_log" and "insert" in r.kind]

    @property
    def video_selects(self) -> list[_Recorded]:
        return [r for r in self.recorded if r.table_name == "videos" and r.kind == "select"]


# --- Video ORM 行ファクトリ ---------------------------------------------------------


def _make_video(*, youtube_video_id: str, posted_at: datetime) -> Video:
    """直近 window の対象となる ``Video`` ORM 行 (private 化候補) を作る。"""
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
    """respx mock 可能な実 ``VideoPrivacyUpdater`` (stub OAuth + 既定 httpx)。"""
    return VideoPrivacyUpdater(_StubOAuth())  # type: ignore[arg-type]


def _route_videos_update() -> respx.Route:
    """``PUT .../v3/videos`` を 200 OK で mock する (privacyStatus 反映)。"""
    return respx.put(url__startswith=YOUTUBE_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"id": "x", "status": {"privacyStatus": "private"}})
    )


# ---------------------------------------------------------------------------
# 本命: scheduler 停止 + 直近 Video private 化 + audit を 1 経路でカバー
# ---------------------------------------------------------------------------


@pytest.mark.fr("FR-102")
@respx.mock
async def test_panic_stop_disables_lists_privatizes_and_audits() -> None:
    """FR-102: panic_stop の全副作用 (1)-(6) を 1 ケースで網羅する。"""
    route = _route_videos_update()
    recent = [
        _make_video(youtube_video_id="vid_A", posted_at=_NOW - timedelta(hours=1)),
        _make_video(youtube_video_id="vid_B", posted_at=_NOW - timedelta(hours=2)),
    ]
    session = SpySession(recent_videos=recent)
    scheduler = _SpyScheduler()
    service = PanicStopService(privacy_updater=_build_updater())

    result = await service.panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=scheduler,  # type: ignore[arg-type]
        window_hours=24,
        set_private=["vid_A", "vid_B"],
        actor="seita",
    )

    # (1) scheduler.disable() を呼ぶ
    assert scheduler.disable_count == 1

    # (2) app_state.scheduler_enabled=false を upsert し、結果も False
    upserts = session.app_state_upserts
    assert len(upserts) == 1
    assert upserts[0].values.get("key") == "scheduler_enabled"
    assert upserts[0].values.get("value") is False
    assert result.scheduler_enabled is False

    # (3) 直近 window の Video を引いて recent_videos に載せる
    assert len(session.video_selects) >= 1
    assert {v.youtube_video_id for v in result.recent_videos} == {"vid_A", "vid_B"}

    # (4) YouTube videos.update(privacyStatus=private) を各 id ぶん打つ (PUT)
    assert route.call_count == 2
    for call in route.calls:
        assert call.request.method == "PUT"
        payload = json.loads(call.request.content.decode())
        assert payload["status"]["privacyStatus"] == "private"
        assert payload["id"] in {"vid_A", "vid_B"}
        assert call.request.headers.get("Authorization") == f"Bearer {_FAKE_TOKEN}"

    # (5) 対応 Video.privacy_status を private に更新する
    assert all(v.privacy_status == "private" for v in recent)
    assert result.updated_count == 2

    # (6) audit_log に action="panic_stop" を 1 行記録し、payload に要点を含む
    audits = session.audit_inserts
    assert len(audits) == 1
    audit_values = audits[0].values
    assert audit_values.get("action") == "panic_stop"
    assert audit_values.get("actor") == "seita"
    payload = audit_values.get("payload")
    assert isinstance(payload, dict)
    assert payload.get("window_hours") == 24
    assert payload.get("updated_count") == 2
    assert set(payload.get("set_private", [])) == {"vid_A", "vid_B"}

    # (7) commit は呼ばない (flush まで)
    assert session.commit_count == 0
    assert session.flush_count >= 1


# ---------------------------------------------------------------------------
# set_private 無し: 候補列挙のみ (private 化 / videos.update なし)
# ---------------------------------------------------------------------------


@respx.mock
async def test_panic_stop_without_set_private_only_lists() -> None:
    """set_private=None なら停止 + 候補列挙のみ。 videos.update は打たず updated_count=0。"""
    route = _route_videos_update()
    recent = [_make_video(youtube_video_id="vid_C", posted_at=_NOW - timedelta(hours=3))]
    session = SpySession(recent_videos=recent)
    scheduler = _SpyScheduler()
    service = PanicStopService(privacy_updater=_build_updater())

    result = await service.panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=scheduler,  # type: ignore[arg-type]
        window_hours=24,
        set_private=None,
    )

    assert scheduler.disable_count == 1
    assert result.scheduler_enabled is False
    assert [v.youtube_video_id for v in result.recent_videos] == ["vid_C"]
    # private 化は無いので videos.update は打たない、 候補は public のまま
    assert not route.called
    assert result.updated_count == 0
    assert recent[0].privacy_status == "public"
    # 列挙のみでも停止操作の audit は記録する
    assert len(session.audit_inserts) == 1
    assert session.commit_count == 0


@respx.mock
async def test_panic_stop_empty_set_private_lists_only() -> None:
    """set_private=[] (空配列) も None と同様、 列挙のみで private 化しない。"""
    route = _route_videos_update()
    session = SpySession(recent_videos=[_make_video(youtube_video_id="vid_D", posted_at=_NOW)])
    service = PanicStopService(privacy_updater=_build_updater())

    result = await service.panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=_SpyScheduler(),  # type: ignore[arg-type]
        set_private=[],
    )

    assert result.updated_count == 0
    assert not route.called


# ---------------------------------------------------------------------------
# scheduler_service=None: disable skip だがフラグ永続化 + audit は行う
# ---------------------------------------------------------------------------


@respx.mock
async def test_panic_stop_without_scheduler_service_skips_disable() -> None:
    """scheduler_service=None なら disable を skip しつつフラグ + audit は残す (best-effort)。"""
    _route_videos_update()
    session = SpySession(recent_videos=[])
    service = PanicStopService(privacy_updater=_build_updater())

    result = await service.panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=None,
        set_private=None,
    )

    # disable は呼べないが、 フラグ永続化と audit は必ず行う
    assert result.scheduler_enabled is False
    assert len(session.app_state_upserts) == 1
    assert session.app_state_upserts[0].values.get("value") is False
    assert len(session.audit_inserts) == 1
    assert session.commit_count == 0


# ---------------------------------------------------------------------------
# 既定 window_hours / 境界
# ---------------------------------------------------------------------------


def test_default_window_hours_is_24() -> None:
    """contracts (window_hours default 24) と内部契約定数が一致する。"""
    assert DEFAULT_WINDOW_HOURS == 24


@respx.mock
async def test_panic_stop_uses_default_window_when_omitted() -> None:
    """window_hours 省略時は DEFAULT_WINDOW_HOURS (24) が audit payload に載る。"""
    _route_videos_update()
    session = SpySession(recent_videos=[])
    service = PanicStopService(privacy_updater=_build_updater())

    result = await service.panic_stop(
        session=session,  # type: ignore[arg-type]
        scheduler_service=_SpyScheduler(),  # type: ignore[arg-type]
    )

    assert isinstance(result, PanicStopResult)
    payload = session.audit_inserts[0].values.get("payload")
    assert isinstance(payload, dict)
    assert payload.get("window_hours") == DEFAULT_WINDOW_HOURS
