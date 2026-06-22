"""構造化ログ (core/logging.py) の単体テスト (FR-110 / ADR-0023)。

FR-110: System MUST loguru で構造化 JSON ログを出力する (ADR-0023)。

検証観点:

- ``setup_logging(serialize=True)`` が JSON Lines (1 行 1 JSON) で出力すること。
- ``setup_logging`` が冪等であること (複数回呼んでも sink が重複しない)。
- ``bind_context`` が ``video_id`` / ``genre`` / ``step`` 等を ``record.extra`` に付与すること。
- ``serialize=False`` (ローカル開発) では JSON ではなく人間可読テキストで出すこと。

loguru の sink にメモリバッファ (``io.StringIO``) を ``add`` し、 serialize 出力を捕捉して
JSON としてパースできるかで検証する (実ファイル / 標準出力には依存しない)。
"""

from __future__ import annotations

import io
import json

import pytest
from loguru import logger

from ymg_backend.core.logging import bind_context, setup_logging


def _capture_serialized(*, level: str = "INFO") -> io.StringIO:
    """loguru の既定 sink を全除去し、 serialize=True のメモリ sink だけを残す。

    ``setup_logging`` は標準出力 sink を張るため、 テストでは行を捕捉できる
    ``StringIO`` sink に差し替える (``logger.remove()`` で既存 sink を一掃してから add)。
    """
    buffer = io.StringIO()
    logger.remove()
    # setup_logging と同じ既定 context キーを注入してフィールドを安定させる。
    logger.configure(extra={"job_id": None, "cycle_id": None, "genre": None, "video_id": None, "step": None})
    logger.add(buffer, level=level, serialize=True)
    return buffer


def _read_lines(buffer: io.StringIO) -> list[dict[str, object]]:
    """バッファ内の各行を JSON としてパースして返す (JSON Lines 検証)。"""
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _restore_logger() -> object:
    """各テスト後に logger を初期状態へ戻す (sink リーク防止)。"""
    yield
    logger.remove()


@pytest.mark.fr("FR-110")
def test_setup_logging_serialize_emits_json_lines() -> None:
    """FR-110: serialize=True では 1 行 1 JSON (JSON Lines) で構造化出力される。"""
    buffer = _capture_serialized()

    logger.info("first")
    logger.warning("second")

    records = _read_lines(buffer)
    assert len(records) == 2
    # 各行が JSON オブジェクトであり、 loguru の構造 (text / record) を持つ。
    first, second = records
    assert "first" in str(first["text"])
    assert "second" in str(second["text"])
    record0 = first["record"]
    assert isinstance(record0, dict)
    assert record0["level"]["name"] == "INFO"


@pytest.mark.fr("FR-110")
def test_setup_logging_is_idempotent() -> None:
    """FR-110: setup_logging を複数回呼んでも sink が重複せず、 行が二重化しない。

    既存 sink を remove してから再構成するため、 2 回 setup しても 1 ログ = 1 行に保たれる。
    log_dir=None で標準出力のみ構成し、 そこへ捕捉用 sink を 1 つだけ足して行数を数える。
    """
    setup_logging(level="INFO", log_dir=None, serialize=True)
    setup_logging(level="INFO", log_dir=None, serialize=True)

    # setup_logging が張った stdout sink はカウントできないため、 捕捉用 sink に絞り直す。
    buffer = _capture_serialized()
    logger.info("once")

    records = _read_lines(buffer)
    assert len(records) == 1  # sink 重複なし → 1 ログ 1 行


@pytest.mark.fr("FR-110")
def test_bind_context_adds_fields_to_extra() -> None:
    """FR-110: bind_context で渡した context が JSON の record.extra に載る (フィルタ軸)。"""
    buffer = _capture_serialized()

    bound = bind_context(video_id="vid-1", genre="lofi", step="render", provider="codex")
    bound.info("contextual log")

    records = _read_lines(buffer)
    assert len(records) == 1
    extra = records[0]["record"]["extra"]  # type: ignore[index]
    assert extra["video_id"] == "vid-1"
    assert extra["genre"] == "lofi"
    assert extra["step"] == "render"
    # 明示引数以外の **extra キーも付与される。
    assert extra["provider"] == "codex"


@pytest.mark.fr("FR-110")
def test_bind_context_omits_none_fields() -> None:
    """FR-110: None の context キーは bind されず、 既定値 (configure の None) のままになる。"""
    buffer = _capture_serialized()

    # video_id のみ指定、 他は None → None キーは bind されない。
    bound = bind_context(video_id="only-vid")
    bound.info("partial context")

    extra = _read_lines(buffer)[0]["record"]["extra"]  # type: ignore[index]
    assert extra["video_id"] == "only-vid"
    # genre は bind されていないため configure の既定値 (None) のまま。
    assert extra["genre"] is None


@pytest.mark.fr("FR-110")
def test_setup_logging_human_readable_is_not_json() -> None:
    """FR-110: serialize=False (ローカル開発) では JSON ではなく人間可読テキストで出す。"""
    buffer = io.StringIO()
    logger.remove()
    logger.configure(extra={})
    logger.add(buffer, level="INFO", serialize=False)

    logger.info("plain text line")

    raw = buffer.getvalue()
    assert "plain text line" in raw
    # serialize=False の行は JSON オブジェクトとしてパースできない (人間可読フォーマット)。
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw.splitlines()[0])
