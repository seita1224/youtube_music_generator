"""DailyCycleOrchestrator (domain/pipeline/daily_cycle.py) の単体テスト (US1)。

統合テスト (T072, tests/integration/test_daily_cycle_pipeline.py) は実 Postgres + respx で
1 本通すが、 本ファイルは **DB / 外部依存を一切起動しない真の単体テスト**として、
オーケストレータの合成ロジック (計画再利用 → 音楽 → AcoustID → 画像 → レンダ → dryrun /
投稿分岐 / 失敗隔離) を検証する。

外部依存の扱い (起動禁止):

- 各 service (planner / finisher / music / acoustid / image / uploader / notifier) は
  呼び出しを記録するだけの軽量 stub に差し替える。
- ``compose_video`` / ``compose_thumbnail`` は daily_cycle モジュール名前空間を monkeypatch し、
  実 ffmpeg / Pillow を起動せず URI を返すダミーに差し替える (統合テストと同方針)。
- DB は ``add`` / ``flush`` / ``commit`` / ``rollback`` / ``get`` / ``execute`` を記録・応答する
  fake セッションを使う (Postgres 固有型に依存しない)。
- storage は base 無し ``StorageAdapter`` (URI 透過解決)。 monkeypatch 後の compose は書き込まない。
"""

from __future__ import annotations

import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from ymg_backend.core.config import Settings
from ymg_backend.domain.errors.errors import FatalError, RecoverableError
from ymg_backend.domain.pipeline import daily_cycle as dc
from ymg_backend.domain.pipeline.acoustid import AcoustidVerdict
from ymg_backend.domain.pipeline.daily_cycle import CycleResult, DailyCycleOrchestrator
from ymg_backend.domain.plans.schemas import (
    DailyPlan,
    DailyPost,
    ReferencedMetrics,
)
from ymg_backend.domain.render.video_compose import VideoArtifact
from ymg_backend.domain.templates.loader import TemplateLoader
from ymg_backend.infrastructure.db.models import (
    AppState,
    AudioTrack,
    DryrunOutput,
    Plan,
    Post,
)
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter
from ymg_backend.llm.base import FinisherRequest, FinisherResponse, LlmUsage

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.asyncio

_GENRE = "lo-fi hip-hop"
_TARGET_DATE = date(2026, 6, 16)


# --- 軽量 fake セッション ----------------------------------------------------------


class _ScalarResult:
    """``execute(...).scalars().all()`` を満たす最小ラッパ。"""

    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """orchestrator が触る ``add`` / ``flush`` / ``commit`` / ``rollback`` / ``get`` /
    ``execute`` を記録・応答する fake セッション (DB なし)。

    - ``execute(Genre 名 select)`` は ``genres`` を返す (enabled ジャンル列挙)。
    - ``get(Plan, pk)`` は ``seeded_plan`` を返す (計画再利用経路)。
    - ``get(AppState, key)`` は ``app_state`` から引く (連続 hit カウント)。
    - ``add`` された ORM 行は ``added`` に蓄積する (Post / DryrunOutput / AppState 観測用)。
    """

    def __init__(
        self,
        *,
        genres: list[str],
        seeded_plan: Plan | None,
    ) -> None:
        self._genres = genres
        self._seeded_plan = seeded_plan
        self.added: list[Any] = []
        self.app_state: dict[str, AppState] = {}
        self.commit_count = 0
        self.flush_count = 0
        self.rollback_count = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, AppState):
            self.app_state[obj.key] = obj

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

    async def execute(self, _stmt: Any) -> _ScalarResult:
        # orchestrator が発行する唯一の execute は enabled Genre 名の select。
        return _ScalarResult(self._genres)

    async def get(self, model: type[Any], pk: Any) -> Any:
        if model is Plan:
            if self._seeded_plan is not None and self._seeded_plan.id == pk:
                return self._seeded_plan
            return None
        if model is AppState:
            return self.app_state.get(pk)
        return None

    def added_of(self, model: type[Any]) -> list[Any]:
        return [obj for obj in self.added if isinstance(obj, model)]


# --- service stub 群 ---------------------------------------------------------------


def _zero_usage() -> LlmUsage:
    return LlmUsage(
        prompt_tokens=1, cached_tokens=0, completion_tokens=1, cost_usd=0.0, duration_ms=1
    )


