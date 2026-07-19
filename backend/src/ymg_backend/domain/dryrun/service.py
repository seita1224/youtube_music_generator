"""dryrun レビューのドメインサービス (US2, T093)。

:class:`DryrunService` は dryrun 成果物のライフサイクル遷移を担う:

- :meth:`approve` — ``pending`` を承認し、 注入された :class:`YouTubeUploader` で
  YouTube へ投稿して ``posted`` まで進める。
- :meth:`reject` — ``pending`` を却下し、 理由 (min4) を保存 + プレビュー動画を削除する。
  保存された ``reject_reason`` は次回 planner が ``REJECTED_REASONS_CONTEXT_KEY`` 経由で
  集約して読むための **記録** になる (planner 側注入は別タスク T096)。
- :meth:`auto_expire` — ``created_at`` から 7 日経過した ``pending`` を一括で
  ``auto_expired`` にし動画を削除する。 retention 日次ジョブが呼ぶ。

設計方針:

- **ステートレス**。 ``session`` は各メソッド引数で受け取り、 ``flush`` までで止める
  (commit は呼び出し側 = ルータ or retention ジョブのセッション境界の責務)。
  :class:`~ymg_backend.domain.plans.planner.PlanGenerator` と同方針。
- ``uploader`` / ``storage`` は **DI** で受け取る (composition root / ルータ依存で構築)。
  service 内で ``get_settings()`` を直叩きしない。 テストでは stub を注入する。
- 不在は ``None`` を返さず :class:`DryrunNotFoundError` を送出 (ルータが 404 に写像)。
  非 ``pending`` は :class:`DryrunStateConflictError` (ルータが 409 に写像)。
  ``reason`` の min4 は service でも再検証し、 違反は :class:`ValueError` (ルータが 422)。
- 状態変更ごとに ``write_audit_log`` で監査ログを残す (ADR-0028 / ADR-0031)。
  action 値: ``dryrun_approved`` / ``dryrun_rejected`` / ``dryrun_auto_expired``。
- 不変志向: 各操作は ORM 行の属性のみを更新し、 storage 削除は冪等
  (``missing_ok=True``) にする。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol

from sqlalchemy import select

from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import DryrunOutput, Post

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

# 却下理由の最小長 (UI 契約 / OpenAPI ``reject`` requestBody minLength と一致)。
_MIN_REASON_LENGTH: Final[int] = 4

# auto_expire の保持期間 (FR / 契約: pending が 7 日経過したら期限切れ)。
_RETENTION_DAYS: Final[int] = 7

# pending からの遷移を許す唯一の状態 (approve / reject はここからのみ可)。
_PENDING: Final[str] = "pending"

# audit_log の action 名 (共有契約 §DryrunService)。
_ACTION_APPROVED: Final[str] = "dryrun_approved"
_ACTION_REJECTED: Final[str] = "dryrun_rejected"
_ACTION_AUTO_EXPIRED: Final[str] = "dryrun_auto_expired"

# audit_log の target_type。
_TARGET_TYPE: Final[str] = "dryrun_output"

# 監査主体 (単一運用者前提, CLAUDE.md / ADR-0031)。
_ACTOR: Final[str] = "seita"


class DryrunError(Exception):
    """dryrun サービスの基底例外 (HTTP 写像はルータ層が担う)。"""


class DryrunNotFoundError(DryrunError):
    """対象 ``DryrunOutput`` が存在しない (ルータが 404 に写像)。"""

    def __init__(self, output_id: uuid.UUID) -> None:
        super().__init__(f"dryrun output {output_id} not found")
        self.output_id: Final[uuid.UUID] = output_id


class DryrunStateConflictError(DryrunError):
    """``pending`` 以外への操作要求 (終端状態 / 二重操作。 ルータが 409 に写像)。"""

    def __init__(self, output_id: uuid.UUID, *, current_state: str, action: str) -> None:
        super().__init__(
            f"dryrun output {output_id} is '{current_state}'; only pending outputs can be {action}"
        )
        self.output_id: Final[uuid.UUID] = output_id
        self.current_state: Final[str] = current_state
        self.action: Final[str] = action


class _Uploader(Protocol):
    """``DryrunService`` が依存する uploader の最小 I/F (DI 用)。

    実体は :class:`~ymg_backend.infrastructure.youtube.uploader.YouTubeUploader`。
    ``upload`` は投稿成功時に ``post.youtube_video_id`` / ``post.posted_at`` をセットし
    ``session.flush()` する (commit は呼び出し側)。 戻り値は ``youtube_video_id``。
    """

    async def upload(self, *, session: AsyncSession, post: Post) -> str: ...


def _utcnow() -> datetime:
    """UTC aware の現在時刻を返す (``DryrunOutput`` の timestamp 列は tz aware)。"""
    return datetime.now(UTC)


class DryrunService:
    """dryrun 成果物の承認 / 却下 / 自動期限切れを行うステートレスサービス。

    Args:
        uploader: 承認時に YouTube 投稿を行う uploader (DI)。
        storage: 却下 / 期限切れ時にプレビュー動画を削除する :class:`StorageAdapter` (DI)。
    """

    __slots__ = ("_storage", "_uploader")

    def __init__(self, *, uploader: _Uploader, storage: StorageAdapter) -> None:
        self._uploader: Final[_Uploader] = uploader
        self._storage: Final[StorageAdapter] = storage

    async def approve(
        self,
        *,
        session: AsyncSession,
        output_id: uuid.UUID,
    ) -> DryrunOutput:
        """``pending`` の成果物を承認し YouTube へ投稿、 ``posted`` まで進める。

        ``pending → approved → posted`` と遷移する。 ``approved`` で ``reviewed_at`` を
        記録した後 uploader 経由で投稿し (``post.youtube_video_id`` / ``post.posted_at``
        は uploader がセット)、 成功したら ``posted`` + ``posted_at`` にする。

        Args:
            session: ``DryrunOutput`` / ``Post`` を読み書きする ``AsyncSession``。
            output_id: 承認対象の ``DryrunOutput.id``。

        Returns:
            ``posted`` まで進んだ ``DryrunOutput`` (flush 済み, commit は呼び出し側)。

        Raises:
            DryrunNotFoundError: 対象が存在しない場合。
            DryrunStateConflictError: ``pending`` 以外の場合。
            ~ymg_backend.domain.errors.errors.YmgError: uploader 由来の投稿失敗。
        """
        output = await self._load(session, output_id)
        self._ensure_pending(output, action="approved")

        post = await self._load_post(session, output.post_id)
        now = _utcnow()
        output.state = "approved"
        output.reviewed_at = now

        video_id = await self._uploader.upload(session=session, post=post)

        output.state = "posted"
        output.posted_at = _utcnow()

        await write_audit_log(
            session,
            action=_ACTION_APPROVED,
            actor=_ACTOR,
            target_type=_TARGET_TYPE,
            target_id=str(output_id),
            payload={"youtube_video_id": video_id, "post_id": str(output.post_id)},
        )
        await session.flush()
        return output

    async def reject(
        self,
        *,
        session: AsyncSession,
        output_id: uuid.UUID,
        reason: str,
    ) -> DryrunOutput:
        """``pending`` の成果物を却下し、 理由を保存 + プレビュー動画を削除する。

        ``reason`` を先に検証 (min4) してから状態を変える。 検証失敗時は一切の副作用を
        起こさない。 動画削除は冪等 (``missing_ok=True``)。 保存された ``reject_reason`` は
        次回 planner が避けるべき理由として読む記録になる (注入は T096)。

        Args:
            session: ``DryrunOutput`` を更新する ``AsyncSession``。
            output_id: 却下対象の ``DryrunOutput.id``。
            reason: 却下理由 (前後空白を除いて最低 4 文字)。

        Returns:
            ``rejected`` になった ``DryrunOutput`` (flush 済み, commit は呼び出し側)。

        Raises:
            ValueError: ``reason`` が min4 未満の場合 (境界での入力検証)。
            DryrunNotFoundError: 対象が存在しない場合。
            DryrunStateConflictError: ``pending`` 以外の場合。
        """
        normalized = self._validate_reason(reason)
        output = await self._load(session, output_id)
        self._ensure_pending(output, action="rejected")

        output.state = "rejected"
        output.reject_reason = normalized
        output.reviewed_at = _utcnow()
        # プレビュー動画は不要になるので削除 (冪等: 既に消えていても許容)。
        self._storage.delete(output.video_uri, missing_ok=True)

        await write_audit_log(
            session,
            action=_ACTION_REJECTED,
            actor=_ACTOR,
            target_type=_TARGET_TYPE,
            target_id=str(output_id),
            payload={"reason": normalized, "post_id": str(output.post_id)},
        )
        await session.flush()
        return output

    async def auto_expire(
        self,
        *,
        session: AsyncSession,
        now: datetime | None = None,
    ) -> list[DryrunOutput]:
        """``created_at`` が 7 日より古い ``pending`` を一括で ``auto_expired`` にする。

        retention 日次ジョブが呼ぶ。 各行を ``auto_expired`` + ``auto_expired_at`` にし、
        プレビュー動画を削除 (冪等) し、 監査ログを残す。 1 件ずつ audit を書くのは
        個別トレーサビリティのため (共有契約 §auto_expire の擬似コードに従う)。

        Args:
            session: ``DryrunOutput`` を読み書きする ``AsyncSession``。
            now: 基準時刻 (省略時は現在 UTC)。 cutoff = ``now - 7 日``。

        Returns:
            期限切れにした ``DryrunOutput`` の一覧 (flush 済み, commit は呼び出し側)。
            該当が無ければ空リスト。
        """
        current = now if now is not None else _utcnow()
        cutoff = current - timedelta(days=_RETENTION_DAYS)
        stmt = select(DryrunOutput).where(
            DryrunOutput.state == _PENDING,
            DryrunOutput.created_at < cutoff,
        )
        rows = (await session.execute(stmt)).scalars().all()

        expired: list[DryrunOutput] = []
        for output in rows:
            output.state = "auto_expired"
            output.auto_expired_at = current
            self._storage.delete(output.video_uri, missing_ok=True)
            await write_audit_log(
                session,
                action=_ACTION_AUTO_EXPIRED,
                actor=_ACTOR,
                target_type=_TARGET_TYPE,
                target_id=str(output.id),
                payload={"post_id": str(output.post_id), "retention_days": _RETENTION_DAYS},
            )
            expired.append(output)

        if expired:
            await session.flush()
        return expired

    # ------------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------------
    @staticmethod
    async def _load(session: AsyncSession, output_id: uuid.UUID) -> DryrunOutput:
        """``output_id`` の ``DryrunOutput`` を取得する (不在は例外)。"""
        output = await session.get(DryrunOutput, output_id)
        if output is None:
            raise DryrunNotFoundError(output_id)
        return output

    @staticmethod
    async def _load_post(session: AsyncSession, post_id: uuid.UUID) -> Post:
        """承認対象 ``DryrunOutput`` に紐づく ``Post`` を取得する (不在は例外)。"""
        post = await session.get(Post, post_id)
        if post is None:
            # FK 上ありえないが、 防御的に not found 扱いにする (整合性破れの可視化)。
            raise DryrunNotFoundError(post_id)
        return post

    @staticmethod
    def _ensure_pending(output: DryrunOutput, *, action: str) -> None:
        """``pending`` 以外なら conflict を送出する (二重操作 / 終端状態の保護)。"""
        if output.state != _PENDING:
            raise DryrunStateConflictError(output.id, current_state=output.state, action=action)

    @staticmethod
    def _validate_reason(reason: str) -> str:
        """却下理由を検証し、 正規化 (前後空白除去) した文字列を返す。"""
        normalized = reason.strip()
        if len(normalized) < _MIN_REASON_LENGTH:
            raise ValueError(f"reject reason must be at least {_MIN_REASON_LENGTH} characters")
        return normalized


__all__ = [
    "DryrunError",
    "DryrunNotFoundError",
    "DryrunService",
    "DryrunStateConflictError",
]
