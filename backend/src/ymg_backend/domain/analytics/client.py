"""YouTube アナリティクス取得クライアント (T101, ADR-0021 / FR-101)。

公開済み動画ごとの日次指標 (retention / views / minutes / avg_duration /
impressions / ctr / traffic_sources) を **YouTube Analytics API**
(``reports.query``) と **YouTube Data API** (``videos.list``) から取得し、
``analytics_daily`` (複合 PK ``youtube_video_id`` + ``metric_date``) へ
Postgres ``INSERT ... ON CONFLICT DO UPDATE`` で upsert する取得層。

設計方針 (US3 内部契約 / 既存 ``AcoustidChecker`` パターン踏襲):

- **HTTP は httpx**。 google-api-python-client は使わず、 取得層を httpx に分離して
  respx で mock 可能にする (uploader は SDK 依存だが本取得層は wire を直接組む)。
  ``http=`` で ``AsyncClient`` を注入でき、 省略時は内部生成 (自前 close)。
- **OAuth トークンは :class:`YouTubeOAuth`**。 ``get_access_token(session=...)`` で
  平文 access token を得て ``Authorization: Bearer`` に載せる (期限前 refresh は
  OAuth 側で完結)。
- **取得指標**:
    - 主指標レポート: ``views`` / ``estimatedMinutesWatched`` /
      ``averageViewDuration`` / ``averageViewPercentage`` (= retention_pct)。
    - impressions レポート: ``impressions`` / ``impressionsCtr`` (= ctr_pct)。
      Analytics に impressions 系が無い (権限/未提供) 場合は欠落として None 据え置き。
    - 流入元レポート: ``dimensions=insightTrafficSourceType`` の views を
      ``traffic_sources`` (JSONB) に組み立てる。
    - Data API ``videos.list``: impressions 系の補完用に Bearer 付きで叩く
      (statistics 等の取得余地。 本実装では存在確認 + 認証検証が主目的)。
- **upsert 形**: 動画 1 件につき 1 回 upsert。 返り値は upsert した動画件数。
  commit は呼び出し側 (scheduler / API) 責務、 本クライアントは ``flush`` まで。
- **エラーは ADR-0028 写像**: 認証/権限 (401/403) は ``RecoverableError``、
  5xx は ``TransientError``、 その他 4xx は ``RecoverableError`` (安全側)、
  接続失敗/タイムアウトは ``TransientError`` (同一ジョブ内リトライ対象)。
- 秘密値 (token) はログ・例外メッセージに出さない。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ymg_backend.domain.errors.errors import RecoverableError, TransientError
from ymg_backend.infrastructure.db.models import AnalyticsDaily, Video

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from ymg_backend.infrastructure.youtube.oauth import YouTubeOAuth


# --- 公開定数 (ADR-0021 エンドポイント。 acoustid の URL 定数と同方針) ---------------

#: YouTube Analytics API ``reports.query`` (GET) エンドポイント。
ANALYTICS_REPORTS_URL: Final[str] = "https://youtubeanalytics.googleapis.com/v2/reports"

#: YouTube Data API ``videos.list`` (GET) エンドポイント。
YOUTUBE_VIDEOS_URL: Final[str] = "https://www.googleapis.com/youtube/v3/videos"

# Analytics ``reports.query`` の主指標 (retention は averageViewPercentage)。
_PRIMARY_METRICS: Final[str] = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage"
)
# impressions / CTR は別レポート (権限により取得できないことがあるため分離)。
_IMPRESSION_METRICS: Final[str] = "views,impressions,impressionsCtr"
# 流入元レポートの指標 (insightTrafficSourceType 別 views)。
_TRAFFIC_METRICS: Final[str] = "views"

# Analytics の列名 → 内部キー。 columnHeaders で順序解決するため定義で参照する。
_COL_VIDEO: Final[str] = "video"
_COL_VIEWS: Final[str] = "views"
_COL_MINUTES: Final[str] = "estimatedMinutesWatched"
_COL_AVG_DURATION: Final[str] = "averageViewDuration"
_COL_AVG_PERCENTAGE: Final[str] = "averageViewPercentage"
_COL_IMPRESSIONS: Final[str] = "impressions"
_COL_CTR: Final[str] = "impressionsCtr"
_COL_TRAFFIC_SOURCE: Final[str] = "insightTrafficSourceType"

# Data API ``videos.list`` の取得 part / 上限。
_VIDEOS_PART: Final[str] = "id,statistics"
_VIDEOS_MAX_RESULTS: Final[int] = 50

# HTTP 設定 (uploader / acoustid と同方針)。
_HTTP_TIMEOUT_SEC: Final[float] = 30.0
_TRANSIENT_STATUS_MIN: Final[int] = 500
_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})

# 公開済みとみなす privacy_status (videos.privacy_status enum: public/unlisted/private/deleted)。
_PUBLISHED_PRIVACY_STATUSES: Final[tuple[str, ...]] = ("public", "unlisted")


class AnalyticsClient:
    """YouTube Analytics + Data API 取得クライアント (T101)。

    Args:
        oauth: access token を供給する :class:`YouTubeOAuth`。
        http: 注入する ``httpx.AsyncClient`` (テスト用)。 省略時は内部生成し、
            :meth:`aclose` でクローズする (外部注入時は何もしない)。
    """

    __slots__ = ("_http", "_oauth", "_owns_http")

    def __init__(self, oauth: YouTubeOAuth, *, http: httpx.AsyncClient | None = None) -> None:
        self._oauth: Final[YouTubeOAuth] = oauth
        self._owns_http: Final[bool] = http is None
        self._http: Final[httpx.AsyncClient] = http or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SEC)

    async def aclose(self) -> None:
        """内部生成した ``httpx.AsyncClient`` をクローズする (外部注入時は何もしない)。"""
        if self._owns_http:
            await self._http.aclose()

    async def fetch_and_upsert(
        self,
        *,
        session: AsyncSession,
        metric_date: date,
        video_ids: list[str] | None = None,
    ) -> int:
        """指定日の動画別指標を取得し ``analytics_daily`` へ upsert する。

        ``video_ids=None`` の場合は ``videos`` テーブルの公開済み動画
        (privacy_status in public/unlisted) を対象にする。 対象が 0 件なら
        API を一切叩かず ``0`` を返す。

        各動画につき主指標 / impressions / 流入元レポートを Analytics API で取得し、
        Data API ``videos.list`` も Bearer 付きで叩いてから、 動画 1 件につき 1 回
        ``INSERT ... ON CONFLICT (youtube_video_id, metric_date) DO UPDATE`` する。
        ``session.flush()`` まで行い commit は呼び出し側に委ねる。

        Args:
            session: ``videos`` 参照 / ``analytics_daily`` upsert 用 ``AsyncSession``。
            metric_date: 集計対象日 (単日。 D-1 集計は呼び出し側が渡す)。
            video_ids: 対象動画 id。 ``None`` なら DB の公開済み動画。

        Returns:
            upsert した動画件数 (= 対象動画数)。

        Raises:
            RecoverableError: 認証/権限エラー (401/403) または 4xx 拒否の場合。
            TransientError: 5xx / 接続失敗 / タイムアウトの場合 (一過性)。
        """
        targets = (
            list(video_ids)
            if video_ids is not None
            else await self._load_published_video_ids(session)
        )
        # 重複を除き、 空入力は即 0 返し (API を叩かない)。
        targets = _dedupe_preserve_order(targets)
        if not targets:
            logger.bind(step="analytics").info("対象動画 0 件のため analytics 取得をスキップします")
            return 0

        token = await self._oauth.get_access_token(session=session)

        primary = await self._fetch_report(
            token=token,
            metric_date=metric_date,
            video_ids=targets,
            metrics=_PRIMARY_METRICS,
            dimensions="video",
        )
        impressions = await self._fetch_report(
            token=token,
            metric_date=metric_date,
            video_ids=targets,
            metrics=_IMPRESSION_METRICS,
            dimensions="video",
        )
        traffic = await self._fetch_report(
            token=token,
            metric_date=metric_date,
            video_ids=targets,
            metrics=_TRAFFIC_METRICS,
            dimensions=f"video,{_COL_TRAFFIC_SOURCE}",
        )
        # Data API videos.list (impressions 補完 + 認証検証)。 戻りは現状参照しない。
        await self._fetch_videos(token=token, video_ids=targets)

        primary_rows = _index_rows_by_video(primary)
        impression_rows = _index_rows_by_video(impressions)
        traffic_by_video = _index_traffic_by_video(traffic)

        # 流入元レポートが ``video`` 次元を持たない (単一動画前提) 場合は ``""`` キーへ
        # 集約されるため、 その集計を全対象動画共通の流入元として使う。
        traffic_fallback = traffic_by_video.get("")

        count = 0
        for video_id in targets:
            video_traffic = traffic_by_video.get(video_id, traffic_fallback)
            values = self._build_values(
                video_id=video_id,
                metric_date=metric_date,
                primary=primary_rows.get(video_id, {}),
                impression=impression_rows.get(video_id, {}),
                traffic=video_traffic,
            )
            await self._upsert(session, values)
            count += 1

        await session.flush()
        logger.bind(step="analytics").info(
            "analytics_daily を upsert しました (metric_date={}, count={})", metric_date, count
        )
        return count

    # --- 内部: 対象動画解決 ----------------------------------------------------

    async def _load_published_video_ids(self, session: AsyncSession) -> list[str]:
        """``videos`` テーブルの公開済み動画 id を取得する (video_ids=None 経路)。"""
        stmt = (
            select(Video.youtube_video_id)
            .where(Video.privacy_status.in_(_PUBLISHED_PRIVACY_STATUSES))
            .order_by(Video.posted_at.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [_row_video_id(row) for row in rows if _row_video_id(row)]

    # --- 内部: HTTP (Analytics API / Data API) ---------------------------------

    async def _fetch_report(
        self,
        *,
        token: str,
        metric_date: date,
        video_ids: Sequence[str],
        metrics: str,
        dimensions: str,
    ) -> dict[str, Any]:
        """Analytics ``reports.query`` を 1 回叩き JSON を返す。 wire 形式は契約準拠。"""
        channel_id = self._oauth_channel_id()
        params: dict[str, str] = {
            "ids": f"channel=={channel_id}",
            "startDate": metric_date.isoformat(),
            "endDate": metric_date.isoformat(),
            "metrics": metrics,
            "dimensions": dimensions,
            "filters": "video==" + ",".join(video_ids),
        }
        return await self._get_json(
            ANALYTICS_REPORTS_URL, params=params, token=token, op="reports.query"
        )

    async def _fetch_videos(self, *, token: str, video_ids: Sequence[str]) -> dict[str, Any]:
        """Data API ``videos.list`` を Bearer 付きで叩く (impressions 補完 / 認証検証)。"""
        params: dict[str, str] = {
            "part": _VIDEOS_PART,
            "id": ",".join(video_ids[:_VIDEOS_MAX_RESULTS]),
            "maxResults": str(_VIDEOS_MAX_RESULTS),
        }
        return await self._get_json(
            YOUTUBE_VIDEOS_URL, params=params, token=token, op="videos.list"
        )

    async def _get_json(
        self, url: str, *, params: Mapping[str, str], token: str, op: str
    ) -> dict[str, Any]:
        """GET を打ち JSON dict を返す。 HTTP / 接続エラーは ADR-0028 へ写像する。"""
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = await self._http.get(url, params=dict(params), headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(
                f"YouTube API への通信に失敗しました ({op})。",
                context={"op": op},
                original=exc,
            ) from exc
        self._raise_for_status(response, op=op)
        body = self._parse_json(response, op=op)
        return body

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, op: str) -> None:
        """ステータスコードを ADR-0028 のエラー分類へ写像する (2xx は素通り)。"""
        status = response.status_code
        if status < 400:
            return
        context = {"op": op, "status_code": status}
        if status in _AUTH_STATUSES:
            raise RecoverableError(
                "YouTube API が認証/権限エラーを返しました "
                "(トークン失効・スコープ不足の可能性)。 再認証が必要かもしれません。",
                context=context,
            )
        if status >= _TRANSIENT_STATUS_MIN:
            raise TransientError(
                f"YouTube API が {status} を返しました (一過性)。",
                context=context,
            )
        raise RecoverableError(
            f"YouTube API がリクエストを拒否しました ({status})。",
            context=context,
        )

    @staticmethod
    def _parse_json(response: httpx.Response, *, op: str) -> dict[str, Any]:
        """レスポンスを JSON dict として解析する。 不正なら recoverable。"""
        try:
            body = response.json()
        except (ValueError, httpx.DecodingError) as exc:
            raise RecoverableError(
                f"YouTube API のレスポンスを JSON として解析できませんでした ({op})。",
                context={"op": op},
                original=exc,
            ) from exc
        if not isinstance(body, dict):
            raise RecoverableError(
                f"YouTube API のレスポンス形式が不正です ({op})。",
                context={"op": op},
            )
        return body

    # --- 内部: 指標 → upsert values --------------------------------------------

    def _build_values(
        self,
        *,
        video_id: str,
        metric_date: date,
        primary: Mapping[str, Any],
        impression: Mapping[str, Any],
        traffic: dict[str, int] | None,
    ) -> dict[str, Any]:
        """3 レポートの行を 1 つの ``analytics_daily`` レコードへ統合する。"""
        views = _to_int(primary.get(_COL_VIEWS)) or _to_int(impression.get(_COL_VIEWS)) or 0
        return {
            "youtube_video_id": video_id,
            "metric_date": metric_date,
            "views": views,
            "estimated_minutes_watched": _to_decimal(primary.get(_COL_MINUTES)) or Decimal("0"),
            "average_view_duration_sec": _to_int(primary.get(_COL_AVG_DURATION)),
            "retention_pct": _to_decimal(primary.get(_COL_AVG_PERCENTAGE)),
            "impressions": _to_int(impression.get(_COL_IMPRESSIONS)),
            "ctr_pct": _to_decimal(impression.get(_COL_CTR)),
            "traffic_sources": traffic if traffic else None,
        }

    async def _upsert(self, session: AsyncSession, values: Mapping[str, Any]) -> None:
        """``analytics_daily`` へ PG ``ON CONFLICT DO UPDATE`` する (PK 衝突時は更新)。"""
        stmt = insert(AnalyticsDaily).values(**values)
        update_cols = {
            key: stmt.excluded[key]
            for key in values
            if key not in ("youtube_video_id", "metric_date")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["youtube_video_id", "metric_date"],
            set_=update_cols,
        )
        await session.execute(stmt)

    def _oauth_channel_id(self) -> str:
        """OAuth 設定からチャンネル id を読む (Analytics ``ids=channel==`` 用)。"""
        # YouTubeOAuth は __slots__ で _settings (Settings) を保持する (US3 内部契約)。
        # channel_id は Settings.youtube_channel_id。 取得層は同一パッケージ契約として参照する。
        settings = getattr(self._oauth, "_settings", None)
        channel_id = getattr(settings, "youtube_channel_id", None)
        if not channel_id:
            raise RecoverableError(
                "YouTube channel id が未設定です (analytics 取得には必須)。",
                context={"op": "reports.query"},
            )
        return str(channel_id)


# --- モジュールヘルパ (純粋関数) ---------------------------------------------------


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    """順序を保ったまま重複を除去する。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _row_video_id(row: Any) -> str:
    """``scalars().all()`` の各要素から video id 文字列を取り出す。

    実 ORM では ``str`` がそのまま来るが、 テストスパイは ``(vid,)`` タプルで返すため
    両方を受ける。
    """
    if isinstance(row, str):
        return row
    if isinstance(row, (tuple, list)) and row:
        return str(row[0])
    return str(row)


