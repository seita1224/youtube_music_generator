"""FR-100: YouTube OAuth が要求するスコープ一式の検証。

FR-100 は ``youtube.upload`` + ``youtube`` + ``yt-analytics.readonly`` の 3 スコープでの
動作を要求する(FR-101 の Analytics 取得 / FR-102 の privacy 変更に必須)。
スコープ定数の内容と、 認可フローが実際にその 3 スコープを要求することを検証する。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from ymg_backend.infrastructure.youtube import oauth
from ymg_backend.infrastructure.youtube.oauth import (
    YOUTUBE_ANALYTICS_SCOPE,
    YOUTUBE_MANAGE_SCOPE,
    YOUTUBE_SCOPES,
    YOUTUBE_UPLOAD_SCOPE,
    run_oauth_flow,
)

_EXPECTED_SCOPES = {
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
}


@pytest.mark.fr("FR-100")
def test_youtube_scopes_include_upload_manage_and_analytics() -> None:
    """FR-100: スコープ定数が upload + 管理 + analytics 読取の 3 つを含む。"""
    assert YOUTUBE_UPLOAD_SCOPE in YOUTUBE_SCOPES
    assert YOUTUBE_MANAGE_SCOPE in YOUTUBE_SCOPES
    assert YOUTUBE_ANALYTICS_SCOPE in YOUTUBE_SCOPES
    assert set(YOUTUBE_SCOPES) == _EXPECTED_SCOPES


class _FakeSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class _FakeCredentials:
    """run_local_server が返す Credentials 風オブジェクト(refresh token あり)。"""

    token = "access-token"
    refresh_token = "refresh-token"
    scopes = YOUTUBE_SCOPES  # tuple(不変)。 oauth 側は list(...) で取り込む。
    expiry = None


@pytest.mark.fr("FR-100")
async def test_run_oauth_flow_requests_all_required_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-100: 認可フローが InstalledAppFlow へ 3 スコープすべてを渡す。

    upload 単独だと FR-101(Analytics)/ FR-102(privacy 変更)が権限不足になるため、
    認可時点で 3 スコープを要求していることが本要件の核心。
    """
    recorded: dict[str, Any] = {}

    class _FakeFlow:
        @classmethod
        def from_client_config(cls, _config: Any, scopes: Any) -> _FakeFlow:
            recorded["scopes"] = list(scopes)
            return cls()

        def run_local_server(self, **_kwargs: Any) -> _FakeCredentials:
            return _FakeCredentials()

    # 遅延 import される google_auth_oauthlib.flow を fake で差し替える。
    parent = types.ModuleType("google_auth_oauthlib")
    flow_mod = types.ModuleType("google_auth_oauthlib.flow")
    flow_mod.InstalledAppFlow = _FakeFlow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", parent)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_mod)

    # DB 書込(_upsert_credential)はスコープ要求とは無関係なので no-op に差し替える。
    async def _fake_upsert(**_kwargs: Any) -> str:
        return "RECORD"

    monkeypatch.setattr(oauth, "_upsert_credential", _fake_upsert)

    settings = types.SimpleNamespace(
        youtube_client_id="client-id",
        youtube_client_secret=_FakeSecret("client-secret"),
        youtube_redirect_uri="http://localhost",
        youtube_channel_id=None,
    )

    result = await run_oauth_flow(
        session=None,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        cipher=None,  # type: ignore[arg-type]
        port=0,
        open_browser=False,
    )

    assert result == "RECORD"
    assert recorded["scopes"] == list(YOUTUBE_SCOPES)
    assert set(recorded["scopes"]) == _EXPECTED_SCOPES
