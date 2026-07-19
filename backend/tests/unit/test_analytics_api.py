"""``/analytics`` 業務ルータの単体テスト (api/analytics.py, US3 T110)。

本ルータは集計 (T103 ``build_analytics_summary``) と推奨 (T106 ``recommend_genres``) の
2 ドメイン結果を contract スキーマへ合成する層。 並行実装の都合でこれらドメインモジュールが
未配置でも本テストが単独で走るよう、 ``sys.modules`` に契約シグネチャ準拠の軽量 stub を
登録してから router を import する (実 DB / 実 YouTube / 実 LLM / HTTP は一切起動しない)。

検証観点:

- 認証: 無認証は 401。
- ``GET /analytics``: ``window_days`` を集計/推奨へ渡す + 両者を contract 形へ合成 (200)。
- ``avg_retention_pct`` / ``retention_ratio_to_primary`` の ``None`` を保持。
- ``window_days`` の境界外 (0 / 181) は 422 (Query の ge/le)。
- 読み取り専用なので commit しない。
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any, Final

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

_USERNAME: Final = "admin"
_PASSWORD: Final = "s3cret"
_AUTH: Final = (_USERNAME, _PASSWORD)


# ---------------------------------------------------------------------------
# 契約シグネチャ準拠の stub ドメイン (T103 / T106)。 router import より前に登録する。
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _GenreSummary:
    genre: str
    role: str
    video_count: int
    avg_retention_pct: float | None
    total_views: int


@dataclass(frozen=True, slots=True)
class _AnalyticsSummaryData:
    window_days: int
    sample_size: int
    by_genre: tuple[_GenreSummary, ...]
    top_videos: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _GenreRecommendation:
    genre: str
    action: str
    retention_ratio_to_primary: float | None
    sample_size: int
    days_elapsed: int
    rationale: str


# build_analytics_summary / recommend_genres が受けた window_days を観測する記録器。
_calls: dict[str, list[int]] = {"summary": [], "recommend": []}
_summary_result: list[_AnalyticsSummaryData] = []
_recommend_result: list[list[_GenreRecommendation]] = []


async def _fake_build_analytics_summary(
    _session: Any, *, window_days: int = 14
) -> _AnalyticsSummaryData:
    _calls["summary"].append(window_days)
    return _summary_result[0]


async def _fake_recommend_genres(
    _session: Any, *, window_days: int = 14
) -> list[_GenreRecommendation]:
    _calls["recommend"].append(window_days)
    return _recommend_result[0]


def _install_domain_stubs() -> None:
    """``ymg_backend.domain.analytics.summary`` / ``...genres.recommend`` を stub 登録する。

    並行実装 (T103/T106) が *未配置* の場合に限り、 router の top-level import を満たすため
    契約の公開関数だけを持つダミーモジュールを ``sys.modules`` に差し込む。 実体が既に
    存在する場合は stub を入れない (入れると ``AnalyticsSummaryData`` 等を持たない dummy が
    ``sys.modules`` に残り、 同一 import を行う他テストの収集を壊すため)。 いずれの場合も
    本テストは公開関数を router 上で monkeypatch するため、 実体の有無に依存しない。
    """
    import importlib.util

    if importlib.util.find_spec("ymg_backend.domain.analytics.summary") is None:
        summary_mod = types.ModuleType("ymg_backend.domain.analytics.summary")
        summary_mod.build_analytics_summary = _fake_build_analytics_summary  # type: ignore[attr-defined]
        sys.modules.setdefault("ymg_backend.domain.analytics.summary", summary_mod)

    if importlib.util.find_spec("ymg_backend.domain.genres.recommend") is None:
        recommend_mod = types.ModuleType("ymg_backend.domain.genres.recommend")
        recommend_mod.recommend_genres = _fake_recommend_genres  # type: ignore[attr-defined]
        sys.modules.setdefault("ymg_backend.domain.genres.recommend", recommend_mod)


_install_domain_stubs()

# stub 登録後に import (top-level import が stub を解決する)。
from ymg_backend.api import analytics as analytics_module  # noqa: E402
from ymg_backend.api.analytics import router  # noqa: E402
from ymg_backend.core.config import Settings, get_settings  # noqa: E402
from ymg_backend.core.security import require_basic_auth  # noqa: E402
from ymg_backend.infrastructure.db.session import get_session  # noqa: E402


class _FakeSession:
    """commit 検知のみ行う最小 stub (集計/推奨は monkeypatch 済みで SQL は走らない)。"""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    _calls["summary"].clear()
    _calls["recommend"].clear()
    _summary_result.clear()
    _recommend_result.clear()


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def client(session: _FakeSession, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # router が import した名前を fake に差し替える (実体が後で landしても本テストは独立)。
    monkeypatch.setattr(analytics_module, "build_analytics_summary", _fake_build_analytics_summary)
    monkeypatch.setattr(analytics_module, "recommend_genres", _fake_recommend_genres)

    app = FastAPI()
    protected = APIRouter(dependencies=[Depends(require_basic_auth)])
    protected.include_router(router)
    app.include_router(protected)

    app.dependency_overrides[get_settings] = lambda: Settings(
        admin_username=_USERNAME,
        admin_password=SecretStr(_PASSWORD),
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


# --- auth ------------------------------------------------------------------
def test_get_analytics_requires_auth(client: TestClient) -> None:
    resp = client.get("/analytics")
    assert resp.status_code == 401


# --- GET /analytics --------------------------------------------------------
def test_get_analytics_composes_summary_and_recommendations(client: TestClient) -> None:
    _summary_result.append(
        _AnalyticsSummaryData(
            window_days=14,
            sample_size=12,
            by_genre=(
                _GenreSummary(
                    genre="lo-fi hip-hop",
                    role="primary",
                    video_count=8,
                    avg_retention_pct=42.5,
                    total_views=10_000,
                ),
                _GenreSummary(
                    genre="ambient",
                    role="experimental",
                    video_count=4,
                    avg_retention_pct=None,
                    total_views=0,
                ),
            ),
            top_videos=(),
        )
    )
    _recommend_result.append(
        [
            _GenreRecommendation(
                genre="ambient",
                action="keep",
                retention_ratio_to_primary=None,
                sample_size=4,
                days_elapsed=10,
                rationale="経過 10 日 (14 日未満) のため継続観察。",
            )
        ]
    )

    resp = client.get("/analytics", auth=_AUTH)
    assert resp.status_code == 200
    body = resp.json()

    assert body["window_days"] == 14
    assert body["sample_size"] == 12

    by_genre = body["by_genre"]
    assert len(by_genre) == 2
    assert by_genre[0] == {
        "genre": "lo-fi hip-hop",
        "role": "primary",
        "video_count": 8,
        "avg_retention_pct": 42.5,
        "total_views": 10_000,
    }
    # None retention を保持する。
    assert by_genre[1]["avg_retention_pct"] is None

    recs = body["recommendations"]
    assert len(recs) == 1
    assert recs[0]["genre"] == "ambient"
    assert recs[0]["action"] == "keep"
    assert recs[0]["retention_ratio_to_primary"] is None
    assert recs[0]["sample_size"] == 4
    assert recs[0]["days_elapsed"] == 10


def test_get_analytics_passes_window_days_to_both_domains(client: TestClient) -> None:
    _summary_result.append(
        _AnalyticsSummaryData(window_days=30, sample_size=0, by_genre=(), top_videos=())
    )
    _recommend_result.append([])

    resp = client.get("/analytics", params={"window_days": 30}, auth=_AUTH)
    assert resp.status_code == 200
    # 同一 window_days を集計/推奨の双方へ渡し、 集計窓を揃える。
    assert _calls["summary"] == [30]
    assert _calls["recommend"] == [30]
    body = resp.json()
    assert body["by_genre"] == []
    assert body["recommendations"] == []


def test_get_analytics_defaults_window_days_to_14(client: TestClient) -> None:
    _summary_result.append(
        _AnalyticsSummaryData(window_days=14, sample_size=0, by_genre=(), top_videos=())
    )
    _recommend_result.append([])

    resp = client.get("/analytics", auth=_AUTH)
    assert resp.status_code == 200
    assert _calls["summary"] == [14]
    assert _calls["recommend"] == [14]


def test_get_analytics_does_not_commit(client: TestClient, session: _FakeSession) -> None:
    _summary_result.append(
        _AnalyticsSummaryData(window_days=14, sample_size=0, by_genre=(), top_videos=())
    )
    _recommend_result.append([])

    resp = client.get("/analytics", auth=_AUTH)
    assert resp.status_code == 200
    # 読み取り専用 GET なので commit しない。
    assert session.commits == 0


@pytest.mark.parametrize("window_days", [0, 181])
def test_get_analytics_rejects_out_of_range_window(client: TestClient, window_days: int) -> None:
    resp = client.get("/analytics", params={"window_days": window_days}, auth=_AUTH)
    assert resp.status_code == 422
