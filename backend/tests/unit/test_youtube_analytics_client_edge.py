"""Critical path テスト: YouTube アナリティクスクライアント追加 (T139)。

未到達行 (82% -> 100%) を覆う追加テスト群。
対象モジュール: ``ymg_backend.domain.analytics.client``

未到達行 (全 35 行) のカテゴリ:
- 114-115  : aclose() で _owns_http=True 経路
- 266-267  : _get_json の TimeoutException/TransportError
- 282-294  : _raise_for_status の 4xx (401/403) / 5xx 経路
- 304-305  : _parse_json の JSON デコード例外
- 311      : _parse_json で body が dict でない
- 363      : _oauth_channel_id の channel_id 未設定
- 391,394  : _row_video_id のタプル / その他型
- 401,408  : _column_names で headers が list でない / dict でない header
- 417,421  : _iter_row_dicts で columns 空 / rows が list でない / row が list でない
- 446      : _index_traffic_by_video で source が空
- 462,465-472 : _to_int の float/str/変換不能
- 480,482,486-488 : _to_decimal の bool/Decimal/変換不能
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from ymg_backend.domain.analytics.client import (
    ANALYTICS_REPORTS_URL,
    YOUTUBE_VIDEOS_URL,
    AnalyticsClient,
    _column_names,
    _index_traffic_by_video,
    _iter_row_dicts,
    _row_video_id,
    _to_decimal,
    _to_int,
)
from ymg_backend.domain.errors.errors import RecoverableError, TransientError

pytestmark = pytest.mark.critical

_FAKE_TOKEN = "fake-access-token-edge"
_CHANNEL_ID = "UC_test_channel_edge"


# ---------------------------------------------------------------------------
# Stub / SpySession (test_youtube_analytics_client.py と同形)
# ---------------------------------------------------------------------------


class _StubOAuth:
    class _Settings:
        youtube_channel_id = _CHANNEL_ID

    def __init__(self) -> None:
        self._settings = self._Settings()

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


class _StubOAuthNoChannel:
    """channel_id を返さない OAuth stub (_oauth_channel_id の RecoverableError 経路用)。"""

    class _Settings:
        youtube_channel_id = ""  # 空文字 = 未設定扱い

    def __init__(self) -> None:
        self._settings = self._Settings()

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


class _StubOAuthNoSettings:
    """_settings を持たない OAuth stub (getattr 経路)。"""

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class SpySession:
    def __init__(self, video_ids: list[str] | None = None) -> None:
        self.flush_count = 0
        self._video_ids: list[str] = video_ids or []

    async def execute(self, statement: Any) -> Any:
        kind = type(statement).__name__.lower()
        if "select" in kind:
            return _ScalarResult([(vid,) for vid in self._video_ids])
        return _ScalarResult([])

    async def flush(self) -> None:
        self.flush_count += 1


def _build_client_with_oauth(oauth: Any) -> AnalyticsClient:
    return AnalyticsClient(oauth)


# ---------------------------------------------------------------------------
# 114-115: aclose() で内部生成 httpx.AsyncClient をクローズする経路
# ---------------------------------------------------------------------------


async def test_aclose_closes_internal_http_client() -> None:
    """http= 省略 (内部生成) で aclose() を呼ぶと AsyncClient.aclose が実行される。"""
    oauth = _StubOAuth()
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]
    assert client._owns_http is True
    # aclose() は例外を出さずに完了すれば良い (内部 AsyncClient は close される)
    await client.aclose()


async def test_aclose_does_not_close_injected_http_client() -> None:
    """http= 外部注入時は aclose() が何もしない (AsyncClient は呼び出し側が管理)。"""
    oauth = _StubOAuth()
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = AnalyticsClient(oauth, http=mock_http)  # type: ignore[arg-type]
    assert client._owns_http is False
    await client.aclose()
    mock_http.aclose.assert_not_called()


# ---------------------------------------------------------------------------
# 266-267: _get_json で TimeoutException / TransportError -> TransientError
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_json_timeout_raises_transient_error() -> None:
    """Analytics API 通信タイムアウト -> TransientError に写像する。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        side_effect=httpx.ReadTimeout("timed out", request=MagicMock())
    )
    respx.get(url__startswith=YOUTUBE_VIDEOS_URL).mock(return_value=httpx.Response(200, json={}))

    oauth = _StubOAuth()
    session = SpySession(video_ids=["vid_TIMEOUT"])
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(TransientError) as exc_info:
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_TIMEOUT"],
        )
    assert "通信に失敗" in str(exc_info.value)


@respx.mock
async def test_get_json_transport_error_raises_transient_error() -> None:
    """Analytics API 接続失敗 (TransportError) -> TransientError に写像する。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    oauth = _StubOAuth()
    session = SpySession(video_ids=["vid_CONN"])
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(TransientError):
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_CONN"],
        )


# ---------------------------------------------------------------------------
# 282-288: _raise_for_status で 401/403 -> RecoverableError
# ---------------------------------------------------------------------------


@respx.mock
async def test_raise_for_status_401_raises_recoverable_error() -> None:
    """Analytics API が 401 -> RecoverableError (認証/権限エラー)。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "unauthorized"}})
    )

    oauth = _StubOAuth()
    session = SpySession()
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(RecoverableError) as exc_info:
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_401"],
        )
    assert (
        "認証" in str(exc_info.value)
        or "401" in str(exc_info.value)
        or "403" in str(exc_info.value)
    )