class _StubFinisher:
    """``{{自由文}}`` をダミー文字列で埋める finisher stub。"""

    async def render(self, req: FinisherRequest) -> FinisherResponse:
        text = f"g[{req.instruction[: req.max_chars]}]"[: req.max_chars]
        return FinisherResponse(text=text, usage=_zero_usage())


class _StubPlanner:
    """既存 Plan 再利用経路では呼ばれないが、 型を満たすための stub。"""

    async def create_daily_plan(
        self, *, session: Any, target_date: date, allowed_genres: list[str]
    ) -> Plan:
        raise AssertionError("既存 Plan 再利用経路では planner は呼ばれない")


class _StubMusic:
    """6 トラックを返す music stub。 acoustid hit 指定時のみ再生成も記録する。"""

    def __init__(self, track_count: int = 6) -> None:
        self._track_count = track_count
        self.regenerated: list[int] = []

    async def submit_and_wait(
        self, *, session: Any, post: Post, daily_post: DailyPost, track_count: int = 6
    ) -> list[AudioTrack]:
        return [
            AudioTrack(
                id=uuid.uuid4(),
                post_id=post.id,
                position=i,
                audio_uri=f"file:///tmp/track_{i}.wav",
                duration_sec=180,
                acoustid_status="not_checked",
                regenerated_count=0,
            )
            for i in range(track_count)
        ]

    async def regenerate_track(
        self, *, session: Any, post: Post, track: AudioTrack, daily_post: DailyPost
    ) -> AudioTrack:
        self.regenerated.append(track.position)
        track.acoustid_status = "not_checked"
        track.regenerated_count += 1
        return track


class _ClearAcoustid:
    """全 clear を返す acoustid stub (統合テストと同じ最小 I/F: consecutive_hits なし)。"""

    async def check_post_tracks(
        self, *, session: Any, post: Post, genre: str, tracks: list[AudioTrack]
    ) -> AcoustidVerdict:
        for t in tracks:
            t.acoustid_status = "clear"
        return AcoustidVerdict(hit_positions=[], consecutive_hits=0, genre_suspended=False)


class _HitThenClearAcoustid:
    """1 回目 position 2 を hit、 2 回目以降 clear を返す (再生成ループ検証用)。"""

    def __init__(self) -> None:
        self.calls = 0

    async def check_post_tracks(
        self, *, session: Any, post: Post, genre: str, tracks: list[AudioTrack]
    ) -> AcoustidVerdict:
        self.calls += 1
        if self.calls == 1:
            return AcoustidVerdict(hit_positions=[2], consecutive_hits=1, genre_suspended=False)
        for t in tracks:
            t.acoustid_status = "clear"
        return AcoustidVerdict(hit_positions=[], consecutive_hits=0, genre_suspended=False)


class _SuspendAcoustid:
    """連続 hit 上限到達でジャンル停止を返す (中断経路の検証用)。"""

    async def check_post_tracks(
        self, *, session: Any, post: Post, genre: str, tracks: list[AudioTrack]
    ) -> AcoustidVerdict:
        return AcoustidVerdict(hit_positions=[0, 1], consecutive_hits=3, genre_suspended=True)


class _ConsecutiveHitsAcoustid:
    """本番互換: ``consecutive_hits`` を受け、 受け取った値を verdict に反映する stub。

    orchestrator がシグネチャ検査で ``consecutive_hits`` を渡すこと、 永続化値を読んで渡し、
    返り値を AppState に書き戻すことを検証する。
    """

    def __init__(self) -> None:
        self.received: list[int] = []

    async def check_post_tracks(
        self,
        *,
        session: Any,
        post: Post,
        genre: str,
        tracks: list[AudioTrack],
        consecutive_hits: int,
    ) -> AcoustidVerdict:
        self.received.append(consecutive_hits)
        for t in tracks:
            t.acoustid_status = "clear"
        return AcoustidVerdict(
            hit_positions=[], consecutive_hits=consecutive_hits + 5, genre_suspended=False
        )


class _StubImage:
    async def submit_and_wait(self, *, session: Any, post: Post, daily_post: DailyPost) -> str:
        return "file:///tmp/background.png"


class _GuardUploader:
    """dryrun では呼ばれてはならない uploader。"""

    async def upload(self, *, session: Any, post: Post) -> str:
        raise AssertionError("dryrun では uploader.upload は呼ばれない")


class _RecordingUploader:
    """投稿経路: video_id を返し呼び出しを記録する。"""

    def __init__(self) -> None:
        self.called = 0

    async def upload(self, *, session: Any, post: Post) -> str:
        self.called += 1
        return "yt-video-123"


