"""AcoustID 指紋プレチェック (FR-010 / FR-011 / FR-012, ADR-0005, Constitution II)。

生成した 6 トラックを投稿前に AcoustID lookup API へかけ、 商用既存曲との一致を
検出する **コンプラプレチェック層**。 著作権 BAN リスクを投稿前に潰すための関門。

責務:

- :class:`AcoustidChecker.check_track`
    1 トラックの指紋を生成 → lookup → スコア判定し、 ``AudioTrack.acoustid_status`` を
    ``not_checked`` から ``clear`` / ``hit`` / ``api_error`` のいずれかへ遷移させる。
    生レスポンス (``acoustid_response``) と指紋ハッシュ (``fingerprint_hash``) を記録する。
- :class:`AcoustidChecker.check_post_tracks`
    1 投稿 (6 トラック) を並列チェックし、 ``hit`` の position 一覧 (再生成対象) を集約する。
    同一ジャンルで **連続 3 回 hit** に達したら当該 ``Genre.enabled=False`` へ更新し、
    監査ログ + Slack ``[ERROR]`` 通知を行う (FR-012 ジャンル一時停止)。

設計方針 (共有契約 / 既存パターン踏襲):

- **fingerprint 生成は注入** (:data:`Fingerprinter`)。 chromaprint / fpcalc バイナリには
    本モジュールから直接依存せず、 注入関数 ``(audio: bytes, duration_sec: int) -> (duration, fp)``
    を呼ぶ。 これによりバイナリ非依存でテスト可能 (respx + stub fingerprinter)。
- **HTTP は httpx**。 5xx / 接続失敗は exponential backoff でリトライし
    (``GpuWorkerClient`` と同方針)、 復旧しなければ ``api_error`` 扱い (例外は送出せず
    プレチェック結果として表現する)。 lookup の ``status != "ok"`` も ``api_error``。
- **commit は呼び出し側 (オーケストレータ)**。 本 checker は ``session.flush()`` まで。
- 連続 hit カウントは ``AppState`` ではなく **引数で受け渡し**、 新しい値を
    :class:`AcoustidVerdict` で返す (状態はオーケストレータが永続化する)。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import httpx
from loguru import logger
from sqlalchemy import update

from ymg_backend.domain.errors import NotificationLevel
from ymg_backend.infrastructure.audit import write_audit_log
from ymg_backend.infrastructure.db.models import Genre

if TYPE_CHECKING:
    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.db.models import AudioTrack, Post
    from ymg_backend.infrastructure.storage.fsspec_wrapper import StorageAdapter


# --- 公開定数 (FR-010〜012, ADR-0005) ---------------------------------------------

# AcoustID lookup API エンドポイント (ADR-0005 / acoustid.org Web Service)。
ACOUSTID_LOOKUP_URL: Final[str] = "https://api.acoustid.org/v2/lookup"

# マッチと判定するスコア閾値 (>= で hit)。 0.0〜1.0。
# 既存商用曲との実質一致を拾いつつ誤検出を抑える保守的な値。
ACOUSTID_MATCH_THRESHOLD: Final[float] = 0.85

# 同一ジャンルで連続して hit した回数の上限。 到達でジャンル一時停止 (FR-012)。
ACOUSTID_CONSECUTIVE_HIT_LIMIT: Final[int] = 3

# lookup の HTTP リトライ設定 (5xx / 接続失敗のみ。 ADR-0028 backoff と同方針)。
_HTTP_MAX_ATTEMPTS: Final[int] = 3
_HTTP_BACKOFF_BASE_SEC: Final[float] = 1.0
_HTTP_TIMEOUT_SEC: Final[float] = 30.0

# audit_log.action (data-model.md `audit_log` / FR-012 ジャンル停止)。
GENRE_SUSPENDED_AUDIT_ACTION: Final[str] = "genre_suspended_acoustid"

#: ``check_track`` の戻り値 (acoustid_status enum と一致)。
AcoustidStatus = Literal["clear", "hit", "api_error"]

# acoustid_status enum 値 (data-model.md `acoustid_status`)。
# Literal で固定し、 status 変数への代入が mypy strict で通るようにする。
_STATUS_CLEAR: Final[Literal["clear"]] = "clear"
_STATUS_HIT: Final[Literal["hit"]] = "hit"
_STATUS_API_ERROR: Final[Literal["api_error"]] = "api_error"

#: 注入する fingerprint 生成関数の型。
#: ``(audio_bytes, duration_sec) -> (duration_sec, base64_fingerprint)``。
#: chromaprint/fpcalc 実体はこの shape に適合させて注入する (バイナリ非依存)。
Fingerprinter = Callable[[bytes, int], "tuple[int, str]"]


class _SlackNotifierLike(Protocol):
    """本 checker が必要とする Slack 通知の最小インターフェース。

    実体は :class:`ymg_backend.infrastructure.slack.notifier.SlackNotifier`。
    循環 import と未実装依存を避けるため Protocol で受ける (構造的型付け)。
    """

    async def notify(
        self,
        *,
        level: NotificationLevel,
        message: str,
        context: Any | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class AcoustidVerdict:
    """1 投稿 (6 トラック) の AcoustID 集約判定 (不変)。

    Attributes:
        hit_positions: ``hit`` だったトラックの position 一覧 (再生成対象)。 昇順。
        consecutive_hits: 更新後の連続 hit 数 (ジャンル横断カウント)。 この post で
            1 件でも hit があれば ``前回 + 1``、 1 件でも clear が出れば ``0`` にリセット、
            api_error のみ (hit も clear も無し) なら据え置き。
        genre_suspended: 連続 hit が :data:`ACOUSTID_CONSECUTIVE_HIT_LIMIT` に達し、
            当該 ``Genre.enabled=False`` へ更新したか (FR-012)。
    """

    hit_positions: list[int]
    consecutive_hits: int
    genre_suspended: bool


class AcoustidChecker:
    """AcoustID 指紋プレチェッカ (FR-010〜012)。

    Args:
        api_key: AcoustID Web Service の API key (``client`` パラメータ)。
        storage: 音声バイトを ``read_bytes(audio_uri)`` で取得するストレージアダプタ。
        notifier: ジャンル停止時に ``[ERROR]`` を送る Slack 通知層。
        fingerprinter: 注入する指紋生成関数 (chromaprint/fpcalc を分離)。 省略時は
            :func:`default_fingerprinter` (fpcalc 実行) を遅延生成する。
        client: 注入する ``httpx.AsyncClient`` (テスト用)。 省略時は内部生成。
    """

    __slots__ = ("_api_key", "_client", "_fingerprinter", "_notifier", "_owns_client", "_storage")

    def __init__(
        self,
        api_key: SecretStr,
        storage: StorageAdapter,
        notifier: _SlackNotifierLike,
        *,
        fingerprinter: Fingerprinter | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key: Final[SecretStr] = api_key
        self._storage: Final[StorageAdapter] = storage
        self._notifier: Final[_SlackNotifierLike] = notifier
        self._fingerprinter: Final[Fingerprinter] = fingerprinter or default_fingerprinter
        self._owns_client: Final[bool] = client is None
        self._client: Final[httpx.AsyncClient] = client or httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_SEC
        )

    async def aclose(self) -> None:
        """内部生成した ``httpx.AsyncClient`` をクローズする (外部注入時は何もしない)。"""
        if self._owns_client:
            await self._client.aclose()

    # --- 1 トラック判定 --------------------------------------------------------

    async def check_track(self, *, session: AsyncSession, track: AudioTrack) -> AcoustidStatus:
        """1 トラックを lookup し、 ``acoustid_status`` を更新して結果を返す。

        フロー: storage から音声バイト取得 → 注入 fingerprinter で指紋生成 →
        lookup → スコア判定 (``clear`` / ``hit``) または通信/API 異常 (``api_error``)。
        いずれの場合も ``track`` の状態を書き換え ``session.flush()`` する
        (commit は呼び出し側)。

        Args:
            session: 呼び出し側が管理する AsyncSession。
            track: 判定対象トラック (``position`` / ``audio_uri`` / ``duration_sec`` を読む)。

        Returns:
            ``"clear"`` / ``"hit"`` / ``"api_error"`` のいずれか (= 更新後の status)。
        """
        log = logger.bind(step="acoustid", track_position=track.position, audio_uri=track.audio_uri)

        audio = self._storage.read_bytes(track.audio_uri)
        duration, fingerprint = self._fingerprinter(audio, track.duration_sec)
        track.fingerprint_hash = _fingerprint_hash(fingerprint)

        lookup = await self._lookup(fingerprint=fingerprint, duration=duration)
        if lookup is None:
            status: AcoustidStatus = _STATUS_API_ERROR
            log.warning("AcoustID lookup が api_error に分類されました")
        else:
            track.acoustid_response = lookup
            score = _max_score(lookup)
            if score is not None and score >= ACOUSTID_MATCH_THRESHOLD:
                status = _STATUS_HIT
                log.warning("AcoustID hit (score={})", f"{score:.3f}")
            else:
                status = _STATUS_CLEAR

        track.acoustid_status = status
        await session.flush()
        return status

    # --- 1 投稿 (6 トラック) 集約判定 + ジャンル停止 -----------------------------

    async def check_post_tracks(
        self,
        *,
        session: AsyncSession,
        post: Post,
        genre: str,
        tracks: list[AudioTrack],
        consecutive_hits: int,
    ) -> AcoustidVerdict:
        """1 投稿の全トラックを並列チェックし、 集約判定を返す (FR-011 / FR-012)。

        各トラックを :meth:`check_track` で並列に判定し、 ``hit`` の position を集約する。
        連続 hit カウントは:

        - この post に 1 件でも ``hit`` があれば ``consecutive_hits + 1``
        - 1 件でも ``clear`` があれば ``0`` にリセット (clear が出たジャンルは停止しない)
        - ``api_error`` のみ (hit も clear も無し) なら ``consecutive_hits`` 据え置き

        新カウントが :data:`ACOUSTID_CONSECUTIVE_HIT_LIMIT` に達したら当該
        ``Genre.enabled=False`` へ更新し、 監査ログ + Slack ``[ERROR]`` 通知を行う。

        Args:
            session: 呼び出し側が管理する AsyncSession。
            post: 対象投稿 (ログ / 監査の文脈用)。
            genre: 対象ジャンル (slug)。 停止対象の ``Genre.name``。
            tracks: 6 トラック (順不同可)。
            consecutive_hits: この post 判定前の連続 hit 数 (オーケストレータが永続化値を渡す)。

        Returns:
            :class:`AcoustidVerdict`。
        """
        statuses = await asyncio.gather(
            *(self.check_track(session=session, track=t) for t in tracks)
        )

        hit_positions = sorted(
            t.position for t, s in zip(tracks, statuses, strict=True) if s == _STATUS_HIT
        )
        has_hit = bool(hit_positions)
        has_clear = any(s == _STATUS_CLEAR for s in statuses)

        new_consecutive = _next_consecutive_hits(
            previous=consecutive_hits, has_hit=has_hit, has_clear=has_clear
        )

        genre_suspended = False
        if has_hit and new_consecutive >= ACOUSTID_CONSECUTIVE_HIT_LIMIT:
            await self._suspend_genre(
                session=session, post=post, genre=genre, consecutive_hits=new_consecutive
            )
            genre_suspended = True

        return AcoustidVerdict(
            hit_positions=hit_positions,
            consecutive_hits=new_consecutive,
            genre_suspended=genre_suspended,
        )

    # --- 内部: ジャンル停止 (FR-012) -------------------------------------------

    async def _suspend_genre(
        self, *, session: AsyncSession, post: Post, genre: str, consecutive_hits: int
    ) -> None:
        """``Genre.enabled=False`` へ更新 + 監査 + Slack ``[ERROR]`` 通知 (FR-012)。

        監査を先に永続化してから通知する (通知系が落ちても証跡が残る)。 commit は
        呼び出し側に委ねる (ここでは flush まで)。
        """
        await session.execute(update(Genre).where(Genre.name == genre).values(enabled=False))
        await write_audit_log(
            session,
            action=GENRE_SUSPENDED_AUDIT_ACTION,
            target_type="genre",
            target_id=genre,
            payload={
                "genre": genre,
                "consecutive_hits": consecutive_hits,
                "post_id": str(post.id),
            },
        )
        logger.bind(step="acoustid", genre=genre).error(
            "AcoustID 連続 {} hit によりジャンル '{}' を一時停止しました",
            consecutive_hits,
            genre,
        )
        await self._notifier.notify(
            level=NotificationLevel.ERROR,
            message=(
                f"[ERROR] AcoustID 連続 {consecutive_hits} hit によりジャンル "
                f"'{genre}' を一時停止しました (FR-012)"
            ),
            context={"genre": genre, "consecutive_hits": consecutive_hits, "post_id": str(post.id)},
        )

    # --- 内部: lookup HTTP -----------------------------------------------------

    async def _lookup(self, *, fingerprint: str, duration: int) -> dict[str, Any] | None:
        """AcoustID lookup を呼び、 ``status=ok`` の JSON を返す。 異常時は ``None``。

        5xx / 接続失敗は exponential backoff でリトライし、 最終試行も失敗なら ``None``。
        4xx・``status != "ok"``・JSON デコード失敗も ``None`` (= ``api_error`` 扱い)。
        例外は送出しない (プレチェック結果として吸収する)。
        """
        data = {
            "client": self._api_key.get_secret_value(),
            "duration": str(duration),
            "fingerprint": fingerprint,
            "meta": "recordings",
        }
        for attempt in range(_HTTP_MAX_ATTEMPTS):
            try:
                response = await self._client.post(ACOUSTID_LOOKUP_URL, data=data)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.bind(step="acoustid").warning(
                    "AcoustID lookup 通信失敗 (attempt={}): {}", attempt + 1, exc
                )
            else:
                if 500 <= response.status_code < 600:
                    logger.bind(step="acoustid").warning(
                        "AcoustID lookup 5xx (attempt={}, status={})",
                        attempt + 1,
                        response.status_code,
                    )
                elif response.status_code >= 400:
                    logger.bind(step="acoustid").warning(
                        "AcoustID lookup 4xx (status={})", response.status_code
                    )
                    return None
                else:
                    parsed = _parse_lookup(response)
                    if parsed is not None:
                        return parsed
                    return None

            if attempt + 1 < _HTTP_MAX_ATTEMPTS:
                await asyncio.sleep(_HTTP_BACKOFF_BASE_SEC * (2**attempt))

        return None


# --- モジュールヘルパ (純粋関数) ---------------------------------------------------


def _next_consecutive_hits(*, previous: int, has_hit: bool, has_clear: bool) -> int:
    """連続 hit カウントの遷移を計算する純粋関数。

    優先順位: hit があれば +1 / 無くて clear があれば 0 / どちらも無ければ据え置き。
    """
    if has_hit:
        return previous + 1
    if has_clear:
        return 0
    return previous


def _parse_lookup(response: httpx.Response) -> dict[str, Any] | None:
    """lookup レスポンスを ``status=ok`` の dict として検証する。 不正なら ``None``。"""
    try:
        body = response.json()
    except (ValueError, httpx.DecodingError):
        return None
    if not isinstance(body, dict) or body.get("status") != "ok":
        return None
    return body


def _max_score(lookup: dict[str, Any]) -> float | None:
    """lookup レスポンスから最大スコアを取り出す。 results 空なら ``None``。"""
    results = lookup.get("results")
    if not isinstance(results, list):
        return None
    scores = [
        float(r["score"])
        for r in results
        if isinstance(r, dict) and isinstance(r.get("score"), (int, float))
    ]
    return max(scores) if scores else None


def _fingerprint_hash(fingerprint: str) -> str:
    """指紋文字列の SHA-256 16 進ダイジェスト (重複検出・索引用)。"""
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def default_fingerprinter(audio: bytes, duration_sec: int) -> tuple[int, str]:
    """既定の fingerprinter (chromaprint ``fpcalc`` を subprocess 実行)。

    本番経路用。 ``audio`` を一時ファイルへ書き出し ``fpcalc -json`` を実行して
    ``(duration, fingerprint)`` を返す。 バイナリ非依存にするため checker からは
    注入可能だが、 未指定時のデフォルトとして fpcalc に委譲する。

    Note:
        テスト・dryrun ではこの関数を呼ばず stub を注入する。 fpcalc 不在環境では
        ``RecoverableError`` 相当の状況になるため、 実運用ではバイナリの存在を
        起動時にチェックする (本モジュールの責務外)。
    """
    import json
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        tmp.write(audio)
        tmp.flush()
        proc = subprocess.run(
            ["fpcalc", "-json", "-length", str(duration_sec), tmp.name],
            capture_output=True,
            text=True,
            check=True,
        )
    parsed: dict[str, Any] = json.loads(proc.stdout)
    return int(parsed["duration"]), str(parsed["fingerprint"])


__all__ = [
    "ACOUSTID_CONSECUTIVE_HIT_LIMIT",
    "ACOUSTID_LOOKUP_URL",
    "ACOUSTID_MATCH_THRESHOLD",
    "GENRE_SUSPENDED_AUDIT_ACTION",
    "AcoustidChecker",
    "AcoustidStatus",
    "AcoustidVerdict",
    "Fingerprinter",
    "default_fingerprinter",
]
