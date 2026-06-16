"""Unit テスト: CommentsClient の取得 + upsert (T102, 外部依存 mock)。

HTTP は respx mock、 OAuth は ``get_access_token`` を monkeypatch、 session は
副作用 (upsert / select) を記録するスパイ。実 YouTube / 実 refresh は一切打たない。

検証観点:
1. commentThreads.list を解析し comments へ upsert、 件数を返す。
2. 重複は youtube_comment_id (unique) で on_conflict_do_update に乗る (insert + on_conflict)。
3. video_ids=None なら videos テーブルの公開済み動画を対象にする。
4. 対象 0 件なら API を叩かず 0 を返す。
5. wire 形式: Authorization: Bearer / videoId / part / pageToken (ページング)。
6. 4xx は RecoverableError、 5xx は TransientError に写像する。
7. 注入 http クライアントを尊重する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx

from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.infrastructure.youtube.comments_client import (
    YOUTUBE_COMMENT_THREADS_URL,
    CommentsClient,
)

pytestmark = pytest.mark.unit

_FAKE_TOKEN = "comments-token"
_CHANNEL_ID = "UC_comments"


# --- session スパイ -----------------------------------------------------------------


@dataclass
class _Recorded:
    table_name: str
    kind: str
    values: dict[str, Any]


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


@dataclass
class SpySession:
    """``AsyncSession`` の最小スパイ (select / insert を記録、 commit は呼ばれない)。"""

    video_ids: list[str] = field(default_factory=list)
    recorded: list[_Recorded] = field(default_factory=list)
    flush_count: int = 0
    commit_count: int = 0

    async def execute(self, statement: Any) -> Any:
        kind = type(statement).__name__.lower()
        if "select" in kind:
            self.recorded.append(_Recorded("videos", "select", {}))
            # scalars().all() 相当: 単一列 select の値を素のスカラで返す。
            return _Result(list(self.video_ids))
        compiled = statement.compile()
        table = getattr(statement, "table", None)
        self.recorded.append(
            _Recorded(table.name if table is not None else "?", kind, dict(compiled.params))
        )
        return _Result([])

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:  # pragma: no cover - 呼ばれてはいけない
        self.commit_count += 1

    @property
    def comment_upserts(self) -> list[_Recorded]:
        return [r for r in self.recorded if r.table_name == "comments"]


# --- OAuth stub ---------------------------------------------------------------------


class _StubOAuth:
    """``YouTubeOAuth`` の代替 (固定トークンを返す)。

    ``YouTubeOAuth`` は ``__slots__`` で ``get_access_token`` を上書きできないため、
    実 OAuth を構築せず、 必要な I/F (``get_access_token``) のみを持つ stub を渡す。
    """

    async def get_access_token(self, *, session: Any) -> str:
        _ = session
        return _FAKE_TOKEN


def _build_oauth() -> _StubOAuth:
    return _StubOAuth()


# --- commentThreads.list レスポンス組み立て ----------------------------------------


def _thread(
    *,
    comment_id: str,
    author: str = "Alice",
    text: str = "great track",
    like_count: int = 3,
    published_at: str = "2026-06-15T10:00:00Z",
) -> dict[str, Any]:
    return {
        "snippet": {
            "topLevelComment": {
                "id": comment_id,
                "snippet": {
                    "authorDisplayName": author,
                    "textDisplay": text,
                    "likeCount": like_count,
                    "publishedAt": published_at,
                },
            }
        }
    }


def _threads_response(
    *, items: list[dict[str, Any]], next_page_token: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": "youtube#commentThreadListResponse", "items": items}
    if next_page_token is not None:
        payload["nextPageToken"] = next_page_token
    return payload


# ---------------------------------------------------------------------------
# 取得 + upsert (本命)
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_and_upsert_parses_and_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    """top-level コメントを解析し comments へ upsert、 件数を返す。"""
    video_id = "vid_A"
    respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_threads_response(
                items=[
                    _thread(comment_id="c1", author="Alice", text="nice", like_count=5),
                    _thread(comment_id="c2", author="Bob", text="cool", like_count=0),
                ]
            ),
        )
    )

    session = SpySession()
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        video_ids=[video_id],
    )

    assert count == 2
    upserts = session.comment_upserts
    assert len(upserts) == 2
    first = upserts[0].values
    assert first.get("youtube_comment_id") == "c1"
    assert first.get("youtube_video_id") == video_id
    assert first.get("author") == "Alice"
    assert first.get("text") == "nice"
    assert first.get("like_count") == 5
    assert isinstance(first.get("published_at"), datetime)
    # 重複回避: insert + on_conflict (upsert) であること。
    assert "insert" in upserts[0].kind
    # commit は呼ばない、 flush は行う。
    assert session.commit_count == 0
    assert session.flush_count >= 1


@respx.mock
async def test_upsert_uses_on_conflict_for_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    """youtube_comment_id (unique) 衝突を on_conflict_do_update で吸収する SQL を発行する。"""
    captured: list[str] = []
    original_execute = SpySession.execute

    async def _spy_execute(self: SpySession, statement: Any) -> Any:
        captured.append(str(statement.compile()))
        return await original_execute(self, statement)

    monkeypatch.setattr(SpySession, "execute", _spy_execute)

    respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(
        return_value=httpx.Response(200, json=_threads_response(items=[_thread(comment_id="dup1")]))
    )
    session = SpySession()
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    await client.fetch_and_upsert(session=session, video_ids=["vid_dup"])  # type: ignore[arg-type]

    insert_sql = next(s for s in captured if "INSERT INTO comments" in s)
    assert "ON CONFLICT" in insert_sql
    assert "youtube_comment_id" in insert_sql


# ---------------------------------------------------------------------------
# video_ids=None -> videos テーブルの公開済み動画
# ---------------------------------------------------------------------------


@respx.mock
async def test_resolves_video_ids_from_db_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """video_ids=None なら videos テーブルから対象動画を取得する。"""
    db_videos = ["vid_1", "vid_2"]

    def _side_effect(request: httpx.Request) -> httpx.Response:
        vid = request.url.params.get("videoId", "")
        return httpx.Response(200, json=_threads_response(items=[_thread(comment_id=f"{vid}_c1")]))

    respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(side_effect=_side_effect)
    session = SpySession(video_ids=db_videos)
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    count = await client.fetch_and_upsert(session=session, video_ids=None)  # type: ignore[arg-type]

    assert any(r.kind == "select" and r.table_name == "videos" for r in session.recorded)
    assert count == 2
    upserted_videos = {r.values.get("youtube_video_id") for r in session.comment_upserts}
    assert upserted_videos == set(db_videos)


@respx.mock
async def test_no_target_videos_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """対象動画が 0 件なら API を叩かず 0 を返す。"""
    route = respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(
        return_value=httpx.Response(200, json=_threads_response(items=[]))
    )
    session = SpySession(video_ids=[])
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    count = await client.fetch_and_upsert(session=session, video_ids=None)  # type: ignore[arg-type]

    assert count == 0
    assert session.comment_upserts == []
    assert not route.called


# ---------------------------------------------------------------------------
# wire 形式 / ページング
# ---------------------------------------------------------------------------


@respx.mock
async def test_request_wire_format_and_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bearer / videoId / part を送り、 nextPageToken でページングする。"""
    video_id = "vid_PAGE"

    def _side_effect(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pageToken")
        if token is None:
            return httpx.Response(
                200,
                json=_threads_response(items=[_thread(comment_id="p1")], next_page_token="PAGE2"),
            )
        return httpx.Response(200, json=_threads_response(items=[_thread(comment_id="p2")]))

    route = respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(side_effect=_side_effect)
    session = SpySession()
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        video_ids=[video_id],
        max_per_video=50,
    )

    assert count == 2
    assert route.call_count == 2
    first_req = route.calls[0].request
    assert first_req.headers.get("Authorization") == f"Bearer {_FAKE_TOKEN}"
    assert first_req.url.params.get("videoId") == video_id
    assert "snippet" in first_req.url.params.get("part", "")
    # 2 回目のリクエストは pageToken を載せている。
    assert route.calls[1].request.url.params.get("pageToken") == "PAGE2"


