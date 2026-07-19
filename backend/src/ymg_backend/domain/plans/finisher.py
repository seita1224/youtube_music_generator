"""仕上げ LLM サービス(``{{自由文}}`` directive の展開、 ADR-0032 / ADR-0017)。

directive parser(:mod:`ymg_backend.domain.directive.parser`)が抽出した
:class:`~ymg_backend.domain.directive.parser.GenerativeSlot`(``{{12字以内のサブタイトル}}``
等の自由文スロット)を、 安価なモデルで短い文字列に展開する仕上げ層。 計画判断
(:class:`~ymg_backend.domain.plans.schemas.DailyPlan` の生成)は別の重い LLM 呼び出し
(planner)が担い、 本サービスは「与えられた指示文に沿った短い文字列の生成だけ」を担う
(``prompts/finisher/title_v1.md`` / ``description_v1.md`` の責務分担と一致)。

公開 I/F は 2 つ:

- :meth:`LlmFinisher.render` — ``llm/base.py`` の :class:`FinisherClient` 契約。 単一 directive
  (``FinisherRequest``)を 1 文字列に展開する。 title / description renderer が
  ``GenerativeSlot`` ごとに呼ぶ。
- :meth:`LlmFinisher.finish` — :class:`ParsedTemplate` 内の全 ``GenerativeSlot`` を
  **1 回の LLM 呼び出し**でバッチ展開し ``slot_id`` → 生成文の dict を返す
  (parser docstring の「複数生成ディレクティブを 1 回の LLM 呼び出しでバッチ展開」設計)。
  戻り値は :func:`~ymg_backend.domain.directive.parser.render_template` の ``generated`` に
  そのまま渡せる。

コスト最適化(ADR-0024):
    finisher は planner と同じ ``provider`` を使い、 ``max_tokens`` を絞って安価に保つ
    (運用早見表「安価用途(finisher)も同 provider を使い max_tokens を絞る」)。
    既定上限は :data:`_DEFAULT_MAX_TOKENS`。 ``render`` は ``FinisherRequest.max_chars`` から
    必要トークン量を見積もって上限を決める。

エラー分類(ADR-0028):
    構造化出力のパース失敗やスロット欠落は :class:`~ymg_backend.domain.errors.QualityError`
    (該当部分のみスキップしデフォルトで続行できる品質低下)として送出する。 provider が
    投げる :class:`~ymg_backend.llm.base.LlmError`(transient / recoverable 等)はそのまま
    伝播させ、 オーケストレータのリトライ / カテゴリ分岐に委ねる。
"""

from __future__ import annotations

from typing import Final

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from ymg_backend.domain.directive.parser import GenerativeSlot, ParsedTemplate
from ymg_backend.domain.errors import QualityError
from ymg_backend.domain.prompts.loader import PromptLoader
from ymg_backend.llm.base import (
    FinisherClient,
    FinisherRequest,
    FinisherResponse,
    LlmMessage,
    LlmProvider,
    LlmRequest,
)

# finisher の context_type(usage_log / LlmRequest.context_type、 ADR-0024)。
_CONTEXT_TYPE: Final[str] = "finisher"

# 単一 directive を展開する際の安価モデル向けトークン上限(ADR-0024)。
# 仕上げは短い文字列生成のため、 max_chars から余裕を見て上限を決める。
_DEFAULT_MAX_TOKENS: Final[int] = 256
# 1 文字あたりの概算トークン数(日本語 / 絵文字混在の安全側見積り)。
# CJK は 1 文字 ≒ 1〜2 token のため、 構造化出力の JSON 装飾分も加味して 2 倍見積もる。
_TOKENS_PER_CHAR: Final[int] = 2
# 構造化出力(JSON object 装飾・キー)に要する固定トークンの下駄。
_STRUCTURE_TOKEN_OVERHEAD: Final[int] = 64
# 仕上げは決定論寄りに振る(過度な発散を避ける、 prompt の「静かで上質なトーン」)。
_FINISHER_TEMPERATURE: Final[float] = 0.6

