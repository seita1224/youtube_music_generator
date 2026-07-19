"""SSE ジョブ進捗ルータの別名エクスポート (US6)。

実装は :mod:`ymg_backend.api.jobs` にある (``GET /jobs/stream``)。 TDD 先行テスト
(``tests/unit/test_event_bus.py``) と内部契約 (a)/(b) は ``api.jobs`` を import 名として
固定しているため、 実体は ``jobs.py`` に置く。 本モジュールはタスク指定の ``api/sse.py``
パスからも同一 ``router`` を参照できるようにする薄い再エクスポート層 (新規発明なし・配線は
``jobs.router`` を後段が include)。
"""

from __future__ import annotations

from ymg_backend.api.jobs import STEPS, router, stream_jobs

__all__ = ["STEPS", "router", "stream_jobs"]
