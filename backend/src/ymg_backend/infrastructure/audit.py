"""audit_log への書き込みヘルパ (ADR-0028, ADR-0031)。

panic-stop や scheduler 切替 / provider 切替などの運用操作を `audit_log`
テーブルへ 1 行 insert する。 data-model.md の `audit_log` カラムに整合。

ORM model 層 (T021) に依存しないよう、 SQLAlchemy Core の軽量 Table 定義を
このモジュール内に閉じて持ち、 呼び出し側から渡された `AsyncSession` で insert
する。 これにより monorepo の他タスクの成果物と疎結合を保つ。
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import Column, MetaData, String, Table, insert
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

# デフォルト actor。 単一運用者前提 (CLAUDE.md / ADR-0031)。
DEFAULT_ACTOR: Final[str] = "seita"

# audit_log への Core Table 定義 (data-model.md `audit_log` と整合)。
# id / created_at は DB 側デフォルト (gen は明示) に任せ、 ここでは挿入列のみ宣言。
_metadata: Final[MetaData] = MetaData()

audit_log_table: Final[Table] = Table(
    "audit_log",
    _metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("actor", String, nullable=False),
    Column("action", String, nullable=False),
    Column("target_type", String, nullable=True),
    Column("target_id", String, nullable=True),
    Column("payload", JSONB, nullable=True),
)


async def write_audit_log(
    session: AsyncSession,
    *,
    action: str,
    actor: str = DEFAULT_ACTOR,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """`audit_log` に 1 行 insert し、 生成した行の UUID を返す。

    Args:
        session: 呼び出し側が管理する SQLAlchemy AsyncSession。 commit は
            呼び出し側のトランザクション境界に委ねる (ここでは flush のみ)。
        action: 操作種別 (例: 'panic_stop_invoked', 'video_set_private')。 必須。
        actor: 操作主体。 既定 'seita' (単一運用者)。
        target_type: 対象種別 (例: 'video', 'post', 'plan')。 任意。
        target_id: 対象 ID (文字列化済)。 任意。
        payload: 付随メタ情報。 任意の JSON シリアライズ可能 dict。

    Returns:
        挿入した audit_log 行の id (UUID)。

    Raises:
        ValueError: `action` または `actor` が空文字の場合 (境界での入力検証)。
    """
    if not action:
        raise ValueError("action must be a non-empty string")
    if not actor:
        raise ValueError("actor must be a non-empty string")

    row_id = uuid.uuid4()
    await session.execute(
        insert(audit_log_table).values(
            id=row_id,
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
        )
    )
    await session.flush()
    return row_id
