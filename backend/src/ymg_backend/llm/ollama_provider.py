"""Ollama ローカル LLM provider 実装 (ADR-0019 / contract: llm-provider-interface.md)。

ローカルの Ollama デーモン (`ollama` Python SDK) を `LlmProvider` 抽象越しに利用する。
構造化出力は ADR-0018 の方針に従い、Ollama のネイティブ structured output が弱いため
chat API の出力を必ず ``model_validate_json`` で後検証する。 Pydantic 検証に失敗した場合は
temperature を下げて最大 2 回リトライし(contract §1)、全失敗で
``LlmError(category="recoverable", retryable=False)`` を送出する。

Ollama はローカル実行のため金銭コストは 0(ADR-0019)。 ``LlmUsage.cost_usd`` は常に 0.0 とし、
トークン数は chat レスポンスの ``prompt_eval_count`` / ``eval_count`` から取得する。
prompt caching は非対応(``supports_caching`` は False、``cacheable`` メッセージは無視)。
"""

from __future__ import annotations

from collections.abc import Sequence

from ollama import AsyncClient, RequestError, ResponseError
from ollama._types import ChatResponse
from pydantic import BaseModel, ValidationError

from ymg_backend.llm.base import (
    LlmError,
    LlmMessage,
    LlmProvider,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)

# ADR-0019: VRAM 制約 (ACE-Step + SDXL と共存) のため軽量モデルを推奨。
_SUPPORTED_MODELS: tuple[str, ...] = (
    "qwen2.5:3b",
    "llama3.2:3b",
    "gemma2:2b",
)

# Ollama はローカル実行のため金銭コスト 0 (ADR-0019)。
_COST_USD: float = 0.0

# Pydantic 検証失敗時に temperature を段階的に下げてリトライする系列 (contract §1)。
# 先頭は元 temperature を使い、以降のリトライで低温化する。最大 2 回リトライ = 計 3 試行。
_RETRY_TEMPERATURES: tuple[float, ...] = (0.3, 0.0)


def _to_ollama_messages(messages: Sequence[LlmMessage]) -> list[dict[str, str]]:
    """``LlmMessage`` を Ollama chat API のメッセージ形式へ変換する。

    ``cacheable`` は Ollama では無視する(ADR-0033: prompt caching 非対応)。
    """
    return [{"role": m.role, "content": m.content} for m in messages]


def _extract_content(response: ChatResponse) -> str:
    """chat レスポンスから assistant メッセージ本文を取り出す。

    本文が欠落している場合は ``LlmError(category="transient", retryable=True)`` を送出する
    (空応答は一過性とみなしてリトライ可能とする)。
    """
    content = response.message.content
    if not content:
        raise LlmError(
            category="transient",
            message="Ollama chat レスポンスに content が含まれていません。",
            retryable=True,
        )
    return content


def _build_usage(response: ChatResponse) -> LlmUsage:
    """chat レスポンスのメトリクスから ``LlmUsage`` を構築する。

    Ollama はナノ秒単位の ``total_duration`` を返すため、ミリ秒へ変換する。
    トークン数が未提供 (None) の場合は 0 として扱う。
    """
    prompt_tokens = response.prompt_eval_count or 0
    completion_tokens = response.eval_count or 0
    total_duration_ns = response.total_duration or 0
    return LlmUsage(
        prompt_tokens=prompt_tokens,
        cached_tokens=0,
        completion_tokens=completion_tokens,
        cost_usd=_COST_USD,
        duration_ms=total_duration_ns // 1_000_000,
    )


class OllamaProvider(LlmProvider):
    """Ollama バックエンドの ``LlmProvider`` 実装。

    ``client`` を外部から注入できるようにし、テスト時は mock client を渡せる。
    省略時は ``base_url`` から ``AsyncClient`` を生成する。
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        client: AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = client if client is not None else AsyncClient(host=base_url)

    async def generate[T: BaseModel](self, req: LlmRequest[T]) -> LlmResponse[T]:
        """構造化出力を要求して Ollama を呼び出し、検証済み応答を返す。

        Ollama の ``format`` に ``response_model`` の JSON Schema を渡して出力を誘導しつつ、
        返ってきた本文を ``model_validate_json`` で後検証する(ADR-0018)。 検証失敗時は
        temperature を下げて最大 2 回リトライし、全失敗で recoverable な ``LlmError`` を送出する。
        """
        ollama_messages = _to_ollama_messages(req.messages)
        schema = req.response_model.model_json_schema()
        temperatures = (req.temperature, *_RETRY_TEMPERATURES)

        last_validation_error: ValidationError | None = None
        raw_text = ""
        for temperature in temperatures:
            response = await self._chat(
                messages=ollama_messages,
                schema=schema,
                temperature=temperature,
                max_tokens=req.max_tokens,
            )
            raw_text = _extract_content(response)
            try:
                parsed = req.response_model.model_validate_json(raw_text)
            except ValidationError as exc:
                last_validation_error = exc
                continue

            return LlmResponse[T](
                parsed=parsed,
                raw_text=raw_text,
                usage=_build_usage(response),
                provider="ollama",
                model=response.model or self._model,
                finish_reason=response.done_reason or "stop",
            )

        raise LlmError(
            category="recoverable",
            message=(
                f"Ollama 出力が {len(temperatures)} 回とも "
                f"{req.response_model.__name__} の検証に失敗しました: {last_validation_error}"
            ),
            retryable=False,
        )

    async def _chat(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, object],
        temperature: float,
        max_tokens: int | None,
    ) -> ChatResponse:
        """Ollama chat API を 1 回呼び出す。 API 障害は transient な ``LlmError`` に変換する。"""
        options: dict[str, object] = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        try:
            return await self._client.chat(
                model=self._model,
                messages=messages,
                format=schema,
                options=options,
                stream=False,
            )
        except (ResponseError, RequestError, ConnectionError, TimeoutError, OSError) as exc:
            raise LlmError(
                category="transient",
                message=f"Ollama chat 呼び出しに失敗しました: {exc}",
                retryable=True,
            ) from exc

    def supported_models(self) -> list[str]:
        """この provider が扱えるモデル ID の一覧 (ADR-0019 推奨の軽量モデル)。"""
        return list(_SUPPORTED_MODELS)

    def supports_caching(self) -> bool:
        """Ollama は prompt caching 非対応のため常に False (ADR-0033)。"""
        return False

    async def health_check(self) -> bool:
        """Ollama デーモンへ到達可能かを ``list`` 呼び出しで確認する。

        到達不能・エラー時は ``False`` を返す(例外は送出しない)。
        """
        try:
            await self._client.list()
        except (ResponseError, RequestError, ConnectionError, TimeoutError, OSError):
            return False
        return True
