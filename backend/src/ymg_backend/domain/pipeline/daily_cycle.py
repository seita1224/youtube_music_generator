"""日次サイクル オーケストレータ (US1 内部契約 / ADR-0031 / ADR-0028)。

Phase C で実装済みの各サービスを共有契約の合成順に従って 1 本のパイプラインに束ねる。
1 サイクル = 1 :class:`DailyPlan` (1〜2 投稿) を以下の順で処理する:

1. **計画**: ``plan_id`` 指定で既存 ``Plan`` があればその ``payload`` を再利用、 無ければ
   :class:`Planner` で LLM 生成し ``Plan`` を永続化する。
2. 各 :class:`DailyPost` について (post 単位で失敗を隔離):
   a. 音楽 6 トラックを :class:`MusicJobRunner` で生成。
   b. :class:`AcoustidChecker` で著作権プレチェック。 ``hit`` トラックは再生成し、 全 clear に
      なるまでループ。 連続 3 回 hit で当該ジャンルを一時停止し当該 post を中断 (FR-012)。
   c. 背景画像を :class:`ImageJobRunner` で生成。
   d. サムネ合成 (:func:`compose_thumbnail`) / タイトル (:func:`render_title`) /
      説明文 (:func:`render_description`) / 動画合成 (:func:`compose_video`)。
   e. ``settings.dryrun_default`` が真なら ``DryrunOutput(state=pending)`` を作成 (投稿しない)。
      偽なら :func:`compliance_gate` を通して :class:`YouTubeUploader` で投稿する。
3. ``Plan.status`` を ``completed`` (致命停止が無ければ) / ``failed`` に更新して
   :class:`CycleResult` を返す。

責務境界 (US1 規約):

- **commit はオーケストレータ責務**。 各 service は ``session.flush()`` まで。 本モジュールが
  状態遷移の確定タイミングで ``session.commit()`` を呼ぶ。
- 失敗は 5 区分 (transient / recoverable / fatal / compliance / quality) に分類する。
  :class:`YmgError` 派生は ``category`` をそのまま採用し、 未分類例外は
  :func:`resolve_category` で ``recoverable`` 扱い。 ``Post.error_category`` /
  ``Post.error_message`` に記録し Slack へ通知する。
- **post 単位で失敗を隔離**する。 1 post の ``failed`` は他 post やサイクルを止めない。
  :class:`FatalError` と DB 不達 (``SQLAlchemyError``) のみサイクル全体を停止する。

``compose_video`` / ``compose_thumbnail`` は **本モジュール名前空間**へ import する
(統合テストが ``monkeypatch.setattr(daily_cycle, "compose_video", ...)`` で差し替えるため)。
そのためパイプライン内ではモジュールグローバルとして参照し、 patch を有効化する。
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final, Protocol, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ymg_backend.core.logging import bind_context
from ymg_backend.domain.errors.errors import (
    ErrorCategory,
    FatalError,
    NotificationLevel,
    RecoverableError,
    YmgError,
    resolve_category,
)
from ymg_backend.domain.pipeline.video_compose import compose_video
from ymg_backend.domain.plans.schemas import ALLOWED_GENRES_CONTEXT_KEY, DailyPlan, DailyPost
from ymg_backend.domain.render.description import render_description
from ymg_backend.domain.render.thumbnail_overlay import compose_thumbnail
from ymg_backend.domain.render.title import render_title
from ymg_backend.domain.templates.loader import TemplateCategory, TemplateLoader
from ymg_backend.infrastructure.db.models import (
    AppState,
    AudioTrack,
    DryrunOutput,
    Genre,
    Plan,
    Post,
)
from ymg_backend.infrastructure.event_bus import (
    ErrorCategoryStr,
    JobEvent,
    JobStatus,
    event_bus,
)
from ymg_backend.infrastructure.youtube.compliance_gate import compliance_gate

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.core.config import Settings
    from ymg_backend.domain.pipeline.acoustid import AcoustidVerdict
    from ymg_backend.domain.pipeline.image_jobs import ImageJobRunner
    from ymg_backend.domain.pipeline.music_jobs import MusicJobRunner
    from ymg_backend.domain.pipeline.planner import Planner
    from ymg_backend.infrastructure.slack.notifier import SlackNotifier
    from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter
    from ymg_backend.llm.base import FinisherClient

# 1 トラックの acoustid hit 再生成ループの最大反復数。 これを超えても hit が残るなら
# 回復見込みなしとして当該 post を failed にする (RecoverableError 相当)。
_MAX_ACOUSTID_LOOPS: Final[int] = 4

# 出力 URI は storage base_uri からの相対パス。StorageAdapter.resolve_uri が base_uri
# (本番 file:///srv/ymg/outputs / テストは tmp) を前置して絶対 URI へ解決する。
_THUMBNAIL_URI_TMPL: Final[str] = "thumbnail/{post_id}/thumbnail.jpg"
_VIDEO_URI_TMPL: Final[str] = "video/{post_id}/final.mp4"

# 連続 hit カウントを永続化する AppState キーの接頭辞 (ジャンル別)。
_ACOUSTID_STATE_PREFIX: Final[str] = "acoustid_consecutive_hits:"

# US6 進捗イベントの job_name (JobHistory.job_name と同語彙)。 全 publish 点で共通。
_JOB_NAME: Final[str] = "daily_cycle"

# description テンプレは現状ジャンル別を持たず共通の ``default.yaml`` を使う (ADR-0034 §(2) /
# domain/render/description は default.yaml の body を骨格にする)。 ジャンル別テンプレが
# 追加されたら ``post.genre`` 解決へ差し替える。
_DESCRIPTION_TEMPLATE_NAME: Final[str] = "default"

# description 固定ブロック (FR-053: AI 開示 / チャンネル宣伝。 逐語挿入)。
_SHARED_AI_DISCLOSURE_FILE: Final[str] = "ai_disclosure.txt"
_SHARED_CHANNEL_PROMO_FILE: Final[str] = "channel_promo.txt"
_SHARED_AI_DISCLOSURE_KEY: Final[str] = "ai_disclosure"
_SHARED_CHANNEL_PROMO_KEY: Final[str] = "channel_promo"


class _PostOutcome(Enum):
    """1 投稿の処理結果 (内部用)。"""

    POSTED = "posted"
    DRYRUN = "dryrun"
    SUSPENDED = "suspended"


# --- 注入依存の構造型 (Protocol) -------------------------------------------------
# オーケストレータが依存する最小 I/F を Protocol で固定する。 統合テストは本番実装と
# 異なる stub (例: 全 clear を返す AcoustID stub / 呼ばれたら落ちる uploader stub) を
# 注入するため、 具象クラスではなく構造型に依存する。


class AcoustidLike(Protocol):
    """オーケストレータが必要とする AcoustID チェッカの最小 I/F。

    本番 :class:`AcoustidChecker.check_post_tracks` は ``consecutive_hits`` を要求するが、
    統合テストの stub はそれを受けない。 オーケストレータは呼び出し前にシグネチャを検査し、
    ``consecutive_hits`` を受ける実装にだけ渡す (:meth:`DailyCycleOrchestrator._check_tracks`)。
    """

    async def check_post_tracks(
        self,
        *,
        session: AsyncSession,
        post: Post,
        genre: str,
        tracks: list[AudioTrack],
    ) -> AcoustidVerdict: ...


class UploaderLike(Protocol):
    """オーケストレータが必要とする YouTube uploader の最小 I/F。"""

    async def upload(self, *, session: AsyncSession, post: Post) -> str: ...


@dataclass(frozen=True)
class CycleResult:
    """1 サイクルの実行結果 (不変)。

    Attributes:
        plan_id: 実行した ``Plan`` の ID (文字列)。
        post_ids: 生成した ``Post`` の ID 一覧 (文字列、 plan の posts と同順)。
        posted_count: YouTube へ投稿した post 数 (dryrun では 0)。
        dryrun_count: ``DryrunOutput`` を作成した post 数 (dryrun_default 真のとき)。
        failed_count: ``failed`` で終わった post 数。
    """

    plan_id: str
    post_ids: list[str]
    posted_count: int
    dryrun_count: int
    failed_count: int


class DailyCycleOrchestrator:
    """日次パイプラインを合成順に駆動するオーケストレータ。

    各サービスは依存注入で受け取り (テスト時は stub に差し替え可能)、 本クラスが
    DB トランザクション境界 (commit) と post 単位の失敗隔離・エラー分類・通知を担う。
    ステートレス (サイクル状態は引数の ``session`` と ORM 行のみ。 インスタンスは不変)。
    """

    __slots__ = (
        "_acoustid",
        "_finisher",
        "_image",
        "_music",
        "_notifier",
        "_planner",
        "_settings",
        "_storage",
        "_template_loader",
        "_uploader",
    )

    def __init__(
        self,
        *,
        settings: Settings,
        planner: Planner,
        finisher: FinisherClient,
        music: MusicJobRunner,
        acoustid: AcoustidLike,
        image: ImageJobRunner,
        uploader: UploaderLike,
        notifier: SlackNotifier,
        storage: StorageAdapter,
        template_loader: TemplateLoader,
    ) -> None:
        self._settings: Final[Settings] = settings
        self._planner: Final[Planner] = planner
        self._finisher: Final[FinisherClient] = finisher
        self._music: Final[MusicJobRunner] = music
        self._acoustid: Final[AcoustidLike] = acoustid
        self._image: Final[ImageJobRunner] = image
        self._uploader: Final[UploaderLike] = uploader
        self._notifier: Final[SlackNotifier] = notifier
        self._storage: Final[StorageAdapter] = storage
        self._template_loader: Final[TemplateLoader] = template_loader

    async def run(
        self,
        *,
        session: AsyncSession,
        target_date: date,
        plan_id: str | None = None,
    ) -> CycleResult:
        """1 日分の計画を実行し、 結果を返す。

        ``plan_id`` 指定かつ既存 ``Plan`` があればその計画を再利用 (planner LLM をスキップ)。
        無ければ planner で生成し ``Plan`` を永続化する。 以降は各投稿を順に処理する。

        Args:
            session: 永続化に使う AsyncSession (本メソッドが commit を呼ぶ)。
            target_date: 計画対象日 (JST)。
            plan_id: 再利用する既存 ``Plan`` の ID。 ``None`` なら新規生成。

        Returns:
            実行サマリ :class:`CycleResult`。

        Raises:
            FatalError: 計画解決不能・DB 不達などサイクル継続不能な致命障害。
        """
        log = bind_context(step="daily_cycle", cycle_id=plan_id or "new")
        allowed_genres = await self._load_enabled_genres(session)

        plan_row, plan = await self._resolve_plan(
            session=session,
            target_date=target_date,
            plan_id=plan_id,
            allowed_genres=allowed_genres,
        )
        plan_id_str = str(plan_row.id)
        log.info("daily cycle started", plan_id=plan_id_str, posts=len(plan.posts))

        plan_row.status = "executing"
        await session.commit()
        _publish_cycle(step="cycle", status="running", plan_id=plan_id_str)  # US6

        post_ids: list[str] = []
        posted_count = 0
        dryrun_count = 0
        failed_count = 0
        had_fatal = False

        for position, daily_post in enumerate(plan.posts):
            post = await self._create_post(
                session=session, plan_row=plan_row, position=position, daily_post=daily_post
            )
            post_ids.append(str(post.id))
            try:
                outcome = await self._process_post(
                    session=session, post=post, daily_post=daily_post
                )
            except FatalError as exc:
                await self._record_post_failure(session, post, exc)
                await self._notifier.notify_error(exc, context={"post_id": str(post.id)})
                failed_count += 1
                had_fatal = True
                log.error("daily cycle aborted by fatal error", post_id=str(post.id))
                break
            except SQLAlchemyError as exc:
                # DB 不達はサイクル継続不能 (記録自体が信頼できない)。 fatal 昇格して停止。
                fatal = FatalError("DB 操作に失敗しました", original=exc)
                await self._notifier.notify_error(fatal, context={"post_id": str(post.id)})
                failed_count += 1
                had_fatal = True
                log.error("daily cycle aborted by db error", post_id=str(post.id))
                break
            except YmgError as exc:
                await self._record_post_failure(session, post, exc)
                await self._notifier.notify_error(exc, context={"post_id": str(post.id)})
                failed_count += 1
                continue
            except Exception as exc:  # 未分類例外は recoverable に集約 (ADR-0028)
                await self._record_post_failure(session, post, exc)
                await self._notifier.notify_error(exc, context={"post_id": str(post.id)})
                failed_count += 1
                continue

            if outcome is _PostOutcome.POSTED:
                posted_count += 1
            elif outcome is _PostOutcome.DRYRUN:
                dryrun_count += 1
            else:  # _PostOutcome.SUSPENDED (ジャンル停止で中断) は failed として数える
                failed_count += 1

        plan_row.status = "failed" if had_fatal else "completed"
        await session.commit()
        _publish_cycle(  # US6: サイクル完了 (had_fatal なら failed)
            step="cycle",
            status="failed" if had_fatal else "succeeded",
            plan_id=plan_id_str,
        )
        log.info(
            "daily cycle finished",
            plan_id=plan_id_str,
            posted=posted_count,
            dryrun=dryrun_count,
            failed=failed_count,
        )
        return CycleResult(
            plan_id=plan_id_str,
            post_ids=post_ids,
            posted_count=posted_count,
            dryrun_count=dryrun_count,
            failed_count=failed_count,
        )

    # ------------------------------------------------------------------
    # 計画解決
    # ------------------------------------------------------------------
    async def _load_enabled_genres(self, session: AsyncSession) -> list[str]:
        """``Genre.enabled=True`` のジャンル名一覧を取得する (計画の許可ジャンル)。"""
        rows = (
            (await session.execute(select(Genre.name).where(Genre.enabled.is_(True))))
            .scalars()
            .all()
        )
        return list(rows)

    async def _resolve_plan(
        self,
        *,
        session: AsyncSession,
        target_date: date,
        plan_id: str | None,
        allowed_genres: list[str],
    ) -> tuple[Plan, DailyPlan]:
        """既存 ``Plan`` の再利用 or planner 新規生成で ``(Plan 行, DailyPlan)`` を返す。

        既存再利用時は ``Plan.payload`` を ``DailyPlan`` として **辞書照合付きで再検証**する
        (許可ジャンル外を弾く)。 新規生成時は :meth:`Planner.create_daily_plan` で
        ``Plan`` / ``PlanMetricSnapshot`` / ``usage_log`` を永続化し commit する。
        """
        existing = await self._load_existing_plan(session, plan_id)
        if existing is not None:
            plan = self._validate_plan_payload(existing, allowed_genres)
            return existing, plan

        plan_row = await self._planner.create_daily_plan(
            session=session, target_date=target_date, allowed_genres=allowed_genres
        )
        await session.commit()
        plan = self._validate_plan_payload(plan_row, allowed_genres)
        return plan_row, plan

    @staticmethod
    async def _load_existing_plan(session: AsyncSession, plan_id: str | None) -> Plan | None:
        """``plan_id`` 指定があれば該当 ``Plan`` を読む (UUID 解析失敗 / 不在は ``None``)。"""
        if plan_id is None:
            return None
        try:
            pk = uuid.UUID(plan_id)
        except ValueError:
            return None
        return await session.get(Plan, pk)

    @staticmethod
    def _validate_plan_payload(plan_row: Plan, allowed_genres: list[str]) -> DailyPlan:
        """``Plan.payload`` を ``DailyPlan`` として辞書照合付きで検証する。

        許可ジャンル外・スキーマ不正は計画として使えないため :class:`FatalError`
        (サイクル継続不能) として送出する。 ``allowed_genres`` 空なら照合をスキップする。
        """
        context = {ALLOWED_GENRES_CONTEXT_KEY: allowed_genres} if allowed_genres else {}
        try:
            return DailyPlan.model_validate(plan_row.payload, context=context)
        except Exception as exc:  # pydantic ValidationError 等を fatal へ集約
            raise FatalError(
                "Plan.payload を DailyPlan として検証できませんでした",
                context={"plan_id": str(plan_row.id)},
                original=exc,
            ) from exc

    async def _create_post(
        self,
        *,
        session: AsyncSession,
        plan_row: Plan,
        position: int,
        daily_post: DailyPost,
    ) -> Post:
        """``Post`` を ``pending`` で作成・永続化して返す (1 投稿の入口)。"""
        post = Post(
            id=uuid.uuid4(),
            plan_id=plan_row.id,
            position=position,
            genre=daily_post.genre,
            payload=daily_post.model_dump(mode="json"),
            status="pending",
        )
        session.add(post)
        await session.commit()
        return post

    # ------------------------------------------------------------------
    # 1 投稿の処理
    # ------------------------------------------------------------------
    async def _process_post(
        self,
        *,
        session: AsyncSession,
        post: Post,
        daily_post: DailyPost,
    ) -> _PostOutcome:
        """1 投稿を音楽〜投稿/dryrun まで処理し、 結果区分を返す。

        例外は呼び出し側 (:meth:`run`) で分類・隔離するため、 ここでは送出させる。
        """
        log = bind_context(step="daily_cycle.post", genre=post.genre, job_id=str(post.id))
        post.status = "generating"
        await session.commit()
        _publish_post(step="post", status="running", post=post)  # US6

        # --- 音楽 6 トラック + AcoustID プレチェック ---
        _publish_post(step="music", status="running", post=post)  # US6
        tracks = await self._music.submit_and_wait(
            session=session, post=post, daily_post=daily_post, track_count=6
        )
        await session.commit()
        _publish_post(step="music", status="succeeded", post=post)  # US6

        _publish_post(step="acoustid", status="running", post=post)  # US6
        suspended = await self._run_acoustid_loop(
            session=session, post=post, daily_post=daily_post, tracks=tracks
        )
        if suspended:
            await self._mark_compliance_suspended(session, post)
            _publish_post(  # US6: ジャンル停止で acoustid 失敗
                step="acoustid",
                status="failed",
                post=post,
                error_category=post.error_category,
                message=post.error_message,
            )
            await self._notifier.notify(
                level=NotificationLevel.ERROR,
                message="AcoustID 連続 hit によりジャンルを一時停止し投稿を中断しました",
                context={"post_id": str(post.id), "genre": post.genre},
            )
            return _PostOutcome.SUSPENDED
        _publish_post(step="acoustid", status="succeeded", post=post)  # US6: 全 clear

        # --- 画像 → サムネ → タイトル/説明 → 動画 ---
        _publish_post(step="image", status="running", post=post)  # US6
        base_image_uri = await self._image.submit_and_wait(
            session=session, post=post, daily_post=daily_post
        )
        await session.commit()
        _publish_post(step="image", status="succeeded", post=post)  # US6

        _publish_post(step="render", status="running", post=post)  # US6
        await self._render_artifacts(
            session=session,
            post=post,
            daily_post=daily_post,
            tracks=tracks,
            base_image_uri=base_image_uri,
        )

        post.status = "generated"
        await session.commit()
        _publish_post(step="render", status="succeeded", post=post)  # US6
        log.info("post artifacts generated", video_uri=post.video_uri)

        # --- 分岐: dryrun か投稿か ---
        _publish_post(step="publish", status="running", post=post)  # US6
        if self._settings.dryrun_default:
            await self._create_dryrun(session=session, post=post)
            await session.commit()
            _publish_post(step="publish", status="succeeded", post=post)  # US6: dryrun 作成
            return _PostOutcome.DRYRUN

        await self._publish(session=session, post=post)
        await session.commit()
        _publish_post(step="publish", status="succeeded", post=post)  # US6: 投稿
        return _PostOutcome.POSTED

    async def _run_acoustid_loop(
        self,
        *,
        session: AsyncSession,
        post: Post,
        daily_post: DailyPost,
        tracks: list[AudioTrack],
    ) -> bool:
        """全 clear になるまで hit トラックを再生成する。 ジャンル停止なら ``True`` を返す。

        ``AcoustidVerdict.hit_positions`` が空になればループ終了。 連続 hit 上限到達
        (``genre_suspended``) なら ``True`` を返し当該 post を中断させる。 再生成を
        ``_MAX_ACOUSTID_LOOPS`` 回繰り返しても hit が残るなら :class:`RecoverableError` 相当の
        品質低下として例外送出 (呼び出し側で post を failed に隔離)。
        """
        for _ in range(_MAX_ACOUSTID_LOOPS):
            verdict = await self._check_tracks(
                session=session, post=post, genre=daily_post.genre, tracks=tracks
            )
            await self._persist_consecutive_hits(
                session=session, genre=daily_post.genre, value=verdict.consecutive_hits
            )
            await session.commit()
            if verdict.genre_suspended:
                return True
            if not verdict.hit_positions:
                return False
            for pos in verdict.hit_positions:
                track = _track_at(tracks, pos)
                regenerated = await self._music.regenerate_track(
                    session=session, post=post, track=track, daily_post=daily_post
                )
                _replace_track(tracks, regenerated)
            await session.commit()
        # 上限到達: hit が解消されない。 回復見込みなしとして品質低下を送出する。
        raise RecoverableError(
            "AcoustID hit がトラック再生成上限内に解消されませんでした",
            context={"post_id": str(post.id), "genre": post.genre},
        )

    async def _check_tracks(
        self,
        *,
        session: AsyncSession,
        post: Post,
        genre: str,
        tracks: list[AudioTrack],
    ) -> AcoustidVerdict:
        """注入された AcoustID チェッカを呼ぶ。 ``consecutive_hits`` 引数の有無を吸収する。

        本番 :class:`AcoustidChecker` は ``consecutive_hits`` を要求し、 統合テスト stub は
        要求しない。 callee のシグネチャを検査し、 受ける場合のみ永続化済みカウントを渡す。
        ``AcoustidLike`` Protocol は ``consecutive_hits`` を宣言しないため、 それを渡す経路は
        動的呼び出し (``getattr`` 経由) とし、 stub 経路は静的に型付けする。
        """
        if _accepts_consecutive_hits(self._acoustid.check_post_tracks):
            consecutive = await self._load_consecutive_hits(session, genre)
            checker = self._acoustid.check_post_tracks
            verdict = await checker(
                session=session,
                post=post,
                genre=genre,
                tracks=tracks,
                consecutive_hits=consecutive,  # type: ignore[call-arg]
            )
            return verdict
        return await self._acoustid.check_post_tracks(
            session=session, post=post, genre=genre, tracks=tracks
        )

    # ------------------------------------------------------------------
    # 連続 hit カウントの永続化 (AppState)
    # ------------------------------------------------------------------
    @staticmethod
    async def _load_consecutive_hits(session: AsyncSession, genre: str) -> int:
        """ジャンル別の連続 hit カウントを ``AppState`` から読む (未設定は 0)。"""
        row = await session.get(AppState, _ACOUSTID_STATE_PREFIX + genre)
        if row is None:
            return 0
        value = row.value
        return value if isinstance(value, int) else 0

    @staticmethod
    async def _persist_consecutive_hits(*, session: AsyncSession, genre: str, value: int) -> None:
        """ジャンル別の連続 hit カウントを ``AppState`` へ upsert する (flush まで)。"""
        key = _ACOUSTID_STATE_PREFIX + genre
        row = await session.get(AppState, key)
        if row is None:
            session.add(AppState(key=key, value=value))
        else:
            row.value = value
        await session.flush()

    # ------------------------------------------------------------------
    # 成果物レンダリング
    # ------------------------------------------------------------------
    async def _render_artifacts(
        self,
        *,
        session: AsyncSession,
        post: Post,
        daily_post: DailyPost,
        tracks: list[AudioTrack],
        base_image_uri: str,
    ) -> None:
        """タイトル / 説明 / サムネ / 動画を生成し ``Post`` の各 URI / テキストを更新する。

        ``compose_video`` / ``compose_thumbnail`` はモジュールグローバル経由で呼び、
        統合テストの monkeypatch を有効化する。 commit は呼び出し側 (:meth:`_process_post`)。
        """
        title_tmpl = self._template_loader.load_genre_template(TemplateCategory.TITLE, post.genre)
        # description はジャンル別テンプレを持たず共通 default を使う (FR-052 / ADR-0034 §(2))。
        desc_tmpl = self._template_loader.load_genre_template(
            TemplateCategory.DESCRIPTION, _DESCRIPTION_TEMPLATE_NAME
        )
        thumb_tmpl = self._template_loader.load_genre_template(
            TemplateCategory.THUMBNAIL, post.genre
        )
        shared = self._load_description_shared()

        post.final_title = await render_title(
            daily_post=daily_post,
            genre=post.genre,
            finisher=self._finisher,
            template=title_tmpl,
        )
        post.final_description = await render_description(
            daily_post=daily_post,
            genre=post.genre,
            finisher=self._finisher,
            template=desc_tmpl,
            shared=shared,
        )

        thumbnail_uri = self._storage.resolve_uri(_THUMBNAIL_URI_TMPL.format(post_id=post.id))
        # モジュールグローバル経由 (test monkeypatch 対応)。
        post.thumbnail_uri = compose_thumbnail(
            base_image_uri=base_image_uri,
            title_text=post.final_title,
            genre=post.genre,
            template=thumb_tmpl,
            storage=self._storage,
            output_uri=thumbnail_uri,
        )

        video_uri = self._storage.resolve_uri(_VIDEO_URI_TMPL.format(post_id=post.id))
        artifact = compose_video(
            audio_tracks=tracks,
            background_image_uri=base_image_uri,
            storage=self._storage,
            output_uri=video_uri,
        )
        post.video_uri = artifact.video_uri
        await session.flush()

    def _load_description_shared(self) -> dict[str, str]:
        """説明文の固定ブロック (AI 開示 / チャンネル宣伝) を逐語テキストで読む (FR-053)。"""
        return {
            _SHARED_AI_DISCLOSURE_KEY: self._template_loader.load_shared_text(
                TemplateCategory.DESCRIPTION, _SHARED_AI_DISCLOSURE_FILE
            ),
            _SHARED_CHANNEL_PROMO_KEY: self._template_loader.load_shared_text(
                TemplateCategory.DESCRIPTION, _SHARED_CHANNEL_PROMO_FILE
            ),
        }

    # ------------------------------------------------------------------
    # dryrun / 投稿
    # ------------------------------------------------------------------
    @staticmethod
    async def _create_dryrun(*, session: AsyncSession, post: Post) -> None:
        """``DryrunOutput(state=pending)`` を作成する (投稿せずレビュー待ちにする)。"""
        if post.video_uri is None:
            raise FatalError(
                "dryrun 作成時に video_uri が未設定です",
                context={"post_id": str(post.id)},
            )
        session.add(
            DryrunOutput(
                id=uuid.uuid4(),
                post_id=post.id,
                state="pending",
                video_uri=post.video_uri,
            )
        )
        await session.flush()

    async def _publish(self, *, session: AsyncSession, post: Post) -> None:
        """compliance_gate を通して YouTube へ投稿し ``posted`` へ遷移させる。"""
        description = post.final_description or ""
        video_uri = post.video_uri or ""
        await compliance_gate(
            session=session,
            post=post,
            description=description,
            video_uri=video_uri,
            notifier=self._notifier,
        )
        post.status = "posting"
        await session.commit()
        video_id = await self._uploader.upload(session=session, post=post)
        post.status = "posted"
        post.youtube_video_id = video_id
        await session.flush()

    # ------------------------------------------------------------------
    # 失敗記録
    # ------------------------------------------------------------------
    async def _mark_compliance_suspended(self, session: AsyncSession, post: Post) -> None:
        """ジャンル停止で中断した post を ``failed`` (compliance) として記録する。"""
        post.status = "failed"
        post.error_category = ErrorCategory.COMPLIANCE.value
        post.error_message = "AcoustID 連続 hit によりジャンルを一時停止しました"
        await session.commit()

    async def _record_post_failure(
        self, session: AsyncSession, post: Post, exc: BaseException
    ) -> None:
        """post を ``failed`` にし error_category / error_message を記録して commit する。

        DB 不達の連鎖を避けるため、 記録の commit が失敗した場合は rollback して握りつぶす
        (上位はすでにエラー通知済み)。
        """
        category = resolve_category(exc)
        post.status = "failed"
        post.error_category = category.value
        post.error_message = str(exc)
        try:
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
        _publish_post(  # US6: post 失敗 (step は post 単位の集約点)
            step="post",
            status="failed",
            post=post,
            error_category=post.error_category,
            message=post.error_message,
        )


# --- モジュール関数ヘルパ ---------------------------------------------------------


def _publish_cycle(*, step: str, status: JobStatus, plan_id: str) -> None:
    """サイクル (plan) 単位の進捗イベントを発行する (best-effort, US6)。

    ``genre`` は持たない (grid の全体行)。 :meth:`EventBus.publish` は例外を漏らさない
    契約 (event_bus.py) なので、 ここでも try/except で囲わずパイプラインを止めない。
    """
    event_bus.publish(
        JobEvent(
            timestamp="",
            job_name=_JOB_NAME,
            step=step,
            status=status,
            context_type="plan",
            context_id=plan_id,
        )
    )


def _publish_post(
    *,
    step: str,
    status: JobStatus,
    post: Post,
    error_category: str | None = None,
    message: str | None = None,
) -> None:
    """post 単位の進捗イベントを発行する (best-effort, US6)。

    ``genre`` = ``post.genre`` (grid 列キー)。 失敗時は ``error_category`` / ``message`` を
    付す。 :meth:`EventBus.publish` は例外を漏らさないため try/except は不要。
    """
    event_bus.publish(
        JobEvent(
            timestamp="",
            job_name=_JOB_NAME,
            step=step,
            status=status,
            genre=post.genre,
            context_type="post",
            context_id=str(post.id),
            error_category=cast(ErrorCategoryStr | None, error_category),
            message=message,
        )
    )


def _accepts_consecutive_hits(func: object) -> bool:
    """callable が ``consecutive_hits`` キーワード引数を受けるか検査する。

    本番 AcoustID チェッカ (``consecutive_hits`` 必須) と統合テスト stub (受けない) の
    双方に対応するための互換シム。 検査不能時は安全側で ``False`` を返す。
    """
    try:
        sig = inspect.signature(func)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return "consecutive_hits" in sig.parameters


def _track_at(tracks: list[AudioTrack], position: int) -> AudioTrack:
    """``position`` に一致する ``AudioTrack`` を返す (再生成対象の特定)。"""
    for track in tracks:
        if track.position == position:
            return track
    raise FatalError(
        "再生成対象トラックが見つかりません",
        context={"position": position},
    )


def _replace_track(tracks: list[AudioTrack], regenerated: AudioTrack) -> None:
    """``tracks`` 内の同 position トラックを再生成版で差し替える (in-place 更新)。

    再生成は既存行を更新する (同一 ``AudioTrack`` インスタンス) ため通常は同一参照だが、
    別インスタンスが返る実装にも備えて position 一致で置換する。
    """
    for idx, track in enumerate(tracks):
        if track.position == regenerated.position:
            tracks[idx] = regenerated
            return


__all__ = [
    "AcoustidLike",
    "CycleResult",
    "DailyCycleOrchestrator",
    "UploaderLike",
]