@respx.mock
async def test_raise_for_status_403_raises_recoverable_error() -> None:
    """Analytics API が 403 -> RecoverableError (権限不足)。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(403, json={"error": {"message": "forbidden"}})
    )

    oauth = _StubOAuth()
    session = SpySession()
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(RecoverableError):
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_403"],
        )


# ---------------------------------------------------------------------------
# 289-293: _raise_for_status で 5xx -> TransientError
# ---------------------------------------------------------------------------


@respx.mock
async def test_raise_for_status_500_raises_transient_error() -> None:
    """Analytics API が 500 -> TransientError (一過性)。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    oauth = _StubOAuth()
    session = SpySession()
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(TransientError) as exc_info:
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_500"],
        )
    assert "500" in str(exc_info.value)


@respx.mock
async def test_raise_for_status_503_raises_transient_error() -> None:
    """Analytics API が 503 -> TransientError。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )

    oauth = _StubOAuth()
    session = SpySession()
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(TransientError):
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_503"],
        )


# ---------------------------------------------------------------------------
# 294-296: _raise_for_status で 4xx (非認証) -> RecoverableError
# ---------------------------------------------------------------------------


@respx.mock
async def test_raise_for_status_404_raises_recoverable_error() -> None:
    """Analytics API が 404 -> RecoverableError (拒否)。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(404, json={"error": {"message": "not found"}})
    )

    oauth = _StubOAuth()
    session = SpySession()
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(RecoverableError) as exc_info:
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_404"],
        )
    assert "404" in str(exc_info.value) or "拒否" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 304-305: _parse_json で JSON デコード失敗 -> RecoverableError
# ---------------------------------------------------------------------------


