"""Slack Incoming Webhook 通知クライアント (FR-114 / FR-115, ADR-0028)。

単一の Incoming Webhook (``Settings.slack_webhook_url``) へ ``httpx`` で JSON POST
する薄い通知層。 メッセージ冒頭にはカテゴリ由来の prefix
(``[FATAL]`` / ``[ERROR]`` / ``[WARN]`` / ``[INFO]``) を付与し、 ``fatal`` / ``compliance``
の重大通知には ``<!channel>`` mention を先頭に添えてチャンネル全体へ周知する
(FR-114: 致命系を即時把握 / FR-115: コンプラ違反を即時把握)。

設計方針:

- webhook 未設定 (``None`` / 空文字 ``SecretStr``) の場合は **no-op** にして HTTP を
  一切打たない。 dryrun・ローカル開発・CI で Slack を構成せずに動かせるようにする。
- 通知失敗 (webhook 側の障害・ネットワーク断) で **業務処理を巻き込まない**。 通知は
  あくまで副作用なので、 送信失敗は loguru WARNING を残すだけで例外を握り潰す
  (通知のために本筋の投稿パイプラインを止めない)。
- :class:`ymg_backend.domain.compliance.validators.SyntheticMediaNotifier` Protocol を
  満たすため :meth:`notify_compliance_violation` を実装する (コンプラガードから疎結合に
  呼ばれる)。
- HTTP は ``httpx`` を使い、 ``client`` を外部注入できるようにしてテストで respx mock を
  当てられる設計にする。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

import httpx
from loguru import logger
from pydantic import SecretStr

from ymg_backend.domain.errors.errors import (
    ErrorCategory,
    NotificationLevel,
    resolve_category,
    slack_prefix_for,
)

if TYPE_CHECKING:
    from ymg_backend.domain.compliance.validators import SyntheticMediaViolationNotice

__all__ = ["SlackNotifier"]

# Slack webhook POST のタイムアウト (秒)。 通知は本筋ではないため短く切る。
_DEFAULT_TIMEOUT_SEC: Final[float] = 10.0

# Slack channel-wide mention トークン。 fatal / compliance のみ先頭に付与する。
_CHANNEL_MENTION: Final[str] = "<!channel>"

# 通知レベル → Slack prefix (errors._LEVEL_SLACK_PREFIX と同値だが非公開のため再掲)。
# CRITICAL は data-model / ADR-0028 の表記揺れ吸収のため [FATAL] に写像する。
_LEVEL_PREFIX: Final[Mapping[NotificationLevel, str]] = {
    NotificationLevel.INFO: "[INFO]",
    NotificationLevel.WARN: "[WARN]",
    NotificationLevel.ERROR: "[ERROR]",
    NotificationLevel.CRITICAL: "[FATAL]",
}

# mention を付与する通知レベル (FR-114: CRITICAL=fatal を即時周知)。
_MENTION_LEVELS: Final[frozenset[NotificationLevel]] = frozenset({NotificationLevel.CRITICAL})

# mention を付与するエラーカテゴリ (FR-114 fatal / FR-115 compliance)。
_MENTION_CATEGORIES: Final[frozenset[ErrorCategory]] = frozenset(
    {ErrorCategory.FATAL, ErrorCategory.COMPLIANCE}
)


def _is_configured(webhook_url: SecretStr | None) -> bool:
    """webhook が有効に設定されているか (None / 空文字は未設定)。"""
    return webhook_url is not None and bool(webhook_url.get_secret_value().strip())


def _compose_text(*, prefix: str, message: str, mention: bool) -> str:
    """Slack へ送る本文を組み立てる (mention → prefix → message の順)。"""
    head = f"{_CHANNEL_MENTION} " if mention else ""
    return f"{head}{prefix} {message}"


def _format_context(context: Mapping[str, object] | None) -> str:
    """context dict を ``key=value`` の安定文字列に整形する (空なら空文字)。"""
    if not context:
        return ""
    pairs = " ".join(f"{key}={value}" for key, value in context.items())
    return f"\n{pairs}"


class SlackNotifier:
    """単一 webhook への Slack 通知クライアント。

    Args:
        webhook_url: Incoming Webhook URL (``Settings.slack_webhook_url``)。
            ``None`` または空文字 ``SecretStr`` の場合は no-op (HTTP を打たない)。
        timeout_sec: POST のタイムアウト秒。 既定 10 秒。
        client: 注入する ``httpx.AsyncClient`` (テスト用)。 省略時は POST ごとに
            一時 client を生成・クローズする。 注入時は所有権を呼び出し側が持つ。
    """

    __slots__ = ("_client", "_timeout_sec", "_webhook_url")

    def __init__(
        self,
        webhook_url: SecretStr | None,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url: Final[SecretStr | None] = webhook_url
        self._timeout_sec: Final[float] = timeout_sec
        self._client: Final[httpx.AsyncClient | None] = client

    async def notify(
        self,
        *,
        level: NotificationLevel,
        message: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        """通知レベルに応じた prefix を付けて webhook へ POST する。

        ``level`` が ``CRITICAL`` の場合は ``<!channel>`` mention を先頭に付与する
        (FR-114)。 webhook 未設定なら no-op。

        Args:
            level: 通知レベル (``INFO`` / ``WARN`` / ``ERROR`` / ``CRITICAL``)。
            message: 通知本文。 prefix の後ろに連結される。
            context: 追加情報 (``post_id`` / ``genre`` 等)。 末尾に ``key=value`` で付記。
        """
        prefix = _LEVEL_PREFIX[level]
        mention = level in _MENTION_LEVELS
        text = _compose_text(prefix=prefix, message=message, mention=mention)
        await self._post(text + _format_context(context))

    async def notify_error(
        self,
        exc: BaseException,
        *,
        context: Mapping[str, object] | None = None,
    ) -> None:
        """例外をカテゴリ解決し、 prefix と mention を付けて通知する。

        ``resolve_category`` → ``slack_prefix_for`` で prefix を決め、 カテゴリが
        ``fatal`` / ``compliance`` の場合は ``<!channel>`` mention を付与する
        (FR-114 / FR-115)。 未分類例外は ``recoverable`` (``[WARN]``) 扱い。

        Args:
            exc: 通知対象の例外。
            context: 追加情報。 末尾に ``key=value`` で付記。
        """
        category = resolve_category(exc)
        prefix = slack_prefix_for(category)
        mention = category in _MENTION_CATEGORIES
        message = f"{type(exc).__name__}: {exc}"
        text = _compose_text(prefix=prefix, message=message, mention=mention)
        await self._post(text + _format_context(context))

    async def notify_compliance_violation(self, notice: SyntheticMediaViolationNotice) -> None:
        """``SyntheticMediaNotifier`` Protocol 実装: コンプラ違反を通知する。

        ガード層 (compliance.validators) が確定させた ``notice.message`` を尊重しつつ、
        コンプラは常に mention 付き ERROR として周知する (FR-115)。

        Args:
            notice: コンプラ違反通知ペイロード (prefix / message 確定済み)。
        """
        mention = True  # compliance は FR-115 により常に <!channel> mention
        text = _compose_text(prefix="", message=notice.message, mention=mention).lstrip()
        await self._post(text)

    async def _post(self, text: str) -> None:
        """Slack webhook へ ``{"text": ...}`` を POST する (失敗は握り潰す)。

        webhook 未設定なら何もしない。 送信失敗 (タイムアウト・接続断・4xx/5xx) は
        通知のために本筋を止めないため loguru WARNING を残して例外を送出しない。
        """
        if not _is_configured(self._webhook_url):
            return
        assert self._webhook_url is not None  # _is_configured が保証 (mypy 向け narrowing)
        url = self._webhook_url.get_secret_value()
        payload = {"text": text}
        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload)
            else:
                async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                    response = await client.post(url, json=payload)
            response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning("Slack 通知の送信に失敗しました (本筋は継続): {}", exc)
