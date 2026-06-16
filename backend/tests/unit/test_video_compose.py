"""video_compose (domain/pipeline/video_compose.py) の単体テスト。

外部依存 (ffmpeg バイナリ / StorageAdapter の実 I/O) は mock し、実プロセスは起動しない。
方針:

- 純関数 ``build_ffmpeg_command`` はコマンド (argv) を直接検証する (副作用なし)。
- ``compose_video`` は ``subprocess.run`` / ``shutil.which`` / ``StorageAdapter`` を stub し、
  ステージング → コマンド構築 → 書き戻しの結線とエラー写像のみを検証する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ymg_backend.domain.errors.errors import ErrorCategory, RecoverableError
from ymg_backend.domain.pipeline.video_compose import (
    VideoArtifact,
    build_ffmpeg_command,
    compose_video,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class _FakeTrack:
    """``AudioTrack`` の最小スタブ (compose_video が読む属性のみ)。"""

    position: int
    audio_uri: str
    duration_sec: int


class _FakeStorage:
    """``StorageAdapter`` の最小スタブ。read は URI→bytes 辞書、write は記録のみ。"""

    def __init__(self, contents: dict[str, bytes]) -> None:
        self._contents = contents
        self.written: dict[str, bytes] = {}

    def read_bytes(self, path_or_uri: str) -> bytes:
        return self._contents[path_or_uri]

    def write_bytes(self, path_or_uri: str, data: bytes) -> None:
        self.written[path_or_uri] = data


def _tracks(count: int = 6) -> list[_FakeTrack]:
    return [
        _FakeTrack(position=i, audio_uri=f"file:///in/track_{i}.wav", duration_sec=300)
        for i in range(count)
    ]


# --- build_ffmpeg_command (純関数) -------------------------------------------------


def test_build_command_has_one_input_per_audio_plus_background() -> None:
    """音声 6 本 + 背景画像 1 枚 = ``-i`` が 7 回現れる。"""
    audio = [f"/tmp/a{i}.audio" for i in range(6)]
    cmd = build_ffmpeg_command(
        audio_paths=audio,
        background_image_path="/tmp/bg.image",
        output_path="/tmp/out.mp4",
    )
    assert cmd[0] == "ffmpeg"
    assert cmd.count("-i") == 7
    # 背景画像はループ入力として渡される。
    assert "-loop" in cmd
    assert cmd[-1] == "/tmp/out.mp4"


def test_build_command_filter_uses_acrossfade_and_showwaves() -> None:
    """filter_complex に acrossfade (連結) と showwaves (波形 overlay) が含まれる。"""
    cmd = build_ffmpeg_command(
        audio_paths=["/tmp/a0.audio", "/tmp/a1.audio"],
        background_image_path="/tmp/bg.image",
        output_path="/tmp/out.mp4",
        crossfade_sec=5.0,
    )
    fc = _filter_complex_of(cmd)
    assert "acrossfade=d=5.0" in fc
    assert "showwaves=" in fc
    assert "overlay=" in fc


def test_build_command_acrossfade_count_is_track_count_minus_one() -> None:
    """N 本連結なら acrossfade は N-1 回現れる (6 本 → 5 回)。"""
    audio = [f"/tmp/a{i}.audio" for i in range(6)]
    fc = _filter_complex_of(
        build_ffmpeg_command(
            audio_paths=audio,
            background_image_path="/tmp/bg.image",
            output_path="/tmp/out.mp4",
        )
    )
    assert fc.count("acrossfade=") == 5


def test_build_command_uses_h264_and_aac() -> None:
    """出力コーデックは ADR-0015 準拠で libx264 + aac。"""
    cmd = build_ffmpeg_command(
        audio_paths=["/tmp/a0.audio", "/tmp/a1.audio"],
        background_image_path="/tmp/bg.image",
        output_path="/tmp/out.mp4",
    )
    assert _value_after(cmd, "-c:v") == "libx264"
    assert _value_after(cmd, "-c:a") == "aac"
    assert "-shortest" in cmd


def test_build_command_rejects_single_track() -> None:
    """トラック 1 本では連結不能 → RecoverableError。"""
    with pytest.raises(RecoverableError) as exc:
        build_ffmpeg_command(
            audio_paths=["/tmp/a0.audio"],
            background_image_path="/tmp/bg.image",
            output_path="/tmp/out.mp4",
        )
    assert exc.value.category is ErrorCategory.RECOVERABLE


@pytest.mark.parametrize("bad", [2.9, 5.1, 0.0, 10.0])
def test_build_command_rejects_out_of_range_crossfade(bad: float) -> None:
    """クロスフェードは ADR-0003 の 3〜5 秒外なら RecoverableError。"""
    with pytest.raises(RecoverableError):
        build_ffmpeg_command(
            audio_paths=["/tmp/a0.audio", "/tmp/a1.audio"],
            background_image_path="/tmp/bg.image",
            output_path="/tmp/out.mp4",
            crossfade_sec=bad,
        )


# --- compose_video (副作用あり / subprocess stub) ----------------------------------


def test_compose_video_writes_output_and_returns_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ステージ → ffmpeg(stub) → 書き戻しが結線され、VideoArtifact を返す。"""
    contents = {t.audio_uri: b"audio-bytes" for t in _tracks()}
    contents["file:///in/bg.png"] = b"image-bytes"
    storage = _FakeStorage(contents)

    captured: dict[str, list[str]] = {}

    def fake_run(command: Sequence[str], **kwargs: object) -> object:
        captured["command"] = list(command)
        # ffmpeg 出力先 (argv 末尾) に mp4 を書いたことにする。
        Path(command[-1]).write_bytes(b"fake-mp4")
        return _CompletedStub(returncode=0)

    monkeypatch.setattr(
        "ymg_backend.domain.pipeline.video_compose.shutil.which",
        lambda _name: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        "ymg_backend.domain.pipeline.video_compose.subprocess.run",
        fake_run,
    )

    artifact = compose_video(
        audio_tracks=_tracks(),  # type: ignore[arg-type]
        background_image_uri="file:///in/bg.png",
        storage=storage,  # type: ignore[arg-type]
        output_uri="file:///srv/ymg/outputs/video/post-1/video.mp4",
    )

    assert isinstance(artifact, VideoArtifact)
    assert artifact.video_uri == "file:///srv/ymg/outputs/video/post-1/video.mp4"
    # 6 本 * 300s = 1800s, クロスフェード 4s * 5 = 20s 重複 → 1780s。
    assert artifact.duration_sec == 1780
    # 出力 URI へ mp4 が書き戻されている。
    assert storage.written["file:///srv/ymg/outputs/video/post-1/video.mp4"] == b"fake-mp4"
    # ffmpeg コマンドが実際に構築・実行された。
    assert captured["command"][0] == "ffmpeg"


