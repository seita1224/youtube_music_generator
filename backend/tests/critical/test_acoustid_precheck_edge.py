"""Critical path テスト: AcoustID 指紋プレチェック 追加 (T139)。

未到達行 (86% -> 100%) を覆う追加テスト群。
対象モジュール: ``ymg_backend.domain.compliance.acoustid``

未到達行:
- 157-158: aclose() で _owns_client=True 経路
- 314-315: _lookup の TimeoutException/TransportError (リトライループ内)
- 326-329: _lookup の 4xx (リトライしない経路)
- 361-362: _parse_lookup の JSON デコード失敗
- 372    : _max_score で results が list でない
- 398-412: default_fingerprinter 本体 (fpcalc subprocess)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from pydantic import SecretStr

from ymg_backend.domain.compliance.acoustid import (
    ACOUSTID_LOOKUP_URL,
    AcoustidChecker,
    _max_score,
    _parse_lookup,
    default_fingerprinter,
)

pytestmark = pytest.mark.critical

_API_KEY = SecretStr("test-acoustid-key-edge")


# ---------------------------------------------------------------------------
# Stubs (test_acoustid_precheck.py と同形)
# ---------------------------------------------------------------------------


class SpySession:
    def __init__(self) -> None:
        self.flush_count = 0

    async def execute(self, statement: Any) -> None:
        compiled = statement.compile()
        _ = compiled  # side-effect 記録は本テストでは不要

    async def flush(self) -> None:
        self.flush_count += 1


@dataclass
class SpyNotifier:
    sent: list[Any] = field(default_factory=list)

    async def notify(self, *, level: Any, message: str, context: Any | None = None) -> None:
        self.sent.append({"level": level, "message": message, "context": context})


class SpyStorage:
    def read_bytes(self, uri: str) -> bytes:
        return b"\x00\x00fake-audio\x00\x00"

    def resolve_uri(self, path: str) -> str:
        return path


@dataclass
class StubTrack:
    position: int
    audio_uri: str
    duration_sec: int = 300
    acoustid_status: str = "not_checked"
    acoustid_response: dict[str, Any] | None = None
    fingerprint_hash: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def _stub_fingerprinter() -> Any:
    def _fp(audio: bytes, duration_sec: int) -> tuple[int, str]:
        return duration_sec, "AQABZ_stub_fingerprint_edge"

    return _fp


def _build_checker(*, notifier: SpyNotifier | None = None) -> AcoustidChecker:
    return AcoustidChecker(
        api_key=_API_KEY,
        storage=SpyStorage(),  # type: ignore[arg-type]
        notifier=notifier or SpyNotifier(),
        fingerprinter=_stub_fingerprinter(),
    )


# ---------------------------------------------------------------------------
# 157-158: aclose() で _owns_client=True 経路
# ---------------------------------------------------------------------------


async def test_aclose_closes_internal_client() -> None:
    """client= 省略 (内部生成) で aclose() を呼ぶと AsyncClient.aclose が実行される。"""
    checker = AcoustidChecker(
        api_key=_API_KEY,
        storage=SpyStorage(),  # type: ignore[arg-type]
        notifier=SpyNotifier(),
        fingerprinter=_stub_fingerprinter(),
    )
    assert checker._owns_client is True
    await checker.aclose()  # 例外なく完了すれば OK


async def test_aclose_does_not_close_injected_client() -> None:
    """client= 外部注入時は aclose() が何もしない。"""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    checker = AcoustidChecker(
        api_key=_API_KEY,
        storage=SpyStorage(),  # type: ignore[arg-type]
        notifier=SpyNotifier(),
        fingerprinter=_stub_fingerprinter(),
        client=mock_client,
    )
    assert checker._owns_client is False
    await checker.aclose()
    mock_client.aclose.assert_not_called()


# ---------------------------------------------------------------------------
# 314-315: _lookup の TimeoutException/TransportError (リトライ後も失敗)
# ---------------------------------------------------------------------------


@respx.mock
async def test_check_track_connection_error_results_in_api_error() -> None:
    """全試行が接続失敗 (TransportError) -> api_error。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    session = SpySession()
    checker = _build_checker()
    track = StubTrack(position=0, audio_uri="file:///t0.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "api_error"
    assert track.acoustid_status == "api_error"


@respx.mock
async def test_check_track_timeout_results_in_api_error() -> None:
    """全試行がタイムアウト (TimeoutException) -> api_error。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        side_effect=httpx.ReadTimeout("timed out", request=MagicMock())
    )
    session = SpySession()
    checker = _build_checker()
    track = StubTrack(position=1, audio_uri="file:///t1.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "api_error"
    assert track.acoustid_status == "api_error"


# ---------------------------------------------------------------------------
# 326-329: _lookup の 4xx (リトライしない)
# ---------------------------------------------------------------------------


@respx.mock
async def test_check_track_4xx_results_in_api_error() -> None:
    """lookup が 400 -> リトライせず即 api_error (None を返す)。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(
        return_value=httpx.Response(400, json={"status": "error", "error": {"message": "bad"}})
    )
    session = SpySession()
    checker = _build_checker()
    track = StubTrack(position=2, audio_uri="file:///t2.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "api_error"
    assert track.acoustid_status == "api_error"


@respx.mock
async def test_check_track_401_results_in_api_error() -> None:
    """lookup が 401 -> api_error (4xx はリトライなし)。"""
    respx.post(ACOUSTID_LOOKUP_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))
    session = SpySession()
    checker = _build_checker()
    track = StubTrack(position=3, audio_uri="file:///t3.wav")

    verdict = await checker.check_track(session=session, track=track)  # type: ignore[arg-type]

    assert verdict == "api_error"


# ---------------------------------------------------------------------------
# 361-362: _parse_lookup の JSON デコード失敗
# ---------------------------------------------------------------------------


def test_parse_lookup_invalid_json_returns_none() -> None:
    """レスポンス body が JSON として解析不能なら None を返す。"""
    response = httpx.Response(200, content=b"not-valid-json!!!")
    result = _parse_lookup(response)
    assert result is None


def test_parse_lookup_status_not_ok_returns_none() -> None:
    """status != 'ok' なら None を返す。"""
    response = httpx.Response(200, json={"status": "error", "error": {"code": 3}})
    result = _parse_lookup(response)
    assert result is None


def test_parse_lookup_not_dict_returns_none() -> None:
    """body が dict でない (例: リスト) なら None を返す。"""
    response = httpx.Response(200, json=["not", "a", "dict"])
    result = _parse_lookup(response)
    assert result is None


def test_parse_lookup_ok_returns_body() -> None:
    """status=ok の正常レスポンスは body dict を返す。"""
    body = {"status": "ok", "results": []}
    response = httpx.Response(200, json=body)
    result = _parse_lookup(response)
    assert result == body


# ---------------------------------------------------------------------------
# 372: _max_score で results が list でない
# ---------------------------------------------------------------------------


def test_max_score_results_not_list_returns_none() -> None:
    """results が list でない (None/str/dict) -> None を返す。"""
    assert _max_score({"results": None}) is None
    assert _max_score({"results": "not-a-list"}) is None
    assert _max_score({"results": {"id": "x"}}) is None
    assert _max_score({}) is None


def test_max_score_empty_results_returns_none() -> None:
    """results が空リスト -> None を返す。"""
    assert _max_score({"results": []}) is None


def test_max_score_scores_without_score_key() -> None:
    """score キーを持たない result はスキップされる。"""
    results = [{"id": "x"}, {"id": "y", "score": 0.9}]
    assert _max_score({"results": results}) == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# 398-412: default_fingerprinter (subprocess fpcalc)
# ---------------------------------------------------------------------------


def test_default_fingerprinter_calls_fpcalc() -> None:
    """default_fingerprinter は fpcalc を subprocess 実行し (duration, fingerprint) を返す。

    実際の fpcalc バイナリは存在しないため subprocess.run をモックして
    JSON 出力を制御する。
    """
    fake_output = '{"duration": 300, "fingerprint": "AQABZ_test_fp"}'

    fake_proc = MagicMock()
    fake_proc.stdout = fake_output

    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        duration, fingerprint = default_fingerprinter(b"fake-audio-bytes", 300)

    mock_run.assert_called_once()
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert "fpcalc" in cmd[0]
    assert "-json" in cmd
    assert str(300) in cmd

    assert duration == 300
    assert fingerprint == "AQABZ_test_fp"
