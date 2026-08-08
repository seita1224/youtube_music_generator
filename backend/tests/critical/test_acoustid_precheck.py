"""Critical path テスト: AcoustID 指紋プレチェック (T071)。

Constitution II / ADR-0005 / FR-010〜FR-012 を 100% カバーする。

検証観点:

1. **clear**: lookup レスポンスがマッチ無し / 低スコアのとき
   ``AudioTrack.acoustid_status`` が ``not_checked → clear`` に更新され、戻り値が
   ``"clear"`` になる。再生成対象に含めない。
2. **hit**: lookup レスポンスが閾値 (``ACOUSTID_MATCH_THRESHOLD``) 以上のスコアを
   返すとき ``hit`` に更新され、``AcoustidVerdict.hit_positions`` に当該 track の
   position が入る。``acoustid_response`` (JSONB) に生レスポンスが保存される。
3. **api_error**: lookup が HTTP 5xx / ``status != "ok"`` を返すとき ``api_error`` に
   更新され、(リトライ後も復旧しなければ) その track はジャンル停止判定に算入しない。
4. **3 連続 hit でジャンル一時停止 (FR-012)**: ``consecutive_hits`` が 3 に到達すると
   対象 ``Genre.enabled=False`` に更新 + ``write_audit_log`` 1 行 + Slack ``[ERROR]`` 通知。
5. **fingerprint 生成は注入**: chromaprint / fpcalc は呼ばず、注入した fingerprinter で
   ``(duration, fingerprint)`` を供給する (バイナリ非依存)。
6. lookup の wire 形式 (POST / client=apikey / fingerprint / duration) を respx で検証する。

HTTP は respx で AcoustID lookup API を mock し、fingerprint 生成は注入で mock する。
session / notifier / storage はスパイ (fake) で副作用を検証する。
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError, dataclass, field
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from ymg_backend.domain.compliance.acoustid import (
    ACOUSTID_CONSECUTIVE_HIT_LIMIT,
    ACOUSTID_LOOKUP_URL,
    ACOUSTID_MATCH_THRESHOLD,
    AcoustidChecker,
    AcoustidVerdict,
)
from ymg_backend.domain.errors import NotificationLevel

pytestmark = pytest.mark.critical

_API_KEY = SecretStr("test-acoustid-key")


# --- スパイ / fake ----------------------------------------------------------------


@dataclass
class _ExecutedStatement:
    """記録した execute() 呼び出し (audit_log insert / Genre update)。"""

    table_name: str
    values: dict[str, Any]
    kind: str  # "insert" | "update"


class SpySession:
    """``AsyncSession`` の最小スパイ。

    ``write_audit_log`` (Core insert) と Genre 無効化 (Core update) の execute/flush
    のみを記録する。ORM クエリ (``session.get`` / ``execute(select(...))``) は本テストの
    対象外 (オーケストレータ責務) なので呼ばれない設計にする。
    """

    def __init__(self) -> None:
        self.executed: list[_ExecutedStatement] = []
        self.flush_count = 0

    async def execute(self, statement: Any) -> None:
        compiled = statement.compile()
        table = getattr(statement, "table", None)
        table_name = table.name if table is not None else "<unknown>"
        kind = type(statement).__name__.lower()  # "insert" / "update"
        self.executed.append(
            _ExecutedStatement(
                table_name=table_name,
                values=dict(compiled.params),
                kind=kind,
            )
        )

    async def flush(self) -> None:
        self.flush_count += 1

    # --- helpers --------------------------------------------------------------
    @property
    def audit_inserts(self) -> list[_ExecutedStatement]:
        return [e for e in self.executed if e.table_name == "audit_log"]

    @property
    def genre_updates(self) -> list[_ExecutedStatement]:
        return [e for e in self.executed if e.table_name == "genres" and e.kind == "update"]


@dataclass
class _SentNotification:
    level: NotificationLevel
    message: str
    context: dict[str, Any]


@dataclass
class SpyNotifier:
    """``SlackNotifier`` のスパイ。``notify`` のみ記録する。"""

    sent: list[_SentNotification] = field(default_factory=list)

    async def notify(
        self,
        *,
        level: NotificationLevel,
        message: str,
        context: Any | None = None,
    ) -> None:
        self.sent.append(
            _SentNotification(level=level, message=message, context=dict(context or {}))
        )

    async def notify_error(
        self, exc: BaseException, *, context: Any | None = None
    ) -> None:  # pragma: no cover - 本テストでは未使用
        self.sent.append(
            _SentNotification(
                level=NotificationLevel.ERROR,
                message=str(exc),
                context=dict(context or {}),
            )
        )


class SpyStorage:
    """``StorageAdapter`` の最小スパイ。``read_bytes`` のみ。

    実体は注入 fingerprinter が読むだけなので中身は不問。呼ばれた URI を記録する。
    """

    def __init__(self) -> None:
        self.read_uris: list[str] = []

    def read_bytes(self, uri: str) -> bytes:
        self.read_uris.append(uri)
        return b"\x00\x00fake-audio\x00\x00"

    def resolve_uri(self, path: str) -> str:
        return path


@dataclass
class StubTrack:
    """``AudioTrack`` 互換の軽量スタブ (DB 非依存)。

    checker は ``position`` / ``audio_uri`` / ``duration_sec`` を読み、
    ``acoustid_status`` / ``acoustid_response`` / ``fingerprint_hash`` を書く。
    """

    position: int
    audio_uri: str
    duration_sec: int = 300
    acoustid_status: str = "not_checked"
    acoustid_response: dict[str, Any] | None = None
    fingerprint_hash: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class StubPost:
    """``Post`` 互換の軽量スタブ。"""

    id: uuid.UUID = field(default_factory=uuid.uuid4)


# --- 注入 fingerprinter ------------------------------------------------------------


def _stub_fingerprinter_factory() -> Any:
    """常に固定の ``(duration, fingerprint)`` を返す注入 fingerprinter。"""

    def _fp(audio: bytes, duration_sec: int) -> tuple[int, str]:
        return duration_sec, "AQABZ_stub_fingerprint"

    return _fp


# --- AcoustID lookup レスポンス組み立て -------------------------------------------


def _lookup_ok(*, score: float, with_recording: bool = True) -> dict[str, Any]:
    """``status=ok`` の lookup レスポンス。``score`` でマッチ強度を制御する。"""
    result: dict[str, Any] = {"id": "result-uuid", "score": score}
    if with_recording:
        result["recordings"] = [
            {
                "id": "rec-uuid",
                "title": "Some Commercial Track",
                "artists": [{"name": "Famous Artist"}],
            }
        ]
    return {"status": "ok", "results": [result]}


def _lookup_no_results() -> dict[str, Any]:
    return {"status": "ok", "results": []}


def _lookup_error_status() -> dict[str, Any]:
    return {"status": "error", "error": {"code": 3, "message": "invalid fingerprint"}}


def _build_checker(
    *,
    notifier: SpyNotifier,
    storage: SpyStorage | None = None,
) -> AcoustidChecker:
    return AcoustidChecker(
        api_key=_API_KEY,
        storage=storage or SpyStorage(),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        fingerprinter=_stub_fingerprinter_factory(),
    )


# ---------------------------------------------------------------------------
# check_track: clear / hit / api_error の 3 分岐
# ---------------------------------------------------------------------------


@respx.mock
async def test_check_track_clear_low_score() -> None:
    """閾値未満スコアは clear に分類し、status を更新する。"""
    route = respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_ok(score=ACOUSTID_MATCH_THRESHOLD - 0.1))
    )
    session = SpySession()
    checker = _build_checker(notifier=SpyNotifier())
    track = StubTrack(position=0, audio_uri="file:///srv/ymg/outputs/music/p/0.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "clear"
    assert track.acoustid_status == "clear"
    assert route.called
    # status 更新で flush まで行う (commit は呼ばない)
    assert session.flush_count >= 1


@respx.mock
async def test_check_track_clear_no_results() -> None:
    """results 空 (マッチ無し) も clear。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_no_results())
    )
    session = SpySession()
    checker = _build_checker(notifier=SpyNotifier())
    track = StubTrack(position=1, audio_uri="file:///srv/ymg/outputs/music/p/1.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "clear"
    assert track.acoustid_status == "clear"


@respx.mock
async def test_check_track_hit_high_score() -> None:
    """閾値以上スコアは hit に分類し、生レスポンスを保存する。"""
    body = _lookup_ok(score=ACOUSTID_MATCH_THRESHOLD + 0.05)
    respx.post(ACOUSTID_LOOKUP_URL).mock(return_value=httpx.Response(200, json=body))
    session = SpySession()
    checker = _build_checker(notifier=SpyNotifier())
    track = StubTrack(position=2, audio_uri="file:///srv/ymg/outputs/music/p/2.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "hit"
    assert track.acoustid_status == "hit"
    # 生レスポンスを監査用に保持
    assert track.acoustid_response is not None
    assert track.acoustid_response["status"] == "ok"
    # fingerprint hash を記録
    assert track.fingerprint_hash is not None


@respx.mock
async def test_check_track_hit_exactly_at_threshold() -> None:
    """境界値: スコア == 閾値 は hit 扱い (>= 判定)。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_ok(score=ACOUSTID_MATCH_THRESHOLD))
    )
    session = SpySession()
    checker = _build_checker(notifier=SpyNotifier())
    track = StubTrack(position=3, audio_uri="file:///srv/ymg/outputs/music/p/3.wav")

    assert await checker.check_track(session=session, track=track) == "hit"  # type: ignore[arg-type]
    assert track.acoustid_status == "hit"


@respx.mock
async def test_check_track_api_error_on_5xx() -> None:
    """HTTP 5xx が (リトライ後も) 続けば api_error に分類する。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(return_value=httpx.Response(503, text="down"))
    session = SpySession()
    checker = _build_checker(notifier=SpyNotifier())
    track = StubTrack(position=4, audio_uri="file:///srv/ymg/outputs/music/p/4.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "api_error"
    assert track.acoustid_status == "api_error"


@respx.mock
async def test_check_track_api_error_on_status_error() -> None:
    """``status != ok`` の JSON も api_error。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_error_status())
    )
    session = SpySession()
    checker = _build_checker(notifier=SpyNotifier())
    track = StubTrack(position=5, audio_uri="file:///srv/ymg/outputs/music/p/5.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "api_error"
    assert track.acoustid_status == "api_error"


@respx.mock
async def test_check_track_wire_format() -> None:
    """lookup は POST / client=apikey / fingerprint / duration を送る。"""
    route = respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_no_results())
    )
    session = SpySession()
    checker = _build_checker(notifier=SpyNotifier())
    track = StubTrack(position=0, audio_uri="file:///x.wav", duration_sec=287)

    await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert route.called
    request = route.calls.last.request
    assert request.method == "POST"
    sent = request.content.decode()
    assert "client=test-acoustid-key" in sent
    assert "fingerprint=AQABZ_stub_fingerprint" in sent
    assert "duration=287" in sent


@respx.mock
async def test_check_track_reads_audio_via_storage() -> None:
    """音声バイトは storage.read_bytes(track.audio_uri) 経由で取得する。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_no_results())
    )
    storage = SpyStorage()
    session = SpySession()
    checker = _build_checker(notifier=SpyNotifier(), storage=storage)
    track = StubTrack(position=0, audio_uri="file:///srv/ymg/outputs/music/p/0.wav")

    await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert storage.read_uris == ["file:///srv/ymg/outputs/music/p/0.wav"]


# ---------------------------------------------------------------------------
# check_post_tracks: 6 track 集約判定 + 連続 hit カウント + ジャンル停止
# ---------------------------------------------------------------------------


@pytest.mark.fr("FR-010")
@respx.mock
async def test_check_post_tracks_all_clear() -> None:
    """FR-010: 全 track clear なら hit_positions 空・連続カウントは 0 にリセット。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_no_results())
    )
    session = SpySession()
    notifier = SpyNotifier()
    checker = _build_checker(notifier=notifier)
    post = StubPost()
    tracks = [StubTrack(position=i, audio_uri=f"file:///t{i}.wav") for i in range(6)]

    verdict = await checker.check_post_tracks(
        session=session,  # type: ignore[arg-type]
        post=post,  # type: ignore[arg-type]
        genre="lo-fi-hip-hop",
        tracks=tracks,  # type: ignore[arg-type]
        consecutive_hits=2,
    )

    assert isinstance(verdict, AcoustidVerdict)
    assert verdict.hit_positions == []
    # clear が出たら連続カウントはリセット
    assert verdict.consecutive_hits == 0
    assert verdict.genre_suspended is False
    assert all(t.acoustid_status == "clear" for t in tracks)
    # ジャンル停止していないので audit / Slack なし
    assert session.genre_updates == []
    assert notifier.sent == []


@respx.mock
async def test_check_post_tracks_partial_hit() -> None:
    """一部 hit なら当該 position のみ hit_positions に入る。"""
    hit_positions = {1, 4}

    def _side_effect(request: httpx.Request) -> httpx.Response:
        # 全 track が同じ fingerprint を送るので、呼び出し順で position を割り当てる。
        idx = _side_effect.counter  # type: ignore[attr-defined]
        _side_effect.counter += 1  # type: ignore[attr-defined]
        if idx in hit_positions:
            return httpx.Response(200, json=_lookup_ok(score=ACOUSTID_MATCH_THRESHOLD + 0.1))
        return httpx.Response(200, json=_lookup_no_results())

    _side_effect.counter = 0  # type: ignore[attr-defined]
    respx.post(ACOUSTID_LOOKUP_URL).mock(side_effect=_side_effect)

    session = SpySession()
    notifier = SpyNotifier()
    checker = _build_checker(notifier=notifier)
    post = StubPost()
    tracks = [StubTrack(position=i, audio_uri=f"file:///t{i}.wav") for i in range(6)]

    verdict = await checker.check_post_tracks(
        session=session,  # type: ignore[arg-type]
        post=post,  # type: ignore[arg-type]
        genre="lo-fi-hip-hop",
        tracks=tracks,  # type: ignore[arg-type]
        consecutive_hits=0,
    )

    assert sorted(verdict.hit_positions) == [1, 4]
    # この post で hit があったため連続カウントは前回 + 1
    assert verdict.consecutive_hits == 1
    assert verdict.genre_suspended is False
    # 当該ジャンルはまだ停止しない
    assert session.genre_updates == []
    assert notifier.sent == []


@pytest.mark.fr("FR-012")
@respx.mock
async def test_check_post_tracks_third_consecutive_hit_suspends_genre() -> None:
    """FR-012: 連続 3 回 hit でジャンルを停止。Genre.enabled=False + audit + Slack。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_ok(score=0.99))
    )
    session = SpySession()
    notifier = SpyNotifier()
    checker = _build_checker(notifier=notifier)
    post = StubPost()
    genre = "synthwave"
    tracks = [StubTrack(position=i, audio_uri=f"file:///t{i}.wav") for i in range(6)]

    # 直前まで 2 連続 hit (= limit - 1)。今回の hit で 3 連続到達。
    verdict = await checker.check_post_tracks(
        session=session,  # type: ignore[arg-type]
        post=post,  # type: ignore[arg-type]
        genre=genre,
        tracks=tracks,  # type: ignore[arg-type]
        consecutive_hits=ACOUSTID_CONSECUTIVE_HIT_LIMIT - 1,
    )

    assert verdict.consecutive_hits == ACOUSTID_CONSECUTIVE_HIT_LIMIT
    assert verdict.genre_suspended is True
    assert verdict.hit_positions  # hit はあった

    # 1) Genre.enabled=False への update が 1 件
    assert len(session.genre_updates) == 1
    update_values = session.genre_updates[0].values
    assert update_values.get("enabled") is False

    # 2) audit_log に 1 行
    assert len(session.audit_inserts) == 1
    audit_values = session.audit_inserts[0].values
    assert (
        genre in str(audit_values.get("target_id", ""))
        or audit_values.get("payload", {}).get("genre") == genre
    )

    # 3) Slack 通知 (ERROR レベル)
    assert len(notifier.sent) == 1
    assert notifier.sent[0].level is NotificationLevel.ERROR


