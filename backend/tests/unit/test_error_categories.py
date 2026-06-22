"""エラーカテゴリ分類器 (domain/errors/errors.py) の単体テスト (FR-111 / ADR-0028)。

FR-111: System MUST エラーを 5 カテゴリ
(transient / recoverable / fatal / compliance / quality) で分類処理する (ADR-0028)。

検証観点:

- 各カテゴリの例外型 → ``resolve_category`` が対応カテゴリを返す。
- 未知例外 (標準例外・サードパーティ例外) → ``DEFAULT_CATEGORY`` (recoverable)。
- ``notification_level_for`` の全カテゴリ写像 (ADR-0028 通知レベル)。
- ``slack_prefix_for`` の全カテゴリ写像 (レベル経由の prefix)。

末尾に FR-113 の仕様乖離を明示する xfail spec test を置く
(spec.md は fatal で scheduler 停止を要求するが、 実装はサイクル中断のみ)。
"""

from __future__ import annotations

import pytest

from ymg_backend.domain.errors.errors import (
    DEFAULT_CATEGORY,
    ComplianceError,
    ErrorCategory,
    FatalError,
    NotificationLevel,
    QualityError,
    RecoverableError,
    TransientError,
    YmgError,
    notification_level_for,
    resolve_category,
    slack_prefix_for,
)

# --- 5 カテゴリの例外型 → カテゴリ解決 ---------------------------------------------

_CATEGORY_CASES = [
    pytest.param(TransientError, ErrorCategory.TRANSIENT, id="transient"),
    pytest.param(RecoverableError, ErrorCategory.RECOVERABLE, id="recoverable"),
    pytest.param(FatalError, ErrorCategory.FATAL, id="fatal"),
    pytest.param(ComplianceError, ErrorCategory.COMPLIANCE, id="compliance"),
    pytest.param(QualityError, ErrorCategory.QUALITY, id="quality"),
]


@pytest.mark.fr("FR-111")
@pytest.mark.parametrize(("exc_type", "expected"), _CATEGORY_CASES)
def test_resolve_category_for_each_ymg_error(
    exc_type: type[YmgError], expected: ErrorCategory
) -> None:
    """FR-111: 各 YmgError サブクラスはクラス属性のカテゴリにそのまま解決される。"""
    exc = exc_type("boom")
    assert resolve_category(exc) is expected
    # クラス属性 category とも一致する (型とカテゴリの対応が崩れていない)。
    assert exc.category is expected


@pytest.mark.fr("FR-111")
@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ValueError("bad"), id="ValueError"),
        pytest.param(RuntimeError("oops"), id="RuntimeError"),
        pytest.param(KeyError("missing"), id="KeyError"),
        pytest.param(Exception("generic"), id="Exception"),
    ],
)
def test_resolve_category_unknown_falls_back_to_default(exc: BaseException) -> None:
    """FR-111: 未知例外は安全側の DEFAULT_CATEGORY (recoverable) に分類される。"""
    assert resolve_category(exc) is DEFAULT_CATEGORY
    assert DEFAULT_CATEGORY is ErrorCategory.RECOVERABLE


# --- カテゴリ → 通知レベル写像 (ADR-0028) ------------------------------------------

_NOTIFICATION_LEVEL_CASES = [
    pytest.param(ErrorCategory.TRANSIENT, NotificationLevel.WARN, id="transient->WARN"),
    pytest.param(ErrorCategory.RECOVERABLE, NotificationLevel.WARN, id="recoverable->WARN"),
    pytest.param(ErrorCategory.QUALITY, NotificationLevel.INFO, id="quality->INFO"),
    pytest.param(ErrorCategory.FATAL, NotificationLevel.CRITICAL, id="fatal->CRITICAL"),
    pytest.param(ErrorCategory.COMPLIANCE, NotificationLevel.ERROR, id="compliance->ERROR"),
]


@pytest.mark.fr("FR-111")
@pytest.mark.parametrize(("category", "expected_level"), _NOTIFICATION_LEVEL_CASES)
def test_notification_level_for_all_categories(
    category: ErrorCategory, expected_level: NotificationLevel
) -> None:
    """FR-111: 全 5 カテゴリで notification_level_for の写像が ADR-0028 と一致する。"""
    assert notification_level_for(category) is expected_level


# --- カテゴリ → Slack prefix 写像 (FR-114: カテゴリ名 prefix) ------------------------

_SLACK_PREFIX_CASES = [
    pytest.param(ErrorCategory.TRANSIENT, "[TRANSIENT]", id="transient->[TRANSIENT]"),
    pytest.param(ErrorCategory.RECOVERABLE, "[RECOVERABLE]", id="recoverable->[RECOVERABLE]"),
    pytest.param(ErrorCategory.QUALITY, "[QUALITY]", id="quality->[QUALITY]"),
    pytest.param(ErrorCategory.FATAL, "[FATAL]", id="fatal->[FATAL]"),
    pytest.param(ErrorCategory.COMPLIANCE, "[COMPLIANCE]", id="compliance->[COMPLIANCE]"),
]


@pytest.mark.fr("FR-111", "FR-114")
@pytest.mark.parametrize(("category", "expected_prefix"), _SLACK_PREFIX_CASES)
def test_slack_prefix_for_all_categories(category: ErrorCategory, expected_prefix: str) -> None:
    """FR-111/FR-114: 全 5 カテゴリで slack_prefix_for がカテゴリ名 prefix を返す。"""
    assert slack_prefix_for(category) == expected_prefix


@pytest.mark.fr("FR-111")
def test_error_category_enum_has_exactly_five_members() -> None:
    """FR-111: ErrorCategory は ちょうど 5 カテゴリ (過不足なし)。"""
    assert {c.value for c in ErrorCategory} == {
        "transient",
        "recoverable",
        "fatal",
        "compliance",
        "quality",
    }


# NOTE: FR-113(fatal 時の scheduler 停止)は infrastructure/scheduler.py の実挙動として
# tests/unit/test_scheduler.py で検証する(SchedulerHaltError → disable())。
