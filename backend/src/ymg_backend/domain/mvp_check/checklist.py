"""MVP リリース判定 6 項目の DB 評価ロジック (T127, ADR-0035 §6)。

ADR-0035 が定める「MVP として公開可能か」の判定 6 項目を、 DB の実績から
``green`` (条件達成) / ``red`` (未達) に写像する read-only ドメイン層。 各項目は対応
テーブルへの件数クエリ (``select(func.count())...``) を 1 本ずつ流し、 結果がしきい値以上
なら ``green``、 未満なら ``red`` とする。

判定 6 項目 (固定 id / 固定順序、 contract ``MvpCheckItem.id`` の enum6 と一致):

==================  ========================================  ====================================
id                  green 条件                                判定ソース (テーブル / 列)
==================  ========================================  ====================================
``dryrun_success``  dryrun が ``posted`` 到達した output >= 3   ``dryrun_outputs.state == 'posted'``
``acoustid_clear``  ``clear`` の track >= 1                     ``audio_tracks.acoustid_status``
``unlisted_post``   ``unlisted`` の video >= 1                  ``videos.privacy_status``
``panic_stop``      ``action == 'panic_stop'`` の行 >= 1        ``audit_log.action``
``oauth_refresh``   refresh で更新された credential >= 1        ``oauth_credentials`` (updated>created)
``slack_categories``Slack 5 カテゴリ分の送信記録 >= 5           ``usage_log`` (件数 >= 5)
==================  ========================================  ====================================

設計方針:

- 判定根拠 (テーブル / 列 / しきい値) は :data:`_CHECK_SPECS` に宣言的に集約し、
  :func:`evaluate_mvp_checklist` はそれを順に評価するだけにする (新項目追加 = spec 1 行)。
- DB は read-only。 commit / flush は行わない (件数 select のみ)。
- 各 spec の ``condition`` は本番セマンティクス (``state == 'posted'`` 等) を ``WHERE`` に
  畳み込む。 件数が 0 のテーブルは自然に ``red`` になる。

``slack_categories`` の判定ソースについて (前提の明示):

Slack 通知それ自体の専用永続化テーブルは現状存在しない。 ADR-0024 の ``usage_log`` が
LLM 呼び出しを含む運用イベントのコスト記録として 5 カテゴリ分の動作実績を代理表現できる
最も近い実テーブルであり、 本判定はそこへの件数 (>= 5) を Slack 5 カテゴリ動作の実績代理と
して扱う。 専用ログが将来追加された場合は :data:`_CHECK_SPECS` の当該行を差し替える。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import ColumnElement, func, select

from ymg_backend.infrastructure.db.models import (
    AudioTrack,
    AuditLog,
    DryrunOutput,
    OAuthCredential,
    UsageLog,
    Video,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

# contract ``MvpCheckItem.id`` の固定 6 値。
MvpCheckId = Literal[
    "dryrun_success",
    "acoustid_clear",
    "unlisted_post",
    "panic_stop",
    "oauth_refresh",
    "slack_categories",
]

# contract ``MvpCheckItem.status``。
MvpCheckStatus = Literal["green", "red"]

# panic-stop の audit action 名 (domain/panic_stop/service.py:54 `_ACTION_PANIC_STOP` と一致)。
_ACTION_PANIC_STOP: Final[str] = "panic_stop"

# 判定対象の enum literal (各 ORM の server_default / data-model.md と一致)。
_DRYRUN_STATE_POSTED: Final[str] = "posted"
_ACOUSTID_STATUS_CLEAR: Final[str] = "clear"
_PRIVACY_STATUS_UNLISTED: Final[str] = "unlisted"

# MVP として要求するしきい値 (ADR-0035: dryrun 3 本連続 / Slack 5 カテゴリ / 他は実績 1 件)。
_THRESHOLD_DRYRUN: Final[int] = 3
_THRESHOLD_SLACK_CATEGORIES: Final[int] = 5
_THRESHOLD_SINGLE: Final[int] = 1


@dataclass(frozen=True)
class MvpCheckItem:
    """MVP 判定 1 項目の結果 (contract ``MvpCheckItem`` と整合)。

    ``detail`` は将来の補足表示用に予約 (現状は常に ``None``)。
    """

    id: MvpCheckId
    label: str
    status: MvpCheckStatus
    detail: str | None = None


@dataclass(frozen=True)
class MvpChecklistResult:
    """MVP 判定 6 項目の集約結果 (contract ``MvpCheck`` と整合)。

    - ``items``: 固定 6 件・固定 id 順。
    - ``completed``: ``green`` の件数 (0..6)。
    - ``total``: 常に 6。
    """

    items: tuple[MvpCheckItem, ...]
    completed: int
    total: int


@dataclass(frozen=True)
class _CheckSpec:
    """1 項目の宣言的判定仕様 (id / 表示ラベル / 件数クエリ / green しきい値)。"""

    id: MvpCheckId
    label: str
    count_query: ColumnElement[bool] | None
    threshold: int


def _dryrun_success_condition() -> ColumnElement[bool]:
    return DryrunOutput.state == _DRYRUN_STATE_POSTED


def _acoustid_clear_condition() -> ColumnElement[bool]:
    return AudioTrack.acoustid_status == _ACOUSTID_STATUS_CLEAR


def _unlisted_post_condition() -> ColumnElement[bool]:
    return Video.privacy_status == _PRIVACY_STATUS_UNLISTED


def _panic_stop_condition() -> ColumnElement[bool]:
    return AuditLog.action == _ACTION_PANIC_STOP


def _oauth_refresh_condition() -> ColumnElement[bool]:
    # refresh で更新された (= updated_at が created_at より進んでいる) credential。
    return OAuthCredential.updated_at > OAuthCredential.created_at


def _slack_categories_condition() -> ColumnElement[bool] | None:
    # usage_log 全件を Slack 5 カテゴリ動作実績の代理として数える (絞り込みなし)。
    return None


# 判定 6 項目の宣言 (固定順序 = contract enum6 の順序)。
_CHECK_SPECS: Final[tuple[_CheckSpec, ...]] = (
    _CheckSpec(
        id="dryrun_success",
        label="dryrun 3 本連続成功",
        count_query=_dryrun_success_condition(),
        threshold=_THRESHOLD_DRYRUN,
    ),
    _CheckSpec(
        id="acoustid_clear",
        label="AcoustID 全 clear 実績",
        count_query=_acoustid_clear_condition(),
        threshold=_THRESHOLD_SINGLE,
    ),
    _CheckSpec(
        id="unlisted_post",
        label="unlisted 1 本投稿実績",
        count_query=_unlisted_post_condition(),
        threshold=_THRESHOLD_SINGLE,
    ),
    _CheckSpec(
        id="panic_stop",
        label="panic-stop 予行",
        count_query=_panic_stop_condition(),
        threshold=_THRESHOLD_SINGLE,
    ),
    _CheckSpec(
        id="oauth_refresh",
        label="OAuth refresh 実績",
        count_query=_oauth_refresh_condition(),
        threshold=_THRESHOLD_SINGLE,
    ),
    _CheckSpec(
        id="slack_categories",
        label="Slack 5 カテゴリ動作",
        count_query=_slack_categories_condition(),
        threshold=_THRESHOLD_SLACK_CATEGORIES,
    ),
)

# spec の id → 件数クエリの起点テーブル (select_from に渡す ORM クラス)。
# condition は WHERE に畳み込むため、 件数対象テーブルは id ごとに明示的に固定する。
_FROM_MODEL_BY_ID: Final[dict[MvpCheckId, type]] = {
    "dryrun_success": DryrunOutput,
    "acoustid_clear": AudioTrack,
    "unlisted_post": Video,
    "panic_stop": AuditLog,
    "oauth_refresh": OAuthCredential,
    "slack_categories": UsageLog,
}


async def _count_for_spec(session: AsyncSession, spec: _CheckSpec) -> int:
    """spec 1 項目の件数クエリを流し、 件数を返す (read-only)。"""
    stmt = select(func.count()).select_from(_FROM_MODEL_BY_ID[spec.id])
    if spec.count_query is not None:
        stmt = stmt.where(spec.count_query)
    return (await session.execute(stmt)).scalar_one()


def _evaluate_one(spec: _CheckSpec, count: int) -> MvpCheckItem:
    """件数としきい値から 1 項目の ``green`` / ``red`` を確定する。"""
    status: MvpCheckStatus = "green" if count >= spec.threshold else "red"
    return MvpCheckItem(id=spec.id, label=spec.label, status=status)


async def evaluate_mvp_checklist(session: AsyncSession) -> MvpChecklistResult:
    """MVP 判定 6 項目を DB から評価して集約結果を返す (read-only)。

    各項目を :data:`_CHECK_SPECS` の順に件数評価し、 ``green`` 件数を ``completed`` に
    集約する。 commit / flush は行わない (HTTP 側がトランザクション境界)。
    """
    items: list[MvpCheckItem] = []
    for spec in _CHECK_SPECS:
        count = await _count_for_spec(session, spec)
        items.append(_evaluate_one(spec, count))

    completed = sum(1 for item in items if item.status == "green")
    return MvpChecklistResult(items=tuple(items), completed=completed, total=len(items))


def mvp_check_ids() -> Sequence[MvpCheckId]:
    """contract 固定順序の 6 つの id を返す (API 層 / テスト共有用)。"""
    return tuple(spec.id for spec in _CHECK_SPECS)


__all__ = [
    "MvpCheckId",
    "MvpCheckItem",
    "MvpCheckStatus",
    "MvpChecklistResult",
    "evaluate_mvp_checklist",
    "mvp_check_ids",
]