def test_compose_video_raises_when_ffmpeg_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg バイナリ不在は RecoverableError。"""
    contents = {t.audio_uri: b"a" for t in _tracks()}
    contents["file:///in/bg.png"] = b"i"
    storage = _FakeStorage(contents)

    monkeypatch.setattr(
        "ymg_backend.domain.pipeline.video_compose.shutil.which",
        lambda _name: None,
    )

    with pytest.raises(RecoverableError) as exc:
        compose_video(
            audio_tracks=_tracks(),  # type: ignore[arg-type]
            background_image_uri="file:///in/bg.png",
            storage=storage,  # type: ignore[arg-type]
            output_uri="file:///out.mp4",
        )
    assert exc.value.category is ErrorCategory.RECOVERABLE
    assert "file:///out.mp4" not in storage.written


def test_compose_video_raises_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg が非ゼロ終了したら RecoverableError (出力は書き戻さない)。"""
    contents = {t.audio_uri: b"a" for t in _tracks()}
    contents["file:///in/bg.png"] = b"i"
    storage = _FakeStorage(contents)

    def fake_run(command: Sequence[str], **kwargs: object) -> object:
        return _CompletedStub(returncode=1, stderr=b"boom")

    monkeypatch.setattr(
        "ymg_backend.domain.pipeline.video_compose.shutil.which",
        lambda _name: "/usr/bin/ffmpeg",
    )
    monkeypatch.setattr(
        "ymg_backend.domain.pipeline.video_compose.subprocess.run",
        fake_run,
    )

    with pytest.raises(RecoverableError) as exc:
        compose_video(
            audio_tracks=_tracks(),  # type: ignore[arg-type]
            background_image_uri="file:///in/bg.png",
            storage=storage,  # type: ignore[arg-type]
            output_uri="file:///out.mp4",
        )
    assert exc.value.category is ErrorCategory.RECOVERABLE
    assert "file:///out.mp4" not in storage.written


def test_compose_video_rejects_too_few_tracks() -> None:
    """1 本では合成不可 → RecoverableError (ストレージに触れない)。"""
    storage = _FakeStorage({})
    with pytest.raises(RecoverableError):
        compose_video(
            audio_tracks=_tracks(1),  # type: ignore[arg-type]
            background_image_uri="file:///in/bg.png",
            storage=storage,  # type: ignore[arg-type]
            output_uri="file:///out.mp4",
        )


# --- ヘルパ ------------------------------------------------------------------------


@dataclass
class _CompletedStub:
    """``subprocess.CompletedProcess`` の最小スタブ。"""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


def _filter_complex_of(cmd: list[str]) -> str:
    return _value_after(cmd, "-filter_complex")


def _value_after(cmd: list[str], flag: str) -> str:
    idx = cmd.index(flag)
    return cmd[idx + 1]