@respx.mock
async def test_max_per_video_caps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_per_video で取得件数を打ち切る。"""
    respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_threads_response(items=[_thread(comment_id=f"c{i}") for i in range(5)]),
        )
    )
    session = SpySession()
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    count = await client.fetch_and_upsert(
        session=session,  # type: ignore[arg-type]
        video_ids=["vid_cap"],
        max_per_video=3,
    )

    assert count == 3
    assert len(session.comment_upserts) == 3


async def test_invalid_max_per_video_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_per_video < 1 は ValueError (境界での入力検証)。"""
    session = SpySession()
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_per_video"):
        await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            video_ids=["vid_x"],
            max_per_video=0,
        )


# ---------------------------------------------------------------------------
# エラー写像 (ADR-0028)
# ---------------------------------------------------------------------------


@respx.mock
async def test_4xx_maps_to_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx は RecoverableError に写像する (リトライしない)。"""
    route = respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(
        return_value=httpx.Response(403, json={"error": {"message": "forbidden"}})
    )
    session = SpySession()
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    with pytest.raises(RecoverableError):
        await client.fetch_and_upsert(session=session, video_ids=["vid_403"])  # type: ignore[arg-type]
    assert route.call_count == 1


@respx.mock
async def test_5xx_maps_to_transient_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx は TransientError に写像し、 backoff 付きで最大 3 試行する。"""
    monkeypatch.setattr(
        "ymg_backend.infrastructure.youtube.comments_client.asyncio.sleep",
        _no_sleep,
    )
    route = respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(
        return_value=httpx.Response(503, json={"error": {"message": "unavailable"}})
    )
    session = SpySession()
    client = CommentsClient(_build_oauth())  # type: ignore[arg-type]
    with pytest.raises(TransientError):
        await client.fetch_and_upsert(session=session, video_ids=["vid_503"])  # type: ignore[arg-type]
    assert route.call_count == 3


# ---------------------------------------------------------------------------
# 注入 http クライアント
# ---------------------------------------------------------------------------


@respx.mock
async def test_uses_injected_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """http= で渡した AsyncClient を使う (取得層が httpx 分離されている保証)。"""
    respx.get(url__startswith=YOUTUBE_COMMENT_THREADS_URL).mock(
        return_value=httpx.Response(200, json=_threads_response(items=[_thread(comment_id="inj1")]))
    )
    async with httpx.AsyncClient() as http:
        session = SpySession()
        client = CommentsClient(_build_oauth(), http=http)  # type: ignore[arg-type]
        count = await client.fetch_and_upsert(
            session=session,  # type: ignore[arg-type]
            video_ids=["vid_inj"],
        )
    assert count == 1


def test_url_constant_sane() -> None:
    assert YOUTUBE_COMMENT_THREADS_URL.startswith("https://")
    assert "commentThreads" in YOUTUBE_COMMENT_THREADS_URL


async def _no_sleep(_seconds: float) -> None:
    """backoff の sleep を no-op に差し替える (テスト高速化)。"""
    return None
