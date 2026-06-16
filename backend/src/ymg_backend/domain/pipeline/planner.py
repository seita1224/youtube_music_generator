"""日次計画 (DailyPlan) 生成サービス (US1 内部契約 ``domain/pipeline/planner``)。

共有契約 (US1) の :class:`Planner` を提供する。 計画生成の実体は
:mod:`ymg_backend.domain.plans.planner` の :class:`PlanGenerator` に既に実装済みで、
本モジュールはその公開シグネチャ (``generate_daily_plan`` / ``create_daily_plan``) を
**契約上のクラス名 / 配置 (``pipeline.planner.Planner``)** で提供する薄い別名層である。

なぜ別名にするか:

- 共有契約は重い計画 LLM を ``Planner`` (``domain/pipeline/planner.py``) として、
  directive 仕上げの軽い LLM を ``LlmFinisher`` (``domain/pipeline/finisher.py``) として
  参照する。 一方、 計画ロジックは ``domain/plans/`` パッケージ (``finisher.py`` と同居) に
  ``PlanGenerator`` として実装された。
- critical テスト (``tests/critical/test_planner_schema.py``) と統合テスト
  (``tests/integration/test_daily_cycle_pipeline.py``) はいずれも
  ``ymg_backend.domain.pipeline.planner.Planner`` を import する。 そのため本モジュールで
  ``Planner = PlanGenerator`` を公開し、 実装の二重化を避けつつ契約を満たす。

``Planner.__init__(provider, prompt_loader)`` は契約の位置引数 (provider, prompt_loader) を
そのまま受ける。 ``PlanGenerator.__init__`` は ``prompt_loader`` をキーワード専用にしているため、
位置・キーワードの両呼び出しに対応する薄いサブクラスを定義する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ymg_backend.domain.plans.planner import MetricSnapshot, PlanGenerator

if TYPE_CHECKING:
    from ymg_backend.domain.prompts.loader import PromptLoader
    from ymg_backend.llm.base import LlmProvider


class Planner(PlanGenerator):
    """計画生成サービス (契約クラス名)。 実装は :class:`PlanGenerator` を継承する。

    契約の ``Planner(provider, prompt_loader)`` を位置引数で受けられるよう、
    ``prompt_loader`` を第 2 位置引数として受理する薄いコンストラクタを足すだけ
    (それ以外の生成ロジック・永続化・辞書照合は :class:`PlanGenerator` に委譲)。
    """

    def __init__(
        self,
        provider: LlmProvider,
        prompt_loader: PromptLoader | None = None,
    ) -> None:
        """planner を構築する。

        Args:
            provider: 計画生成に使う :class:`LlmProvider` (重い判断用の主力モデル)。
            prompt_loader: prompt 解決ローダ。 省略時は既定の ``backend/prompts``。
        """
        super().__init__(provider, prompt_loader=prompt_loader)


__all__ = [
    "MetricSnapshot",
    "Planner",
]
