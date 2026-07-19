"""AcoustidChecker (domain/pipeline/acoustid.py) の単体テスト。

critical テスト (tests/critical/test_acoustid_precheck.py) が分岐網羅・wire 形式・
ジャンル停止・frozen verdict を担保するため、 本 unit テストは **重複を避け** て
以下の補完観点に絞る:

- 純粋ヘルパ (``_next_consecutive_hits`` / ``_max_score`` / ``_fingerprint_hash``) の単体挙動。
- lookup の HTTP リトライ復旧 (5xx → 200) と最終失敗 (常時 5xx → api_error)。
- 不正 JSON / results 形不整合の api_error 吸収。
- 注入 client の所有権 (``aclose`` が外部注入 client を閉じない)。

外部依存 (AcoustID) は respx で mock し、 fingerprint は注入 stub。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from ymg_backend.domain.pipeline.acoustid import (
    ACOUSTID_LOOKUP_URL,
    ACOUSTID_MATCH_THRESHOLD,
    AcoustidChecker,
    _fingerprint_hash,
    _max_score,
    _next_consecutive_hits,
    default_fingerprinter,
)

_API_KEY = SecretStr("unit-key")


class _SpySession:
    """flush 回数のみ記録する最小 AsyncSession スパイ。"""

    def __init__(self) -> None:
        self.flush_count = 0

    async def execute(self, statement: Any) -> None:  # pragma: no cover - 本ファイルでは未使用
        raise AssertionError("execute should not be called in these unit tests")

    async def flush(self) -> None:
        self.flush_count += 1


@dataclass
class _SpyNotifier:
    sent: list[tuple[Any, str]] = field(default_factory=list)

    async def notify(self, *, level: Any, message: str, context: Any | None = None) -> None:
        self.sent.append((level, message))


class _SpyStorage:
    def read_bytes(self, uri: str) -> bytes:
        return b"audio-bytes"

    def resolve_uri(self, path: str) -> str:
        return path


@dataclass
class _StubTrack:
    position: int
    audio_uri: str
    duration_sec: int = 300
    acoustid_status: str = "not_checked"
    acoustid_response: dict[str, Any] | None = None
    fingerprint_hash: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def _fingerprinter(audio: bytes, duration_sec: int) -> tuple[int, str]:
    return duration_sec, "FP_UNIT"


def _build_checker(
    *, client: httpx.AsyncClient | None = None, notifier: _SpyNotifier | None = None
) -> AcoustidChecker:
    return AcoustidChecker(
        api_key=_API_KEY,
        storage=_SpyStorage(),  # type: ignore[arg-type]
        notifier=notifier or _SpyNotifier(),
        fingerprinter=_fingerprinter,
        client=client,
    )


# --- 純粋ヘルパ -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("previous", "has_hit", "has_clear", "expected"),
    [
        (0, True, False, 1),  # hit → +1
        (2, True, True, 3),  # hit 優先 (clear があっても +1)
        (2, False, True, 0),  # clear のみ → リセット
        (2, False, False, 2),  # api_error のみ → 据え置き
    ],
)
def test_next_consecutive_hits(
    previous: int, has_hit: bool, has_clear: bool, expected: int
) -> None:
    assert (
        _next_consecutive_hits(previous=previous, has_hit=has_hit, has_clear=has_clear) == expected
    )


def test_max_score_picks_maximum() -> None:
    lookup = {"status": "ok", "results": [{"score": 0.4}, {"score": 0.91}, {"score": 0.7}]}
    assert _max_score(lookup) == pytest.approx(0.91)


def test_max_score_none_when_empty_or_malformed() -> None:
    assert _max_score({"status": "ok", "results": []}) is None
    assert _max_score({"status": "ok", "results": "nope"}) is None
    assert _max_score({"status": "ok", "results": [{"no_score": 1}]}) is None


def test_fingerprint_hash_is_stable_sha256() -> None:
    h1 = _fingerprint_hash("FP_UNIT")
    h2 = _fingerprint_hash("FP_UNIT")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex
    assert _fingerprint_hash("OTHER") != h1


# --- lookup リトライ / 異常吸収 ----------------------------------------------------


@respx.mock
async def test_lookup_recovers_after_transient_5xx() -> None:
    """5xx → 200 のリトライ復旧で clear (例外を送出せず分類する)。"""
    route = respx.post(ACOUSTID_LOOKUP_URL).mock(
        side_effect=[
            httpx.Response(503, text="down"),
            httpx.Response(200, json={"status": "ok", "results": []}),
        ]
    )
    session = _SpySession()
    checker = _build_checker()
    track = _StubTrack(position=0, audio_uri="file:///a.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "clear"
    assert track.acoustid_status == "clear"
    assert route.call_count == 2  # 1 回リトライして復旧
    assert session.flush_count >= 1


@respx.mock
async def test_lookup_persistent_5xx_is_api_error() -> None:
    """常時 5xx は最終的に api_error。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(return_value=httpx.Response(500, text="boom"))
    checker = _build_checker()
    track = _StubTrack(position=1, audio_uri="file:///b.wav")

    assert await checker.check_track(session=_SpySession(), track=track) == "api_error"  # type: ignore[arg-type]
    assert track.acoustid_status == "api_error"
    assert track.acoustid_response is None  # 異常時は生レスポンスを保存しない


