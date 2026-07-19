"""日次サイクル オーケストレータの統合テスト (T072)。

`DailyCycleOrchestrator.run()` を 1 本通し、 plan -> music x6 -> AcoustID(全 clear)
-> image -> thumbnail -> title/description -> video_compose -> (dryrun_default=True 前提で)
DryrunOutput 作成 まで到達することを検証する。

外部依存の扱い (起動禁止のため全て mock/stub):

- GPU worker: respx で HTTP を mock する (``POST /generate/{music,image}`` → 202 JobAccepted,
  ``GET /jobs/{id}`` → succeeded + output_uri)。 実際の ``GpuWorkerClient`` をそのまま使い、
  HTTP 境界だけ差し替えることで、 ジョブ投入〜ポーリングの実コードを通す。
- AcoustID: 契約上 ``AcoustidChecker`` がオーケストレータに注入される依存なので、
  全 clear を返す stub をオーケストレータへ渡す (pyacoustid 実呼び出しは行わない)。
- YouTube: ``dryrun_default=True`` のため uploader は呼ばれない契約。 呼ばれたら失敗する
  guard stub を渡し、 dryrun 分岐が正しいことも同時に検証する。
- ffmpeg / Pillow: ``compose_video`` / ``compose_thumbnail`` を monkeypatch で stub 化し、
  ダミー mp4 / png を storage に書いて成果物 URI を返す (実バイナリ・実フォントに依存しない)。
- LLM (planner / finisher): 構造化出力を返す stub provider / finisher を注入する。

DB: パイプラインは ORM 行 (Post/Plan/GpuJob/AudioTrack/DryrunOutput) を Postgres 固有型
(JSONB/UUID/ENUM) で永続化するため SQLite では動かせない。 実 Postgres へ接続できない
環境では skip する (CI の postgres service では実行される)。

実装モジュール (daily_cycle 等) が未実装の TDD RED 段階では import 不能のため、
モジュール解決失敗時も skip する (実装が入った時点で本テストが緑になる契約)。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ymg_backend.core.config import Settings
from ymg_backend.domain.plans.schemas import (
    DailyPlan,
    DailyPost,
    ReferencedMetrics,
)
from ymg_backend.domain.templates.loader import TemplateLoader
from ymg_backend.infrastructure.db.models import (
    AudioTrack,
    Base,
    DryrunOutput,
    Genre,
    Plan,
    Post,
)
from ymg_backend.infrastructure.gpu_worker_client import GpuWorkerClient
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter
from ymg_backend.llm.base import (
    FinisherClient,
    FinisherRequest,
    FinisherResponse,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)

# 未実装モジュール群 (TDD RED 段階)。 import 失敗時は本ファイル全体を collection 時点で
# skip する。 公開シンボルは module オブジェクト経由で参照し (例:
# ``pipeline_daily_cycle.DailyCycleOrchestrator``)、 トップレベル import を増やさない
# (importorskip ゲートより前に解決させない / ruff の import 整列と衝突させないため)。
pipeline_daily_cycle = pytest.importorskip("ymg_backend.domain.pipeline.daily_cycle")
pipeline_acoustid = pytest.importorskip("ymg_backend.domain.pipeline.acoustid")
pipeline_planner = pytest.importorskip("ymg_backend.domain.pipeline.planner")
pipeline_music = pytest.importorskip("ymg_backend.domain.pipeline.music_jobs")
pipeline_image = pytest.importorskip("ymg_backend.domain.pipeline.image_jobs")
render_video = pytest.importorskip("ymg_backend.domain.render.video_compose")
render_thumb = pytest.importorskip("ymg_backend.domain.render.thumbnail_overlay")
slack_mod = pytest.importorskip("ymg_backend.infrastructure.slack.notifier")

pytestmark = pytest.mark.integration

_GPU_BASE_URL = "http://gpu-worker.test"
_TARGET_GENRE = "lo-fi hip-hop"


# --- DB 接続ヘルパ ----------------------------------------------------------------


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


# --- LLM / finisher stub ----------------------------------------------------------


def _zero_usage() -> LlmUsage:
    return LlmUsage(
        prompt_tokens=10,
        cached_tokens=0,
        completion_tokens=10,
        cost_usd=0.0,
        duration_ms=1,
    )


class _StubLlmProvider:
    """planner が要求する DailyPlan を直接返す stub provider。

    ``generate`` は ``response_model`` を尊重し、 渡された DailyPlan を parsed として返す。
    planner 実装が provider をどう呼ぶかに依存しないよう、 response_model が DailyPlan の
    ときだけ固定プランを返し、 それ以外は空 dict から構築する。
    """

    def __init__(self, plan: DailyPlan) -> None:
        self._plan = plan

    async def generate(self, req: LlmRequest[Any]) -> LlmResponse[Any]:
        parsed: Any
        if req.response_model is DailyPlan:
            parsed = self._plan
        else:  # 念のためのフォールバック (本テストでは到達しない想定)
            parsed = req.response_model.model_construct()
        return LlmResponse(
            parsed=parsed,
            raw_text=self._plan.model_dump_json(),
            usage=_zero_usage(),
            provider="openai",
            model="gpt-4.1",
            finish_reason="stop",
        )

    def supported_models(self) -> list[str]:
        return ["gpt-4.1"]

    def supports_caching(self) -> bool:
        return False

    async def health_check(self) -> bool:
        return True


class _StubFinisher(FinisherClient):
    """``{{自由文}}`` 展開を決定的なダミー文字列で埋める finisher stub。"""

    async def render(self, req: FinisherRequest) -> FinisherResponse:
        text = f"gen[{req.instruction[: req.max_chars]}]"[: req.max_chars]
        return FinisherResponse(text=text, usage=_zero_usage())


# --- AcoustID stub (全 clear) -----------------------------------------------------


class _ClearAcoustidChecker:
    """全 track を clear と判定する AcoustID stub (連続 hit カウント 0)。

    オーケストレータは ``check_post_tracks`` を呼んで ``AcoustidVerdict`` を受け取る契約。
    全 clear = ``hit_positions`` 空・``genre_suspended=False`` を返し、 再生成ループに
    入らせない。 ``check_track`` も clear を返す。
    """

    async def check_track(self, *, session: AsyncSession, track: AudioTrack) -> str:
        track.acoustid_status = "clear"
        return "clear"

    async def check_post_tracks(
        self,
        *,
        session: AsyncSession,
        post: Post,
        genre: str,
        tracks: list[AudioTrack],
    ) -> Any:
        for t in tracks:
            t.acoustid_status = "clear"
        return pipeline_acoustid.AcoustidVerdict(
            hit_positions=[],
            consecutive_hits=0,
            genre_suspended=False,
        )


# --- YouTube uploader guard stub --------------------------------------------------


class _UploaderMustNotBeCalled:
    """dryrun_default=True では upload は呼ばれない契約。 呼ばれたら test を落とす。"""

    async def upload(self, *, session: AsyncSession, post: Post) -> str:
        raise AssertionError("uploader.upload は dryrun モードでは呼ばれてはならない")


# --- fixtures ---------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """実 Postgres の AsyncSession を払い出す。 スキーマは metadata から作成する。"""
    _require_db()
    engine = create_async_engine(_async_url())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    try:
        async with sessionmaker() as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_genre(db_session: AsyncSession) -> str:
    """テンプレが存在する ``lo-fi hip-hop`` ジャンルを enabled で投入する。"""
    existing = await db_session.get(Genre, _TARGET_GENRE)
    if existing is None:
        db_session.add(
            Genre(
                name=_TARGET_GENRE,
                display_name="Lo-Fi Hip Hop",
                bpm_min=70,
                bpm_max=90,
                description="チル系の代表ジャンル。 統合テスト用シード。",
                role="primary",
                enabled=True,
            )
        )
        await db_session.commit()
    return _TARGET_GENRE


def _daily_plan(plan_id: str, target_date: date) -> DailyPlan:
    post = DailyPost(
        genre=_TARGET_GENRE,
        mood="rainy late-night lounge",
        bpm_range=(70, 90),
        visual_direction="rain on a neon window, warm desk lamp, lo-fi study room",
        title_directive="Lo-Fi Hip Hop {{duration}}min | {{12字以内の日本語サブタイトル}}",
        description_directive="夜のチル作業用 BGM。 {{シーン説明を一文で}} #lofi #chill #study",
        thumbnail_directive=None,
        schedule_jst=None,
    )
    return DailyPlan(
        plan_id=plan_id,
        target_date=target_date,
        posts=[post],
        rationale="retention が高い lo-fi 帯を厚めに当て、 雨夜ムードで滞在時間を伸ばす。",
        referenced_metrics=ReferencedMetrics(
            window_days=14,
            sample_size=20,
            top_metrics_summary="lo-fi 帯の平均視聴維持率が他ジャンルより 8pt 高い傾向。",
        ),
    )


@pytest.fixture
def storage(tmp_path: Any) -> StorageAdapter:
    base = f"file://{tmp_path}/outputs"
    return StorageAdapter(base_uri=base)


@pytest.fixture
def settings() -> Settings:
    """dryrun_default=True の設定。 GPU base URL は respx mock 先を指す。"""
    return Settings(
        dryrun_default=True,
        gpu_worker_base_url=_GPU_BASE_URL,
        acoustid_api_key=SecretStr("test-acoustid-key"),
        slack_webhook_url=SecretStr(""),
        fernet_key=SecretStr(""),
    )


# --- ffmpeg / Pillow stub (monkeypatch) -------------------------------------------


def _install_render_stubs(monkeypatch: pytest.MonkeyPatch, storage: StorageAdapter) -> None:
    """``compose_video`` / ``compose_thumbnail`` を storage へダミー出力する stub に差し替える。

    orchestrator は ``daily_cycle`` モジュール名前空間から両関数を参照する想定なので、
    そのシンボルを直接 patch する (実 ffmpeg / 実フォントを起動しない)。
    """

    def _fake_compose_video(
        *,
        audio_tracks: list[AudioTrack],
        background_image_uri: str,
        storage: StorageAdapter,
        output_uri: str,
    ) -> Any:
        storage.write_bytes(output_uri, b"\x00\x00\x00\x18ftypmp42dummy-mp4")
        total = sum(t.duration_sec for t in audio_tracks)
        return render_video.VideoArtifact(video_uri=output_uri, duration_sec=total)

    def _fake_compose_thumbnail(
        *,
        base_image_uri: str,
        title_text: str,
        genre: str,
        template: Any,
        storage: StorageAdapter,
        output_uri: str,
    ) -> str:
        storage.write_bytes(output_uri, b"\x89PNG\r\n\x1a\n-dummy-thumb")
        return output_uri

    monkeypatch.setattr(pipeline_daily_cycle, "compose_video", _fake_compose_video, raising=False)
    monkeypatch.setattr(
        pipeline_daily_cycle, "compose_thumbnail", _fake_compose_thumbnail, raising=False
    )


# --- GPU worker respx mock --------------------------------------------------------


def _register_gpu_mock(router: respx.MockRouter) -> None:
    """GPU worker の HTTP を mock する。 投入は 202 受理、 ポーリングは即 succeeded。"""

    def _accept(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        job_id = str(uuid.uuid4())
        # output_uri はリクエスト本文に含まれるのでそれを succeeded 時に返せるよう保持。
        _GPU_JOB_OUTPUT[job_id] = _extract_output_uri(body)
        return httpx.Response(202, json={"job_id": job_id, "status": "queued"})

    def _job_status(request: httpx.Request) -> httpx.Response:
        job_id = request.url.path.rsplit("/", 1)[-1]
        output_uri = _GPU_JOB_OUTPUT.get(job_id, "file:///tmp/unknown.bin")
        return httpx.Response(
            200,
            json={
                "job_id": job_id,
                "status": "succeeded",
                "output_uri": output_uri,
                "duration_ms": 1200,
                "vram_peak_mb": 8000,
            },
        )

    router.post(f"{_GPU_BASE_URL}/generate/music").mock(side_effect=_accept)
    router.post(f"{_GPU_BASE_URL}/generate/image").mock(side_effect=_accept)
    router.get(url__regex=rf"{_GPU_BASE_URL}/jobs/.+").mock(side_effect=_job_status)


_GPU_JOB_OUTPUT: dict[str, str] = {}


def _extract_output_uri(body: str) -> str:
    try:
        payload = json.loads(body)
    except ValueError:
        return "file:///tmp/unknown.bin"
    uri = payload.get("output_uri")
    return uri if isinstance(uri, str) else "file:///tmp/unknown.bin"


# --- orchestrator 組み立て --------------------------------------------------------


def _build_orchestrator(
    *,
    settings: Settings,
    plan: DailyPlan,
    storage: StorageAdapter,
) -> Any:
    provider = _StubLlmProvider(plan)
    finisher = _StubFinisher()
    gpu_client = GpuWorkerClient(settings.gpu_worker_base_url)
    music = pipeline_music.MusicJobRunner(
        gpu_client, storage, poll_interval_sec=0.0, timeout_sec=30.0
    )
    image = pipeline_image.ImageJobRunner(
        gpu_client, storage, poll_interval_sec=0.0, timeout_sec=30.0
    )
    notifier = slack_mod.SlackNotifier(None)
    planner = pipeline_planner.Planner(provider, _planner_prompt_loader())
    return pipeline_daily_cycle.DailyCycleOrchestrator(
        settings=settings,
        planner=planner,
        finisher=finisher,
        music=music,
        acoustid=_ClearAcoustidChecker(),
        image=image,
        uploader=_UploaderMustNotBeCalled(),
        notifier=notifier,
        storage=storage,
        template_loader=TemplateLoader(),
    )


def _planner_prompt_loader() -> Any:
    from ymg_backend.domain.prompts.loader import PromptLoader

    return PromptLoader()


# --- テスト本体 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_cycle_dryrun_creates_dryrun_output(
    db_session: AsyncSession,
    seeded_genre: str,
    storage: StorageAdapter,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存 Plan を再利用し、 1 サイクルを完走 → DryrunOutput が pending で 1 件できる。"""
    plan_id = str(uuid.uuid4())
    target_date = date(2026, 6, 16)
    plan = _daily_plan(plan_id, target_date)

    # 既存 Plan を入れておき planner LLM 呼び出しをスキップさせる (オーケストレータ側分岐)。
    db_session.add(
        Plan(
            id=uuid.UUID(plan_id),
            cycle="daily",
            target_date=target_date,
            target_week_start=None,
            payload=plan.model_dump(mode="json"),
            rationale=plan.rationale,
            status="generated",
            llm_provider="openai",
            llm_model="gpt-4.1",
            llm_prompt_version="planner/system_v1",
        )
    )
    await db_session.commit()

    _install_render_stubs(monkeypatch, storage)

    with respx.mock(assert_all_called=False) as router:
        _register_gpu_mock(router)
        orchestrator = _build_orchestrator(settings=settings, plan=plan, storage=storage)
        result = await orchestrator.run(
            session=db_session, target_date=target_date, plan_id=plan_id
        )

    assert isinstance(result, pipeline_daily_cycle.CycleResult)
    assert result.plan_id == plan_id
    assert len(result.post_ids) == 1
    assert result.dryrun_count == 1
    assert result.posted_count == 0
    assert result.failed_count == 0

    # --- DB 観測: Post / AudioTrack / DryrunOutput ---
    posts = (
        (await db_session.execute(select(Post).where(Post.plan_id == uuid.UUID(plan_id))))
        .scalars()
        .all()
    )
    assert len(posts) == 1
    post = posts[0]
    assert post.genre == _TARGET_GENRE
    assert post.video_uri is not None
    assert post.thumbnail_uri is not None
    assert post.final_title is not None
    assert post.final_description is not None
    # dryrun 経路では未投稿のまま (posted にはならない)。
    assert post.status in {"generated", "generating"}
    assert post.youtube_video_id is None
    assert post.posted_at is None

    tracks = (
        (await db_session.execute(select(AudioTrack).where(AudioTrack.post_id == post.id)))
        .scalars()
        .all()
    )
    assert len(tracks) == 6
    assert all(t.acoustid_status == "clear" for t in tracks)

    dryruns = (
        (await db_session.execute(select(DryrunOutput).where(DryrunOutput.post_id == post.id)))
        .scalars()
        .all()
    )
    assert len(dryruns) == 1
    assert dryruns[0].state == "pending"
    assert dryruns[0].video_uri == post.video_uri

    # 成果物がストレージに書き出されている (ダミー mp4 / png)。
    assert storage.exists(post.video_uri)
    assert storage.exists(post.thumbnail_uri)