@respx.mock
async def test_check_post_tracks_clear_resets_consecutive_counter() -> None:
    """全 clear なら連続カウントは 0 に戻る (limit 直前でも停止しない)。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_lookup_no_results())
    )
    session = SpySession()
    notifier = SpyNotifier()
    checker = _build_checker(notifier=notifier)
    tracks = [StubTrack(position=i, audio_uri=f"file:///t{i}.wav") for i in range(6)]

    verdict = await checker.check_post_tracks(
        session=session,  # type: ignore[arg-type]
        post=StubPost(),  # type: ignore[arg-type]
        genre="ambient",
        tracks=tracks,  # type: ignore[arg-type]
        consecutive_hits=ACOUSTID_CONSECUTIVE_HIT_LIMIT - 1,
    )

    assert verdict.consecutive_hits == 0
    assert verdict.genre_suspended is False
    assert session.genre_updates == []
    assert notifier.sent == []


@respx.mock
async def test_check_post_tracks_api_error_does_not_count_as_hit() -> None:
    """api_error の track はジャンル停止判定 (連続 hit) に算入しない。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(return_value=httpx.Response(503, text="down"))
    session = SpySession()
    notifier = SpyNotifier()
    checker = _build_checker(notifier=notifier)
    tracks = [StubTrack(position=i, audio_uri=f"file:///t{i}.wav") for i in range(6)]

    verdict = await checker.check_post_tracks(
        session=session,  # type: ignore[arg-type]
        post=StubPost(),  # type: ignore[arg-type]
        genre="chillhop",
        tracks=tracks,  # type: ignore[arg-type]
        consecutive_hits=ACOUSTID_CONSECUTIVE_HIT_LIMIT - 1,
    )

    # hit ではないので停止しない。連続カウントは据え置き (hit でも clear でもない)。
    assert verdict.genre_suspended is False
    assert verdict.hit_positions == []
    assert session.genre_updates == []
    assert all(t.acoustid_status == "api_error" for t in tracks)


# ---------------------------------------------------------------------------
# AcoustidVerdict は frozen (不変)
# ---------------------------------------------------------------------------


def test_verdict_is_frozen() -> None:
    verdict = AcoustidVerdict(hit_positions=[1], consecutive_hits=1, genre_suspended=False)
    with pytest.raises(FrozenInstanceError):
        verdict.consecutive_hits = 2  # type: ignore[misc]


def test_constants_sane() -> None:
    assert ACOUSTID_CONSECUTIVE_HIT_LIMIT == 3
    assert 0.0 < ACOUSTID_MATCH_THRESHOLD <= 1.0
    assert ACOUSTID_LOOKUP_URL.endswith("/lookup")
