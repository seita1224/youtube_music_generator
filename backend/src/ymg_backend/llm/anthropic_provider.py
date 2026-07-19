"""Anthropic provider 実装 (ADR-0018 / ADR-0019 / ADR-0028 / ADR-0033)。

公式 ``anthropic`` SDK の Messages API を用い、 構造化出力は ``tool_use`` 経由で実現する
(Pydantic ``response_model`` → tool ``input_schema``)。 prompt caching は
``cache_control: {type: "ephemeral"}`` を system block / tool に付与して有効化する。

重要 (FR-022 / ADR-0019): Anthropic SDK のサブスクリプション認証は 2026-02-19 に公式禁止。
本 provider は ``auth_mode`` が ``api_key`` 以外 (特に ``subscription``) の場合、 生成時に
``LlmError(category="fatal", retryable=False)`` を送出して拒否する。 実際の起動時拒否は
factory (T034) が担うが、 provider 側でもサブスク認証を受け付けない不変条件を保証する。

cost_usd は本層では計算せず ``0.0`` を返す。 単価解決と ``cost_usd`` 計算は pricing /
usage_writer 層 (T032 / T033, ADR-0024) の責務である。
"""

from __future__ import annotations

import json
import time
from typing import Final, Literal

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)
from anthropic.types import (
    CacheControlEphemeralParam,
    ContentBlock,
    Message,
    MessageParam,
    TextBlockParam,
    ToolChoiceToolParam,
    ToolParam,
    ToolUseBlock,
)
from pydantic import BaseModel, ValidationError

from ymg_backend.llm.base import (
    LlmError,
    LlmMessage,
    LlmProvider,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)

# api_key のみ許可 (ADR-0019)。 subscription / その他は拒否。
_ALLOWED_AUTH_MODE: Final[str] = "api_key"

# Pydantic validation 失敗時の最大リトライ回数 (ADR-0028 recoverable)。
_MAX_VALIDATION_RETRIES: Final[int] = 2

# structured output 用 tool の固定名。
_STRUCTURED_TOOL_NAME: Final[str] = "structured_output"

# リトライ時に temperature を下げる係数 (ADR-0018 「temperature を下げてリトライ」)。
_RETRY_TEMPERATURE_FACTOR: Final[float] = 0.5

# Anthropic がサポートするモデル ID (contracts/llm-provider-interface.md)。
_SUPPORTED_MODELS: Final[tuple[str, ...]] = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)

_DEFAULT_MAX_TOKENS: Final[int] = 4096

_EPHEMERAL: Final[CacheControlEphemeralParam] = CacheControlEphemeralParam(type="ephemeral")