@respx.mock
async def test_lookup_4xx_is_api_error_without_retry() -> None:
    """4xx はリトライせず即 api_error。"""
    route = respx.post(ACOUSTID_LOOKUP_URL).mock(return_value=httpx.Response(400, text="bad"))
    checker = _build_checker()
    track = _StubTrack(position=2, audio_uri="file:///c.wav")

    assert await checker.check_track(session=_SpySession(), track=track) == "api_error"  # type: ignore[arg-type]
    assert route.call_count == 1


@respx.mock
async def test_lookup_malformed_json_is_api_error() -> None:
    """JSON デコード失敗は api_error として吸収する。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(
            200, content=b"not-json", headers={"content-type": "text/plain"}
        )
    )
    checker = _build_checker()
    track = _StubTrack(position=3, audio_uri="file:///d.wav")

    assert await checker.check_track(session=_SpySession(), track=track) == "api_error"  # type: ignore[arg-type]


@respx.mock
async def test_hit_sets_fingerprint_hash_and_response() -> None:
    """hit 時に fingerprint_hash と生レスポンスを記録する。"""
    body = {"status": "ok", "results": [{"score": ACOUSTID_MATCH_THRESHOLD + 0.01}]}
    respx.post(ACOUSTID_LOOKUP_URL).mock(return_value=httpx.Response(200, json=body))
    checker = _build_checker()
    track = _StubTrack(position=4, audio_uri="file:///e.wav")

    assert await checker.check_track(session=_SpySession(), track=track) == "hit"  # type: ignore[arg-type]
    assert track.fingerprint_hash == _fingerprint_hash("FP_UNIT")
    assert track.acoustid_response == body


# --- client 所有権 ---------------------------------------------------------------


async def test_aclose_does_not_close_injected_client() -> None:
    """外部注入 client は checker.aclose() で閉じない (所有権は呼び出し側)。"""
    injected = httpx.AsyncClient()
    checker = _build_checker(client=injected)

    await checker.aclose()

    assert injected.is_closed is False
    await injected.aclose()


# --- default_fingerprinter は fpcalc を subprocess で呼ぶ (バイナリは起動しない) ----


def test_default_fingerprinter_invokes_fpcalc(monkeypatch: pytest.MonkeyPatch) -> None:
    """default_fingerprinter は fpcalc を subprocess 経由で呼び JSON を解釈する。

    実バイナリは起動せず ``subprocess.run`` を差し替えて検証する (環境非依存)。
    注入経路がデフォルトと分離していること、 fpcalc の JSON を ``(duration, fp)`` に
    マップすることを保証する。
    """
    import subprocess

    captured: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout='{"duration": 123.0, "fingerprint": "AQAB_real"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    duration, fingerprint = default_fingerprinter(b"\x00wav\x00", 120)

    assert (duration, fingerprint) == (123, "AQAB_real")
    assert captured["cmd"][0] == "fpcalc"
