"""投稿直前コンプラガード (Constitution II / ADR-0020).

YouTube ``videos.insert`` の直前に **必ず** 通す薄いガード。 実体の検証ロジックは
T070 で実装済みの :func:`enforce_synthetic_media_compliance` (domain/compliance) に委譲し、
ここでは uploader が組み立てる upload body の ``status`` ブロックを構築して検証する。
``status.containsSyntheticMedia=true`` が存在しない場合は ``ComplianceError`` で投稿を
停止し、 audit_log 記録 + Slack 通知が enforcer 側で実行される (FR-006/007/100, ADR-0020)。

設計方針:

- 通知先の ``SlackNotifier`` (notify / notify_error 系) を、 enforcer が要求する
  ``SyntheticMediaNotifier`` Protocol (``notify_compliance_violation``) へ
  :class:`_SyntheticMediaNotifierAdapter` で橋渡しする (通知層との疎結合を保つ)。
- ガードは body 構築 + 検証のみで、 副作用 (audit / 通知 / 例外送出) は enforcer に集約。
- commit は呼び出し側 (オーケストレータ) 責務。 enforcer は audit を flush まで行う。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from ymg_backend.domain.compliance.validators import (
    SyntheticMediaViolationNotice,
    enforce_synthetic_media_compliance,
)
from ymg_backend.domain.errors.errors import NotificationLevel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.db.models import Post
    from ymg_backend.infrastructure.slack.notifier import SlackNotifier

# upload body のキー (ADR-0020 / domain.compliance.validators と一致)。
_STATUS_KEY: Final[str] = "status"
_SYNTHETIC_MEDIA_KEY: Final[str] = "containsSyntheticMedia"


class _SyntheticMediaNotifierAdapter:
    """``SlackNotifier`` を enforcer の ``SyntheticMediaNotifier`` Protocol へ適合させる。

    enforcer は ``notify_compliance_violation(notice)`` だけを呼ぶ。 本アダプタは
    notice を ``SlackNotifier.notify`` 呼び出しへ変換する (prefix は notice 側で確定済み)。
    """

    __slots__ = ("_notifier",)

    def __init__(self, notifier: SlackNotifier) -> None:
        self._notifier = notifier

    async def notify_compliance_violation(self, notice: SyntheticMediaViolationNotice) -> None:
        """違反 notice を ERROR レベルの Slack 通知へ転送する。"""
        await self._notifier.notify(
            level=NotificationLevel.ERROR,
            message=notice.message,
            context={"video_ref": notice.video_ref, "reason": notice.reason},
        )


def _build_upload_status_body(*, description: str, video_uri: str) -> dict[str, Any]:
    """検証対象となる upload body を構築する (常に合成メディアフラグを付与)。

    uploader が ``videos.insert`` に渡す body と同じ ``status`` 構造を再現し、
    ``containsSyntheticMedia=true`` が確実に含まれることをガードで検証できるようにする。
    """
    return {
        "snippet": {"description": description},
        "status": {_SYNTHETIC_MEDIA_KEY: True},
        "video_uri": video_uri,
    }


async def compliance_gate(
    *,
    session: AsyncSession,
    post: Post,
    description: str,
    video_uri: str,
    notifier: SlackNotifier,
) -> None:
    """アップロード直前の合成メディア開示ガード (ADR-0020)。

    upload body の ``status.containsSyntheticMedia=true`` を T070 の enforcer で検証する。
    違反時は enforcer が audit_log 記録 + Slack 通知を行ったうえで ``ComplianceError`` を
    送出し、 投稿を停止する。 正常時は何も返さず投稿続行を許可する。

    Args:
        session: audit_log 書き込み用 ``AsyncSession`` (commit は呼び出し側)。
        post: 対象投稿 (``post.id`` を audit target_id / video_ref に使う)。
        description: 最終説明文 (開示文を含む)。 body の snippet に載せる。
        video_uri: 投稿対象動画の URI (証跡用)。
        notifier: 違反通知に使う ``SlackNotifier``。

    Raises:
        ComplianceError: ``containsSyntheticMedia=true`` が body に無い場合 (投稿停止)。
    """
    video_ref = str(post.id)
    body = _build_upload_status_body(description=description, video_uri=video_uri)
    await enforce_synthetic_media_compliance(
        body,
        video_ref=video_ref,
        session=session,
        notifier=_SyntheticMediaNotifierAdapter(notifier),
        target_type="post",
        target_id=video_ref,
    )
