"""LLM provider abstraction (contract: llm-provider-interface.md).

ADR-0018(structured output)+ ADR-0019(provider 実装)
+ ADR-0024(コストトラッキング)+ ADR-0028(エラー分類)+ ADR-0033(prompt caching)
を統合した抽象 interface。backend は本 interface 越しにのみ LLM を呼ぶ。
"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MessageRole = Literal["system", "user", "assistant"]
LlmProviderName = Literal["openai", "anthropic", "ollama"]
ErrorCategory = Literal["transient", "recoverable", "fatal", "compliance", "quality"]


class LlmMessage(BaseModel):
    """単一の会話メッセージ。

    cacheable=True の場合、provider 固有のキャッシュ機構を有効化する(ADR-0033):
    Anthropic は ``cache_control: {type: "ephemeral"}`` を付与、OpenAI は先頭固定部分の
    自動キャッシュに依存、Ollama は無視する。
    """

    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str
    cacheable: bool = False


class LlmRequest[T: BaseModel](BaseModel):
    """構造化出力(ADR-0018)を要求する LLM 呼び出しの入力。

    ``response_model`` は LLM 出力をパースする Pydantic class で、
    各 provider はこれを JSON Schema / tool input_schema に変換する。
    """

    model_config = ConfigDict(frozen=True)

    messages: list[LlmMessage]
    response_model: type[T]
    temperature: float = 0.7
    max_tokens: int | None = None
    timeout_sec: int = 60
    context_type: str | None = None  # 'planner' / 'finisher' / 'analyzer'
    context_id: str | None = None  # plan_id / post_id
    prompt_version: str | None = None  # 'planner/system_v1'


class LlmUsage(BaseModel):
    """1 回の LLM 呼び出しのトークン使用量とコスト(ADR-0024)。

    ``cost_usd`` は ``model_pricing`` から ``(provider, model, effective_from)`` を解決して
    算出する。prompt caching hit は ``cached_per_1m_usd`` で割引価格を適用する。
    """

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int
    cached_tokens: int = 0  # prompt caching hit 量
    completion_tokens: int
    cost_usd: float
    duration_ms: int


class LlmResponse[T: BaseModel](BaseModel):
    """構造化出力を伴う LLM 応答。``parsed`` は ``LlmRequest.response_model`` のインスタンス。"""

    model_config = ConfigDict(frozen=True)

    parsed: T
    raw_text: str
    usage: LlmUsage
    provider: LlmProviderName
    model: str
    finish_reason: str


class LlmError(Exception):
    """provider 実装が投げる例外。category は ADR-0028 のエラー分類に従う。"""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        retryable: bool,
    ) -> None:
        self.category: ErrorCategory = category
        self.message: str = message
        self.retryable: bool = retryable
        super().__init__(message)


class LlmProvider(ABC):
    """全 provider が実装する抽象 interface。

    エラーは :class:`LlmError` を ``ErrorCategory``(ADR-0028)付きで送出する。
    """

    @abstractmethod
    async def generate[T: BaseModel](self, req: LlmRequest[T]) -> LlmResponse[T]:
        """構造化出力を要求して LLM を呼び出し、パース済み応答を返す。"""
        ...

    @abstractmethod
    def supported_models(self) -> list[str]:
        """この provider が扱えるモデル ID の一覧。"""
        ...

    @abstractmethod
    def supports_caching(self) -> bool:
        """prompt caching に対応していれば True。"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """provider が到達可能かを確認する。"""
        ...


class FinisherRequest(BaseModel):
    """仕上げ LLM(``{{自由文}}`` 展開用)の入力(ADR-0032)。"""

    model_config = ConfigDict(frozen=True)

    instruction: str  # "12字以内の日本語サブタイトル" 等
    context: dict[str, str] = Field(default_factory=dict)  # {"genre": ..., "mood": ...}
    max_chars: int  # 文字数制約


class FinisherResponse(BaseModel):
    """仕上げ LLM の出力。"""

    model_config = ConfigDict(frozen=True)

    text: str
    usage: LlmUsage


class FinisherClient(ABC):
    """仕上げ LLM のショートカット(ADR-0032)。

    実装は :class:`LlmProvider` 経由で Haiku 級の安価なモデルを使う。
    """

    @abstractmethod
    async def render(self, req: FinisherRequest) -> FinisherResponse:
        """instruction と context から制約付きの自由文を生成する。"""
        ...