class _RecordingNotifier:
    """Slack 通知を記録する stub (HTTP は打たない)。"""

    def __init__(self) -> None:
        self.notify_calls: list[dict[str, Any]] = []
        self.error_calls: list[BaseException] = []

    async def notify(self, *, level: Any, message: str, context: Any = None) -> None:
        self.notify_calls.append({"level": level, "message": message, "context": context})

    async def notify_error(self, exc: BaseException, *, context: Any = None) -> None:
        self.error_calls.append(exc)


# --- fixtures ---------------------------------------------------------------------


def _daily_post() -> DailyPost:
    return DailyPost(
        genre=_GENRE,
        mood="rainy late-night lounge",
        bpm_range=(70, 90),
        visual_direction="rain on a neon window, warm desk lamp, lo-fi study room",
        title_directive="Lo-Fi Hip Hop {{duration}}min | {{12字以内の日本語サブタイトル}}",
        description_directive="夜のチル作業用 BGM。 {{シーン説明を一文で}} #lofi #chill",
        thumbnail_directive=None,
        schedule_jst=None,
    )


def _daily_plan(plan_id: str) -> DailyPlan:
    return DailyPlan(
        plan_id=plan_id,
        target_date=_TARGET_DATE,
        posts=[_daily_post()],
        rationale="retention が高い lo-fi 帯を厚めに当て、 雨夜ムードで滞在時間を伸ばす。",
        referenced_metrics=ReferencedMetrics(
            window_days=14,
            sample_size=20,
            top_metrics_summary="lo-fi 帯の平均視聴維持率が他ジャンルより 8pt 高い傾向。",
        ),
    )


def _seeded_plan(plan_id: str) -> Plan:
    plan = _daily_plan(plan_id)
    return Plan(
        id=uuid.UUID(plan_id),
        cycle="daily",
        target_date=_TARGET_DATE,
        target_week_start=None,
        payload=plan.model_dump(mode="json"),
        rationale=plan.rationale,
        status="generated",
        llm_provider="openai",
        llm_model="gpt-4.1",
        llm_prompt_version="planner/system_v1",
    )


def _install_render_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_compose_video(
        *,
        audio_tracks: list[AudioTrack],
        background_image_uri: str,
        storage: StorageAdapter,
        output_uri: str,
    ) -> VideoArtifact:
        return VideoArtifact(
            video_uri=output_uri,
            duration_sec=sum(t.duration_sec for t in audio_tracks),
        )

    def _fake_compose_thumbnail(
        *,
        base_image_uri: str,
        title_text: str,
        genre: str,
        template: Any,
        storage: StorageAdapter,
        output_uri: str,
    ) -> str:
        return output_uri

    monkeypatch.setattr(dc, "compose_video", _fake_compose_video, raising=True)
    monkeypatch.setattr(dc, "compose_thumbnail", _fake_compose_thumbnail, raising=True)


def _templates_root() -> Path:
    """``backend/templates`` の実ルートパスを返す (テスト用 tmp コピーの元)。"""
    # tests/unit/test_daily_cycle.py -> backend/templates。
    return Path(__file__).resolve().parents[2] / "templates"


def _build_template_loader(tmp_path: Path) -> TemplateLoader:
    """実テンプレを tmp へコピーし、 title を hyphen slug 名に正規化したローダを返す。

    ``templates/title/*.yaml`` は loader の slug (hyphen, ``lo-fi-hip-hop``) に統一済み
    なので、 下の rename ループは通常 no-op の防御的処理 (将来 underscore 名が混入しても
    tmp コピー上で hyphen へ寄せて実レンダラを通す)。 元ファイルは編集しない。
    """
    dst = tmp_path / "templates"
    shutil.copytree(_templates_root(), dst)
    title_dir = dst / "title"
    for path in list(title_dir.glob("*.yaml")):
        hyphen = path.with_name(path.stem.replace("_", "-") + path.suffix)
        if hyphen != path:
            path.rename(hyphen)
    return TemplateLoader(root=dst)


