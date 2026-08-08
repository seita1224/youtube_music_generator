"""6 トラック連結 + showwaves overlay による 30 分動画合成 (ADR-0003 / ADR-0015)。

1 投稿あたり 6 本の AI 生成楽曲 (各 5 分) を ffmpeg ``acrossfade`` で連結し、SDXL 生成の
固定背景画像に ``showwaves`` 波形 overlay を重ねて 1 本の mp4 (H.264 + AAC, 1080p30) を
生成する。

設計方針:

- **コマンド構築は純関数** (:func:`build_ffmpeg_command`) として分離する。ローカル実パスのみを
  入力に取り、ffmpeg の ``argv`` (``list[str]``) を返す。これにより副作用なしでコマンド検証
  (テスト) ができる。
- **副作用 (I/O + subprocess)** は :func:`compose_video` に閉じる。``StorageAdapter`` で
  入力 (音声 / 画像) をローカル一時ディレクトリへステージし、ffmpeg を ``subprocess`` で実行、
  生成された mp4 を出力 URI へ書き戻す。ステージングにより ``file://`` 以外 (s3:// 等) の
  バックエンドでもバイナリ ffmpeg を扱える (ffmpeg はローカルパスのみ受ける)。
- 失敗 (ffmpeg 非ゼロ終了・バイナリ不在・タイムアウト) は ADR-0028 の :class:`RecoverableError`
  に写像する (該当 post のみスキップ + Slack 通知、他 post は継続)。

公開シグネチャは US1 共有契約 (video_compose) に準拠する。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ymg_backend.core.logging import bind_context
from ymg_backend.domain.errors.errors import RecoverableError

if TYPE_CHECKING:
    from ymg_backend.infrastructure.db.models import AudioTrack
    from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

__all__ = [
    "VideoArtifact",
    "build_ffmpeg_command",
    "compose_video",
]

# --- ffmpeg / 出力フォーマット定数 (ADR-0003 / ADR-0015) ----------------------------
# 動画解像度・フレームレート (ADR-0015: 1080p / 30fps)。
_VIDEO_WIDTH: Final[int] = 1920
_VIDEO_HEIGHT: Final[int] = 1080
_FRAME_RATE: Final[int] = 30

# showwaves 波形ビジュアライザのサイズ・モード・色 (ADR-0015 の例に準拠)。
# 画面下部に半透明白の中心線波形を重ねる (長尺で疲れにくい控えめな動き)。
_WAVE_WIDTH: Final[int] = 1920
_WAVE_HEIGHT: Final[int] = 300
_WAVE_MODE: Final[str] = "cline"
_WAVE_COLORS: Final[str] = "white@0.7"

# acrossfade のクロスフェード尺 (ADR-0003: 3〜5 秒)。範囲外は拒否する。
_DEFAULT_CROSSFADE_SEC: Final[float] = 4.0
_CROSSFADE_MIN_SEC: Final[float] = 3.0
_CROSSFADE_MAX_SEC: Final[float] = 5.0

# 連結に必要な最小トラック数 (1 本では acrossfade 連結が成立しないため 2 本以上)。
_MIN_TRACK_COUNT: Final[int] = 2

# コーデック (ADR-0015: H.264 + AAC, YouTube 推奨)。
_VIDEO_CODEC: Final[str] = "libx264"
_AUDIO_CODEC: Final[str] = "aac"

# ffmpeg 実行タイムアウト (秒)。30 分動画のレンダリングは ADR-0015 で 3〜5 分想定のため、
# 余裕を持って 30 分を上限とする (超過は環境異常とみなし RecoverableError)。
_FFMPEG_TIMEOUT_SEC: Final[float] = 1800.0

# ステージしたローカル入力ファイルの拡張子 (ffmpeg はコンテナを内容から判定するが、
# 拡張子を付けておくとデマックス選択が安定する)。
_AUDIO_SUFFIX: Final[str] = ".audio"
_IMAGE_SUFFIX: Final[str] = ".image"
_OUTPUT_NAME: Final[str] = "video.mp4"


@dataclass(frozen=True)
class VideoArtifact:
    """合成済み動画の成果物 (不変)。

    Attributes:
        video_uri: 生成された mp4 の出力 URI (``Post.video_uri`` に設定する)。
        duration_sec: 合成後の概算尺 (秒)。トラック尺合計からクロスフェード重複分を引いた値。
    """

    video_uri: str
    duration_sec: int


def _validate_crossfade(crossfade_sec: float) -> float:
    """クロスフェード尺が ADR-0003 の許容範囲 (3〜5 秒) 内かを検証する。"""
    if not _CROSSFADE_MIN_SEC <= crossfade_sec <= _CROSSFADE_MAX_SEC:
        raise RecoverableError(
            "crossfade_sec は "
            f"{_CROSSFADE_MIN_SEC}〜{_CROSSFADE_MAX_SEC} 秒の範囲で指定してください: {crossfade_sec}",
            context={"crossfade_sec": crossfade_sec},
        )
    return crossfade_sec


def _build_acrossfade_filter(track_count: int, crossfade_sec: float) -> tuple[str, str]:
    """``acrossfade`` で N トラックを順次連結する filter_complex 断片を組み立てる。

    各音声入力 (ffmpeg 入力 index ``0..track_count-1``) を左から順に 2 本ずつ
    ``acrossfade`` で重ねて連結する。三重以上の連結は中間ラベル (``[a01]`` 等) を介する。

    Args:
        track_count: 連結する音声トラック数 (>= 2)。
        crossfade_sec: クロスフェード尺 (秒)。

    Returns:
        ``(filter 断片, 最終音声ラベル)``。最終音声ラベルは ``[wave]`` overlay の入力に使う。
    """
    # 各入力の音声ストリームに明示ラベルを与える ([0:a] → [a0] 等)。
    label_decls = "".join(f"[{idx}:a]anull[a{idx}];" for idx in range(track_count))

    segments: list[str] = [label_decls]
    current_label = "[a0]"
    for idx in range(1, track_count):
        out_label = "[aout]" if idx == track_count - 1 else f"[a0{idx}]"
        segments.append(
            f"{current_label}[a{idx}]acrossfade=d={crossfade_sec}:c1=tri:c2=tri{out_label};"
        )
        current_label = out_label
    return "".join(segments), current_label


def build_ffmpeg_command(
    *,
    audio_paths: list[str],
    background_image_path: str,
    output_path: str,
    crossfade_sec: float = _DEFAULT_CROSSFADE_SEC,
) -> list[str]:
    """ffmpeg の ``argv`` を組み立てる純関数 (副作用なし)。

    入力は **ローカル実パス** (URI でなく). 6 本の音声を ``acrossfade`` で連結し、ループ表示する
    背景画像に ``showwaves`` 波形 overlay を重ねて 1 本の mp4 を生成するコマンドを返す。

    Args:
        audio_paths: 連結する音声ファイルのローカルパス (出現順 = 連結順, 2 本以上)。
        background_image_path: 背景に敷く SDXL 生成画像のローカルパス。
        output_path: 出力 mp4 のローカルパス。
        crossfade_sec: クロスフェード尺 (秒, 3〜5)。

    Returns:
        ``subprocess`` に渡せる ffmpeg 引数リスト。

    Raises:
        RecoverableError: トラック数が 2 未満、または ``crossfade_sec`` が範囲外の場合。
    """
    if len(audio_paths) < _MIN_TRACK_COUNT:
        raise RecoverableError(
            f"動画合成には最低 {_MIN_TRACK_COUNT} 本の音声が必要です: {len(audio_paths)} 本",
            context={"track_count": len(audio_paths)},
        )
    crossfade = _validate_crossfade(crossfade_sec)

    track_count = len(audio_paths)
    mix_filter, audio_label = _build_acrossfade_filter(track_count, crossfade)

    # showwaves 波形を生成し、背景画像 (入力 index = track_count) に下部 overlay する。
    # 背景画像は静止画スケール後にループ表示し、最終尺は -shortest で音声長に揃える。
    scale_filter = f"[{track_count}:v]scale={_VIDEO_WIDTH}:{_VIDEO_HEIGHT},setsar=1[bg];"
    wave_filter = (
        f"{audio_label}showwaves="
        f"s={_WAVE_WIDTH}x{_WAVE_HEIGHT}:mode={_WAVE_MODE}:colors={_WAVE_COLORS}[wave];"
    )
    overlay_filter = "[bg][wave]overlay=0:H-h[vout]"
    filter_complex = f"{mix_filter}{scale_filter}{wave_filter}{overlay_filter}"

    command: list[str] = ["ffmpeg", "-y", "-hide_banner", "-nostdin"]
    # 音声入力 (出現順)。
    for path in audio_paths:
        command += ["-i", path]
    # 背景画像 (ループ入力)。フレームレートを与えて静止画を動画化する。
    command += ["-loop", "1", "-framerate", str(_FRAME_RATE), "-i", background_image_path]
    command += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        audio_label,
        "-c:v",
        _VIDEO_CODEC,
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(_FRAME_RATE),
        "-c:a",
        _AUDIO_CODEC,
        "-shortest",
        output_path,
    ]
    return command


def _estimate_duration_sec(audio_tracks: list[AudioTrack], crossfade_sec: float) -> int:
    """連結後の概算尺 (秒) を求める。

    各トラック尺の合計から、隣接ペアごとに重なるクロスフェード分 (``(N-1) * crossfade``) を
    引いた値。負にはならないよう 0 で下限を取る。
    """
    total = sum(track.duration_sec for track in audio_tracks)
    overlap = crossfade_sec * (len(audio_tracks) - 1)
    return max(0, int(total - overlap))


def _stage_inputs(
    *,
    audio_tracks: list[AudioTrack],
    background_image_uri: str,
    storage: StorageAdapter,
    work_dir: Path,
) -> tuple[list[str], str]:
    """音声 / 背景画像を ``StorageAdapter`` 経由でローカル一時ディレクトリへ複製する。

    ffmpeg はローカルパスのみ扱うため、s3:// 等のバックエンドでも一度ローカルへ落とす。

    Returns:
        ``(音声ローカルパス列, 背景画像ローカルパス)``。
    """
    audio_paths: list[str] = []
    for track in audio_tracks:
        data = storage.read_bytes(track.audio_uri)
        local = work_dir / f"track_{track.position:02d}{_AUDIO_SUFFIX}"
        local.write_bytes(data)
        audio_paths.append(str(local))

    image_bytes = storage.read_bytes(background_image_uri)
    image_path = work_dir / f"background{_IMAGE_SUFFIX}"
    image_path.write_bytes(image_bytes)
    return audio_paths, str(image_path)


def _run_ffmpeg(command: list[str]) -> None:
    """ffmpeg を ``subprocess`` で実行し、失敗を :class:`RecoverableError` に写像する。

    バイナリ不在・非ゼロ終了・タイムアウトはいずれも環境/入力起因で同一 post のみスキップ
    すべき事象のため recoverable に分類する (リトライしても同じ結果になりうる)。
    """
    log = bind_context(step="video_compose")
    if shutil.which("ffmpeg") is None:
        raise RecoverableError(
            "ffmpeg バイナリが見つかりません (PATH を確認してください)",
            context={"binary": "ffmpeg"},
        )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecoverableError(
            f"ffmpeg がタイムアウトしました ({_FFMPEG_TIMEOUT_SEC} 秒超過)",
            context={"timeout_sec": _FFMPEG_TIMEOUT_SEC},
            original=exc,
        ) from exc
    except OSError as exc:  # 実行権限なし等
        raise RecoverableError(
            f"ffmpeg の起動に失敗しました: {exc}",
            context={"binary": "ffmpeg"},
            original=exc,
        ) from exc

    if completed.returncode != 0:
        stderr_tail = completed.stderr.decode("utf-8", errors="replace")[-500:]
        log.error("ffmpeg failed", returncode=completed.returncode, stderr_tail=stderr_tail)
        raise RecoverableError(
            f"ffmpeg が非ゼロ終了しました (code={completed.returncode}): {stderr_tail}",
            context={"returncode": completed.returncode},
        )


def compose_video(
    *,
    audio_tracks: list[AudioTrack],
    background_image_uri: str,
    storage: StorageAdapter,
    output_uri: str,
    crossfade_sec: float = _DEFAULT_CROSSFADE_SEC,
) -> VideoArtifact:
    """6 トラック連結 + showwaves overlay で 1 本の 30 分 mp4 を合成する (同期)。

    入力 (音声 / 背景画像) を ``StorageAdapter`` でローカルへステージし、ffmpeg を
    ``subprocess`` で実行、生成 mp4 を ``output_uri`` へ書き戻す。コマンド構築自体は
    :func:`build_ffmpeg_command` (純関数) に委譲する。

    Args:
        audio_tracks: 連結する ``AudioTrack`` 群 (出現順 = 連結順, 通常 6 本)。各 ``audio_uri`` /
            ``duration_sec`` を参照する。
        background_image_uri: 背景に敷く SDXL 生成画像の URI。
        storage: 入出力 URI を解決する ``StorageAdapter``。
        output_uri: 生成 mp4 の出力 URI (``file:///srv/ymg/outputs/video/<post_id>/...`` 形式)。
        crossfade_sec: クロスフェード尺 (秒, 3〜5)。既定 4 秒。

    Returns:
        ``VideoArtifact`` (``video_uri`` = ``output_uri``, ``duration_sec`` = 概算尺)。

    Raises:
        RecoverableError: トラック数不足・``crossfade_sec`` 範囲外・入力読込失敗・ffmpeg 失敗の場合。
    """
    if len(audio_tracks) < _MIN_TRACK_COUNT:
        raise RecoverableError(
            f"動画合成には最低 {_MIN_TRACK_COUNT} 本のトラックが必要です: {len(audio_tracks)} 本",
            context={"track_count": len(audio_tracks)},
        )
    crossfade = _validate_crossfade(crossfade_sec)
    log = bind_context(step="video_compose")

    try:
        with tempfile.TemporaryDirectory(prefix="ymg_video_") as tmp:
            work_dir = Path(tmp)
            audio_paths, image_path = _stage_inputs(
                audio_tracks=audio_tracks,
                background_image_uri=background_image_uri,
                storage=storage,
                work_dir=work_dir,
            )
            output_path = work_dir / _OUTPUT_NAME
            command = build_ffmpeg_command(
                audio_paths=audio_paths,
                background_image_path=image_path,
                output_path=str(output_path),
                crossfade_sec=crossfade,
            )
            log.info(
                "composing video",
                track_count=len(audio_tracks),
                crossfade_sec=crossfade,
                output_uri=output_uri,
            )
            _run_ffmpeg(command)
            video_bytes = output_path.read_bytes()
    except OSError as exc:  # ステージング/書込のローカル I/O 失敗
        raise RecoverableError(
            f"動画合成のローカル I/O に失敗しました: {exc}",
            context={"output_uri": output_uri},
            original=exc,
        ) from exc

    storage.write_bytes(output_uri, video_bytes)
    duration_sec = _estimate_duration_sec(audio_tracks, crossfade)
    log.info("video composed", output_uri=output_uri, duration_sec=duration_sec)
    return VideoArtifact(video_uri=output_uri, duration_sec=duration_sec)
