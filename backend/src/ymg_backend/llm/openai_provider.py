"""OpenAI provider 実装 (ADR-0018 / ADR-0019 / ADR-0024 / ADR-0028 / ADR-0033).

公式 ``openai`` SDK の **Responses API** + JSON Schema structured output
(``responses.parse(text_format=...)``、strict)を使い、:class:`LlmProvider` を実装する。

設計方針:

- **構造化出力**: ``response_model`` を ``text_format`` に渡し SDK の strict JSON Schema 変換に委ねる。
  パース済みインスタンスは ``ParsedResponse.output_parsed`` で受け取る (ADR-0018)。
- **Prompt caching**: OpenAI Responses API は prefix 1024 token 以上で **自動** キャッシュが効く
  (ADR-0033)。``LlmMessage.cacheable=True`` のメッセージが含まれる場合は
  ``prompt_cache_key`` を付与してキャッシュルーティングを安定させる。hit 量は
  ``usage.input_tokens_details.cached_tokens`` から取得する。
- **コスト**: ``cost_usd`` は本 provider では算出しない。``model_pricing`` 参照 (T032 pricing) と
  ``usage_log`` 書き込み (T033 usage_writer) のラッパが後段で確定させるため ``0.0`` を返す (ADR-0024)。
- **エラー分類**: OpenAI SDK 例外を :class:`LlmError`(``ErrorCategory`` 付き、ADR-0028)へマップする。

本 provider はネットワーク呼び出しの実行のみを担い、リトライ・コスト集計・usage 永続化は
上位レイヤ(factory / usage_writer)に委譲する薄いラッパに保つ (ADR-0019「差分を局所化」)。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

import openai
from openai import AsyncOpenAI, omit
from pydantic import BaseModel, ValidationError

from ymg_backend.domain.errors import ErrorCategory
from ymg_backend.llm.base import (
    LlmError,
    LlmMessage,
    LlmProvider,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)

if TYPE_CHECKING:
    from openai.types.responses import ParsedResponse

# 本 provider が扱う既定モデル一覧 (ADR-0019 / contract: llm-provider-interface.md)。
# 単価表 (model_pricing) と独立した「呼び出し可能」判定のための静的リスト。
_SUPPORTED_MODELS: Final[tuple[str, ...]] = (
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4o",
    "gpt-4o-mini",
    "o3",
    "o4-mini",
)

# Pydantic validation 失敗時のリトライ上限 (contract §1: 最大 2 回)。
_MAX_VALIDATION_RETRIES: Final[int] = 2

# リトライ毎の temperature 低下量(再現性を上げて構造化失敗を抑える、contract §1)。
_TEMPERATURE_DECAY: Final[float] = 0.2


def _err_category_value(category: ErrorCategory) -> str:
    """``ErrorCategory`` enum を base.LlmError が受ける Literal 文字列へ変換する。

    ``ErrorCategory`` は ``StrEnum`` で値が ``"transient"`` 等の Literal と一致するため、
    ``str(...)`` で安全に渡せる。
    """
    return str(category)


def _make_error(
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool,
    original: Exception | None = None,
) -> LlmError:
    """``ErrorCategory``(ADR-0028)付きの :class:`LlmError` を生成する。"""
    err = LlmError(
        category=_err_category_value(category),  # type: ignore[arg-type]
        message=message,
        retryable=retryable,
    )
    if original is not None:
        err.__cause__ = original
    return err


def _map_openai_error(exc: openai.OpenAIError) -> LlmError:
    """OpenAI SDK 例外を :class:`LlmError`(ADR-0028 のカテゴリ付き)へマップする。

    分類は contract: llm-provider-interface.md §4 のエラー分類表に従う。

    - 429 / 5xx / timeout / connection → ``transient`` (retryable=True)
    - 認証エラー (401) → ``fatal`` (retryable=False)
    - 権限エラー (403) → ``recoverable`` (retryable=False)
    - quota / 上記以外の 4xx → ``recoverable`` (retryable=False、安全側)
    """
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return _make_error(
            ErrorCategory.TRANSIENT,
            f"OpenAI への接続に失敗しました: {exc}",
            retryable=True,
            original=exc,
        )
    if isinstance(exc, openai.RateLimitError):
        return _make_error(
            ErrorCategory.TRANSIENT,
            f"OpenAI rate limit に達しました (429): {exc}",
            retryable=True,
            original=exc,
        )
    if isinstance(exc, openai.AuthenticationError):
        return _make_error(
            ErrorCategory.FATAL,
            f"OpenAI 認証に失敗しました (401): {exc}",
            retryable=False,
            original=exc,
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return _make_error(
            ErrorCategory.RECOVERABLE,
            f"OpenAI 権限エラー (403): {exc}",
            retryable=False,
            original=exc,
        )
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status >= 500:
            return _make_error(
                ErrorCategory.TRANSIENT,
                f"OpenAI サーバエラー ({status}): {exc}",
                retryable=True,
                original=exc,
            )
        # その他 4xx(quota 枯渇含む)は安全側で recoverable 扱い (ADR-0028 DEFAULT)。
        return _make_error(
            ErrorCategory.RECOVERABLE,
            f"OpenAI API エラー ({status}): {exc}",
            retryable=False,
            original=exc,
        )
    # 上記以外の OpenAIError(SDK 内部エラー等)も安全側 recoverable。
    return _make_error(
        ErrorCategory.RECOVERABLE,
        f"OpenAI 呼び出しでエラーが発生しました: {exc}",
        retryable=False,
        original=exc,
    )


def _split_messages(messages: list[LlmMessage]) -> tuple[str | None, list[dict[str, str]]]:
    """メッセージ列を ``instructions``(system 連結)と ``input``(会話)に分割する。

    Responses API は system プロンプトを ``instructions`` で受け取る。先頭に固定 system を
    置くことで prefix の自動 prompt caching(ADR-0033、1024 token 以上)が効きやすくなる。
    複数 system メッセージは順序を保って改行連結する。
    """
    system_parts: list[str] = []
    conversation: list[dict[str, str]] = []
    for msg in messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        else:
            conversation.append({"role": msg.role, "content": msg.content})
    instructions = "\n\n".join(system_parts) if system_parts else None
    return instructions, conversation


def _build_prompt_cache_key[T: BaseModel](req: LlmRequest[T]) -> str | None:
    """``cacheable`` メッセージがある場合に prompt_cache_key を組み立てる (ADR-0033)。

    OpenAI のキャッシュは自動だが、``prompt_cache_key`` を渡すと同一 prefix のリクエストが
    同じキャッシュにルーティングされやすくなる。context_type / prompt_version から安定キーを作る。
    cacheable メッセージが無ければ ``None``(キー指定なし)。
    """
    if not any(m.cacheable for m in req.messages):
        return None
    parts = [p for p in (req.context_type, req.prompt_version) if p]
    return ":".join(parts) if parts else None


def _to_usage[T: BaseModel](response: ParsedResponse[T], duration_ms: int) -> LlmUsage:
    """``ParsedResponse.usage`` を :class:`LlmUsage` へ変換する (ADR-0024)。

    ``cost_usd`` はここでは確定せず ``0.0``。後段の pricing/usage_writer が
    ``model_pricing`` を参照して算出する(本 provider は DB を参照しない)。
    """
    usage = response.usage
    if usage is None:
        return LlmUsage(
            prompt_tokens=0,
            cached_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            duration_ms=duration_ms,
        )
    cached = usage.input_tokens_details.cached_tokens if usage.input_tokens_details else 0
    return LlmUsage(
        prompt_tokens=usage.input_tokens,
        cached_tokens=cached,
        completion_tokens=usage.output_tokens,
        cost_usd=0.0,
        duration_ms=duration_ms,
    )


class OpenAIProvider(LlmProvider):
    """OpenAI Responses API による :class:`LlmProvider` 実装 (ADR-0019)。

    ``auth_mode = "api_key"``(従量課金、ToS クリア、デフォルト)を主用途とする。
    ``codex_oauth`` 用に ``base_url`` を差し替えられるよう、トークンと base_url を
    コンストラクタで受け取る(OAuth トークン取得自体は呼び出し側 / factory の責務)。

    秘密情報(API key / OAuth トークン)は本クラス内に保持せず、生成済みの
    :class:`openai.AsyncOpenAI` クライアントを受け取る設計とし、漏洩面を最小化する。
    """

    def __init__(self, client: AsyncOpenAI, *, model: str) -> None:
        """provider を初期化する。

        Args:
            client: 認証済みの :class:`openai.AsyncOpenAI`(api_key / base_url は呼び出し側で設定)。
            model: 既定で使用するモデル ID(``supported_models`` のいずれか)。
        """
        self._client: Final[AsyncOpenAI] = client
        self._model: Final[str] = model

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        *,
        model: str,
        base_url: str | None = None,
    ) -> OpenAIProvider:
        """API key(または codex_oauth トークン)から provider を生成するファクトリ。

        Args:
            api_key: ``OPENAI_API_KEY`` もしくは Codex OAuth アクセストークン。
            model: 既定モデル ID。
            base_url: ``codex_oauth`` 時に差し替える base URL(api_key 時は ``None``)。
        """
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return cls(client, model=model)

    def supported_models(self) -> list[str]:
        """この provider が扱えるモデル ID の一覧。"""
        return list(_SUPPORTED_MODELS)

    def supports_caching(self) -> bool:
        """OpenAI Responses API は prompt caching に対応する (ADR-0033)。"""
        return True

    async def health_check(self) -> bool:
        """provider が到達可能かを軽量に確認する(models.list を叩く)。

        ネットワーク到達性のみを判定し、例外時は ``False`` を返す(送出しない)。
        """
        try:
            await self._client.models.list()
        except openai.OpenAIError:
            return False
        return True

    async def generate[T: BaseModel](self, req: LlmRequest[T]) -> LlmResponse[T]:
        """構造化出力を要求して OpenAI を呼び出し、パース済み応答を返す (ADR-0018)。

        Pydantic validation 失敗時は temperature を下げて最大 :data:`_MAX_VALIDATION_RETRIES`
        回までリトライし、全失敗で ``recoverable``(retryable=False)を送出する (contract §1)。
        OpenAI SDK 例外は :class:`LlmError`(ADR-0028 カテゴリ付き)へマップする。
        """
        instructions, conversation = _split_messages(req.messages)
        cache_key = _build_prompt_cache_key(req)
        last_validation_error: ValidationError | None = None

        for attempt in range(_MAX_VALIDATION_RETRIES + 1):
            temperature = max(0.0, req.temperature - _TEMPERATURE_DECAY * attempt)
            started = time.monotonic()
            try:
                response = await self._client.responses.parse(
                    model=self._model,
                    input=conversation,  # type: ignore[arg-type]
                    instructions=instructions,
                    text_format=req.response_model,
                    temperature=temperature,
                    max_output_tokens=req.max_tokens if req.max_tokens is not None else omit,
                    prompt_cache_key=cache_key if cache_key is not None else omit,
                    timeout=req.timeout_sec,
                )
            except ValidationError as exc:
                # SDK は text_format に対する構造化出力検証を responses.parse 内部で行い、
                # 失敗時に ValidationError を送出する(model_validate の前段)。
                # これは temperature 低下リトライの対象 (contract §1)。
                last_validation_error = exc
                continue
            except openai.OpenAIError as exc:
                raise _map_openai_error(exc) from exc

            duration_ms = int((time.monotonic() - started) * 1000)
            self._raise_if_incomplete(response)

            parsed = response.output_parsed
            if parsed is None:
                # strict schema 下でも output_parsed が無い = 構造化失敗。リトライ対象。
                last_validation_error = None
                continue

            try:
                # output_parsed は SDK が text_format で検証済みだが、契約上ここでも保証する。
                validated = req.response_model.model_validate(parsed.model_dump())
            except ValidationError as exc:
                last_validation_error = exc
                continue

            return LlmResponse[T](
                parsed=validated,
                raw_text=response.output_text,
                usage=_to_usage(response, duration_ms),
                provider="openai",
                model=self._model,
                finish_reason=response.status or "unknown",
            )

        # リトライ全失敗 → recoverable(計画 LLM 側で fallback、retryable=False)。
        detail = f": {last_validation_error}" if last_validation_error is not None else "(空出力)"
        raise _make_error(
            ErrorCategory.RECOVERABLE,
            f"OpenAI 構造化出力が {_MAX_VALIDATION_RETRIES + 1} 回連続で検証に失敗しました{detail}",
            retryable=False,
            original=last_validation_error,
        )

    @staticmethod
    def _raise_if_incomplete[T: BaseModel](response: ParsedResponse[T]) -> None:
        """``max_output_tokens`` 超過を ``quality``(retryable=True)として送出する (contract §4)。

        ``content_filter`` 等それ以外の incomplete 理由は ``recoverable`` 扱い。
        """
        details = response.incomplete_details
        if details is None or details.reason is None:
            return
        if details.reason == "max_output_tokens":
            raise _make_error(
                ErrorCategory.QUALITY,
                "OpenAI 応答が max_output_tokens で打ち切られました(max_tokens を増やして再試行)",
                retryable=True,
            )
        raise _make_error(
            ErrorCategory.RECOVERABLE,
            f"OpenAI 応答が不完全です (reason={details.reason})",
            retryable=False,
        )