# finisher の system prompt 既定領域 / 名前(``prompts/finisher/title_v1.md`` 等)。
_PROMPT_AREA: Final[str] = "finisher"
_DEFAULT_PROMPT_NAME: Final[str] = "title"


class _SingleSlotOutput(BaseModel):
    """単一 directive 展開(:meth:`LlmFinisher.render`)の構造化出力。

    安価モデルの構造化出力を 1 フィールドに固定し、 余計なキーの混入を防ぐ。
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(description="instruction に沿って生成した制約付きの文字列")


class _SlotBatchOutput(BaseModel):
    """複数 directive 一括展開(:meth:`LlmFinisher.finish`)の構造化出力。

    ``slots`` は ``slot_id`` → 生成文字列の dict。 スロット数によらずスキーマが固定なので、
    1 テンプレ内の任意個の ``GenerativeSlot`` を 1 回の呼び出しで展開できる。
    """

    model_config = ConfigDict(frozen=True)

    slots: dict[str, str] = Field(
        default_factory=dict,
        description="directive の slot_id をキー、 生成文字列を値とする dict",
    )


def _budget_for_chars(max_chars: int) -> int:
    """``max_chars`` から構造化出力の ``max_tokens`` 上限を見積もる。

    文字数上限に概算トークン換算と JSON 装飾分の下駄を加え、 既定上限で頭打ちにする。
    安価モデルでも指示文字数を出し切れる最小限の予算に収める(ADR-0024)。
    """
    if max_chars <= 0:
        return _DEFAULT_MAX_TOKENS
    estimate = max_chars * _TOKENS_PER_CHAR + _STRUCTURE_TOKEN_OVERHEAD
    return min(estimate, _DEFAULT_MAX_TOKENS)


def _format_context(context: dict[str, str]) -> str:
    """context dict を ``key: value`` 改行区切りの安定テキストに整形する。

    キー順を固定(ソート)してプロンプトを決定論的にし、 prompt caching の前方一致を
    壊しにくくする(ADR-0033)。 空 dict なら ``"(なし)"`` を返す。
    """
    if not context:
        return "(なし)"
    return "\n".join(f"- {key}: {context[key]}" for key in sorted(context))


class LlmFinisher(FinisherClient):
    """安価モデルで自由文 directive を展開する仕上げサービス(ADR-0032)。

    ``provider`` は planner と同じ :class:`LlmProvider` を共有し、 ``max_tokens`` を絞って
    安価に保つ。 system prompt は :class:`PromptLoader` 経由で ``finisher/<name>_v<N>.md`` を
    解決する(既定は ``title``)。 description の仕上げに切り替える場合は ``prompt_name``
    を ``"description"`` にしたインスタンスを使う。

    ステートレス(session を持たず、 DB 永続化はオーケストレータ責務)で、 不変フィールド
    のみを保持する。
    """

    def __init__(
        self,
        provider: LlmProvider,
        *,
        prompt_loader: PromptLoader | None = None,
        prompt_name: str = _DEFAULT_PROMPT_NAME,
        prompt_version: int | None = None,
    ) -> None:
        """仕上げサービスを構築する。

        Args:
            provider: 共有 :class:`LlmProvider`(安価モデルを ``max_tokens`` 制限で使う)。
            prompt_loader: prompt 解決ローダ。 省略時は既定の ``backend/prompts`` を使う。
            prompt_name: ``finisher/<prompt_name>_v<N>.md`` の名前(``"title"`` / ``"description"``)。
            prompt_version: 明示バージョン。 ``None`` なら最新版を解決する。
        """
        self._provider: Final[LlmProvider] = provider
        self._prompt_loader: Final[PromptLoader] = prompt_loader or PromptLoader()
        self._prompt_name: Final[str] = prompt_name
        self._prompt_version: Final[int | None] = prompt_version

    async def render(self, req: FinisherRequest) -> FinisherResponse:
        """単一 directive を制約付きの自由文に展開する(``FinisherClient`` 契約)。

        ``llm/base.py`` の :meth:`FinisherClient.render` 実装。 title / description renderer が
        ``GenerativeSlot`` ごとに呼ぶ。 ``instruction`` と ``context`` を system prompt 付きで
        provider に渡し、 1 フィールドの構造化出力(:class:`_SingleSlotOutput`)を得る。

        Args:
            req: instruction(指示文)/ context(genre / mood 等)/ max_chars(文字数上限)。

        Returns:
            :class:`FinisherResponse`(生成文字列 + :class:`LlmUsage`)。

        Raises:
            QualityError: 構造化出力が空 / 文字数上限超過などで利用不能な場合。
            ~ymg_backend.llm.base.LlmError: provider 側のエラー(transient / recoverable 等)。
        """
        system_text = self._load_system_prompt()
        user_text = self._build_single_user_prompt(req)
        request: LlmRequest[_SingleSlotOutput] = LlmRequest(
            messages=[
                LlmMessage(role="system", content=system_text, cacheable=True),
                LlmMessage(role="user", content=user_text),
            ],
            response_model=_SingleSlotOutput,
            temperature=_FINISHER_TEMPERATURE,
            max_tokens=_budget_for_chars(req.max_chars),
            context_type=_CONTEXT_TYPE,
            prompt_version=self._prompt_ref(),
        )
        response = await self._provider.generate(request)
        text = response.parsed.text.strip()
        self._validate_single(text, req)
        logger.debug(
            "finisher.render slot expanded",
            chars=len(text),
            max_chars=req.max_chars,
            cost_usd=response.usage.cost_usd,
        )
        return FinisherResponse(text=text, usage=response.usage)

    async def finish(
        self,
        parsed: ParsedTemplate,
        context: dict[str, str],
    ) -> dict[str, str]:
        """テンプレ内の全 ``GenerativeSlot`` を 1 回の呼び出しで展開する。

        parser docstring の「複数生成ディレクティブを 1 回の LLM 呼び出しでバッチ展開」設計に
        従い、 ``parsed.generative_slots`` をまとめて provider に渡し、 ``slot_id`` →
        生成文字列の dict を得る。 戻り値は :func:`render_template` の ``generated`` 引数に
        そのまま渡せる。

        生成スロットが無い場合は LLM を呼ばず空 dict を返す(無駄なコストを避ける)。

        Args:
            parsed: :func:`parse_template` の結果。
            context: 変数名 → 値の辞書(genre / mood / visual_direction 等)。 LLM の参考情報
                として整形して渡す。

        Returns:
            ``slot_id`` → 生成文字列の dict(全 ``GenerativeSlot`` 分を網羅)。

        Raises:
            QualityError: 一部スロットの生成結果が欠落していた場合。
            ~ymg_backend.llm.base.LlmError: provider 側のエラー。
        """
        slots = parsed.generative_slots
        if not slots:
            return {}
        system_text = self._load_system_prompt()
        user_text = self._build_batch_user_prompt(slots, context)
        max_chars_total = sum(_estimate_slot_chars(slot) for slot in slots)
        request: LlmRequest[_SlotBatchOutput] = LlmRequest(
            messages=[
                LlmMessage(role="system", content=system_text, cacheable=True),
                LlmMessage(role="user", content=user_text),
            ],
            response_model=_SlotBatchOutput,
            temperature=_FINISHER_TEMPERATURE,
            max_tokens=_budget_for_chars(max_chars_total),
            context_type=_CONTEXT_TYPE,
            prompt_version=self._prompt_ref(),
        )
        response = await self._provider.generate(request)
        generated = self._collect_batch(slots, response.parsed.slots)
        logger.debug(
            "finisher.finish batch expanded",
            slot_count=len(slots),
            cost_usd=response.usage.cost_usd,
        )
        return generated

    def _load_system_prompt(self) -> str:
        """設定された finisher prompt(``finisher/<name>_v<N>.md``)を解決する。"""
        resolved = self._prompt_loader.load(
            _PROMPT_AREA, self._prompt_name, version=self._prompt_version
        )
        return resolved.text

    def _prompt_ref(self) -> str:
        """``LlmRequest.prompt_version`` に記録するバージョン参照 ID(``finisher/title_v1``)。"""
        resolved = self._prompt_loader.load(
            _PROMPT_AREA, self._prompt_name, version=self._prompt_version
        )
        return resolved.ref

    @staticmethod
    def _build_single_user_prompt(req: FinisherRequest) -> str:
        """単一 directive 用の user メッセージを組み立てる。"""
        return (
            "## directive\n"
            f"{req.instruction}\n\n"
            "## 文字数上限\n"
            f"{req.max_chars} 文字以内\n\n"
            "## コンテキスト\n"
            f"{_format_context(req.context)}\n\n"
            "上記の directive を文字数上限内で展開し、 text フィールドに 1 文字列だけ返してください。"
        )

    @staticmethod
    def _build_batch_user_prompt(
        slots: tuple[GenerativeSlot, ...],
        context: dict[str, str],
    ) -> str:
        """複数 directive 一括展開用の user メッセージを組み立てる。

        各スロットを ``slot_id`` 付きで列挙し、 LLM に slots dict のキーとして使わせる。
        """
        lines = [f"- {slot.slot_id}: {slot.instruction}" for slot in slots]
        directive_block = "\n".join(lines)
        return (
            "## directive 一覧(slot_id: 指示)\n"
            f"{directive_block}\n\n"
            "## コンテキスト\n"
            f"{_format_context(context)}\n\n"
            "各 directive を指示の文字数上限内で展開し、 slots フィールドに "
            "slot_id をキー、 生成文字列を値とする dict として **全 slot_id 分** 返してください。"
        )

    @staticmethod
    def _validate_single(text: str, req: FinisherRequest) -> None:
        """単一展開結果の空 / 文字数上限を検証する(ADR-0017 / ADR-0028 quality)。

        絵文字 directive など空文字を許す指示もあるため、 空文字自体は許容する
        (``title_v1.md`` の「不要なら空文字を返す」)。 上限超過のみ品質低下として弾く。
        """
        if req.max_chars > 0 and len(text) > req.max_chars:
            raise QualityError(
                "finisher output exceeds max_chars",
                context={
                    "max_chars": req.max_chars,
                    "actual_chars": len(text),
                    "instruction": req.instruction,
                },
            )

    @staticmethod
    def _collect_batch(
        slots: tuple[GenerativeSlot, ...],
        produced: dict[str, str],
    ) -> dict[str, str]:
        """バッチ生成結果から全 ``slot_id`` 分を回収し、 欠落を品質エラーにする。

        余計なキー(指示外の slot_id)は無視し、 必要な ``slot_id`` が揃っているかだけを
        検証する。 値は前後空白を除去する。
        """
        generated: dict[str, str] = {}
        missing: list[str] = []
        for slot in slots:
            value = produced.get(slot.slot_id)
            if value is None:
                missing.append(slot.slot_id)
                continue
            generated[slot.slot_id] = value.strip()
        if missing:
            raise QualityError(
                "finisher batch output is missing slots",
                context={"missing_slot_ids": missing},
            )
        return generated


def _estimate_slot_chars(slot: GenerativeSlot) -> int:
    """1 スロットの想定生成文字数を粗く見積もる(``max_tokens`` 予算用)。

    指示文に明示の文字数上限が無い前提で、 指示文長に比例した安全側の見積りを使う。
    指示文が長いほど生成も長くなりがちなため、 指示文長 + 一定の下駄を返す。
    """
    return len(slot.instruction) + 32


__all__ = [
    "FinisherRequest",
    "FinisherResponse",
    "LlmFinisher",
]
