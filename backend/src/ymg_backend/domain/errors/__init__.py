"""エラーカテゴリと例外階層 (ADR-0028).

5 カテゴリの例外型、カテゴリ解決ロジック、Slack 通知レベル・prefix マッピングを公開する。
詳細実装は :mod:`ymg_backend.domain.errors.errors`。
"""

from __future__ import annotations

from ymg_backend.domain.errors.errors import (
    DEFAULT_CATEGORY,
    ComplianceError,
    ErrorCategory,
    FatalError,
    NotificationLevel,
    QualityError,
    RecoverableError,
    SchedulerHaltError,
    TransientError,
    YmgError,
    notification_level_for,
    resolve_category,
    slack_prefix_for,
    slack_prefix_for_exc,
)

__all__ = [
    "DEFAULT_CATEGORY",
    "ComplianceError",
    "ErrorCategory",
    "FatalError",
    "NotificationLevel",
    "QualityError",
    "RecoverableError",
    "SchedulerHaltError",
    "TransientError",
    "YmgError",
    "notification_level_for",
    "resolve_category",
    "slack_prefix_for",
    "slack_prefix_for_exc",
]
