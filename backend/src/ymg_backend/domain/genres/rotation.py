"""承認済み WeeklyPlan を ``genres`` テーブルへ反映する rotation (T105 / FR-038)。

週次の改善計画 (``WeeklyPlan``) が人間承認された後、 その内容を ``genres`` 辞書へ適用し、
日次サイクルの許可ジャンル (``Genre.enabled=True``) を更新するためのモジュール。
``api/plans.py:_load_enabled_genres`` が ``Genre.enabled=True`` を拾う形なので、 ここで
``enabled`` / ``role`` を書き換えるだけで日次側 (``daily_cycle``) は無改修で追従する。

反映ルール (US3 契約 / ADR-0033):

- ``avoid_genres``        → ``Genre.enabled = False`` (避けるジャンルを無効化)
- ``experiment_slots[].genre`` → ``Genre.enabled = True`` かつ ``role = "experiment"``
  (行が無ければ最小限の placeholder で新規作成。 ``display_name`` / ``description`` は
   後段の管理 UI / 手動補完を想定した暫定値)
- ``genre_distribution`` のジャンル → ``Genre.enabled = True`` を保証
  (配分に載っているのに無効化されていると日次計画が genre 辞書照合で弾かれるため)

``avoid_genres`` と ``experiment_slots`` / ``genre_distribution`` が同一ジャンルを指す矛盾は、
**enable 優先** で解消する (配分・実験に載るジャンルは必ず有効化される)。 ``avoid`` は
「配分にも実験にも無いジャンルを無効化する」意味として扱う。

``Genre.role`` は ENUM ではなく自由文字列 (``main`` / ``extension`` / ``experiment``、
``alembic/versions/001_initial.py`` のシード値に整合)。 本モジュールは experiment_slot の
ジャンルを ``experiment`` に設定するのみで、 ``experiment → extension / main`` の昇格は
``recommend`` の判定 + 管理 UI 承認 (``api/genres.py`` promote) の責務とする。

commit は呼び出し側 (approve API または承認後フック) が行う。 ここでは新規 ``Genre`` 行の
``add`` と既存行のフィールド更新までで、 ``flush`` は audit 書き込み (``write_audit_log``)
経由で発生する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from loguru import logger
from sqlalchemy import select

from ymg_backend.domain.plans.schemas import WeeklyPlan
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import Genre

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.db.models import Plan

# experiment_slot のジャンルへ設定する role (シード値 ``experiment`` に整合)。
EXPERIMENT_ROLE: Final[str] = "experiment"

# 新規作成する Genre 行の placeholder description (管理 UI / 手動補完までの暫定値)。
_NEW_GENRE_DESCRIPTION: Final[str] = "experiment_slot 投入により自動作成 (要レビュー)"

# audit action 名 (rotation 適用を 1 件記録)。
_AUDIT_ACTION: Final[str] = "weekly_rotation_applied"


@dataclass(frozen=True, slots=True)
class RotationResult:
    """rotation 適用の差分サマリ (audit / ログ / 呼び出し側の検証用、 不変)。

    Attributes:
        disabled: ``enabled`` を ``True`` → ``False`` に変更したジャンル名 (昇順)。
        enabled: ``enabled`` を ``False`` → ``True`` に変更 / 新規作成したジャンル名 (昇順)。
        role_changes: ``role`` を変更したジャンルの ``(genre, new_role)`` 組 (昇順)。
    """

    disabled: tuple[str, ...]
    enabled: tuple[str, ...]
    role_changes: tuple[tuple[str, str], ...]


def _parse_weekly_plan(plan_payload: dict[str, Any]) -> WeeklyPlan:
    """``Plan.payload`` (JSONB) を ``WeeklyPlan`` として読む。

    payload は ``WeeklyPlanGenerator._persist_plan`` が ``model_dump(mode="json")`` で
    書いた形を想定する。 ここでは genre 辞書照合 (context) は行わない — 承認済み計画は
    生成時に既に辞書照合済みであり、 反映フェーズで辞書外ジャンルを弾くと既存の
    experiment_slot 新規作成が成り立たないため (新ジャンルは反映時点ではまだ辞書に無い)。
    """
    return WeeklyPlan.model_validate(plan_payload)


async def apply_weekly_rotation(session: AsyncSession, plan: Plan) -> RotationResult:
    """承認済み WeeklyPlan を ``genres`` テーブルへ反映し、 差分を返す (T105 / FR-038)。

    ``plan.payload`` を ``WeeklyPlan`` として読み、 以下を適用する:

    1. ``genre_distribution`` / ``experiment_slots`` に載るジャンルを ``enabled=True`` に保証。
    2. ``experiment_slots[].genre`` は ``role="experiment"`` に設定。 ``genres`` に行が
       無ければ最小限の placeholder で新規作成する。
    3. ``avoid_genres`` のうち上記で有効化しなかったジャンルを ``enabled=False`` に。

    変更があれば ``audit_log`` に 1 件記録する (commit は呼び出し側)。

    Args:
        session: ``genres`` を参照・更新する AsyncSession (commit は呼び出し側)。
        plan: 承認済み週次計画レコード (``cycle="weekly"`` 前提)。 ``payload`` を読む。

    Returns:
        適用差分の :class:`RotationResult` (disabled / enabled / role_changes)。

    Raises:
        ValueError: ``plan.cycle`` が ``"weekly"`` でない場合 (境界での入力検証)。
        pydantic.ValidationError: ``plan.payload`` が ``WeeklyPlan`` として不正な場合。
    """
    if plan.cycle != "weekly":
        raise ValueError(f"apply_weekly_rotation requires a weekly plan, got cycle={plan.cycle!r}")

    weekly = _parse_weekly_plan(plan.payload)

    # enable すべきジャンル集合 = 配分 + 実験枠。 avoid と衝突した場合は enable を優先する。
    experiment_genres = {slot.genre for slot in weekly.experiment_slots}
    enable_targets = set(weekly.genre_distribution) | experiment_genres
    disable_targets = {g for g in weekly.avoid_genres if g not in enable_targets}

    # 関係するジャンルを 1 クエリで読み、 name → Genre の索引にする (個別 get の N+1 回避)。
    involved = enable_targets | disable_targets
    existing = await _load_genres(session, involved)

    disabled: list[str] = []
    enabled: list[str] = []
    role_changes: list[tuple[str, str]] = []

    # 1. experiment 枠: enabled=True + role=experiment (無ければ新規作成)。
    for name in sorted(experiment_genres):
        genre = existing.get(name)
        if genre is None:
            genre = _create_experiment_genre(name)
            session.add(genre)
            existing[name] = genre
            enabled.append(name)
            role_changes.append((name, EXPERIMENT_ROLE))
            continue
        if not genre.enabled:
            genre.enabled = True
            enabled.append(name)
        if genre.role != EXPERIMENT_ROLE:
            genre.role = EXPERIMENT_ROLE
            role_changes.append((name, EXPERIMENT_ROLE))

    # 2. 配分ジャンル: enabled=True を保証 (role は触らない)。
    for name in sorted(weekly.genre_distribution):
        genre = existing.get(name)
        if genre is None:
            # 配分に載るが辞書に無いジャンルは生成時の照合で弾かれているはずだが、
            # 防御的に experiment placeholder として作成し有効化する。
            genre = _create_experiment_genre(name)
            session.add(genre)
            existing[name] = genre
            enabled.append(name)
            role_changes.append((name, EXPERIMENT_ROLE))
            continue
        if not genre.enabled:
            genre.enabled = True
            enabled.append(name)

    # 3. avoid ジャンル: enabled=False (enable 対象と衝突しないもののみ)。
    for name in sorted(disable_targets):
        genre = existing.get(name)
        if genre is None:
            continue  # 辞書に無いジャンルは無効化対象なし (no-op)。
        if genre.enabled:
            genre.enabled = False
            disabled.append(name)

    result = RotationResult(
        disabled=tuple(sorted(disabled)),
        enabled=tuple(sorted(enabled)),
        role_changes=tuple(sorted(role_changes)),
    )

    if result.disabled or result.enabled or result.role_changes:
        await write_audit_log(
            session,
            action=_AUDIT_ACTION,
            target_type="plan",
            target_id=str(plan.id),
            payload={
                "target_week_start": weekly.target_week_start.isoformat(),
                "disabled": list(result.disabled),
                "enabled": list(result.enabled),
                "role_changes": [list(rc) for rc in result.role_changes],
            },
        )

    logger.info(
        "genres.apply_weekly_rotation applied",
        plan_id=str(plan.id),
        disabled=result.disabled,
        enabled=result.enabled,
        role_changes=result.role_changes,
    )
    return result


async def _load_genres(session: AsyncSession, names: set[str]) -> dict[str, Genre]:
    """``names`` に含まれる ``Genre`` 行を ``name -> Genre`` の索引で取得する。

    ``names`` が空なら DB へ問い合わせず空 dict を返す。
    """
    if not names:
        return {}
    stmt = select(Genre).where(Genre.name.in_(names))
    rows = (await session.execute(stmt)).scalars().all()
    return {row.name: row for row in rows}


def _create_experiment_genre(name: str) -> Genre:
    """experiment_slot 用の最小限 ``Genre`` 行を生成する (placeholder)。

    ``display_name`` は ``name`` をそのまま流用し、 ``description`` は暫定値、 ``role`` は
    ``experiment``、 ``enabled=True``。 BPM 等は ``None`` (後段の管理 UI / 手動補完を想定)。
    ``created_at`` / ``updated_at`` は DB の ``server_default`` (``now()``) に委ねる。
    """
    return Genre(
        name=name,
        display_name=name,
        description=_NEW_GENRE_DESCRIPTION,
        role=EXPERIMENT_ROLE,
        enabled=True,
    )


__all__ = [
    "EXPERIMENT_ROLE",
    "RotationResult",
    "apply_weekly_rotation",
]
