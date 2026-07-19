"""Unit テスト: usage_log 書込 (llm/usage_writer.py, FR-025)。

FR-025: 全 LLM 呼び出し(成功 / 失敗)を ``usage_log`` テーブルへ provider / model /
prompt_tokens / completion_tokens / cost_usd / context_type / context_id 付きで 1 行
記録できることを検証する。

外部依存ゼロの真の単体テスト:

- ``AsyncSession`` は ``execute`` に渡された ``Insert`` 文を compile して params を
  捕捉する fake (``_CaptureSession``) で代替する。 実 DB は起動しない。
- ``write_usage_log`` / ``write_usage_from_response`` の実 API・引数名・戻り値は
  ``src/ymg_backend/llm/usage_writer.py`` を確認して合わせている。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Insert

from ymg_backend.llm.base import LlmResponse, LlmUsage
from ymg_backend.llm.usage_writer import (
    usage_log_table,
    write_usage_from_response,
    write_usage_log,
)

pytestmark = pytest.mark.asyncio


class _CaptureSession:
    """``execute(Insert)`` の compile 済み params を捕捉するだけの AsyncSession 代替。

    ``write_usage_log`` は ``session.execute(insert(...).values(**values))`` の後に
    ``session.flush()`` を呼ぶ契約。 本 fake は実 DB なしでその 1 行分の values を観測する。
    """

    def __init__(self) -> None:
        self.inserted: dict[str, Any] | None = None
        self.flush_count = 0

    async def execute(self, statement: Any) -> None:
        assert isinstance(statement, Insert)
        assert statement.table is usage_log_table
        self.inserted = dict(statement.compile().params)

    async def flush(self) -> None:
        self.flush_count += 1


# ===========================================================================
# write_usage_log: 正常系 (全列が正しく書かれる)
# ===========================================================================
@pytest.mark.fr("FR-025")
async def test_write_usage_log_persists_all_columns() -> None:
    """FR-025: provider/model/tokens/cost/context_type/context_id が usage_log 行に書かれる。"""
    session = _CaptureSession()
    context_id = uuid.uuid4()

    row_id = await write_usage_log(
        session,  # type: ignore[arg-type]
        provider="openai",
        model="gpt-4.1",
        prompt_tokens=1000,
        cached_tokens=200,
        completion_tokens=150,
        cost_usd=Decimal("0.012345"),
        auth_mode="api_key",
        duration_ms=42,
        context_type="planner",
        context_id=context_id,
        prompt_version="planner/system_v1",
    )

    assert isinstance(row_id, uuid.UUID)
    assert session.flush_count == 1
    params = session.inserted
    assert params is not None
    assert params["id"] == row_id  # 戻り値と挿入 id が一致
    assert params["provider"] == "openai"
    assert params["model"] == "gpt-4.1"
    assert params["prompt_tokens"] == 1000
    assert params["cached_tokens"] == 200
    assert params["completion_tokens"] == 150
    assert params["cost_usd"] == Decimal("0.012345")
    assert params["context_type"] == "planner"
    assert params["context_id"] == context_id
    assert params["auth_mode"] == "api_key"
    assert params["prompt_version"] == "planner/system_v1"
    assert params["error_message"] is None


# ===========================================================================
# write_usage_log: 失敗時の token=0 記録 (usage 不明)
# ===========================================================================
@pytest.mark.fr("FR-025")
async def test_write_usage_log_records_failure_with_zero_tokens() -> None:
    """FR-025: 失敗時は token=0 / cost=0 / error_message 付きで記録できる。"""
    session = _CaptureSession()

    await write_usage_log(
        session,  # type: ignore[arg-type]
        provider="anthropic",
        model="claude-sonnet-4-6",
        prompt_tokens=0,
        cached_tokens=0,
        completion_tokens=0,
        cost_usd=Decimal("0"),
        context_type="finisher",
        error_message="provider timeout",
    )

    params = session.inserted
    assert params is not None
    assert params["prompt_tokens"] == 0
    assert params["completion_tokens"] == 0
    assert params["cost_usd"] == Decimal("0")
    assert params["error_message"] == "provider timeout"
    assert params["context_id"] is None  # 未指定は None


# ===========================================================================
# write_usage_log: context_id の UUID 正規化 (str -> UUID, 空 -> None)
# ===========================================================================
@pytest.mark.fr("FR-025")
async def test_write_usage_log_normalizes_context_id_from_str() -> None:
    """FR-025: 文字列 context_id は UUID へ正規化され、 空文字は None になる。"""
    raw = "0190b3aa-0000-7000-8000-000000000001"

    session = _CaptureSession()
    await write_usage_log(
        session,  # type: ignore[arg-type]
        provider="ollama",
        model="qwen2.5:3b",
        prompt_tokens=10,
        cached_tokens=0,
        completion_tokens=5,
        cost_usd=Decimal("0"),
        context_id=raw,
    )
    assert session.inserted is not None
    assert session.inserted["context_id"] == uuid.UUID(raw)

    empty_session = _CaptureSession()
    await write_usage_log(
        empty_session,  # type: ignore[arg-type]
        provider="ollama",
        model="qwen2.5:3b",
        prompt_tokens=10,
        cached_tokens=0,
        completion_tokens=5,
        cost_usd=Decimal("0"),
        context_id="",
    )
    assert empty_session.inserted is not None
    assert empty_session.inserted["context_id"] is None


# ===========================================================================
# write_usage_log: 入力検証 (境界)
# ===========================================================================
@pytest.mark.fr("FR-025")
async def test_write_usage_log_rejects_cached_exceeding_prompt() -> None:
    """FR-025: cached_tokens > prompt_tokens は ValueError(境界での入力検証)。"""
    session = _CaptureSession()
    with pytest.raises(ValueError, match="cached_tokens"):
        await write_usage_log(
            session,  # type: ignore[arg-type]
            provider="openai",
            model="gpt-4.1",
            prompt_tokens=100,
            cached_tokens=200,
            completion_tokens=10,
            cost_usd=Decimal("0"),
        )


@pytest.mark.fr("FR-025")
async def test_write_usage_log_rejects_empty_model() -> None:
    """FR-025: 空 model は ValueError で拒否する。"""
    session = _CaptureSession()
    with pytest.raises(ValueError, match="model"):
        await write_usage_log(
            session,  # type: ignore[arg-type]
            provider="openai",
            model="",
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=5,
            cost_usd=Decimal("0"),
        )


# ===========================================================================
# write_usage_from_response: LlmResponse からの転記 (cost は Decimal 変換)
# ===========================================================================
def _response() -> LlmResponse[dict[str, Any]]:
    return LlmResponse(
        parsed={"title": "Lofi"},
        raw_text='{"title": "Lofi"}',
        usage=LlmUsage(
            prompt_tokens=2000,
            cached_tokens=1800,
            completion_tokens=120,
            cost_usd=0.001440,
            duration_ms=15,
        ),
        provider="anthropic",
        model="claude-sonnet-4-6",
        finish_reason="stop",
    )


@pytest.mark.fr("FR-025")
async def test_write_usage_from_response_transcribes_usage() -> None:
    """FR-025: LlmResponse.usage の provider/model/tokens/cost を usage_log へ転記する。

    ``cost_usd`` は float のため ``Decimal(str(...))`` 変換で精度劣化を避ける契約。
    """
    session = _CaptureSession()
    context_id = uuid.uuid4()

    row_id = await write_usage_from_response(
        session,  # type: ignore[arg-type]
        _response(),
        auth_mode="api_key",
        context_type="planner",
        context_id=context_id,
        prompt_version="planner/system_v1",
    )

    assert isinstance(row_id, uuid.UUID)
    params = session.inserted
    assert params is not None
    assert params["provider"] == "anthropic"
    assert params["model"] == "claude-sonnet-4-6"
    assert params["prompt_tokens"] == 2000
    assert params["cached_tokens"] == 1800
    assert params["completion_tokens"] == 120
    assert params["cost_usd"] == Decimal(str(0.001440))
    assert params["duration_ms"] == 15
    assert params["context_type"] == "planner"
    assert params["context_id"] == context_id
