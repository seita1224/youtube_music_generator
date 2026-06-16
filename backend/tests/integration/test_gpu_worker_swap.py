"""US7 (T126): GPU worker は ``GPU_WORKER_BASE_URL`` 切替だけで差し替えられる。

ADR-0031 「backend → GPU worker は HTTP のみ」が成立するなら、 worker の差し替えは
**接続先 URL を変えるだけ**で済み、 backend のコード変更は一切要らないはずである。 本テストは
その契約を実証する: ``GpuWorkerClient`` は構築時に渡された ``base_url`` 以外の worker 実装に
依存せず、 リクエストは必ずその URL へ出る。

実証方法 (respx で 2 つの異なる base URL の mock worker を立てる):

1. ``Settings.gpu_worker_base_url`` を URL-A にして ``GpuWorkerClient(settings.gpu_worker_base_url)``
   を構築 → リクエストが **URL-A の mock** に出て、 URL-B の mock は呼ばれないことを確認。
2. 同じ構築コードのまま ``Settings.gpu_worker_base_url`` を URL-B に差し替える → 今度は
   **URL-B の mock** に出ることを確認。 ``GpuWorkerClient(settings.gpu_worker_base_url)`` という
   構築式 (main.py / pipeline と同一) は不変であり、 切替に backend コード変更が不要なことを示す。

env (``GPU_WORKER_BASE_URL``) → ``Settings.gpu_worker_base_url`` への写像は
``core.config.Settings`` のフィールド定義 (config.py:79) が担う。 本テストは ``Settings(...)`` に
URL を直接渡し、 env 経由でも同値であることを別途確認する。

実依存・docker 起動はしない。 HTTP は respx で mock する。 ``tests/integration/conftest.py`` の
専用テスト DB セッション fixture (autouse) が適用されるが、 本テスト自体は DB を使わない。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ymg_backend.core.config import Settings
from ymg_backend.infrastructure.gpu_worker_client import GpuWorkerClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# 2 つの「別物の」mock GPU worker。 host を変えて respx が URL でルーティングを分離できる
# ようにする (port 違いでも良いが host 違いの方が取り違えが起きない)。
_BASE_URL_A = "http://gpu-worker-a.test"
_BASE_URL_B = "http://gpu-worker-b.test"

# 各 worker が返す health。 status は両者とも "ok" だが worker_version で出所を識別する。
_VERSION_A = "worker-a-1.0.0"
_VERSION_B = "worker-b-2.0.0"


def _settings(base_url: str) -> Settings:
    """``gpu_worker_base_url`` だけを差し替えた ``Settings`` を返す。

    他フィールドは既定値。 production では env ``GPU_WORKER_BASE_URL`` がこの値を供給する
    (config.py:79)。 ここでは切替対象を 1 点に絞るため kwargs で直接指定する。
    """
    return Settings(gpu_worker_base_url=base_url)


def _register_health(router: respx.MockRouter, base_url: str, worker_version: str) -> respx.Route:
    """``base_url`` の ``GET /health`` を ``worker_version`` 入りの 200 で応答させる。"""
    return router.get(f"{base_url}/health").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "gpu_available": True,
                "vram_free_mb": 12000,
                "worker_version": worker_version,
            },
        )
    )


async def test_client_built_from_settings_hits_configured_base_url() -> None:
    """``GpuWorkerClient(settings.gpu_worker_base_url)`` は設定された URL にのみ出る。

    URL-A を設定した client が URL-A の mock を呼び、 URL-B の mock は **一切呼ばれない**
    ことを確認する。 worker_version=A が返ることで、 確かに A の worker に届いていると分かる。
    """
    with respx.mock(assert_all_called=False) as router:
        route_a = _register_health(router, _BASE_URL_A, _VERSION_A)
        route_b = _register_health(router, _BASE_URL_B, _VERSION_B)

        settings = _settings(_BASE_URL_A)
        # main.py / pipeline と同一の構築式。 swap で変わるのは settings の値だけ。
        async with GpuWorkerClient(settings.gpu_worker_base_url) as client:
            health = await client.health()

    assert health.worker_version == _VERSION_A
    assert route_a.called
    assert route_a.call_count == 1
    # 別 worker (URL-B) には 1 度も到達していない = 取り違えゼロ。
    assert not route_b.called
    # 実際に投げられたリクエストの host が URL-A 側であることを直接確認する。
    sent = route_a.calls.last.request
    assert str(sent.url) == f"{_BASE_URL_A}/health"


async def test_swapping_settings_base_url_redirects_without_code_change() -> None:
    """``Settings.gpu_worker_base_url`` を A→B に変えるだけで宛先が B に切替わる。

    backend 側の構築コード ``GpuWorkerClient(settings.gpu_worker_base_url)`` は 2 回とも同一。
    変えるのは ``Settings`` が運ぶ URL のみ。 これが env ``GPU_WORKER_BASE_URL`` 切替だけで
    worker を差し替えられる (= backend コード変更不要) という US7 swap 契約の核心。
    """
    with respx.mock(assert_all_called=False) as router:
        route_a = _register_health(router, _BASE_URL_A, _VERSION_A)
        route_b = _register_health(router, _BASE_URL_B, _VERSION_B)

        # --- 1 回目: URL-A を指す Settings ---
        settings_a = _settings(_BASE_URL_A)
        async with GpuWorkerClient(settings_a.gpu_worker_base_url) as client_a:
            health_a = await client_a.health()

        # --- 2 回目: 構築コードはそのまま、 Settings の URL だけ B へ差し替え ---
        settings_b = _settings(_BASE_URL_B)
        async with GpuWorkerClient(settings_b.gpu_worker_base_url) as client_b:
            health_b = await client_b.health()

    # それぞれが自分の worker に到達している。
    assert health_a.worker_version == _VERSION_A
    assert health_b.worker_version == _VERSION_B
    assert route_a.call_count == 1
    assert route_b.call_count == 1
    assert str(route_a.calls.last.request.url) == f"{_BASE_URL_A}/health"
    assert str(route_b.calls.last.request.url) == f"{_BASE_URL_B}/health"


async def test_base_url_flows_from_env_via_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env ``GPU_WORKER_BASE_URL`` → ``Settings`` → client の宛先まで一貫することを確認する。

    切替を運用で行うのは env なので、 env 経由でも宛先が URL-B になることを実証する
    (kwargs 直指定と env 指定が等価で、 config.py:79 の写像が効いていることの裏取り)。
    ``Settings`` は ``.env`` も読むため、 ``_env_file=None`` でファイルを無効化し env のみを源にする。
    """
    monkeypatch.setenv("GPU_WORKER_BASE_URL", _BASE_URL_B)
    settings = Settings(_env_file=None)
    assert settings.gpu_worker_base_url == _BASE_URL_B

    with respx.mock(assert_all_called=False) as router:
        route_a = _register_health(router, _BASE_URL_A, _VERSION_A)
        route_b = _register_health(router, _BASE_URL_B, _VERSION_B)

        async with GpuWorkerClient(settings.gpu_worker_base_url) as client:
            health = await client.health()

    assert health.worker_version == _VERSION_B
    assert route_b.called
    assert not route_a.called
