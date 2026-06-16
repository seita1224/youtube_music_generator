"""US7 (T126) unit: ``GpuWorkerClient`` は構築時の base URL にだけ依存する。

integration 版 (test_gpu_worker_swap.py) が ``Settings`` 経由の swap 全体を見るのに対し、
本 unit はその土台 — client が **渡された base URL の worker にだけ** リクエストを出す — を
DB / Settings 抜きで素早く確認する。 これが成り立つから、 上位は env を変えるだけで worker を
差し替えられる。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ymg_backend.infrastructure.gpu_worker_client import GpuWorkerClient

pytestmark = pytest.mark.asyncio

_BASE_URL_A = "http://gpu-a.test"
_BASE_URL_B = "http://gpu-b.test"


def _ok_health(worker_version: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "ok",
            "gpu_available": True,
            "vram_free_mb": 8000,
            "worker_version": worker_version,
        },
    )


@pytest.mark.parametrize(
    ("base_url", "worker_version"),
    [(_BASE_URL_A, "a"), (_BASE_URL_B, "b")],
)
async def test_client_requests_only_its_own_base_url(base_url: str, worker_version: str) -> None:
    """同一コードで base URL だけ変えると、 リクエストはその URL にのみ出る。"""
    other = _BASE_URL_B if base_url == _BASE_URL_A else _BASE_URL_A
    with respx.mock(assert_all_called=False) as router:
        own = router.get(f"{base_url}/health").mock(return_value=_ok_health(worker_version))
        foreign = router.get(f"{other}/health").mock(return_value=_ok_health("other"))

        async with GpuWorkerClient(base_url) as client:
            health = await client.health()

    assert health.worker_version == worker_version
    assert own.call_count == 1
    assert not foreign.called
    assert str(own.calls.last.request.url) == f"{base_url}/health"
