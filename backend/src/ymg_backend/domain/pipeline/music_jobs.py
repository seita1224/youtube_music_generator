"""1 投稿 = 6 トラックの音楽生成ディスパッチャ (ADR-0031 / US1 内部契約)。

``DailyPost`` 1 件につき GPU worker へ ACE-Step 音楽生成ジョブを ``track_count`` 回投入し、
各ジョブを ``gpu_jobs`` に永続化、 ``GET /jobs/{id}`` を完了 (succeeded / failed) まで指数
バックオフでポーリングする。 成功したトラックを ``audio_tracks`` に永続化し、 その一覧を返す。

責務境界 (US1 規約):

- 本サービスは ``session.flush()`` まで。 ``commit`` はオーケストレータの責務。
- ``AsyncSession`` は引数で受け取り、 内部で生成しない。
- 失敗は ``YmgError`` 派生で送出する (``category`` が ``Post.error_category`` ENUM と一致):
  worker 側 ``failed`` / ポーリングタイムアウトは一過性とみなし :class:`TransientError`、
  再生成上限到達など回復見込みのない状態は :class:`RecoverableError`。
- GPU 投入は必ず ``GpuJob`` レコードを残す (job_type / status / request_payload /
  output_uri / error_message / worker_endpoint)。
- 出力 URI は base_uri からの相対パス ``music/<post_id>/<name>`` を
  ``StorageAdapter.resolve_uri`` に渡して解決する (本番は base が
  ``file:///srv/ymg/outputs`` のため同配下へ、 テストは tmp 配下へ書かれる)。

音楽プロンプトは ``DailyPost`` のジャンル / ムード / 視覚意図と BPM レンジから合成する
(``music`` カテゴリのテンプレは存在しないため、 計画 LLM の出力をそのまま素材にする)。

トラック ``position`` は data-model.md / ``audio_tracks`` の ``CHECK (position BETWEEN 0 AND 5)``
に従い 0 始まり (0..track_count-1) で永続化する。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Final

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.domain.plans.schemas import DailyPost
from ymg_backend.infrastructure.db.models import AudioTrack, GpuJob, Post
from ymg_backend.infrastructure.gpu_worker_client import (
    GpuWorkerClient,
    JobStatus,
    MusicGenerateRequest,
)
from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter

# 1 投稿あたりの既定トラック数 (ADR-0004 / data-model.md 「6 トラックの個別メタ」)。
_DEFAULT_TRACK_COUNT: Final[int] = 6

# 各トラックの生成尺 (秒)。 ACE-Step 投入の ``duration_sec`` は 30..600 の範囲制約があるため
# その範囲内に収める (MusicGenerateRequest の Field 制約と整合)。
_TRACK_DURATION_SEC: Final[int] = 180

# ポーリングのバックオフ上限 (秒)。 指数増加が暴走しないよう頭打ちにする。
_MAX_BACKOFF_SEC: Final[float] = 30.0

# 1 トラックの再生成試行の上限。 これを超えても clear にならない場合は呼び出し側で
# RecoverableError として扱う (本サービスは regenerate を 1 回行うだけで、 上限管理は
# ``AudioTrack.regenerated_count`` に記録しつつオーケストレータが判断する)。
_MAX_REGENERATION: Final[int] = 3


def _utcnow() -> datetime:
    """タイムゾーン付き現在時刻 (UTC)。 ``started_at`` / ``finished_at`` 用。"""
    return datetime.now(UTC)


def _build_music_prompt(daily_post: DailyPost, genre: str) -> str:
    """``DailyPost`` から ACE-Step 用の音楽プロンプトを合成する。

    ジャンル / ムード / 視覚意図を 1 行に連結する。 ``MusicGenerateRequest.prompt`` は
    ``min_length=8`` 制約があるため、 連結結果は必ずそれを満たす (genre + mood で十分)。
    """
    parts = [genre, daily_post.mood, daily_post.visual_direction]
    return ", ".join(part.strip() for part in parts if part and part.strip())


def _resolve_bpm(daily_post: DailyPost) -> int | None:
    """``DailyPost.bpm_range`` から代表 BPM を解決する。

    レンジ (min, max) の中央値を採用し、 worker の ``bpm`` 制約 (50..200) に収まる場合のみ
    返す。 範囲外・未指定なら ``None`` (worker 側でジャンル既定に委ねる)。
    """
    bpm_range = daily_post.bpm_range
    if bpm_range is None:
        return None
    low, high = bpm_range
    midpoint = (low + high) // 2
    if 50 <= midpoint <= 200:
        return midpoint
    return None


def _worker_endpoint_of(client: GpuWorkerClient) -> str:
    """GPU worker の base URL を解決する (``gpu_jobs.worker_endpoint`` 用、 NOT NULL)。

    ``GpuWorkerClient`` は公開アクセサを持たないため、 内包する ``httpx.AsyncClient`` の
    公開属性 ``base_url`` から取得する。 取得不能時は空でない既定文字列で代替し、
    NOT NULL 制約違反を防ぐ (記録目的のため厳密性より頑健性を優先)。
    """
    inner = getattr(client, "_client", None)
    base_url = getattr(inner, "base_url", None)
    text = str(base_url) if base_url is not None else ""
    return text or "unknown"


class MusicJobRunner:
    """1 投稿分の音楽トラックを GPU worker へ投入・ポーリングし永続化するサービス。

    Args:
        client: GPU worker クライアント (HTTP 境界)。 リトライは client 内蔵。
        storage: 出力 URI 解決用のストレージアダプタ。
        poll_interval_sec: ポーリングの初回待機秒 (指数バックオフの基準)。 ``0.0`` 可。
        timeout_sec: 1 トラックあたりの完了待ちタイムアウト秒。 超過で
            :class:`TransientError`。
        worker_endpoint: ``gpu_jobs.worker_endpoint`` に記録する URL。 省略時は
            ``client`` から解決する。

    本サービスはステートレス (各メソッドは引数で渡された ``session`` / ``post`` のみを操作し、
    インスタンス状態を書き換えない)。
    """

    __slots__ = ("_client", "_poll_interval_sec", "_storage", "_timeout_sec", "_worker_endpoint")

    def __init__(
        self,
        client: GpuWorkerClient,
        storage: StorageAdapter,
        *,
        poll_interval_sec: float = 5.0,
        timeout_sec: float = 1800.0,
        worker_endpoint: str | None = None,
    ) -> None:
        if poll_interval_sec < 0:
            raise ValueError("poll_interval_sec must be >= 0")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be > 0")
        self._client: Final[GpuWorkerClient] = client
        self._storage: Final[StorageAdapter] = storage
        self._poll_interval_sec: Final[float] = poll_interval_sec
        self._timeout_sec: Final[float] = timeout_sec
        self._worker_endpoint: Final[str] = worker_endpoint or _worker_endpoint_of(client)

    async def submit_and_wait(
        self,
        *,
        session: AsyncSession,
        post: Post,
        daily_post: DailyPost,
        track_count: int = _DEFAULT_TRACK_COUNT,
    ) -> list[AudioTrack]:
        """``track_count`` 件の音楽ジョブを投入し、 全完了まで待って ``AudioTrack`` を返す。

        各トラックを順に投入・ポーリングし、 ``succeeded`` で ``AudioTrack`` (position 0..n-1) を
        生成・flush する。 最初のジョブ ID を ``post.music_job_id`` に記録する
        (``posts.music_job_id`` は単一 FK のため代表ジョブを保持する)。

        Raises:
            ValueError: ``track_count`` が 1 未満、 または DB 制約 (0..5) を超える場合。
            TransientError: いずれかのトラックが worker 側 ``failed`` / タイムアウトの場合。
            RecoverableError: worker の ``failed`` が回復可能カテゴリの場合。
        """
        if track_count < 1:
            raise ValueError("track_count must be >= 1")
        if track_count > _DEFAULT_TRACK_COUNT:
            raise ValueError(f"track_count must be <= {_DEFAULT_TRACK_COUNT} (position 0..5)")

        log = logger.bind(step="music", post_id=str(post.id), genre=post.genre)
        prompt = _build_music_prompt(daily_post, post.genre)
        bpm = _resolve_bpm(daily_post)

        tracks: list[AudioTrack] = []
        for position in range(track_count):
            job, status = await self._run_single(
                session=session,
                post=post,
                position=position,
                prompt=prompt,
                bpm=bpm,
            )
            if position == 0:
                post.music_job_id = job.id
            track = self._build_track(
                post=post,
                position=position,
                output_uri=self._require_output_uri(status, position=position),
                bpm=bpm,
                subtheme=daily_post.mood,
            )
            session.add(track)
            tracks.append(track)
        await session.flush()
        log.info("music tracks generated", track_count=len(tracks))
        return tracks

    async def regenerate_track(
        self,
        *,
        session: AsyncSession,
        post: Post,
        track: AudioTrack,
        daily_post: DailyPost,
    ) -> AudioTrack:
        """1 トラックを再生成し、 既存 ``AudioTrack`` 行を更新して返す (acoustid hit 時)。

        新規 seed (= 試行回数連動) で投入し直し、 ``audio_uri`` / ``bpm`` を差し替え、
        ``acoustid_status`` を ``not_checked`` に戻し ``regenerated_count`` を +1 する。

        Raises:
            RecoverableError: 再生成上限 (``_MAX_REGENERATION``) に到達済みの場合。
            TransientError: worker 側 ``failed`` / タイムアウトの場合。
        """
        if track.regenerated_count >= _MAX_REGENERATION:
            raise RecoverableError(
                "音楽トラックの再生成上限に到達しました",
                context={
                    "post_id": str(post.id),
                    "position": track.position,
                    "regenerated_count": track.regenerated_count,
                },
            )

        log = logger.bind(step="music_regen", post_id=str(post.id), genre=post.genre)
        prompt = _build_music_prompt(daily_post, post.genre)
        bpm = _resolve_bpm(daily_post)
        # 再生成は同一プロンプト + 異なる seed で多様性を出す (試行回数を seed に写像)。
        seed = track.regenerated_count + 1

        _job, status = await self._run_single(
            session=session,
            post=post,
            position=track.position,
            prompt=prompt,
            bpm=bpm,
            seed=seed,
        )
        track.audio_uri = self._require_output_uri(status, position=track.position)
        track.bpm = bpm
        track.acoustid_status = "not_checked"
        track.acoustid_response = None
        track.fingerprint_hash = None
        track.regenerated_count += 1
        await session.flush()
        log.info(
            "music track regenerated",
            position=track.position,
            regenerated_count=track.regenerated_count,
        )
        return track

    async def _run_single(
        self,
        *,
        session: AsyncSession,
        post: Post,
        position: int,
        prompt: str,
        bpm: int | None,
        seed: int | None = None,
    ) -> tuple[GpuJob, JobStatus]:
        """1 トラック分のジョブを投入・永続化し、 完了まで待って ``(GpuJob, JobStatus)`` を返す。

        ``GpuJob`` 行は投入直後に ``queued`` で flush し、 完了後に status / output_uri /
        計測値 / ``finished_at`` を更新する。 ``succeeded`` 以外は ``GpuJob`` を ``failed`` で
        記録した上で例外を送出する。
        """
        output_uri = self._storage.resolve_uri(f"music/{post.id}/track_{position}.wav")
        request = MusicGenerateRequest(
            prompt=prompt,
            duration_sec=_TRACK_DURATION_SEC,
            output_uri=output_uri,
            bpm=bpm,
            seed=seed,
        )
        job = GpuJob(
            id=uuid.uuid4(),
            job_type="music",
            status="queued",
            request_payload=request.model_dump(mode="json", exclude_none=True),
            output_uri=None,
            worker_endpoint=self._worker_endpoint,
            started_at=_utcnow(),
        )
        session.add(job)
        await session.flush()

        accepted = await self._client.generate_music(request)
        job.status = "running"
        await session.flush()

        status = await self._poll_until_done(job_id=accepted.job_id, post=post, position=position)
        job.status = status.status
        job.output_uri = status.output_uri
        job.vram_peak_mb = status.vram_peak_mb
        job.duration_ms = status.duration_ms
        job.finished_at = _utcnow()
        if status.status != "succeeded":
            job.error_message = status.error.message if status.error else "music job failed"
            await session.flush()
            raise self._failure_from_status(status, post=post, position=position)
        await session.flush()
        return job, status

    async def _poll_until_done(self, *, job_id: str, post: Post, position: int) -> JobStatus:
        """``GET /jobs/{id}`` を ``succeeded`` / ``failed`` まで指数バックオフでポーリングする。

        累積待機が ``timeout_sec`` を超えたら :class:`TransientError` を送出する。
        個々の HTTP 失敗 (接続 / 5xx) は client 側でリトライ済み (本ループでは即伝播)。
        """
        elapsed = 0.0
        backoff = self._poll_interval_sec
        while True:
            status = await self._client.get_job(job_id)
            if status.status in ("succeeded", "failed"):
                return status
            if elapsed >= self._timeout_sec:
                raise TransientError(
                    "音楽ジョブが時間内に完了しませんでした",
                    context={
                        "post_id": str(post.id),
                        "position": position,
                        "job_id": job_id,
                        "timeout_sec": self._timeout_sec,
                    },
                )
            wait = min(backoff, _MAX_BACKOFF_SEC) if backoff > 0 else 0.0
            await asyncio.sleep(wait)
            elapsed += wait if wait > 0 else 0.0
            # backoff=0 (テスト等) では elapsed が進まないため、 最小刻みで timeout を前進させる。
            if wait == 0:
                elapsed += self._poll_interval_sec or 1.0
            backoff = backoff * 2 if backoff > 0 else 0.0

    def _build_track(
        self,
        *,
        post: Post,
        position: int,
        output_uri: str,
        bpm: int | None,
        subtheme: str | None,
    ) -> AudioTrack:
        """成功したジョブ出力から ``AudioTrack`` を構築する (``acoustid_status`` 既定 not_checked)。"""
        return AudioTrack(
            id=uuid.uuid4(),
            post_id=post.id,
            position=position,
            audio_uri=output_uri,
            duration_sec=_TRACK_DURATION_SEC,
            bpm=bpm,
            music_key=None,
            subtheme=subtheme,
            acoustid_status="not_checked",
            regenerated_count=0,
        )

    @staticmethod
    def _require_output_uri(status: JobStatus, *, position: int) -> str:
        """``succeeded`` ジョブから ``output_uri`` を取り出す (欠落は契約違反)。"""
        if not status.output_uri:
            raise RecoverableError(
                "成功した音楽ジョブに output_uri がありません",
                context={"position": position, "job_id": status.job_id},
            )
        return status.output_uri

    @staticmethod
    def _failure_from_status(
        status: JobStatus, *, post: Post, position: int
    ) -> TransientError | RecoverableError:
        """worker の ``failed`` 状態をエラー分類へ写像する。

        worker が ``ErrorDetail.category`` を ``recoverable`` (以上の非一過性) と申告した場合は
        :class:`RecoverableError`、 それ以外 (申告なし含む) は再投入で回復しうる
        :class:`TransientError` として扱う。
        """
        detail = status.error
        message = detail.message if detail else "音楽ジョブが failed で終了しました"
        context = {
            "post_id": str(post.id),
            "position": position,
            "job_id": status.job_id,
            "worker_category": detail.category if detail else None,
        }
        if detail is not None and detail.category in ("recoverable", "fatal", "compliance"):
            return RecoverableError(message, context=context)
        return TransientError(message, context=context)
