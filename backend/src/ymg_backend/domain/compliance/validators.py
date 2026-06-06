"""投稿前コンプラバリデーション層 (Constitution II / ADR-0020, ADR-0028)。

YouTube ``videos.insert`` の upload body に ``status.containsSyntheticMedia=true``
が確実に含まれているかを投稿直前に検証する **コンプラガード層**。 ここを通らない
限り動画は投稿されない (ADR-0020「投稿直前のバリデーション層で必ず確認」)。

責務分割:

- :func:`assert_contains_synthetic_media`
    upload body を検査し、 違反なら ``ComplianceError`` を raise するだけの純粋ガード。
    副作用 (通知 / 監査) を持たないため、 単体テスト・dryrun でも安全に走る。
- :func:`enforce_synthetic_media_compliance`
    ガードに加え、 違反時に **(1) 投稿停止 (例外再送出) + (2) audit_log 記録 +
    (3) Slack 通知** を行う「投稿経路用」のエンフォーサ。 audit を先に永続化してから
    通知するため、 通知系が落ちても証跡が残る (Compliance-First)。

Slack 通知の実体 (T090) には :class:`SyntheticMediaNotifier` Protocol 経由で依存し、
通知層と疎結合を保つ。 監査書き込みは :func:`ymg_backend.infrastructure.audit.write_audit_log`
を再利用する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.domain.errors import (
    ComplianceError,
    ErrorCategory,
    NotificationLevel,
    slack_prefix_for,
)
from ymg_backend.infrastructure.audit import write_audit_log

# audit_log.action 値 (data-model.md `audit_log`、 ADR-0028 `compliance_violation`)。
COMPLIANCE_AUDIT_ACTION: Final[str] = "compliance_violation"

# upload body のキー (ADR-0020 `videos.insert` body 構造)。
_STATUS_KEY: Final[str] = "status"
_SYNTHETIC_MEDIA_KEY: Final[str] = "containsSyntheticMedia"

# 違反理由コード (context / payload / Slack に載せる安定識別子)。
_REASON_BODY_NOT_MAPPING: Final[str] = "body_not_mapping"
_REASON_MISSING_STATUS: Final[str] = "missing_status"
_REASON_MISSING_FLAG: Final[str] = "missing_flag"
_REASON_FLAG_NOT_TRUE: Final[str] = "flag_not_true"


@dataclass(frozen=True, slots=True)
class SyntheticMediaViolationNotice:
    """containsSyntheticMedia 違反の Slack 通知ペイロード (不変)。

    通知層 (T090) はこの notice を受け取り webhook に送る。 ガード層側で
    レベル・prefix・本文を確定させ、 通知実体が誤って軽い扱いをしないようにする。
    """

    video_ref: str
    reason: str
    level: NotificationLevel
    prefix: str
    message: str

    @classmethod
    def from_violation(cls, *, video_ref: str, reason: str) -> SyntheticMediaViolationNotice:
        """違反情報から ERROR レベルの通知を生成する (ADR-0028 compliance=ERROR)。"""
        level = NotificationLevel.ERROR
        prefix = slack_prefix_for(ErrorCategory.COMPLIANCE)
        message = (
            f"{prefix} containsSyntheticMedia バリデーション違反: "
            f"video_ref={video_ref} reason={reason}. 投稿を停止しました (ADR-0020)。"
        )
        return cls(
            video_ref=video_ref,
            reason=reason,
            level=level,
            prefix=prefix,
            message=message,
        )


@runtime_checkable
class SyntheticMediaNotifier(Protocol):
    """コンプラ違反 Slack 通知の最小インターフェース (T090 実体に依存しない)。"""

    async def notify_compliance_violation(self, notice: SyntheticMediaViolationNotice) -> None: ...


def _detect_violation(body: object) -> str | None:
    """upload body を検査し、 違反理由コードを返す (違反なしなら ``None``)。"""
    if not isinstance(body, Mapping):
        return _REASON_BODY_NOT_MAPPING

    status = body.get(_STATUS_KEY)
    if not isinstance(status, Mapping):
        return _REASON_MISSING_STATUS

    if _SYNTHETIC_MEDIA_KEY not in status:
        return _REASON_MISSING_FLAG

    flag = status[_SYNTHETIC_MEDIA_KEY]
    # 厳格に bool True のみ許可。 文字列 "true" や 1 等の緩い値は拒否する。
    if flag is not True:
        return _REASON_FLAG_NOT_TRUE

    return None


def assert_contains_synthetic_media(body: object, *, video_ref: str) -> None:
    """upload body に ``status.containsSyntheticMedia=true`` があることを保証する。

    純粋ガード: 違反時は ``ComplianceError`` を raise するだけで、 通知・監査は行わない。

    Args:
        body: ``videos.insert`` の body (``{"snippet": ..., "status": {...}}``)。
        video_ref: 対象識別子 (post_id / youtube_video_id 等)。 ログ・監査用。 非空必須。

    Raises:
        ValueError: ``video_ref`` が空白のみの場合 (境界での入力検証)。
        ComplianceError: ``containsSyntheticMedia`` が ``True`` でない場合。
            ``context`` に ``video_ref`` と ``reason`` を格納する。
    """
    if not video_ref.strip():
        raise ValueError("video_ref must be a non-empty string")

    reason = _detect_violation(body)
    if reason is None:
        return

    raise ComplianceError(
        f"containsSyntheticMedia=true が upload body に存在しません "
        f"(video_ref={video_ref}, reason={reason}). 投稿を停止します (ADR-0020)。",
        context={"video_ref": video_ref, "reason": reason},
    )


async def enforce_synthetic_media_compliance(
    body: object,
    *,
    video_ref: str,
    session: AsyncSession,
    notifier: SyntheticMediaNotifier,
    target_type: str = "video",
    target_id: str | None = None,
) -> None:
    """投稿経路用エンフォーサ: ガード + 投稿停止 + audit_log + Slack 通知。

    body が正常なら何もせず返る (投稿続行)。 違反時は以下を順に実行する:

    1. ``audit_log`` に ``compliance_violation`` を 1 行記録 (証跡を先に永続化)。
    2. Slack 通知 (ERROR レベル) を送出。
    3. ``ComplianceError`` を再 raise して投稿を停止させる。

    audit を通知より先に行うため、 Slack 障害時でも監査証跡は残る。

    Args:
        body: ``videos.insert`` の body。
        video_ref: 対象識別子 (post_id / youtube_video_id 等)。 非空必須。
        session: audit_log 書き込み用 ``AsyncSession`` (commit は呼び出し側境界)。
        notifier: Slack 通知層 (Protocol)。
        target_type: audit_log.target_type (既定 ``"video"``)。
        target_id: audit_log.target_id (未指定時は ``video_ref`` を採用)。

    Raises:
        ValueError: ``video_ref`` が空白のみの場合。
        ComplianceError: ``containsSyntheticMedia`` が ``True`` でない場合 (投稿停止)。
    """
    try:
        assert_contains_synthetic_media(body, video_ref=video_ref)
    except ComplianceError as exc:
        reason = str(exc.context.get("reason", "unknown"))
        await _record_violation(
            session=session,
            notifier=notifier,
            video_ref=video_ref,
            reason=reason,
            target_type=target_type,
            target_id=target_id if target_id is not None else video_ref,
        )
        raise


async def _record_violation(
    *,
    session: AsyncSession,
    notifier: SyntheticMediaNotifier,
    video_ref: str,
    reason: str,
    target_type: str,
    target_id: str,
) -> None:
    """違反を audit_log に記録し、 続いて Slack へ通知する。"""
    payload: dict[str, Any] = {
        "reason": reason,
        "video_ref": video_ref,
        "check": _SYNTHETIC_MEDIA_KEY,
    }
    await write_audit_log(
        session,
        action=COMPLIANCE_AUDIT_ACTION,
        actor="compliance_gate",
        target_type=target_type,
        target_id=target_id,
        payload=payload,
    )
    notice = SyntheticMediaViolationNotice.from_violation(video_ref=video_ref, reason=reason)
    await notifier.notify_compliance_violation(notice)