def _build_orchestrator(
    *,
    settings: Settings,
    acoustid: Any,
    uploader: Any,
    notifier: Any,
    template_loader: TemplateLoader,
    music: Any | None = None,
) -> DailyCycleOrchestrator:
    return DailyCycleOrchestrator(
        settings=settings,
        planner=_StubPlanner(),  # type: ignore[arg-type]
        finisher=_StubFinisher(),  # type: ignore[arg-type]
        music=music or _StubMusic(),  # type: ignore[arg-type]
        acoustid=acoustid,
        image=_StubImage(),  # type: ignore[arg-type]
        uploader=uploader,
        notifier=notifier,
        storage=StorageAdapter(),
        template_loader=template_loader,
    )


def _settings(*, dryrun: bool) -> Settings:
    return Settings(
        dryrun_default=dryrun,
        gpu_worker_base_url="http://gpu-worker.test",
        acoustid_api_key=SecretStr("k"),
        slack_webhook_url=SecretStr(""),
        fernet_key=SecretStr(""),
    )


# --- テスト本体 -------------------------------------------------------------------


async def test_dryrun_creates_dryrun_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """dryrun_default=True: 既存 Plan を再利用し DryrunOutput(pending) を 1 件作る。"""
    _install_render_stubs(monkeypatch)
    plan_id = str(uuid.uuid4())
    session = _FakeSession(genres=[_GENRE], seeded_plan=_seeded_plan(plan_id))
    notifier = _RecordingNotifier()
    orch = _build_orchestrator(
        settings=_settings(dryrun=True),
        acoustid=_ClearAcoustid(),
        uploader=_GuardUploader(),
        notifier=notifier,
        template_loader=_build_template_loader(tmp_path),
    )

    result = await orch.run(session=session, target_date=_TARGET_DATE, plan_id=plan_id)  # type: ignore[arg-type]

    assert isinstance(result, CycleResult)
    assert result.plan_id == plan_id
    assert len(result.post_ids) == 1
    assert result.dryrun_count == 1
    assert result.posted_count == 0
    assert result.failed_count == 0

    posts = session.added_of(Post)
    assert len(posts) == 1
    post = posts[0]
    assert post.genre == _GENRE
    assert post.status == "generated"
    assert post.final_title is not None
    assert post.final_description is not None
    assert post.thumbnail_uri is not None
    assert post.video_uri is not None
    assert post.youtube_video_id is None

    dryruns = session.added_of(DryrunOutput)
    assert len(dryruns) == 1
    assert dryruns[0].state == "pending"
    assert dryruns[0].video_uri == post.video_uri
    assert not notifier.error_calls


@pytest.mark.fr("FR-001")
async def test_post_path_uploads_when_not_dryrun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FR-001: dryrun_default=False: compliance_gate を通して uploader が呼ばれ posted になる。"""
    _install_render_stubs(monkeypatch)
    plan_id = str(uuid.uuid4())
    session = _FakeSession(genres=[_GENRE], seeded_plan=_seeded_plan(plan_id))
    uploader = _RecordingUploader()
    orch = _build_orchestrator(
        settings=_settings(dryrun=False),
        acoustid=_ClearAcoustid(),
        uploader=uploader,
        notifier=_RecordingNotifier(),
        template_loader=_build_template_loader(tmp_path),
    )

    result = await orch.run(session=session, target_date=_TARGET_DATE, plan_id=plan_id)  # type: ignore[arg-type]

    assert result.posted_count == 1
    assert result.dryrun_count == 0
    assert uploader.called == 1
    post = session.added_of(Post)[0]
    assert post.status == "posted"
    assert post.youtube_video_id == "yt-video-123"
    assert not session.added_of(DryrunOutput)


@pytest.mark.fr("FR-011")
async def test_hit_track_is_regenerated_then_clears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FR-011: AcoustID hit の track だけ再生成され、 全 clear 後に dryrun へ進む。"""
    _install_render_stubs(monkeypatch)
    plan_id = str(uuid.uuid4())
    session = _FakeSession(genres=[_GENRE], seeded_plan=_seeded_plan(plan_id))
    music = _StubMusic()
    orch = _build_orchestrator(
        settings=_settings(dryrun=True),
        acoustid=_HitThenClearAcoustid(),
        uploader=_GuardUploader(),
        notifier=_RecordingNotifier(),
        music=music,
        template_loader=_build_template_loader(tmp_path),
    )

    result = await orch.run(session=session, target_date=_TARGET_DATE, plan_id=plan_id)  # type: ignore[arg-type]

    assert result.dryrun_count == 1
    assert result.failed_count == 0
    assert music.regenerated == [2]  # hit だった position 2 のみ再生成


