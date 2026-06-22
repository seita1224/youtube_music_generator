"""Critical path テスト: LLM provider (T035, Constitution II / ADR-0027)。

contracts/llm-provider-interface.md「テスト要件」を respx による provider 別 mock で
100% カバーする:

1. Pydantic validation 失敗時の最大 2 回リトライ(temperature 低下)+ ``recoverable`` 例外
   (OpenAI / Anthropic / Ollama)
2. Anthropic + subscription の起動時拒否(FR-022, category="fatal")
3. prompt caching hit カウント(``LlmUsage.cached_tokens``)
4. cost 計算(``model_pricing`` ベース、cached 割引込み)

provider は ``cost_usd=0.0`` を返す薄いラッパであり、cost は pricing 層
(``compute_cost`` / ``calculate_cost_usd``)が確定させる(ADR-0024)。本テストは
両者(provider が返す usage と pricing が算出する cost)を結合して検証する。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from openai import AsyncOpenAI
from pydantic import BaseModel, field_validator

from ymg_backend.llm.anthropic_provider import AnthropicProvider
from ymg_backend.llm.base import (
    LlmError,
    LlmMessage,
    LlmRequest,
)
from ymg_backend.llm.ollama_provider import OllamaProvider
from ymg_backend.llm.openai_provider import (
    _MAX_VALIDATION_RETRIES,
    _TEMPERATURE_DECAY,
    OpenAIProvider,
)
from ymg_backend.llm.pricing import ModelPricing, compute_cost

pytestmark = pytest.mark.critical

# --- 各 provider の wire endpoint(respx base_url + path)------------------------
_OPENAI_BASE = "https://api.openai.com/v1"
_OPENAI_RESPONSES = "/responses"
_ANTHROPIC_BASE = "https://api.anthropic.com"
_ANTHROPIC_MESSAGES = "/v1/messages"
_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_CHAT = "/api/chat"


class _Plan(BaseModel):
    """テスト用の構造化出力モデル。``bpm < 60`` を validation 失敗トリガにする。"""

    title: str
    bpm: int

    @field_validator("bpm")
    @classmethod
    def _validate_bpm(cls, value: int) -> int:
        if value < 60:
            raise ValueError("bpm must be >= 60")
        return value


# ---------------------------------------------------------------------------
# OpenAI Responses API mock helpers
# ---------------------------------------------------------------------------
def _openai_body(*, bpm: int, prompt_tokens: int = 100, cached_tokens: int = 0) -> dict[str, Any]:
    """OpenAI Responses API の最小レスポンス body(structured output 1 件)を組む。"""
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": "gpt-4.1",
        "status": "completed",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"title": "Lofi", "bpm": bpm}),
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": prompt_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens": 20,
            "total_tokens": prompt_tokens + 20,
        },
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 0.7,
        "top_p": 1.0,
    }


def _openai_provider() -> OpenAIProvider:
    """SDK 自身のリトライ(max_retries)を無効化した OpenAIProvider を作る。"""
    client = AsyncOpenAI(api_key="test-key", max_retries=0)
    return OpenAIProvider(client, model="gpt-4.1")


def _request() -> LlmRequest[_Plan]:
    return LlmRequest(
        messages=[LlmMessage(role="user", content="make a plan")],
        response_model=_Plan,
        temperature=0.7,
    )


# ---------------------------------------------------------------------------
# Anthropic Messages API mock helpers
# ---------------------------------------------------------------------------
def _anthropic_body(*, bpm: int, input_tokens: int = 100, cache_read: int = 0) -> dict[str, Any]:
    """Anthropic Messages API の最小 tool_use レスポンス body を組む。"""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "content": [
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "structured_output",
                "input": {"title": "Lofi", "bpm": bpm},
            }
        ],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": 20,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
        },
    }


def _anthropic_provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key", default_model="claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Ollama chat API mock helpers
# ---------------------------------------------------------------------------
def _ollama_body(*, bpm: int, prompt_eval: int = 50, eval_count: int = 12) -> dict[str, Any]:
    """Ollama /api/chat の最小レスポンス body を組む。"""
    return {
        "model": "qwen2.5:3b",
        "created_at": "2026-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": json.dumps({"title": "Lofi", "bpm": bpm}),
        },
        "done": True,
        "done_reason": "stop",
        "total_duration": 2_000_000_000,
        "prompt_eval_count": prompt_eval,
        "eval_count": eval_count,
    }


def _ollama_provider() -> OllamaProvider:
    return OllamaProvider(_OLLAMA_BASE, "qwen2.5:3b")


# ===========================================================================
# 1. OpenAI: validation 失敗 → temperature 低下リトライ → 成功
# ===========================================================================
@respx.mock
@pytest.mark.fr("FR-024")
async def test_openai_retries_on_validation_failure_with_temperature_decay() -> None:
    """FR-024: 1 回目 validation 失敗 → 温度を下げて再試行 → 2 回目成功(contract §1)。"""
    route = respx.post(f"{_OPENAI_BASE}{_OPENAI_RESPONSES}").mock(
        side_effect=[
            httpx.Response(200, json=_openai_body(bpm=10)),  # 失敗(bpm < 60)
            httpx.Response(200, json=_openai_body(bpm=90)),  # 成功
        ]
    )

    resp = await _openai_provider().generate(_request())

    assert resp.parsed.bpm == 90
    assert resp.provider == "openai"
    assert route.call_count == 2

    temps = [json.loads(call.request.content)["temperature"] for call in route.calls]
    # 1 回目 = 元 temperature、2 回目 = temperature - decay。
    assert temps[0] == pytest.approx(0.7)
    assert temps[1] == pytest.approx(0.7 - _TEMPERATURE_DECAY)
    assert temps[1] < temps[0]


@respx.mock
async def test_openai_raises_recoverable_after_all_retries_exhausted() -> None:
    """最大リトライ(計 3 試行)全失敗で recoverable(retryable=False)を送出する。"""
    total_attempts = _MAX_VALIDATION_RETRIES + 1
    route = respx.post(f"{_OPENAI_BASE}{_OPENAI_RESPONSES}").mock(
        side_effect=[httpx.Response(200, json=_openai_body(bpm=10)) for _ in range(total_attempts)]
    )

    with pytest.raises(LlmError) as exc_info:
        await _openai_provider().generate(_request())

    assert exc_info.value.category == "recoverable"
    assert exc_info.value.retryable is False
    assert route.call_count == total_attempts


# ===========================================================================
# 2. OpenAI: prompt caching hit カウント
# ===========================================================================
@respx.mock
async def test_openai_reports_cached_tokens_from_usage() -> None:
    """``usage.input_tokens_details.cached_tokens`` を ``LlmUsage.cached_tokens`` に反映する。"""
    respx.post(f"{_OPENAI_BASE}{_OPENAI_RESPONSES}").mock(
        return_value=httpx.Response(
            200, json=_openai_body(bpm=90, prompt_tokens=1500, cached_tokens=1024)
        )
    )

    resp = await _openai_provider().generate(_request())

    assert resp.usage.prompt_tokens == 1500
    assert resp.usage.cached_tokens == 1024
    assert resp.usage.cost_usd == 0.0  # provider は cost を確定しない(ADR-0024)


# ===========================================================================
# 3. Anthropic: validation 失敗 → リトライ → 成功 / 全失敗 → recoverable
# ===========================================================================
@respx.mock
async def test_anthropic_retries_on_validation_failure() -> None:
    """tool_use input が validation 失敗 → temperature を下げて再試行 → 成功(contract §1)。"""
    route = respx.post(f"{_ANTHROPIC_BASE}{_ANTHROPIC_MESSAGES}").mock(
        side_effect=[
            httpx.Response(200, json=_anthropic_body(bpm=10)),  # 失敗
            httpx.Response(200, json=_anthropic_body(bpm=90)),  # 成功
        ]
    )

    resp = await _anthropic_provider().generate(_request())

    assert resp.parsed.bpm == 90
    assert resp.provider == "anthropic"
    assert route.call_count == 2

    temps = [json.loads(call.request.content)["temperature"] for call in route.calls]
    assert temps[1] < temps[0]  # リトライで温度が下がる


@respx.mock
async def test_anthropic_raises_recoverable_after_all_retries_exhausted() -> None:
    """Anthropic も全リトライ失敗で recoverable(retryable=False)を送出する。"""
    total_attempts = _MAX_VALIDATION_RETRIES + 1
    route = respx.post(f"{_ANTHROPIC_BASE}{_ANTHROPIC_MESSAGES}").mock(
        side_effect=[
            httpx.Response(200, json=_anthropic_body(bpm=10)) for _ in range(total_attempts)
        ]
    )

    with pytest.raises(LlmError) as exc_info:
        await _anthropic_provider().generate(_request())

    assert exc_info.value.category == "recoverable"
    assert exc_info.value.retryable is False
    assert route.call_count == total_attempts


# ===========================================================================
# 4. Anthropic: prompt caching hit カウント
# ===========================================================================
@respx.mock
async def test_anthropic_reports_cache_read_tokens() -> None:
    """``usage.cache_read_input_tokens`` を ``LlmUsage.cached_tokens`` に反映する(ADR-0033)。"""
    respx.post(f"{_ANTHROPIC_BASE}{_ANTHROPIC_MESSAGES}").mock(
        return_value=httpx.Response(
            200, json=_anthropic_body(bpm=90, input_tokens=2000, cache_read=1800)
        )
    )

    resp = await _anthropic_provider().generate(_request())

    assert resp.usage.prompt_tokens == 2000
    assert resp.usage.cached_tokens == 1800
    assert resp.usage.cost_usd == 0.0


# ===========================================================================
# 5. Anthropic + subscription の起動時拒否(FR-022)
# ===========================================================================
@pytest.mark.fr("FR-022")
def test_anthropic_rejects_subscription_auth_mode() -> None:
    """FR-022: subscription 認証は生成時に fatal(retryable=False)で拒否する。"""
    with pytest.raises(LlmError) as exc_info:
        AnthropicProvider(
            api_key="test-key",
            default_model="claude-sonnet-4-6",
            auth_mode="subscription",
        )

    assert exc_info.value.category == "fatal"
    assert exc_info.value.retryable is False


def test_anthropic_accepts_api_key_auth_mode() -> None:
    """api_key 認証は許可される(対照: subscription 拒否との境界)。"""
    provider = AnthropicProvider(
        api_key="test-key",
        default_model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    assert provider.supports_caching() is True


# ===========================================================================
# 6. Ollama: validation 失敗 → リトライ → 成功 / 全失敗 → recoverable
# ===========================================================================
@respx.mock
async def test_ollama_retries_on_validation_failure() -> None:
    """Ollama の後検証失敗 → temperature を下げて再試行 → 成功(contract §1)。"""
    route = respx.post(f"{_OLLAMA_BASE}{_OLLAMA_CHAT}").mock(
        side_effect=[
            httpx.Response(200, json=_ollama_body(bpm=10)),  # 失敗
            httpx.Response(200, json=_ollama_body(bpm=90)),  # 成功
        ]
    )

    resp = await _ollama_provider().generate(_request())

    assert resp.parsed.bpm == 90
    assert resp.provider == "ollama"
    assert resp.usage.cost_usd == 0.0  # ローカル実行はコスト 0(ADR-0019)
    assert route.call_count == 2

    temps = [json.loads(call.request.content)["options"]["temperature"] for call in route.calls]
    assert temps[1] < temps[0]  # リトライで温度が下がる


@respx.mock
async def test_ollama_raises_recoverable_after_all_retries_exhausted() -> None:
    """Ollama も全試行失敗で recoverable(retryable=False)を送出する。"""
    # provider は元 temperature + _RETRY_TEMPERATURES(2 段)= 計 3 試行。
    route = respx.post(f"{_OLLAMA_BASE}{_OLLAMA_CHAT}").mock(
        side_effect=[httpx.Response(200, json=_ollama_body(bpm=10)) for _ in range(3)]
    )

    with pytest.raises(LlmError) as exc_info:
        await _ollama_provider().generate(_request())

    assert exc_info.value.category == "recoverable"
    assert exc_info.value.retryable is False
    assert route.call_count == 3


@respx.mock
async def test_ollama_never_reports_cached_tokens() -> None:
    """Ollama は prompt caching 非対応のため cached_tokens は常に 0(ADR-0033)。"""
    respx.post(f"{_OLLAMA_BASE}{_OLLAMA_CHAT}").mock(
        return_value=httpx.Response(200, json=_ollama_body(bpm=90))
    )

    resp = await _ollama_provider().generate(_request())

    assert resp.usage.cached_tokens == 0
    assert _ollama_provider().supports_caching() is False


# ===========================================================================
# 7. cost 計算: provider の usage(cached 込み)を pricing 層で確定する(ADR-0024)
# ===========================================================================
def _pricing(*, cached: Decimal | None) -> ModelPricing:
    return ModelPricing(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_per_1m_usd=Decimal("3.0000"),
        cached_per_1m_usd=cached,
        output_per_1m_usd=Decimal("15.0000"),
        effective_from=dt.date(2026, 1, 1),
    )


def test_cost_computation_applies_cache_discount() -> None:
    """cached tokens は割引単価で計算する(uncached=input, cached=cached_rate, output=output)。"""
    pricing = _pricing(cached=Decimal("0.3000"))
    # prompt 2000(うち 1800 cached)、output 20。
    # uncached = 200/1e6 * 3.0 = 0.0006
    # cached   = 1800/1e6 * 0.3 = 0.00054
    # output   = 20/1e6 * 15.0 = 0.0003
    cost = compute_cost(
        pricing,
        prompt_tokens=2000,
        cached_tokens=1800,
        completion_tokens=20,
    )
    assert cost == Decimal("0.001440")


def test_cost_computation_without_cache_rate_uses_input_rate() -> None:
    """``cached_per_1m_usd`` が None の場合、cached 分にも input 単価を適用する(割引なし)。"""
    pricing = _pricing(cached=None)
    # prompt 1000(うち 500 cached)、output 100。
    # input 全量 = 1000/1e6 * 3.0 = 0.003
    # output     = 100/1e6 * 15.0 = 0.0015
    cost = compute_cost(
        pricing,
        prompt_tokens=1000,
        cached_tokens=500,
        completion_tokens=100,
    )
    assert cost == Decimal("0.004500")


def test_cost_computation_rejects_cached_exceeding_prompt() -> None:
    """cached_tokens > prompt_tokens は境界違反として ValueError(入力検証)。"""
    pricing = _pricing(cached=Decimal("0.3000"))
    with pytest.raises(ValueError, match="cached_tokens"):
        compute_cost(
            pricing,
            prompt_tokens=100,
            cached_tokens=200,
            completion_tokens=10,
        )


@respx.mock
async def test_provider_usage_feeds_cost_computation_end_to_end() -> None:
    """provider が返す usage(cached 込み)を pricing 層に渡して cost を確定する結合経路。"""
    respx.post(f"{_ANTHROPIC_BASE}{_ANTHROPIC_MESSAGES}").mock(
        return_value=httpx.Response(
            200, json=_anthropic_body(bpm=90, input_tokens=2000, cache_read=1800)
        )
    )

    resp = await _anthropic_provider().generate(_request())
    pricing = _pricing(cached=Decimal("0.3000"))

    cost = compute_cost(
        pricing,
        prompt_tokens=resp.usage.prompt_tokens,
        cached_tokens=resp.usage.cached_tokens,
        completion_tokens=resp.usage.completion_tokens,
    )
    # input 200/1e6*3 + cached 1800/1e6*0.3 + output 20/1e6*15
    assert cost == Decimal("0.001440")
