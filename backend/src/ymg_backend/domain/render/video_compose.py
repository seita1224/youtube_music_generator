"""動画合成 (ffmpeg) の契約配置 ``domain/render/video_compose`` (US1 内部契約)。

共有契約は動画合成を ``domain/render/video_compose.py`` に置くと定めている
(``compose_video`` / ``VideoArtifact``)。 実装は ``domain/pipeline/video_compose.py`` に
ffmpeg ラッパとして既にあるため、 本モジュールはその公開シンボルを **契約上の配置**で
再エクスポートする薄い別名層である (実装の二重化を避ける)。

統合テスト (``tests/integration/test_daily_cycle_pipeline.py``) は
``ymg_backend.domain.render.video_compose`` を import し ``VideoArtifact`` を参照する。
本モジュールがその import を解決させる。 実際の動画合成 (subprocess / ffmpeg) は
``domain/pipeline/video_compose`` の実装が行う。
"""

from __future__ import annotations

from ymg_backend.domain.pipeline.video_compose import (
    VideoArtifact,
    build_ffmpeg_command,
    compose_video,
)

__all__ = [
    "VideoArtifact",
    "build_ffmpeg_command",
    "compose_video",
]
