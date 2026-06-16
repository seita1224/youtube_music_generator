"""YouTube OAuth2 トークン管理 (ADR-0012 / FR 認証).

YouTube への投稿は OAuth2 + リフレッシュトークンを必要とする (ADR-0012)。
本モジュールは以下を担う:

1. :class:`YouTubeOAuth` — ``oauth_credentials`` テーブルから暗号化済みトークンを
   読み出し ``Fernet`` 復号 (ADR-0012)、 access token が失効していれば refresh token で
   更新し、 更新後トークンを再暗号化して永続化する。 呼び出し側 (uploader) には
   平文 access token のみを返す。
2. :func:`run_oauth_flow` — 一度きりの OAuth 認可フロー (``make youtube-auth`` 想定)。
   ローカルサーバで認可を受け、 取得した refresh / access token を暗号化して保存する。

設計方針:

- google-auth (``google.oauth2.credentials.Credentials``) を refresh の実体に使う。
  refresh の HTTP 呼び出しは ``Refresher`` Protocol 越しに注入でき、 テストでは実 HTTP を
  打たない mock を渡せる。
- トークン失効 (refresh 失敗) は ``RecoverableError`` に写像する (ADR-0028: OAuth トークン
  失効は recoverable = 当該ジョブのみスキップ + Slack 通知)。 復号失敗は ``FatalError``
  (``TokenCipher`` 内で送出, ADR-0028)。
- commit はオーケストレータ責務。 本モジュールは ``session.flush`` まで (既存方針)。
- 秘密値 (token) はログ・例外メッセージに出さない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ymg_backend.core.config import Settings
from ymg_backend.core.security import TokenCipher
from ymg_backend.domain.errors.errors import RecoverableError
from ymg_backend.infrastructure.db.models import OAuthCredential

if TYPE_CHECKING:
    from collections.abc import Sequence

# OAuth サービス識別子 (oauth_credentials.service, ADR-0012)。
SERVICE_YOUTUBE: Final[str] = "youtube"

# Google OAuth2 トークンエンドポイント (refresh に使用)。
GOOGLE_TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"

# 投稿に必要な OAuth スコープ (videos.insert)。
YOUTUBE_UPLOAD_SCOPE: Final[str] = "https://www.googleapis.com/auth/youtube.upload"

# access token の失効前バッファ。 残り時間がこれを切ったら事前に refresh する。
_REFRESH_LEEWAY: Final[timedelta] = timedelta(minutes=5)


class Refresher(Protocol):
    """``Credentials`` を in-place で refresh する処理の最小インターフェース。

    既定実装は google-auth の ``Credentials.refresh`` を ``GoogleAuthRequest`` 付きで
    呼ぶ。 テストでは実 HTTP を打たない実装を注入できる。
    """

    # 位置引数で受け渡す (引数名は実装側で自由に命名できる)。
    def __call__(self, credentials: Credentials, /) -> None: ...


def _default_refresher(credentials: Credentials) -> None:
    """google-auth の標準 refresh (token エンドポイントへ HTTP POST)。"""
    # google-auth は py.typed だが refresh は untyped 扱いになるため抑制する。
    credentials.refresh(GoogleAuthRequest())  # type: ignore[no-untyped-call]


def _to_aware_utc(value: datetime | None) -> datetime | None:
    """naive datetime を UTC aware として解釈する (google-auth は naive UTC を扱う)。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class YouTubeOAuth:
    """OAuth2 access token の取得 + 期限前自動 refresh (ADR-0012)。

    Args:
        settings: ``youtube_client_id`` / ``youtube_client_secret`` /
            ``youtube_channel_id`` を持つ設定。
        cipher: ``OAuthCredential`` の BYTEA トークンを復号/暗号化する ``TokenCipher``。
        refresher: ``Credentials`` を refresh する処理 (テスト注入用)。 省略時は
            google-auth の標準 refresh を使う。
    """

    __slots__ = ("_cipher", "_refresher", "_settings")

    def __init__(
        self,
        settings: Settings,
        cipher: TokenCipher,
        *,
        refresher: Refresher | None = None,
    ) -> None:
        self._settings: Final[Settings] = settings
        self._cipher: Final[TokenCipher] = cipher
        self._refresher: Final[Refresher] = (
            refresher if refresher is not None else _default_refresher
        )

    async def get_access_token(self, *, session: AsyncSession) -> str:
        """有効な access token を返す (期限前なら refresh して再暗号化保存)。

        Args:
            session: ``OAuthCredential`` 読み書き用 ``AsyncSession`` (commit は呼び出し側)。

        Returns:
            平文 access token。

        Raises:
            RecoverableError: 認証情報未登録、 refresh token 不在、 refresh 失敗
                (トークン失効) の場合 (ADR-0028: recoverable)。
            FatalError: Fernet 復号失敗の場合 (``TokenCipher`` が送出)。
        """
        record = await self._load_credential(session)
        access_token = self._cipher.decrypt(record.access_token_encrypted)
        refresh_token = self._cipher.decrypt(record.refresh_token_encrypted)
        if not refresh_token:
            raise RecoverableError(
                "YouTube OAuth refresh token が空です。 再認証 (make youtube-auth) が必要です。",
                context={"service": SERVICE_YOUTUBE},
            )

        credentials = self._build_credentials(
            access_token=access_token,
            refresh_token=refresh_token,
            scopes=list(record.scopes),
            expiry=_to_aware_utc(record.expires_at),
        )

        if not self._needs_refresh(credentials):
            return access_token

        refreshed = self._refresh(credentials)
        await self._persist_refreshed(session=session, record=record, credentials=refreshed)
        return str(refreshed.token)

    async def _load_credential(self, session: AsyncSession) -> OAuthCredential:
        """登録済みの YouTube ``OAuthCredential`` を取得する (無ければ recoverable)。"""
        channel_id = self._settings.youtube_channel_id or None
        stmt = select(OAuthCredential).where(OAuthCredential.service == SERVICE_YOUTUBE)
        if channel_id is not None:
            stmt = stmt.where(OAuthCredential.channel_id == channel_id)
        record = (await session.execute(stmt)).scalars().first()
        if record is None:
            raise RecoverableError(
                "YouTube OAuth 認証情報が未登録です。 再認証 (make youtube-auth) が必要です。",
                context={"service": SERVICE_YOUTUBE, "channel_id": channel_id},
            )
        return record

    def _build_credentials(
        self,
        *,
        access_token: str,
        refresh_token: str,
        scopes: Sequence[str],
        expiry: datetime | None,
    ) -> Credentials:
        """復号済みトークンから google-auth ``Credentials`` を構築する。"""
        return Credentials(  # type: ignore[no-untyped-call]
            token=access_token or None,
            refresh_token=refresh_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=self._settings.youtube_client_id,
            client_secret=self._settings.youtube_client_secret.get_secret_value(),
            scopes=list(scopes) or [YOUTUBE_UPLOAD_SCOPE],
            expiry=expiry.replace(tzinfo=None) if expiry is not None else None,
        )

    @staticmethod
    def _needs_refresh(credentials: Credentials) -> bool:
        """access token が無い / 失効間近なら refresh が必要と判定する。"""
        if not credentials.token:
            return True
        expiry = _to_aware_utc(credentials.expiry)
        if expiry is None:
            # 失効時刻不明なら安全側で refresh する。
            return True
        return datetime.now(UTC) >= (expiry - _REFRESH_LEEWAY)

    def _refresh(self, credentials: Credentials) -> Credentials:
        """refresh token を使って access token を更新する (失敗は recoverable)。"""
        try:
            self._refresher(credentials)
        except Exception as exc:  # google-auth は RefreshError 等を送出
            raise RecoverableError(
                "YouTube OAuth access token の refresh に失敗しました "
                "(refresh token 失効の可能性)。 再認証 (make youtube-auth) が必要です。",
                context={"service": SERVICE_YOUTUBE},
                original=exc,
            ) from exc
        if not credentials.token:
            raise RecoverableError(
                "YouTube OAuth refresh は成功しましたが access token が空でした。",
                context={"service": SERVICE_YOUTUBE},
            )
        logger.info("youtube oauth access token を refresh しました (service={})", SERVICE_YOUTUBE)
        return credentials

    async def _persist_refreshed(
        self,
        *,
        session: AsyncSession,
        record: OAuthCredential,
        credentials: Credentials,
    ) -> None:
        """refresh 後のトークンを再暗号化して保存する (flush まで)。"""
        record.access_token_encrypted = self._cipher.encrypt(str(credentials.token))
        # refresh token は通常変わらないが、 ローテーションされた場合は更新する。
        if credentials.refresh_token:
            record.refresh_token_encrypted = self._cipher.encrypt(credentials.refresh_token)
        record.expires_at = _to_aware_utc(credentials.expiry)
        await session.flush()


