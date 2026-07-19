"""dryrun レビュー ライフサイクルの統合テスト (T091)。

`DryrunService` の 3 つの状態遷移を実 Postgres 上で 1 本ずつ通す:

1. ``pending → approved → posted``:
   uploader stub が ``youtube_video_id`` を返し、 ``DryrunOutput.state`` が
   最終的に ``posted`` へ、 ``Post.youtube_video_id`` / ``posted_at`` がセットされる。
2. ``pending → rejected``:
   ``reason`` (min4) が ``DryrunOutput.reject_reason`` に保存され、 動画が storage から
   delete され (冪等)、 planner への否認理由フィードバックとして「DB の rejected 行に
   reject_reason が残る」ことを観測する (次回 planner が REJECTED_REASONS 経由で読む契約)。
3. ``pending → auto_expired``:
   freezegun で ``created_at`` から 7 日経過させ、 retention 相当の
   ``auto_expire(now=...)`` が当該行を ``auto_expired`` へ遷移させ動画を delete する。

外部依存の扱い (起動禁止):

- YouTube: 実 ``YouTubeUploader`` は使わず、 ``upload(session=, post=) -> youtube_video_id``
  を満たす stub を ``DryrunService`` に DI する。 stub は本物の uploader 契約と同じく
  ``post.youtube_video_id`` / ``post.posted_at`` をセットして文字列 id を返す。
- storage: 実 ``StorageAdapter`` を ``file://`` (tmp_path) で使い、 ダミー mp4 を実際に
  書き出して、 reject / auto_expire で物理削除されることまで観測する (実 ffmpeg 不要)。
- audit: 実 ``write_audit_log`` がそのまま走り ``audit_log`` 行を残す。

DB: ``DryrunOutput`` / ``Post`` は Postgres 固有型 (UUID / ENUM / JSONB) を使うため
SQLite では動かせない。 実 Postgres へ接続できない環境では skip する
(CI の postgres service では実行される)。

``DryrunService`` が未実装の TDD RED 段階では import 不能のため、 モジュール解決失敗時も
skip する (実装が入った時点で本テストが緑になる契約)。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from freezegun import freeze_time
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ymg_backend.infrastructure.audit import audit_log_table
from ymg_backend.infrastructure.db.models import (
    Base,
    DryrunOutput,
    Genre,
    Plan,
    Post,
)
from ymg_backend.infrastructure.db.models.base import _ENUM_VALUES
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

# 未実装モジュール (TDD RED 段階)。 import 失敗時は本ファイル全体を collection 時点で skip。
# 公開シンボルは module オブジェクト経由で参照し、 importorskip ゲートより前に解決させない。
dryrun_service_mod = pytest.importorskip("ymg_backend.domain.dryrun.service")

pytestmark = pytest.mark.integration

_TARGET_GENRE = "lo-fi hip-hop"
_DUMMY_MP4 = b"\x00\x00\x00\x18ftypmp42dummy-mp4-for-dryrun"


# --- DB 接続ヘルパ (test_daily_cycle_pipeline.py 踏襲) ------------------------------


def _sync_url() -> str:
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def _async_url() -> str:
    user = os.environ.get("POSTGRES_USER", "ymg")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "ymg")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


def _require_db() -> None:
    """実 Postgres へ接続できなければ skip する (同期ドライバで軽く疎通確認)。"""
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url())
    try:
        engine.connect().close()
    except OperationalError as exc:
        pytest.skip(f"postgres へ接続できないため skip: {exc}")
    finally:
        engine.dispose()


def _create_enum_types(conn: Any) -> None:
    """native ENUM 型を idempotent に作成する。

    ORM カラムは ``create_type=False`` で既存型を参照するだけなので、 ``create_all`` は
    ``CREATE TYPE`` を発行しない。 マイグレーション適用順に依存せず本テスト単体で
    スキーマを成立させるため、 ``base.py`` の ``_ENUM_VALUES`` と同一定義で型を先に作る。
    """
    for name, values in _ENUM_VALUES.items():
        labels = ", ".join(f"'{v}'" for v in values)
        conn.execute(
            text(
                "DO $$ BEGIN "
                f"CREATE TYPE {name} AS ENUM ({labels}); "
                "EXCEPTION WHEN duplicate_object THEN null; END $$;"
            )
        )


# --- uploader stub (approve 経路) -------------------------------------------------


class _StubUploader:
    """``YouTubeUploader.upload`` 契約を満たす stub。

    本物同様 ``post.youtube_video_id`` / ``post.posted_at`` をセットして flush し、
    生成した ``youtube_video_id`` を返す。 呼び出し回数を記録する。
    """

    def __init__(self, video_id: str) -> None:
        self._video_id = video_id
        self.calls: list[uuid.UUID] = []

    async def upload(self, *, session: AsyncSession, post: Post) -> str:
        self.calls.append(post.id)
        post.youtube_video_id = self._video_id
        post.posted_at = datetime.now(UTC)
        post.status = "posted"
        await session.flush()
        return self._video_id


class _UploaderMustNotBeCalled:
    """reject / auto_expire では upload は呼ばれない契約。 呼ばれたら test を落とす。"""

    async def upload(self, *, session: AsyncSession, post: Post) -> str:
        raise AssertionError("uploader.upload は approve 以外では呼ばれてはならない")


# --- fixtures ---------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """実 Postgres の AsyncSession を払い出す。 ENUM 型 + スキーマを idempotent に作成する。

    各テストは実コミットを行うため、 前回 run の残骸 (例: ``youtube_video_id`` の一意制約に
    衝突するハードコード値) で再実行が壊れる。 共有 DB でも冪等に走るよう、 本テストが書き込む
    テーブルを依存順 (子→親) で先に空にしてから払い出す。 ``genres`` は共有シードのため残す。
    """
    _require_db()
    engine = create_async_engine(_async_url())
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: _create_enum_types(sync_conn))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("TRUNCATE TABLE dryrun_outputs, audit_log, posts, plans CASCADE"))
    sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    try:
        async with sessionmaker() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
def storage(tmp_path: Any) -> StorageAdapter:
    base = f"file://{tmp_path}/outputs"
    return StorageAdapter(base_uri=base)


@pytest_asyncio.fixture
async def seeded_genre(db_session: AsyncSession) -> str:
    """Post の FK 先となる ``lo-fi hip-hop`` ジャンルを enabled で投入する。"""
    existing = await db_session.get(Genre, _TARGET_GENRE)
    if existing is None:
        db_session.add(
            Genre(
                name=_TARGET_GENRE,
                display_name="Lo-Fi Hip Hop",
                bpm_min=70,
                bpm_max=90,
                description="dryrun ライフサイクル統合テスト用シード。",
                role="primary",
                enabled=True,
            )
        )
        await db_session.commit()
    return _TARGET_GENRE


async def _seed_pending_output(
    *,
    session: AsyncSession,
    storage: StorageAdapter,
    genre: str,
    created_at: datetime | None = None,
) -> DryrunOutput:
    """Plan → Post → pending DryrunOutput を 1 件作り、 動画ダミーを storage へ書く。

    ``video_uri`` は base_uri 配下の相対パスとし、 storage に実バイトを書き込む。
    ``created_at`` を渡すと DryrunOutput の生成時刻を上書きする (auto_expire の経過判定用)。
    """
    plan_id = uuid.uuid4()
    post_id = uuid.uuid4()
    output_id = uuid.uuid4()
    video_uri = storage.resolve_uri(f"{post_id}/video.mp4")
    storage.write_bytes(video_uri, _DUMMY_MP4)

    # 親→子の順に flush する。 これらのモデル間には relationship が無く、
    # 単一 flush だと unit-of-work が FK 依存を解決できず子 (DryrunOutput) が
    # 先に INSERT されて FK 違反になる。 依存順を明示して保証する。
    session.add(
        Plan(
            id=plan_id,
            cycle="daily",
            target_date=datetime.now(UTC).date(),
            target_week_start=None,
            payload={"plan_id": str(plan_id), "posts": []},
            rationale="dryrun lifecycle seed",
            status="generated",
            llm_provider="openai",
            llm_model="gpt-4.1",
            llm_prompt_version="planner/system_v1",
        )
    )
    await session.flush()
    session.add(
        Post(
            id=post_id,
            plan_id=plan_id,
            position=0,
            genre=genre,
            payload={"genre": genre},
            status="generated",
            final_title="Lo-Fi Hip Hop 60min | 夜の作業用",
            final_description="夜のチル作業用 BGM。 #lofi #chill #study",
            thumbnail_uri=storage.resolve_uri(f"{post_id}/thumb.png"),
            video_uri=video_uri,
        )
    )
    await session.flush()
    output = DryrunOutput(
        id=output_id,
        post_id=post_id,
        state="pending",
        video_uri=video_uri,
    )
    if created_at is not None:
        output.created_at = created_at
    session.add(output)
    await session.commit()
    return output


def _build_service(
    *,
    uploader: Any,
    storage: StorageAdapter,
) -> Any:
    """``DryrunService`` を契約どおり ``uploader`` / ``storage`` の DI で構築する。"""
    return dryrun_service_mod.DryrunService(uploader=uploader, storage=storage)


async def _count_audit(session: AsyncSession, *, action: str, target_id: str) -> int:
    """指定 action / target_id の audit_log 行数を数える (フィードバック / 監査の観測)。"""
    result = await session.execute(
        select(audit_log_table.c.id).where(
            audit_log_table.c.action == action,
            audit_log_table.c.target_id == target_id,
        )
    )
    return len(result.all())


# --- テスト本体: approve → posted -------------------------------------------------


@pytest.mark.asyncio
async def test_approve_transitions_pending_to_posted(
    db_session: AsyncSession,
    seeded_genre: str,
    storage: StorageAdapter,
) -> None:
    """pending を承認すると uploader が投稿し state=posted / youtube_video_id がセットされる。"""
    output = await _seed_pending_output(session=db_session, storage=storage, genre=seeded_genre)
    video_id = "yt-video-abc123"
    uploader = _StubUploader(video_id)
    service = _build_service(uploader=uploader, storage=storage)

    result = await service.approve(session=db_session, output_id=output.id)
    await db_session.commit()

    assert isinstance(result, DryrunOutput)
    assert result.state == "posted"
    assert result.posted_at is not None
    assert result.reviewed_at is not None
    # uploader は対象 Post で 1 回だけ呼ばれる。
    assert uploader.calls == [output.post_id]

    # DB 再読込で永続化を確認。 ``populate_existing`` で identity-map の expired インスタンスへの
    # 同期 lazy-load (MissingGreenlet) を避け、 非同期パスで新規 SELECT させる。
    reloaded = await db_session.get(DryrunOutput, output.id, populate_existing=True)
    assert reloaded is not None
    assert reloaded.state == "posted"

    post = await db_session.get(Post, output.post_id, populate_existing=True)
    assert post is not None
    assert post.youtube_video_id == video_id
    assert post.posted_at is not None

    # 動画は approve 経路では削除されない (投稿成功後も配信可)。
    assert storage.exists(output.video_uri)

    # audit: dryrun_approved が 1 行。
    assert await _count_audit(db_session, action="dryrun_approved", target_id=str(output.id)) == 1


@pytest.mark.asyncio
async def test_approve_non_pending_conflicts(
    db_session: AsyncSession,
    seeded_genre: str,
    storage: StorageAdapter,
) -> None:
    """既に終端状態の output を再承認しようとすると例外 (= 409 に写像される) になる。"""
    output = await _seed_pending_output(session=db_session, storage=storage, genre=seeded_genre)
    service = _build_service(uploader=_StubUploader("yt-1"), storage=storage)
    await service.approve(session=db_session, output_id=output.id)
    await db_session.commit()

    with pytest.raises(Exception):  # noqa: B017 - service 固有例外 (Conflict 系) を許容
        await service.approve(session=db_session, output_id=output.id)


# --- テスト本体: reject -----------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_saves_reason_deletes_video_and_feeds_planner(
    db_session: AsyncSession,
    seeded_genre: str,
    storage: StorageAdapter,
) -> None:
    """却下で reason 保存 + 動画削除 + planner フィードバック源 (rejected 行) が残る。"""
    output = await _seed_pending_output(session=db_session, storage=storage, genre=seeded_genre)
    assert storage.exists(output.video_uri)
    reason = "サムネが暗すぎて視認性が低い"
    uploader = _UploaderMustNotBeCalled()
    service = _build_service(uploader=uploader, storage=storage)

    result = await service.reject(session=db_session, output_id=output.id, reason=reason)
    await db_session.commit()

    assert result.state == "rejected"
    assert result.reject_reason == reason
    assert result.reviewed_at is not None

    # 動画は物理削除される (冪等な delete)。
    assert not storage.exists(output.video_uri)

    # planner フィードバック源: rejected 行の reject_reason を新しい順に集約できる。
    rejected_reasons = (
        (
            await db_session.execute(
                select(DryrunOutput.reject_reason)
                .where(DryrunOutput.state == "rejected")
                .order_by(DryrunOutput.reviewed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    assert reason in rejected_reasons

    # audit: dryrun_rejected が 1 行。
    assert await _count_audit(db_session, action="dryrun_rejected", target_id=str(output.id)) == 1


@pytest.mark.asyncio
async def test_reject_short_reason_rejected(
    db_session: AsyncSession,
    seeded_genre: str,
    storage: StorageAdapter,
) -> None:
    """却下理由 min4 未満は service 層で弾かれ、 状態は pending のまま動画も残る。"""
    output = await _seed_pending_output(session=db_session, storage=storage, genre=seeded_genre)
    service = _build_service(uploader=_UploaderMustNotBeCalled(), storage=storage)

    with pytest.raises(Exception):  # noqa: B017 - 入力検証例外 (ValueError 等) を許容
        await service.reject(session=db_session, output_id=output.id, reason="ng")

    reloaded = await db_session.get(DryrunOutput, output.id, populate_existing=True)
    assert reloaded is not None
    assert reloaded.state == "pending"
    assert storage.exists(output.video_uri)


# --- テスト本体: auto_expire ------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_expire_after_7_days_expires_and_deletes_video(
    db_session: AsyncSession,
    seeded_genre: str,
    storage: StorageAdapter,
) -> None:
    """created_at から 7 日経過した pending を retention 相当処理が auto_expired にする。"""
    created_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    output = await _seed_pending_output(
        session=db_session,
        storage=storage,
        genre=seeded_genre,
        created_at=created_at,
    )
    assert storage.exists(output.video_uri)
    service = _build_service(uploader=_UploaderMustNotBeCalled(), storage=storage)

    # 7 日 + 1 時間後を「現在」として auto_expire を駆動する (freezegun で固定)。
    now = created_at + timedelta(days=7, hours=1)
    with freeze_time(now):
        expired = await service.auto_expire(session=db_session)
    await db_session.commit()

    expired_ids = {o.id for o in expired}
    assert output.id in expired_ids

    reloaded = await db_session.get(DryrunOutput, output.id, populate_existing=True)
    assert reloaded is not None
    assert reloaded.state == "auto_expired"
    assert reloaded.auto_expired_at is not None

    # 動画は物理削除される。
    assert not storage.exists(output.video_uri)

    # audit: dryrun_auto_expired が 1 行。
    assert (
        await _count_audit(db_session, action="dryrun_auto_expired", target_id=str(output.id)) == 1
    )


@pytest.mark.asyncio
async def test_auto_expire_keeps_recent_pending(
    db_session: AsyncSession,
    seeded_genre: str,
    storage: StorageAdapter,
) -> None:
    """7 日未満の pending は auto_expire の対象外 (動画も残り state は pending)。"""
    created_at = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    output = await _seed_pending_output(
        session=db_session,
        storage=storage,
        genre=seeded_genre,
        created_at=created_at,
    )
    service = _build_service(uploader=_UploaderMustNotBeCalled(), storage=storage)

    now = created_at + timedelta(days=3)  # 7 日未満
    with freeze_time(now):
        expired = await service.auto_expire(session=db_session)
    await db_session.commit()

    assert all(o.id != output.id for o in expired)
    reloaded = await db_session.get(DryrunOutput, output.id, populate_existing=True)
    assert reloaded is not None
    assert reloaded.state == "pending"
    assert storage.exists(output.video_uri)