class AnthropicProvider(LlmProvider):
    """Anthropic Messages API ベースの :class:`LlmProvider` 実装。

    Args:
        api_key: ``ANTHROPIC_API_KEY``。
        default_model: ``generate`` で使用するモデル ID。
        auth_mode: 認証方式。 ``api_key`` のみ許可し、 それ以外 (``subscription`` 等) は
            生成時に ``LlmError(fatal)`` を送出する (FR-022)。
        client: テスト用に注入する ``AsyncAnthropic``。 未指定なら ``api_key`` から生成する。

    Raises:
        LlmError: ``auth_mode`` が ``api_key`` 以外の場合 (category="fatal")。
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        auth_mode: str = _ALLOWED_AUTH_MODE,
        client: AsyncAnthropic | None = None,
    ) -> None:
        if auth_mode != _ALLOWED_AUTH_MODE:
            raise LlmError(
                category="fatal",
                message=(
                    "Anthropic は API key 認証のみサポートします "
                    f"(指定された auth_mode='{auth_mode}')。"
                    " サブスクリプション認証は 2026-02-19 に公式禁止されました (FR-022)。"
                ),
                retryable=False,
            )
        self._default_model: Final[str] = default_model
        self._client: Final[AsyncAnthropic] = client or AsyncAnthropic(api_key=api_key)

    async def generate[T: BaseModel](self, req: LlmRequest[T]) -> LlmResponse[T]:
        """tool_use 経由で構造化出力を取得し、 Pydantic でパースして返す。

        Pydantic validation 失敗時は temperature を下げて最大 2 回までリトライし、
        全失敗で ``LlmError(category="recoverable", retryable=False)`` を送出する (ADR-0028)。
        """
        system_blocks = self._build_system_blocks(req.messages)
        messages = self._build_messages(req.messages)
        tool = self._build_tool(req.response_model, cacheable=bool(system_blocks))
        tool_choice = ToolChoiceToolParam(type="tool", name=_STRUCTURED_TOOL_NAME)
        max_tokens = req.max_tokens or _DEFAULT_MAX_TOKENS

        last_error: ValidationError | None = None
        for attempt in range(_MAX_VALIDATION_RETRIES + 1):
            temperature = req.temperature * (_RETRY_TEMPERATURE_FACTOR**attempt)
            started = time.monotonic()
            message = await self._call_api(
                system_blocks=system_blocks,
                messages=messages,
                tool=tool,
                tool_choice=tool_choice,
                model=self._default_model,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_sec=req.timeout_sec,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            tool_block = self._extract_tool_use(message.content)
            raw_text = self._dump_json(tool_block.input)
            try:
                parsed = req.response_model.model_validate(tool_block.input)
            except ValidationError as exc:
                last_error = exc
                continue

            usage = LlmUsage(
                prompt_tokens=message.usage.input_tokens,
                cached_tokens=message.usage.cache_read_input_tokens or 0,
                completion_tokens=message.usage.output_tokens,
                cost_usd=0.0,
                duration_ms=duration_ms,
            )
            return LlmResponse[T](
                parsed=parsed,
                raw_text=raw_text,
                usage=usage,
                provider="anthropic",
                model=message.model,
                finish_reason=message.stop_reason or "end_turn",
            )

        raise LlmError(
            category="recoverable",
            message=(
                "Anthropic structured output が "
                f"{_MAX_VALIDATION_RETRIES + 1} 回連続で Pydantic validation に失敗しました: "
                f"{last_error}"
            ),
            retryable=False,
        )

    def supported_models(self) -> list[str]:
        """Anthropic がサポートするモデル ID の一覧。"""
        return list(_SUPPORTED_MODELS)

    def supports_caching(self) -> bool:
        """Anthropic は ``cache_control: ephemeral`` をサポートする。"""
        return True

    async def health_check(self) -> bool:
        """最小トークンの呼び出しで provider 到達性を確認する。"""
        try:
            await self._client.messages.create(
                model=self._default_model,
                max_tokens=1,
                messages=[MessageParam(role="user", content="ping")],
            )
        except (AuthenticationError, PermissionDeniedError):
            return False
        except APIStatusError:
            return False
        except (APIConnectionError, APITimeoutError):
            return False
        return True

    async def _call_api(
        self,
        *,
        system_blocks: list[TextBlockParam],
        messages: list[MessageParam],
        tool: ToolParam,
        tool_choice: ToolChoiceToolParam,
        model: str,
        max_tokens: int,
        temperature: float,
        timeout_sec: int,
    ) -> Message:
        """Messages API を呼び出し、 SDK 例外を ``LlmError`` (ADR-0028) に分類する。"""
        try:
            return await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_blocks,
                messages=messages,
                tools=[tool],
                tool_choice=tool_choice,
                timeout=timeout_sec,
            )
        except (AuthenticationError, PermissionDeniedError) as exc:
            raise LlmError(
                category="fatal",
                message=f"Anthropic 認証エラー: {exc}",
                retryable=False,
            ) from exc
        except (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError) as exc:
            raise LlmError(
                category="transient",
                message=f"Anthropic 一過性エラー: {exc}",
                retryable=True,
            ) from exc
        except APIStatusError as exc:
            raise LlmError(
                category="recoverable",
                message=f"Anthropic API エラー (status={exc.status_code}): {exc}",
                retryable=False,
            ) from exc

    @staticmethod
    def _build_system_blocks(messages: list[LlmMessage]) -> list[TextBlockParam]:
        """system role のメッセージを Anthropic の ``system`` block へ変換する。

        ``cacheable=True`` の block には ``cache_control: ephemeral`` を付与する (ADR-0033)。
        """
        blocks: list[TextBlockParam] = []
        for msg in messages:
            if msg.role != "system":
                continue
            if msg.cacheable:
                blocks.append(
                    TextBlockParam(type="text", text=msg.content, cache_control=_EPHEMERAL)
                )
            else:
                blocks.append(TextBlockParam(type="text", text=msg.content))
        return blocks

    @staticmethod
    def _build_messages(messages: list[LlmMessage]) -> list[MessageParam]:
        """user / assistant メッセージを Anthropic の ``messages`` へ変換する。

        ``cacheable=True`` の user/assistant message は content block 化して
        ``cache_control: ephemeral`` を付与する (ADR-0033)。
        """
        out: list[MessageParam] = []
        for msg in messages:
            if msg.role == "system":
                continue
            role: Literal["user", "assistant"] = msg.role
            if msg.cacheable:
                block = TextBlockParam(type="text", text=msg.content, cache_control=_EPHEMERAL)
                out.append(MessageParam(role=role, content=[block]))
            else:
                out.append(MessageParam(role=role, content=msg.content))
        return out

    @staticmethod
    def _build_tool(response_model: type[BaseModel], *, cacheable: bool) -> ToolParam:
        """``response_model`` の JSON Schema から structured output 用 tool を生成する。

        ``cacheable=True`` の場合、 tool 定義にも ``cache_control: ephemeral`` を付与して
        schema 部分のキャッシュ hit 率を高める (ADR-0033)。
        """
        schema = response_model.model_json_schema()
        description = (
            response_model.__doc__ or f"Return a structured {response_model.__name__} object."
        ).strip()
        tool = ToolParam(
            name=_STRUCTURED_TOOL_NAME,
            description=description,
            input_schema=schema,
        )
        if cacheable:
            tool["cache_control"] = _EPHEMERAL
        return tool

    @staticmethod
    def _extract_tool_use(content: list[ContentBlock]) -> ToolUseBlock:
        """応答 content から ``tool_use`` block を取り出す。

        tool_use が無い場合は ``quality`` カテゴリの ``LlmError`` を送出する
        (max_tokens 不足等でツール呼び出しが完結しなかったケース, ADR-0028)。
        """
        for block in content:
            if isinstance(block, ToolUseBlock) and block.name == _STRUCTURED_TOOL_NAME:
                return block
        raise LlmError(
            category="quality",
            message="Anthropic 応答に structured_output tool_use block が含まれていません。",
            retryable=True,
        )

    @staticmethod
    def _dump_json(value: object) -> str:
        """tool_use の input (dict) を JSON 文字列にダンプする (raw_text 用)。"""
        return json.dumps(value, ensure_ascii=False)