async def run_oauth_flow(
    *,
    session: AsyncSession,
    settings: Settings,
    cipher: TokenCipher,
    port: int = 0,
    open_browser: bool = True,
) -> OAuthCredential:
    """一度きりの OAuth 認可フロー (``make youtube-auth`` 想定)。

    ローカルサーバで Google 認可を受け、 取得した refresh / access token を
    暗号化して ``oauth_credentials`` に upsert する。 CLI から同期的に呼ぶ想定だが、
    DB 書き込みを伴うため async とする (commit は呼び出し側 CLI 責務)。

    Args:
        session: ``OAuthCredential`` upsert 用 ``AsyncSession``。
        settings: YouTube OAuth クライアント設定。
        cipher: トークン暗号化用 ``TokenCipher``。
        port: ローカルコールバックサーバの待受ポート (0 = 自動割当)。
        open_browser: ブラウザを自動で開くか。

    Returns:
        永続化した (flush 済み) ``OAuthCredential`` レコード。

    Raises:
        RecoverableError: 認可フローが refresh token を返さなかった場合。
    """
    # 遅延 import: 認可フローは CLI 実行時のみ必要 (通常経路の import を軽くする)。
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret.get_secret_value(),
            "redirect_uris": [settings.youtube_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": GOOGLE_TOKEN_URI,
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=[YOUTUBE_UPLOAD_SCOPE])
    # access_type=offline + prompt=consent で確実に refresh token を取得する。
    credentials = flow.run_local_server(
        port=port,
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",
    )
    if not credentials.refresh_token:
        raise RecoverableError(
            "OAuth フローが refresh token を返しませんでした "
            "(access_type=offline / prompt=consent を確認してください)。",
            context={"service": SERVICE_YOUTUBE},
        )

    record = await _upsert_credential(
        session=session,
        settings=settings,
        cipher=cipher,
        credentials=credentials,
    )
    logger.info(
        "youtube oauth 認証情報を保存しました (service={}, channel_id={})",
        SERVICE_YOUTUBE,
        settings.youtube_channel_id or None,
    )
    return record