async def test_genre_suspended_aborts_post_as_compliance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """連続 hit 上限でジャンル停止 → 当該 post は compliance failed、 ERROR 通知。"""
    _install_render_stubs(monkeypatch)
    plan_id = str(uuid.uuid4())
    session = _FakeSession(genres=[_GENRE], seeded_plan=_seeded_plan(plan_id))
    notifier = _RecordingNotifier()
    orch = _build_orchestrator(
        settings=_settings(dryrun=True),
        acoustid=_SuspendAcoustid(),
        uploader=_GuardUploader(),
        notifier=notifier,
        template_loader=_build_template_loader(tmp_path),
    )

    result = await orch.run(session=session, target_date=_TARGET_DATE, plan_id=plan_id)  # type: ignore[arg-type]

    assert result.failed_count == 1
    assert result.dryrun_count == 0
    assert result.posted_count == 0
    post = session.added_of(Post)[0]
    assert post.status == "failed"
    assert post.error_category == "compliance"
    assert len(notifier.notify_calls) == 1  # ERROR 通知が 1 件
    assert not session.added_of(DryrunOutput)


async def test_consecutive_hits_threaded_through_appstate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """本番互換 checker には永続化済み consecutive_hits を渡し、 戻り値を AppState に書き戻す。"""
    _install_render_stubs(monkeypatch)
    plan_id = str(uuid.uuid4())
    session = _FakeSession(genres=[_GENRE], seeded_plan=_seeded_plan(plan_id))
    # 事前に連続 hit カウント 2 を仕込む。
    key = dc._ACOUSTID_STATE_PREFIX + _GENRE
    session.app_state[key] = AppState(key=key, value=2)
    acoustid = _ConsecutiveHitsAcoustid()
    orch = _build_orchestrator(
        settings=_settings(dryrun=True),
        acoustid=acoustid,
        uploader=_GuardUploader(),
        notifier=_RecordingNotifier(),
        template_loader=_build_template_loader(tmp_path),
    )

    result = await orch.run(session=session, target_date=_TARGET_DATE, plan_id=plan_id)  # type: ignore[arg-type]

    assert result.dryrun_count == 1
    assert acoustid.received == [2]  # 永続化値 2 が渡る
    assert session.app_state[key].value == 7  # checker が返した 2+5 が書き戻される


async def test_music_failure_isolates_post_as_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """music の RecoverableError は当該 post を failed に隔離し、 通知してサイクルは完了する。"""
    _install_render_stubs(monkeypatch)
    plan_id = str(uuid.uuid4())
    session = _FakeSession(genres=[_GENRE], seeded_plan=_seeded_plan(plan_id))
    notifier = _RecordingNotifier()

    class _FailingMusic:
        async def submit_and_wait(
            self, *, session: Any, post: Post, daily_post: DailyPost, track_count: int = 6
        ) -> list[AudioTrack]:
            raise RecoverableError("worker failed", context={"post_id": str(post.id)})

    orch = _build_orchestrator(
        settings=_settings(dryrun=True),
        acoustid=_ClearAcoustid(),
        uploader=_GuardUploader(),
        notifier=notifier,
        music=_FailingMusic(),
        template_loader=_build_template_loader(tmp_path),
    )

    result = await orch.run(session=session, target_date=_TARGET_DATE, plan_id=plan_id)  # type: ignore[arg-type]

    assert result.failed_count == 1
    assert result.dryrun_count == 0
    post = session.added_of(Post)[0]
    assert post.status == "failed"
    assert post.error_category == "recoverable"
    assert len(notifier.error_calls) == 1


async def test_invalid_plan_payload_raises_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Plan.payload が許可ジャンル外なら FatalError でサイクル停止 (検証段で送出)。"""
    _install_render_stubs(monkeypatch)
    plan_id = str(uuid.uuid4())
    seeded = _seeded_plan(plan_id)
    # 許可ジャンルに無いジャンルだけを enabled とし、 辞書照合を失敗させる。
    session = _FakeSession(genres=["synthwave"], seeded_plan=seeded)
    orch = _build_orchestrator(
        settings=_settings(dryrun=True),
        acoustid=_ClearAcoustid(),
        uploader=_GuardUploader(),
        notifier=_RecordingNotifier(),
        template_loader=_build_template_loader(tmp_path),
    )

    with pytest.raises(FatalError):
        await orch.run(session=session, target_date=_TARGET_DATE, plan_id=plan_id)  # type: ignore[arg-type]