def _column_names(report: Mapping[str, Any]) -> list[str]:
    """``columnHeaders`` から列名一覧を取り出す。"""
    headers = report.get("columnHeaders")
    if not isinstance(headers, list):
        return []
    names: list[str] = []
    for header in headers:
        if isinstance(header, dict):
            name = header.get("name")
            names.append(str(name) if name is not None else "")
        else:
            names.append("")
    return names


def _iter_row_dicts(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``columnHeaders`` + ``rows`` を列名キーの dict 一覧へ展開する。"""
    columns = _column_names(report)
    rows = report.get("rows")
    if not columns or not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        result.append({col: row[i] if i < len(row) else None for i, col in enumerate(columns)})
    return result


def _index_rows_by_video(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``dimensions=video`` レポートを ``{video_id: row_dict}`` へ索引化する。"""
    indexed: dict[str, dict[str, Any]] = {}
    for row in _iter_row_dicts(report):
        video_id = row.get(_COL_VIDEO)
        if isinstance(video_id, str) and video_id:
            indexed[video_id] = row
    return indexed


def _index_traffic_by_video(report: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """流入元レポートを ``{video_id: {source: views}}`` へ索引化する。

    ``dimensions=video,insightTrafficSourceType`` の場合は video 別に集計し、
    ``video`` 列が無い (流入元のみの) 場合は単一動画前提で ``""`` キーへ集約する。
    """
    indexed: dict[str, dict[str, int]] = {}
    for row in _iter_row_dicts(report):
        source = row.get(_COL_TRAFFIC_SOURCE)
        if not isinstance(source, str) or not source:
            continue
        video_id = row.get(_COL_VIDEO)
        key = video_id if isinstance(video_id, str) and video_id else ""
        views = _to_int(row.get(_COL_VIEWS)) or 0
        indexed.setdefault(key, {})[source] = views
    # video 列を持たないレポート (単一動画) は全動画共通の流入元として扱えるよう
    # 空キーへ集約済み。 呼び出し側 (_build_values) が video_id で引けない場合に備え、
    # 単一エントリのみなら全 video へ波及できるよう、 そのまま返す。
    return indexed


def _to_int(value: Any) -> int | None:
    """API 値を ``int`` へ変換する (None / 変換不能は None)。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _to_decimal(value: Any) -> Decimal | None:
    """API 値を ``Decimal`` へ変換する (None / 変換不能は None)。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    return None


__all__ = [
    "ANALYTICS_REPORTS_URL",
    "YOUTUBE_VIDEOS_URL",
    "AnalyticsClient",
]