async def _upsert_credential(
    *,
    session: AsyncSession,
    settings: Settings,
    cipher: TokenCipher,
    credentials: Credentials,
) -> OAuthCredential:
    """認可結果を暗号化して ``oauth_credentials`` へ upsert する (flush まで)。"""
    import uuid

    channel_id = settings.youtube_channel_id or None
    stmt = select(OAuthCredential).where(OAuthCredential.service == SERVICE_YOUTUBE)
    if channel_id is not None:
        stmt = stmt.where(OAuthCredential.channel_id == channel_id)
    record = (await session.execute(stmt)).scalars().first()

    access_encrypted = cipher.encrypt(str(credentials.token or ""))
    refresh_encrypted = cipher.encrypt(str(credentials.refresh_token))
    scopes = list(credentials.scopes) if credentials.scopes else [YOUTUBE_UPLOAD_SCOPE]
    expires_at = _to_aware_utc(credentials.expiry)

    if record is None:
        record = OAuthCredential(
            id=uuid.uuid4(),
            service=SERVICE_YOUTUBE,
            channel_id=channel_id,
            access_token_encrypted=access_encrypted,
            refresh_token_encrypted=refresh_encrypted,
            scopes=scopes,
            expires_at=expires_at,
        )
        session.add(record)
    else:
        record.access_token_encrypted = access_encrypted
        record.refresh_token_encrypted = refresh_encrypted
        record.scopes = scopes
        record.expires_at = expires_at
    await session.flush()
    return record
