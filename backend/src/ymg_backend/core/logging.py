"""構造化ログ(loguru JSON Lines)セットアップ。

FR-110 / ADR-0023 準拠:

- フォーマット = JSON Lines(``serialize=True``)
- 出力先 = 標準出力(コンテナ / systemd ログ)+ ファイル(日次ローテーション、 retention 90 日)
- 共通 context フィールド(``video_id`` / ``genre`` / ``step`` 等)を bind してフィルタ可能にする

``setup_logging`` は冪等で、 呼ぶたびに既存 sink を除去してから再構成する。
``bind_context`` は context を付与した logger を返す薄いヘルパ。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

__all__ = ["LogContext", "bind_context", "setup_logging"]

# ADR-0023: ファイルローテーション / retention 設定
_ROTATION: Final = "00:00"  # 日次(深夜 0 時)ローテーション
_RETENTION: Final = "90 days"  # retention 90 日
_COMPRESSION: Final = "gz"  # ローテーション済みログを圧縮

# ADR-0023: 全ログ共通の context キー(管理 UI フィルタ軸)
_DEFAULT_CONTEXT: Final[dict[str, str | None]] = {
    "job_id": None,
    "cycle_id": None,
    "genre": None,
    "video_id": None,
    "step": None,
}

# 型エイリアス: bind 可能な context 値
LogContext = dict[str, object]


def setup_logging(
    level: str = "INFO",
    *,
    log_dir: Path | str | None = None,
    serialize: bool = True,
) -> None:
    """loguru を JSON Lines sink で初期化する。

    Args:
        level: 最小ログレベル(``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR`` 等)。
            ``.env`` の ``LOG_LEVEL``(既定 ``INFO``)を渡す想定。
        log_dir: ファイル sink の出力先ディレクトリ。 ``None`` の場合は標準出力のみ。
        serialize: ``True`` で JSON Lines 出力(本番)、 ``False`` で人間可読(ローカル開発)。

    Notes:
        冪等。 既存の sink をすべて除去してから再構成するため、
        起動時・テスト間で複数回呼んでも sink が重複しない。
    """
    normalized_level = level.strip().upper()

    logger.remove()

    # 全ログに既定 context キーを extra として注入し、 JSON フィールドを安定させる
    logger.configure(extra=dict(_DEFAULT_CONTEXT))

    # 標準出力 sink(コンテナ / systemd ログ)
    logger.add(
        sys.stdout,
        level=normalized_level,
        serialize=serialize,
        backtrace=False,
        diagnose=False,
        enqueue=True,
    )

    # ファイル sink(日次ローテーション + retention 90 日 + 圧縮)
    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        logger.add(
            directory / "ymg_backend_{time:YYYY-MM-DD}.jsonl",
            level=normalized_level,
            serialize=True,
            rotation=_ROTATION,
            retention=_RETENTION,
            compression=_COMPRESSION,
            backtrace=False,
            diagnose=False,
            enqueue=True,
        )


def bind_context(
    *,
    video_id: str | None = None,
    genre: str | None = None,
    step: str | None = None,
    job_id: str | None = None,
    cycle_id: str | None = None,
    **extra: object,
) -> Logger:
    """指定 context を bind した logger を返す。

    ADR-0023 の主要フィルタ軸(``video_id`` / ``genre`` / ``step``)を
    明示引数として受け、 ``None`` のキーは除外して bind する。

    Args:
        video_id: 対象動画 ID。
        genre: ジャンル名。
        step: パイプライン step 名。
        job_id: ジョブ ID。
        cycle_id: サイクル ID。
        **extra: 追加の任意 context キー(例: ``provider`` / ``model``)。

    Returns:
        context を付与した loguru ``Logger``。
    """
    context: LogContext = {
        "video_id": video_id,
        "genre": genre,
        "step": step,
        "job_id": job_id,
        "cycle_id": cycle_id,
        **extra,
    }
    bound = {key: value for key, value in context.items() if value is not None}
    return logger.bind(**bound)
