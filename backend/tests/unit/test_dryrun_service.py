"""``DryrunService`` の単体テスト (domain/dryrun/service.py, T093 の TDD 先行)。

実 DB / 実 YouTube / 実 storage を一切起動せず、 in-memory の :class:`_FakeSession`、
upload 契約を満たす stub uploader、 delete を記録する :class:`_FakeStorage` で
3 つの状態遷移ロジックを検証する。

検証観点:

- ``approve``: pending のみ → uploader.upload を呼び ``posted`` へ。 不在は例外、 非 pending は
  Conflict 系例外。
- ``reject``: reason(min4) 保存 + ``storage.delete(video_uri, missing_ok=True)`` + ``rejected``。
  reason が短い (min4 未満) と入力検証例外。
- ``auto_expire``: 7 日経過 pending を ``auto_expired`` にし動画 delete。

``DryrunService`` 未実装の TDD RED 段階では import 不能のため module ごと skip する。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

dryrun_service_mod = pytest.importorskip("ymg_backend.domain.dryrun.service")

pytestmark = pytest.mark.unit


# --- in-memory fakes --------------------------------------------------------------


@dataclass
class _FakePost:
    """ORM ``Post`` の最小スタンドイン。"""

    id: uuid.UUID
    video_uri: str
    final_title: str = "t"
    final_description: str = "d"
    youtube_video_id: str | None = None
    posted_at: datetime | None = None
    status: str = "generated"


@dataclass
class _FakeOutput:
    """ORM ``DryrunOutput`` の最小スタンドイン。"""

    id: uuid.UUID
    post_id: uuid.UUID
    video_uri: str
    state: str = "pending"
    reject_reason: str | None = None
    reviewed_at: datetime | None = None
    auto_expired_at: datetime | None = None
    posted_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


@dataclass
class _FakeSession:
    """``get`` / ``execute`` / ``flush`` のみ持つ in-memory セッション。

    ``execute`` は audit_log への insert (戻り不要) と DryrunOutput の select 両方を
    受ける。 select は ``_select_rows`` をそのまま返し、 insert は空結果を返す。
    """

    outputs: dict[uuid.UUID, _FakeOutput] = field(default_factory=dict)
    posts: dict[uuid.UUID, _FakePost] = field(default_factory=dict)
    select_rows: list[Any] = field(default_factory=list)
    flushes: int = 0

    async def get(self, model: Any, pk: uuid.UUID) -> Any | None:
        name = getattr(model, "__name__", str(model))
        if "Output" in name:
            return self.outputs.get(pk)
        if "Post" in name:
            return self.posts.get(pk)
        return None

    async def execute(self, statement: Any) -> Any:
        # audit_log への insert は戻りを使わない。 select は select_rows を返す。
        is_insert = statement.__class__.__name__.lower().startswith("insert")
        if is_insert:
            return _ScalarsResult([])
        return _ScalarsResult(self.select_rows)

    async def flush(self) -> None:
        self.flushes += 1


class _FakeStorage:
    """``delete`` / ``exists`` を記録する storage stub。"""

    def __init__(self, existing: set[str]) -> None:
        self._existing = set(existing)
        self.deleted: list[str] = []

    def delete(self, uri: str, *, missing_ok: bool = False) -> None:
        self.deleted.append(uri)
        self._existing.discard(uri)

    def exists(self, uri: str) -> bool:
        return uri in self._existing


class _StubUploader:
    def __init__(self, video_id: str) -> None:
        self._video_id = video_id
        self.calls: list[uuid.UUID] = []

    async def upload(self, *, session: Any, post: _FakePost) -> str:
        self.calls.append(post.id)
        post.youtube_video_id = self._video_id
        post.posted_at = datetime.now(UTC)
        post.status = "posted"
        return self._video_id


class _UploaderMustNotBeCalled:
    async def upload(self, *, session: Any, post: _FakePost) -> str:
        raise AssertionError("upload は呼ばれてはならない")


# --- helpers ----------------------------------------------------------------------


def _make_pair(state: str = "pending") -> tuple[_FakeSession, _FakeOutput, _FakePost]:
    post_id = uuid.uuid4()
    out_id = uuid.uuid4()
    uri = f"file:///tmp/{post_id}/video.mp4"
    post = _FakePost(id=post_id, video_uri=uri)
    output = _FakeOutput(id=out_id, post_id=post_id, video_uri=uri, state=state)
    session = _FakeSession(outputs={out_id: output}, posts={post_id: post})
    return session, output, post


def _service(uploader: Any, storage: _FakeStorage) -> Any:
    return dryrun_service_mod.DryrunService(uploader=uploader, storage=storage)


# --- approve ----------------------------------------------------------------------


@pytest.mark.fr("FR-061")
@pytest.mark.asyncio
async def test_approve_uploads_and_sets_posted() -> None:
    """FR-061: pending を approve すると uploader.upload を呼び posted に遷移する。"""
    session, output, post = _make_pair()
    storage = _FakeStorage({output.video_uri})
    uploader = _StubUploader("yt-xyz")

    result = await _service(uploader, storage).approve(session=session, output_id=output.id)

    assert result.state == "posted"
    assert result.posted_at is not None
    assert uploader.calls == [post.id]
    assert post.youtube_video_id == "yt-xyz"
    # approve では動画削除しない。
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_approve_missing_raises() -> None:
    session = _FakeSession()
    storage = _FakeStorage(set())
    with pytest.raises(Exception):  # noqa: B017 - NotFound 系
        await _service(_StubUploader("y"), storage).approve(session=session, output_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_approve_non_pending_conflict() -> None:
    session, output, _ = _make_pair(state="posted")
    storage = _FakeStorage({output.video_uri})
    with pytest.raises(Exception):  # noqa: B017 - Conflict 系
        await _service(_UploaderMustNotBeCalled(), storage).approve(
            session=session, output_id=output.id
        )


# --- reject -----------------------------------------------------------------------


@pytest.mark.fr("FR-061")
@pytest.mark.asyncio
async def test_reject_saves_reason_and_deletes_video() -> None:
    """FR-061: reject は reason を保存し video を削除して rejected に遷移する。"""
    session, output, _ = _make_pair()
    storage = _FakeStorage({output.video_uri})
    reason = "ノイズが目立つため不採用"

    result = await _service(_UploaderMustNotBeCalled(), storage).reject(
        session=session, output_id=output.id, reason=reason
    )

    assert result.state == "rejected"
    assert result.reject_reason == reason
    assert result.reviewed_at is not None
    assert output.video_uri in storage.deleted


@pytest.mark.asyncio
async def test_reject_short_reason_raises() -> None:
    session, output, _ = _make_pair()
    storage = _FakeStorage({output.video_uri})
    with pytest.raises(Exception):  # noqa: B017 - 入力検証 (min4)
        await _service(_UploaderMustNotBeCalled(), storage).reject(
            session=session, output_id=output.id, reason="ng"
        )
    assert output.state == "pending"
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_reject_non_pending_conflict() -> None:
    session, output, _ = _make_pair(state="rejected")
    storage = _FakeStorage({output.video_uri})
    with pytest.raises(Exception):  # noqa: B017 - Conflict 系
        await _service(_UploaderMustNotBeCalled(), storage).reject(
            session=session, output_id=output.id, reason="十分に長い理由"
        )


# --- auto_expire ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_expire_expires_old_pending() -> None:
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    old = _FakeOutput(
        id=uuid.uuid4(),
        post_id=uuid.uuid4(),
        video_uri="file:///tmp/old/video.mp4",
        state="pending",
        created_at=now - timedelta(days=7, hours=1),
    )
    session = _FakeSession(outputs={old.id: old}, select_rows=[old])
    storage = _FakeStorage({old.video_uri})

    expired = await _service(_UploaderMustNotBeCalled(), storage).auto_expire(
        session=session, now=now
    )

    assert any(o.id == old.id for o in expired)
    assert old.state == "auto_expired"
    assert old.auto_expired_at is not None
    assert old.video_uri in storage.deleted