@respx.mock
async def test_parse_json_decode_error_raises_recoverable_error() -> None:
    """Analytics API が不正な JSON を返す -> RecoverableError。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(200, content=b"not-json!!!")
    )

    oauth = _StubOAuth()
    session = SpySession()
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(RecoverableError) as exc_info:
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_BADJSON"],
        )
    assert "JSON" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 310-314: _parse_json で body が dict でない -> RecoverableError
# ---------------------------------------------------------------------------


@respx.mock
async def test_parse_json_non_dict_body_raises_recoverable_error() -> None:
    """Analytics API が JSON 配列を返す (dict でない) -> RecoverableError。"""
    respx.get(url__startswith=ANALYTICS_REPORTS_URL).mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )

    oauth = _StubOAuth()
    session = SpySession()
    client = AnalyticsClient(oauth)  # type: ignore[arg-type]

    with pytest.raises(RecoverableError) as exc_info:
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_NONDICT"],
        )
    assert "形式" in str(exc_info.value) or "reports.query" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 363: _oauth_channel_id で channel_id 未設定 -> RecoverableError
# ---------------------------------------------------------------------------


@respx.mock
async def test_oauth_channel_id_missing_raises_recoverable_error() -> None:
    """channel_id が空文字の OAuth stub -> RecoverableError。"""
    session = SpySession()
    client = _build_client_with_oauth(_StubOAuthNoChannel())

    with pytest.raises(RecoverableError) as exc_info:
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_NOCHAN"],
        )
    assert "channel id" in str(exc_info.value)


@respx.mock
async def test_oauth_no_settings_attr_raises_recoverable_error() -> None:
    """_settings 属性を持たない OAuth stub -> channel_id=None -> RecoverableError。"""
    session = SpySession()
    client = _build_client_with_oauth(_StubOAuthNoSettings())

    with pytest.raises(RecoverableError) as exc_info:
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            metric_date=date(2026, 6, 15),
            video_ids=["vid_NOSETTINGS"],
        )
    assert "channel id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 391, 394: _row_video_id のタプル / その他型
# ---------------------------------------------------------------------------


def test_row_video_id_tuple() -> None:
    """タプル形式 (SpySession が返す形) から video_id を取り出す。"""
    assert _row_video_id(("vid_A",)) == "vid_A"


def test_row_video_id_list() -> None:
    """リスト形式からも video_id を取り出す。"""
    assert _row_video_id(["vid_B", "extra"]) == "vid_B"


def test_row_video_id_other_type() -> None:
    """str でも tuple/list でもない場合は str() で変換する。"""
    result = _row_video_id(42)
    assert result == "42"


def test_row_video_id_str() -> None:
    """str 入力はそのまま返す。"""
    assert _row_video_id("vid_C") == "vid_C"


# ---------------------------------------------------------------------------
# 401, 408: _column_names の異常系
# ---------------------------------------------------------------------------


def test_column_names_headers_not_list() -> None:
    """columnHeaders が list でない場合は空リストを返す。"""
    assert _column_names({"columnHeaders": "not-a-list"}) == []
    assert _column_names({"columnHeaders": None}) == []
    assert _column_names({}) == []


def test_column_names_non_dict_header() -> None:
    """headerが dict でない要素 (str/int) は空文字として扱う。"""
    result = _column_names({"columnHeaders": ["string_header", 42, {"name": "ok"}]})
    assert result == ["", "", "ok"]


def test_column_names_dict_header_name_none() -> None:
    """dict header の name が None の場合は空文字。"""
    result = _column_names({"columnHeaders": [{"name": None}]})
    assert result == [""]


# ---------------------------------------------------------------------------
# 417, 421: _iter_row_dicts の異常系
# ---------------------------------------------------------------------------


def test_iter_row_dicts_no_columns() -> None:
    """columnHeaders が空 (columns=[]) の場合は空リストを返す。"""
    result = _iter_row_dicts({"columnHeaders": [], "rows": [[1, 2]]})
    assert result == []


def test_iter_row_dicts_rows_not_list() -> None:
    """rows が list でない場合は空リストを返す。"""
    result = _iter_row_dicts({"columnHeaders": [{"name": "video"}], "rows": "not-a-list"})
    assert result == []


def test_iter_row_dicts_row_not_list() -> None:
    """rows 内の要素が list でない行はスキップする。"""
    result = _iter_row_dicts(
        {
            "columnHeaders": [{"name": "video"}, {"name": "views"}],
            "rows": [
                "not-a-list-row",
                ["vid_A", 100],  # 有効な行
            ],
        }
    )
    assert len(result) == 1
    assert result[0]["video"] == "vid_A"


# ---------------------------------------------------------------------------
# 446: _index_traffic_by_video で source が空/非str
# ---------------------------------------------------------------------------


def test_index_traffic_by_video_empty_source_skipped() -> None:
    """insightTrafficSourceType が空文字/None の行はスキップされる。
    video 列がないレポートは "" キーへ集約される (単一動画前提の設計)。
    """
    report: dict[str, Any] = {
        "columnHeaders": [
            {"name": "insightTrafficSourceType"},
            {"name": "views"},
        ],
        "rows": [
            ["", 100],  # 空文字 source -> スキップ
            [None, 50],  # None source -> スキップ
            ["YT_SEARCH", 200],  # 有効 (video 列なしなので "" キーへ)
        ],
    }
    result = _index_traffic_by_video(report)
    # 空文字/None の source 行はスキップされる (キーとして登録されない)
    flat_sources = {k for bucket in result.values() for k in bucket}
    assert "" not in flat_sources
    # 有効な行は "" キー (video 列なし = 単一動画前提) に入る
    assert result.get("", {}).get("YT_SEARCH") == 200


# ---------------------------------------------------------------------------
# 462, 465-472: _to_int の各型分岐
# ---------------------------------------------------------------------------


def test_to_int_none() -> None:
    assert _to_int(None) is None


def test_to_int_bool() -> None:
    """bool は None を返す (int のサブクラスだが別扱い)。"""
    assert _to_int(True) is None
    assert _to_int(False) is None


def test_to_int_int() -> None:
    assert _to_int(123) == 123
    assert _to_int(0) == 0


def test_to_int_float() -> None:
    """float は int に丸める。"""
    assert _to_int(3.7) == 3
    assert _to_int(0.0) == 0


def test_to_int_str_valid() -> None:
    """数値文字列は int に変換する。"""
    assert _to_int("42") == 42
    assert _to_int("3.14") == 3


def test_to_int_str_invalid() -> None:
    """変換できない文字列は None を返す。"""
    assert _to_int("not-a-number") is None
    assert _to_int("") is None


def test_to_int_other_type() -> None:
    """その他の型 (list, dict) は None を返す。"""
    assert _to_int([1, 2]) is None
    assert _to_int({"a": 1}) is None


# ---------------------------------------------------------------------------
# 480, 482, 486-488: _to_decimal の各型分岐
# ---------------------------------------------------------------------------


def test_to_decimal_none() -> None:
    assert _to_decimal(None) is None


def test_to_decimal_bool() -> None:
    """bool は None を返す。"""
    assert _to_decimal(True) is None
    assert _to_decimal(False) is None


def test_to_decimal_decimal() -> None:
    """Decimal 入力はそのまま返す。"""
    d = Decimal("3.14")
    assert _to_decimal(d) == d


def test_to_decimal_int() -> None:
    assert _to_decimal(42) == Decimal("42")


def test_to_decimal_float() -> None:
    assert _to_decimal(3.14) == Decimal("3.14")


def test_to_decimal_str_valid() -> None:
    assert _to_decimal("2.718") == Decimal("2.718")


def test_to_decimal_str_invalid() -> None:
    """変換できない文字列は None を返す。"""
    assert _to_decimal("not-a-decimal") is None
    assert _to_decimal("") is None


def test_to_decimal_other_type() -> None:
    """その他の型 (list) は None を返す。"""
    assert _to_decimal([1, 2]) is None
