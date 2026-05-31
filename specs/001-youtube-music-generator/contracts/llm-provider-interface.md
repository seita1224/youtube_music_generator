# Contract: LLMProvider Interface

> ADR-0018(structured output)+ ADR-0019(provider 実装)+ ADR-0024(コストトラッキング)+ ADR-0033(prompt caching)を統合した抽象 interface。 backend は本 interface 越しにのみ LLM を呼ぶ。

## Provider 一覧

| Provider | auth_mode | 用途 | キャッシュ |
|---|---|---|---|
| `openai`    | `api_key`     | 主に Sonnet/Opus 相当 (`gpt-4.1`, `o-series`) | Responses API 自動(1024 token 以上) |
| `openai`    | `codex_oauth` | 個人実験範囲(マネタイズ ToS グレー) | 同上 |
| `anthropic` | `api_key`     | Claude (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`) | `cache_control: ephemeral` |
| `ollama`    | —             | ローカルフォールバック (`qwen2.5:3b`, `llama3.2:3b`) | キャッシュなし |

**禁止組み合わせ:** `anthropic` + `subscription`(2026-02-19 公式禁止) → 起動時バリデーションで拒否。

## Python interface (Pydantic v2 想定)

```python
from abc import ABC, abstractmethod
from typing import TypeVar, Generic, Literal
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LlmMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str
    cacheable: bool = False  # True なら provider 固有のキャッシュ機構を有効化


class LlmRequest(BaseModel, Generic[T]):
    messages: list[LlmMessage]
    response_model: type[T]                   # Pydantic class for structured output
    temperature: float = 0.7
    max_tokens: int | None = None
    timeout_sec: int = 60
    context_type: str | None = None           # 'planner' / 'finisher' / 'analyzer'
    context_id: str | None = None             # plan_id / post_id
    prompt_version: str | None = None         # 'planner/system_v1'


class LlmUsage(BaseModel):
    prompt_tokens: int
    cached_tokens: int = 0
    completion_tokens: int
    cost_usd: float
    duration_ms: int


class LlmResponse(BaseModel, Generic[T]):
    parsed: T
    raw_text: str
    usage: LlmUsage
    provider: Literal["openai", "anthropic", "ollama"]
    model: str
    finish_reason: str


class LlmProvider(ABC):
    """All providers must implement this. Errors raise LlmError with ErrorCategory (ADR-0028)."""

    @abstractmethod
    async def generate(self, req: LlmRequest[T]) -> LlmResponse[T]: ...

    @abstractmethod
    def supported_models(self) -> list[str]: ...

    @abstractmethod
    def supports_caching(self) -> bool: ...

    @abstractmethod
    async def health_check(self) -> bool: ...


class LlmError(Exception):
    def __init__(
        self,
        category: Literal["transient", "recoverable", "fatal", "compliance", "quality"],
        message: str,
        retryable: bool,
    ):
        self.category = category
        self.message = message
        self.retryable = retryable
        super().__init__(message)
```

## 動作要件

### 1. Structured Output(ADR-0018)

- `response_model` (Pydantic class) を必ず受け取り、 LLM 出力を `parsed` フィールドで返す
- provider 固有の方法:
  - OpenAI: Responses API + `text.format = json_schema(strict=true)`
  - Anthropic: `tool_use` 経由 + Pydantic schema → tool input_schema
  - Ollama: 出力後に `model_validate_json` で検証(ネイティブ structured output 弱)
- Pydantic validation 失敗時:
  - 最大 2 回まで temperature を下げてリトライ(ADR-0028 `recoverable`)
  - リトライ全失敗で `LlmError(category="recoverable", retryable=False)` を投げる

### 2. Prompt Caching(ADR-0033)

- `LlmMessage.cacheable=True` のメッセージはプロバイダ固有のキャッシュ機構を有効化:
  - Anthropic: 該当 message に `cache_control: { type: "ephemeral" }` を付与
  - OpenAI: messages の先頭固定部分は Responses API のキャッシュが自動で効く(1024 token 以上)
  - Ollama: 無視
- `LlmUsage.cached_tokens` で hit 量を返す

### 3. コストトラッキング(ADR-0024)

- 全呼び出しは usage_log に provider / model / tokens / cost / context を記録
- `model_pricing` テーブルから `(provider, model, effective_from)` を解決して `cost_usd` を計算
- prompt caching hit は `cached_per_1m_usd`(あれば)で割引価格適用
- 月予算超過(50% / 80% / 100%)は別ジョブで監視し Slack 通知

### 4. エラー分類(ADR-0028)

| 状況 | category | retryable |
|---|---|---|
| API 429 / 5xx / timeout | `transient` | true |
| Pydantic validation 失敗(リトライ後も) | `recoverable` | false(計画 LLM 側で fallback) |
| 認証エラー / quota 完全枯渇 | `fatal` | false |
| Anthropic SDK にサブスク auth が渡された | `fatal` | false(起動時に拒否すべき) |
| max_tokens 超過 | `quality` | true (max_tokens 増やしてリトライ) |
| compliance トリガ(将来) | `compliance` | false |

### 5. プロンプトバージョン(ADR-0033)

- `prompt_version` を usage_log と `plans.llm_prompt_version` に保存
- system prompt は `prompts/planner/system_v1.md` のようにバージョン番号付きで管理、 backend は version 文字列を渡すだけ
- バージョンの切替は `app_state` または env で制御(将来 ADR)

## 仕上げ LLM のショートカット(ADR-0032)

仕上げ LLM(`{{自由文}}` 展開用)は別エントリで以下を提供。

```python
class FinisherRequest(BaseModel):
    instruction: str               # "12字以内の日本語サブタイト" 等
    context: dict[str, str]        # {"genre": ..., "mood": ...}
    max_chars: int                 # 制約


class FinisherResponse(BaseModel):
    text: str
    usage: LlmUsage


class FinisherClient:
    async def render(self, req: FinisherRequest) -> FinisherResponse: ...
```

実装は LlmProvider 経由で Haiku 級安価モデルを使う。

## テスト要件(ADR-0027 critical path)

- Pydantic validation 失敗時のリトライ動作(temperature 低下含む)
- prompt caching の hit / miss を usage に正しく反映
- `cost_usd` 計算が `model_pricing` と一致
- Anthropic + `subscription` 起動拒否
- LlmError の category がすべて分岐ハンドラで処理される

`backend/tests/critical/test_llm_provider.py` で provider 別 mock(respx 等)を使って結合テスト。

## 関連

- ADR-0002, ADR-0018, ADR-0019, ADR-0024, ADR-0028, ADR-0032, ADR-0033
- [backend-api.yaml](./backend-api.yaml) `/llm/*` endpoints
- [data-model.md](../data-model.md) `usage_log` / `model_pricing`
