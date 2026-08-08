"""エラーカテゴリ分類と例外階層 (ADR-0028).

5 カテゴリ ``transient / recoverable / fatal / compliance / quality`` を例外型として表現し、
中央ハンドラ(APScheduler のジョブラッパ・FastAPI ミドルウェア)がカテゴリに応じて挙動を分岐できるようにする。

- ``category``: 例外が属するカテゴリ(``ErrorCategory``)。
- ``context``: 構造化ログ・usage_log 用の付随情報(job_id / video_id / step など)。不変コピーで保持する。
- ``original``: ラップ元の例外(あれば)。

カテゴリ判定・Slack 通知レベル・通知 prefix の解決ロジックも本モジュールに集約する。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final


class ErrorCategory(StrEnum):
    """ADR-0028 の 5 カテゴリ.

    値は data-model.md の ``error_category`` ENUM(``transient`` 等)と一致させる。
    """

    TRANSIENT = "transient"
    RECOVERABLE = "recoverable"
    FATAL = "fatal"
    COMPLIANCE = "compliance"
    QUALITY = "quality"


class NotificationLevel(StrEnum):
    """Slack 通知レベル (ADR-0028 「Slack 通知レベル」節)."""

    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# --- カテゴリ → 通知レベルのマッピング (ADR-0028) ---------------------------------
# transient リトライ成功は通知なし(ログのみ)。
# transient リトライ失敗時は recoverable へ昇格し WARN 通知となるため、
# transient 単体の通知レベルは WARN を割り当てる(昇格後の挙動と一致)。
_CATEGORY_NOTIFICATION_LEVEL: Final[Mapping[ErrorCategory, NotificationLevel]] = MappingProxyType(
    {
        ErrorCategory.TRANSIENT: NotificationLevel.WARN,
        ErrorCategory.RECOVERABLE: NotificationLevel.WARN,
        ErrorCategory.QUALITY: NotificationLevel.INFO,
        ErrorCategory.FATAL: NotificationLevel.CRITICAL,
        ErrorCategory.COMPLIANCE: NotificationLevel.ERROR,
    }
)

# --- 通知レベル → Slack prefix のマッピング ([FATAL] 等) ---------------------------
# notify(level=...) 経路(エラーカテゴリを伴わない budget アラート等)で使用する。
_LEVEL_SLACK_PREFIX: Final[Mapping[NotificationLevel, str]] = MappingProxyType(
    {
        NotificationLevel.INFO: "[INFO]",
        NotificationLevel.WARN: "[WARN]",
        NotificationLevel.ERROR: "[ERROR]",
        NotificationLevel.CRITICAL: "[FATAL]",
    }
)

# --- カテゴリ → Slack prefix のマッピング (FR-114) -----------------------------------
# spec.md / Clarifications はカテゴリ名 prefix を要求する(level ベースではなく category 名で
# 識別する)。 notify_error はこの写像でカテゴリ名 prefix を付与する。
_CATEGORY_SLACK_PREFIX: Final[Mapping[ErrorCategory, str]] = MappingProxyType(
    {
        ErrorCategory.TRANSIENT: "[TRANSIENT]",
        ErrorCategory.RECOVERABLE: "[RECOVERABLE]",
        ErrorCategory.QUALITY: "[QUALITY]",
        ErrorCategory.FATAL: "[FATAL]",
        ErrorCategory.COMPLIANCE: "[COMPLIANCE]",
    }
)


class YmgError(Exception):
    """全 YMG 例外の基底.

    ``category`` は各サブクラスがクラス属性として固定し、インスタンス生成時に
    ``context`` / ``original`` を受け取る。``context`` は不変コピー(``MappingProxyType``)で保持し、
    呼び出し側の dict 変更が例外に波及しないようにする。
    """

    category: ErrorCategory

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, Any] | None = None,
        original: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message: Final[str] = message
        self.context: Final[Mapping[str, Any]] = MappingProxyType(dict(context or {}))
        self.original: Final[Exception | None] = original

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(category={self.category.value!r}, "
            f"message={self.message!r}, context={dict(self.context)!r}, "
            f"original={self.original!r})"
        )


class TransientError(YmgError):
    """一過性エラー(ネットタイムアウト・5xx・短時間 rate limit).

    同一ジョブ内で最大 3 回・exponential backoff(1s→2s→4s)でリトライし、
    失敗時は ``recoverable`` へ昇格する(昇格は呼び出し側ハンドラの責務)。
    """

    category = ErrorCategory.TRANSIENT


class RecoverableError(YmgError):
    """回復可能エラー(OAuth トークン失効・ディスク容量不足・API key 無効・Codex quota 切れ).

    該当ジョブのみスキップ + Slack 通知。他ジョブは継続する。
    分類不明時のデフォルト(安全側)もこのカテゴリ。
    """

    category = ErrorCategory.RECOVERABLE


class FatalError(YmgError):
    """致命的エラー(DB 接続不可・Fernet 鍵無効・設定ファイル破損).

    サイクル全体を停止 + Slack 通知(CRITICAL)。
    """

    category = ErrorCategory.FATAL


class SchedulerHaltError(FatalError):
    """scheduler 全体の停止を要する致命障害(FR-113).

    ``FatalError`` のうち、 当該サイクルの中断だけでなく **scheduler の自動停止**(投稿系ジョブの
    除去)まで必要なインフラ級 fatal を表す。 例: DB 不達(記録自体が信頼できない)、 PostgreSQL
    ダンプ失敗、 AcoustID API ダウンのリトライ枯渇。 翌日の cron を空振りさせず人手の介入を促す。

    一方、 個別枠の content fatal(企画解決不能など)は通常の :class:`FatalError` とし、
    当該枠の中断に留める。 本例外はシステム状態 ``stopped`` への遷移を要する
    (ADR-0044: fatal → ``stopped`` + Slack 通知)。
    """


class ComplianceError(YmgError):
    """コンプラ違反(containsSyntheticMedia 未設定・AcoustID マッチ・プロンプトポリシー違反).

    該当動画の投稿を停止 + Slack 通知(ERROR) + 構造化ログに ``compliance_violation`` を記録。
    BAN リスク直結のため最優先で扱う。
    """

    category = ErrorCategory.COMPLIANCE


class QualityError(YmgError):
    """品質低下(LLM 構造化リトライ N 回失敗・サムネ生成異常・コメント分析空).

    該当部分のみスキップしデフォルト値で続行 + Slack 通知(INFO)。
    """

    category = ErrorCategory.QUALITY


# 分類不明時のデフォルトカテゴリ(安全側 = recoverable, ADR-0028「悪い影響」緩和策)。
DEFAULT_CATEGORY: Final[ErrorCategory] = ErrorCategory.RECOVERABLE


def resolve_category(exc: BaseException) -> ErrorCategory:
    """例外からエラーカテゴリを解決する.

    ``YmgError`` のサブクラスはクラス属性 ``category`` をそのまま採用する。
    未知の例外(標準例外・サードパーティ例外)は安全側の ``DEFAULT_CATEGORY``(recoverable)に分類する。

    Args:
        exc: 分類対象の例外。

    Returns:
        解決された ``ErrorCategory``。
    """
    if isinstance(exc, YmgError):
        return exc.category
    return DEFAULT_CATEGORY


def notification_level_for(category: ErrorCategory) -> NotificationLevel:
    """カテゴリに対応する Slack 通知レベルを返す (ADR-0028).

    Args:
        category: 対象カテゴリ。

    Returns:
        対応する ``NotificationLevel``。
    """
    return _CATEGORY_NOTIFICATION_LEVEL[category]


def slack_prefix_for(category: ErrorCategory) -> str:
    """カテゴリに対応する Slack 通知 prefix(``[FATAL]`` 等)を返す (FR-114).

    spec.md / Clarifications に従い**カテゴリ名 prefix** を返す
    (``transient`` → ``[TRANSIENT]`` 等)。 level ベースではない。

    Args:
        category: 対象カテゴリ。

    Returns:
        ``[TRANSIENT]`` / ``[RECOVERABLE]`` / ``[QUALITY]`` / ``[FATAL]`` /
        ``[COMPLIANCE]`` のいずれか。
    """
    return _CATEGORY_SLACK_PREFIX[category]


def slack_prefix_for_exc(exc: BaseException) -> str:
    """例外から Slack 通知 prefix を直接解決するショートカット.

    Args:
        exc: 対象の例外。

    Returns:
        カテゴリ名 prefix(``[TRANSIENT]`` 等、 :func:`slack_prefix_for` 参照)。
    """
    return slack_prefix_for(resolve_category(exc))
