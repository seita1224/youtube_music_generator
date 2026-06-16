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
    FatalError,
    NotificationLevel,
    RecoverableError,
)

# 未実装モジュール (TDD RED 段階)。 import 失敗時は本ファイルを skip する。
# 公開シンボルは module オブジェクト経由で参照し (``notifier_mod.SlackNotifier``)、
# トップレベル import を増やさない (未実装モジュールへの直 import を避ける)。
notifier_mod = pytest.importorskip("ymg_backend.infrastructure.slack.notifier")

_WEBHOOK = "https://hooks.slack.test/services/T000/B000/XXXX"

pytestmark = pytest.mark.asyncio


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


async def test_notify_posts_message_to_webhook() -> None:
    """webhook 設定時は webhook URL へ POST し、 本文に message を含める。"""
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


async def test_notify_error_uses_error_prefix_for_compliance() -> None:
    """ComplianceError は [ERROR] prefix で通知される (slack_prefix_for 準拠)。"""
    notifier = notifier_mod.SlackNotifier(SecretStr(_WEBHOOK))
    with respx.mock as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.notify_error(ComplianceError("synthetic media flag missing"))
        assert route.call_count == 1
        sent = route.calls.last.request.read().decode()
        assert "[ERROR]" in sent


async def test_notify_error_uses_fatal_prefix_for_fatal() -> None:
    """FatalError は [FATAL] prefix で通知される。"""
    notifier = notifier_mod.SlackNotifier(SecretStr(_WEBHOOK))
    with respx.mock as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.notify_error(FatalError("db unreachable"))
        assert route.call_count == 1
        assert "[FATAL]" in route.calls.last.request.read().decode()


async def test_notify_error_uses_warn_prefix_for_recoverable() -> None:
    """RecoverableError は [WARN] prefix で通知される。"""
    notifier = notifier_mod.SlackNotifier(SecretStr(_WEBHOOK))
    with respx.mock as router:
        route = router.post(_WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.notify_error(RecoverableError("oauth token expired"))
        assert route.call_count == 1
        assert "[WARN]" in route.calls.last.request.read().decode()
