"""セキュリティプリミティブ: Fernet 暗号化と Basic 認証 (T024 / T027)。

本モジュールは 2 つの独立した責務を提供する:

1. **Fernet 対称鍵暗号化** (ADR-0012): OAuth トークンを PostgreSQL の
   ``BYTEA`` カラムへ暗号化保存するためのヘルパ。鍵は ``Settings.fernet_key``
   (``SecretStr``) から受け取る。鍵不正・復号失敗は ``FatalError`` (ADR-0028)
   として扱う (DB 接続不可・Fernet 鍵無効はサイクル全体停止カテゴリ)。

2. **HTTP Basic 認証** (ADR-0013): 管理 UI / API 全 endpoint に効く FastAPI
   依存性。``secrets.compare_digest`` で定数時間比較しタイミング攻撃を防ぐ。
   資格情報は ``Settings.admin_username`` / ``Settings.admin_password``
   (``SecretStr``) から取得する。

設計方針:

- 不変性: ``TokenCipher`` は構築済み ``Fernet`` を 1 つ保持するだけで、
  内部状態を書き換えない。
- 秘密値は ``SecretStr`` 経由で受け取り、ログ・例外メッセージへ平文を出さない。
- 境界での入力検証: 鍵の妥当性は生成時に検証し、無効なら即 ``FatalError``。
"""

from __future__ import annotations

import secrets
from typing import Annotated, Final

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import SecretStr

from ymg_backend.core.config import Settings, get_settings
from ymg_backend.domain.errors.errors import FatalError

# ============================================================================
# Fernet 暗号化 (ADR-0012, T027)
# ============================================================================

_UTF8: Final = "utf-8"


def _build_fernet(key: SecretStr) -> Fernet:
    """``SecretStr`` の Fernet 鍵から ``Fernet`` を生成する (境界での検証)。

    Args:
        key: urlsafe base64 エンコードされた 32 バイト鍵を保持する ``SecretStr``。

    Returns:
        構築済みの ``Fernet`` インスタンス。

    Raises:
        FatalError: 鍵が空、または Fernet 規格 (base64 / 32 バイト) に
            適合しない場合。起動時に検知してサイクルを止める (ADR-0028)。
    """
    raw = key.get_secret_value()
    if not raw:
        raise FatalError(
            "Fernet key is empty; set FERNET_KEY (.env) to a valid urlsafe base64 32-byte key.",
            context={"component": "fernet"},
        )
    try:
        return Fernet(raw.encode(_UTF8))
    except (ValueError, TypeError) as exc:
        # 鍵の中身 (秘密値) はメッセージへ含めない。
        raise FatalError(
            "Fernet key is invalid; expected urlsafe base64-encoded 32-byte key.",
            context={"component": "fernet"},
            original=exc,
        ) from exc


class TokenCipher:
    """Fernet による対称鍵暗号化ラッパ (ADR-0012)。

    OAuth トークン (str) を ``BYTEA`` 保存用の ciphertext (bytes) へ変換し、
    逆変換する。``Fernet`` は AES-128-CBC + HMAC-SHA256 を内包するため、
    改竄・鍵不一致は復号時に ``InvalidToken`` として検出される。

    Args:
        fernet: 構築済みの ``Fernet`` インスタンス。

    通常は :func:`build_cipher_from_key` / :func:`build_cipher_from_settings`
    を使って ``Settings`` から生成する。
    """

    __slots__ = ("_fernet",)

    def __init__(self, fernet: Fernet) -> None:
        self._fernet = fernet

    def encrypt(self, plaintext: str) -> bytes:
        """平文トークンを暗号化し ciphertext (bytes) を返す。

        Args:
            plaintext: 暗号化する文字列 (空文字も可)。

        Returns:
            ``BYTEA`` カラムへそのまま格納できる Fernet ciphertext。
        """
        return self._fernet.encrypt(plaintext.encode(_UTF8))

    def decrypt(self, ciphertext: bytes) -> str:
        """ciphertext を復号し平文トークンを返す。

        Args:
            ciphertext: :meth:`encrypt` が生成した Fernet ciphertext。

        Returns:
            復号した平文文字列。

        Raises:
            FatalError: 鍵不一致・改竄・不正なトークン形式で復号できない場合
                (ADR-0028: Fernet 復号失敗は fatal)。
        """
        try:
            return self._fernet.decrypt(ciphertext).decode(_UTF8)
        except InvalidToken as exc:
            raise FatalError(
                "Fernet decryption failed (key mismatch or tampered ciphertext).",
                context={"component": "fernet"},
                original=exc,
            ) from exc


