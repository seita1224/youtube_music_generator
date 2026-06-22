"""SlackNotifier (infrastructure/slack/notifier.py) の単体テスト (T072 付随)。

契約 (共有契約書 / errors.slack_prefix_for):

- ``SlackNotifier(webhook_url: SecretStr | None)`` — ``None`` / 空文字なら no-op (HTTP を打たない)。
- ``notify(level, message, context=None)`` — webhook へ JSON POST する。
- ``notify_error(exc, context=None)`` — ``resolve_category`` → ``slack_prefix_for`` で
  ``[INFO]/[WARN]/[ERROR]/[FATAL]`` の prefix を付けて通知する。

外部依存 (Slack webhook) は respx で mock し、 実 HTTP は打たない。
TDD RED 段階で notifier 未実装なら importorskip で skip する。
"""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from ymg_backend.domain.errors.errors import (
    ComplianceError,
    ErrorCategory,
    FatalError,
    NotificationLevel,
    QualityError,
    RecoverableError,
    TransientError,
    YmgError,
    slack_prefix_for,
)

# 未実装モジュール (TDD RED 段階)。 import 失敗時は本ファイルを skip する。
# 公開シンボルは module オブジェクト経由で参照し (``notifier_mod.SlackNotifier``)、
# トップレベル import を増やさない (未実装モジュールへの直 import を避ける)。
notifier_mod = pytest.importorskip("ymg_backend.infrastructure.slack.notifier")

_WEBHOOK = "https://hooks.slack.test/services/T000/B000/XXXX"

# asyncio_mode="auto" (pyproject) が coroutine テストを自動 marker するため、 module 全体への
# 明示 asyncio mark は張らない (同期の FR-114 spec test に asyncio mark が漏れるのを避ける)。


async def test_notify_with_none_webhook_is_noop() -> None:
    """webhook 未設定 (None) なら HTTP を一切打たない (no-op)。"""
    notifier = notifier_mod.SlackNotifier(None)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200, text="ok"))
        await notifier.notify(level=NotificationLevel.INFO, message="hello")
        assert route.call_count == 0


async def test_notify_with_empty_secret_is_noop() -> None:
    """空文字 SecretStr も未設定とみなし no-op とする。"""
    notifier = notifier_mod.SlackNotifier(SecretStr(""))
    with respx.mock(assert_all_called=False) as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.notify(level=NotificationLevel.WARN, message="warn")
        assert route.call_count == 0


@pytest.mark.fr("FR-114")
async def test_notify_posts_message_to_webhook() -> None:
    """FR-114: webhook 設定時は単一 webhook URL へ POST し、 本文に message を含める。"""
    notifier = notifier_mod.SlackNotifier(SecretStr(_WEBHOOK))
    with respx.mock as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200, text="ok"))
        await notifier.notify(
            level=NotificationLevel.ERROR,
            message="something broke",
            context={"post_id": "abc"},
        )
        assert route.call_count == 1
        sent = route.calls.last.request.read().decode()
        assert "something broke" in sent


@pytest.mark.fr("FR-114")
async def test_notify_error_uses_compliance_prefix_for_compliance() -> None:
    """FR-114: ComplianceError はカテゴリ名 prefix [COMPLIANCE] で通知される。"""
    notifier = notifier_mod.SlackNotifier(SecretStr(_WEBHOOK))
    with respx.mock as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.notify_error(ComplianceError("synthetic media flag missing"))
        assert route.call_count == 1
        sent = route.calls.last.request.read().decode()
        assert "[COMPLIANCE]" in sent


@pytest.mark.fr("FR-114")
async def test_notify_error_uses_fatal_prefix_for_fatal() -> None:
    """FR-114: FatalError はカテゴリ prefix [FATAL] を冒頭に付けて通知される。"""
    notifier = notifier_mod.SlackNotifier(SecretStr(_WEBHOOK))
    with respx.mock as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.notify_error(FatalError("db unreachable"))
        assert route.call_count == 1
        assert "[FATAL]" in route.calls.last.request.read().decode()


@pytest.mark.fr("FR-114")
async def test_notify_error_uses_recoverable_prefix_for_recoverable() -> None:
    """FR-114: RecoverableError はカテゴリ名 prefix [RECOVERABLE] で通知される。"""
    notifier = notifier_mod.SlackNotifier(SecretStr(_WEBHOOK))
    with respx.mock as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.notify_error(RecoverableError("oauth token expired"))
        assert route.call_count == 1
        assert "[RECOVERABLE]" in route.calls.last.request.read().decode()


# --- FR-115: fatal / compliance のみ <!channel> mention --------------------------

_MENTION_CASES = [
    pytest.param(FatalError, True, id="fatal->mention"),
    pytest.param(ComplianceError, True, id="compliance->mention"),
    pytest.param(TransientError, False, id="transient->no-mention"),
    pytest.param(RecoverableError, False, id="recoverable->no-mention"),
    pytest.param(QualityError, False, id="quality->no-mention"),
]


@pytest.mark.fr("FR-115")
@pytest.mark.parametrize(("exc_type", "expects_mention"), _MENTION_CASES)
async def test_notify_error_channel_mention_only_for_fatal_and_compliance(
    exc_type: type[YmgError], expects_mention: bool
) -> None:
    """FR-115: fatal / compliance のみ送信本文に <!channel> mention を含める。

    respx で実送信 body を捕捉し、 mention の有無をカテゴリ別に検証する。
    """
    notifier = notifier_mod.SlackNotifier(SecretStr(_WEBHOOK))
    with respx.mock as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.notify_error(exc_type("boom"))
        assert route.call_count == 1
        sent = route.calls.last.request.read().decode()
        assert ("<!channel>" in sent) is expects_mention


# --- FR-114: prefix は spec.md のカテゴリ名 -----------------------------------------
#
# spec.md / Clarifications はカテゴリ名 prefix [FATAL] / [COMPLIANCE] / [TRANSIENT] /
# [RECOVERABLE] / [QUALITY] を要求する。 実装(slack_prefix_for)はこの写像どおりに返す。

_SPEC_CATEGORY_PREFIX_CASES = [
    pytest.param(ErrorCategory.FATAL, "[FATAL]", id="fatal->[FATAL]"),
    pytest.param(ErrorCategory.COMPLIANCE, "[COMPLIANCE]", id="compliance->[COMPLIANCE]"),
    pytest.param(ErrorCategory.TRANSIENT, "[TRANSIENT]", id="transient->[TRANSIENT]"),
    pytest.param(ErrorCategory.RECOVERABLE, "[RECOVERABLE]", id="recoverable->[RECOVERABLE]"),
    pytest.param(ErrorCategory.QUALITY, "[QUALITY]", id="quality->[QUALITY]"),
]


@pytest.mark.fr("FR-114")
@pytest.mark.parametrize(("category", "spec_prefix"), _SPEC_CATEGORY_PREFIX_CASES)
def test_slack_prefix_uses_spec_category_names(
    category: ErrorCategory, spec_prefix: str
) -> None:
    """FR-114: prefix は spec.md のカテゴリ名([COMPLIANCE]/[TRANSIENT] 等)である。"""
    assert slack_prefix_for(category) == spec_prefix