def build_cipher_from_key(key: SecretStr) -> TokenCipher:
    """Fernet 鍵 (``SecretStr``) から ``TokenCipher`` を生成する。

    Args:
        key: urlsafe base64 の 32 バイト Fernet 鍵を保持する ``SecretStr``。

    Returns:
        構築済みの ``TokenCipher``。

    Raises:
        FatalError: 鍵が空または不正な場合 (:func:`_build_fernet` 参照)。
    """
    return TokenCipher(_build_fernet(key))


def build_cipher_from_settings(settings: Settings | None = None) -> TokenCipher:
    """``Settings.fernet_key`` から ``TokenCipher`` を生成する。

    Args:
        settings: 設定オブジェクト。``None`` の場合は ``get_settings()`` を使う。

    Returns:
        構築済みの ``TokenCipher``。

    Raises:
        FatalError: ``fernet_key`` が空または不正な場合。
    """
    resolved = settings if settings is not None else get_settings()
    return build_cipher_from_key(resolved.fernet_key)


# ============================================================================
# HTTP Basic 認証 (ADR-0013, T024)
# ============================================================================

# auto_error=False: 資格情報欠如時も自前で 401 + WWW-Authenticate を返すことで、
# 認証成否を一貫したタイミング・レスポンスで扱う。
_basic_scheme: Final = HTTPBasic(auto_error=False)


def _credentials_match(credentials: HTTPBasicCredentials, settings: Settings) -> bool:
    """資格情報が設定と一致するか定数時間比較で判定する。

    ``secrets.compare_digest`` を username / password 双方に適用し、片方だけ
    短絡評価されることによるタイミングリーク (ユーザー名の存在判定) を防ぐ。

    Args:
        credentials: クライアントが送信した Basic 認証資格情報。
        settings: 期待値 (``admin_username`` / ``admin_password``) を持つ設定。

    Returns:
        username と password の両方が一致すれば ``True``。
    """
    expected_user = settings.admin_username.encode(_UTF8)
    expected_password = settings.admin_password.get_secret_value().encode(_UTF8)
    user_ok = secrets.compare_digest(credentials.username.encode(_UTF8), expected_user)
    password_ok = secrets.compare_digest(credentials.password.encode(_UTF8), expected_password)
    # 短絡 (and) を避け、両比較を常に実行してからまとめて判定する。
    return user_ok & password_ok


def require_basic_auth(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Basic 認証を強制する FastAPI 依存性 (ADR-0013)。

    全 endpoint に ``Depends(require_basic_auth)`` で適用する。認証成功時は
    認証済みユーザー名を返す (ハンドラやログで利用可能)。

    Args:
        credentials: ``HTTPBasic`` が抽出した資格情報 (未送信時は ``None``)。
        settings: 期待する資格情報を持つ設定。

    Returns:
        認証済みのユーザー名。

    Raises:
        HTTPException: 資格情報が無い、または一致しない場合に 401 を返す。
            ``WWW-Authenticate: Basic`` ヘッダを付与しブラウザに再認証を促す。
    """
    if credentials is None or not _credentials_match(credentials, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# 型注釈用エイリアス: ``user: BasicAuthUser`` で依存性を簡潔に宣言できる。
BasicAuthUser = Annotated[str, Depends(require_basic_auth)]
