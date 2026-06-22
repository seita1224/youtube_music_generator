"""Critical path test: Fernet 暗号化 / 復号ヘルパ (T026, Constitution II).

ADR-0012 (OAuth トークンを Fernet 対称鍵暗号化で PostgreSQL に保存) の暗号化
プリミティブを 100% カバーで検証する。対象は ``core/security.py`` の
``TokenCipher`` および設定からの生成ヘルパ。

検証観点:

- 暗号化 / 復号のラウンドトリップ (ASCII / 非 ASCII / 空文字)
- 鍵不一致 (別鍵で生成した ciphertext を復号できない)
- 改竄検出 (ciphertext を 1 バイト書き換えると復号失敗)
- 不正な Fernet 鍵 / 空鍵 での生成は ``FatalError`` (ADR-0028)
- 設定 (``SecretStr``) からの生成ヘルパ

Marker: ``critical`` (100% カバレッジ必須)。
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from pydantic import SecretStr

from ymg_backend.core.config import Settings
from ymg_backend.core.security import (
    TokenCipher,
    build_cipher_from_key,
    build_cipher_from_settings,
    require_basic_auth,
)
from ymg_backend.domain.errors.errors import FatalError

pytestmark = pytest.mark.critical


@pytest.fixture
def fernet_key() -> str:
    """テスト用に生成した有効な Fernet 鍵 (urlsafe base64)。"""
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def cipher(fernet_key: str) -> TokenCipher:
    """有効な鍵で生成した ``TokenCipher``。"""
    return build_cipher_from_key(SecretStr(fernet_key))


# --- ラウンドトリップ -------------------------------------------------------------


@pytest.mark.fr("FR-083")
@pytest.mark.parametrize(
    "plaintext",
    [
        "ya29.a0AfH6SMexample-refresh-token",
        "",  # 空文字
        "日本語のトークン文字列",  # 非 ASCII (UTF-8)
        "x" * 4096,  # 長い文字列
        "line1\nline2\twith\x00null",  # 制御文字を含む
    ],
)
def test_encrypt_decrypt_roundtrip(cipher: TokenCipher, plaintext: str) -> None:
    """FR-083: 暗号化 → 復号で元の平文に戻る。"""
    ciphertext = cipher.encrypt(plaintext)

    assert isinstance(ciphertext, bytes)
    assert ciphertext != plaintext.encode("utf-8")  # 平文がそのまま出ていない
    assert cipher.decrypt(ciphertext) == plaintext


def test_encrypt_is_nondeterministic(cipher: TokenCipher) -> None:
    """Fernet は IV / timestamp を含むため、同一平文でも ciphertext は毎回異なる。"""
    plaintext = "same-token-value"

    first = cipher.encrypt(plaintext)
    second = cipher.encrypt(plaintext)

    assert first != second
    assert cipher.decrypt(first) == plaintext
    assert cipher.decrypt(second) == plaintext


def test_decrypt_str_alias(cipher: TokenCipher) -> None:
    """``decrypt`` は str を返す (BYTEA カラムから読んだ bytes を受け取る)。"""
    ciphertext = cipher.encrypt("token")
    assert isinstance(cipher.decrypt(ciphertext), str)


# --- 鍵不一致 ---------------------------------------------------------------------


def test_decrypt_with_wrong_key_raises(fernet_key: str) -> None:
    """別鍵で暗号化した ciphertext は復号できず ``FatalError`` を送出する。"""
    cipher_a = build_cipher_from_key(SecretStr(fernet_key))
    cipher_b = build_cipher_from_key(SecretStr(Fernet.generate_key().decode("ascii")))

    ciphertext = cipher_a.encrypt("secret")

    with pytest.raises(FatalError) as exc_info:
        cipher_b.decrypt(ciphertext)

    assert exc_info.value.category.value == "fatal"


# --- 改竄検出 ---------------------------------------------------------------------


def test_decrypt_tampered_ciphertext_raises(cipher: TokenCipher) -> None:
    """ciphertext を 1 バイト書き換えると HMAC 検証に失敗し ``FatalError``。"""
    ciphertext = bytearray(cipher.encrypt("secret"))
    # 末尾 (HMAC 部) を反転させて改竄する。
    ciphertext[-1] ^= 0xFF

    with pytest.raises(FatalError):
        cipher.decrypt(bytes(ciphertext))


def test_decrypt_garbage_raises(cipher: TokenCipher) -> None:
    """Fernet トークン形式ですらないバイト列は復号できない。"""
    with pytest.raises(FatalError):
        cipher.decrypt(b"not-a-fernet-token")


def test_decrypt_empty_ciphertext_raises(cipher: TokenCipher) -> None:
    """空の ciphertext は復号できない。"""
    with pytest.raises(FatalError):
        cipher.decrypt(b"")


# --- 鍵の生成 / バリデーション -----------------------------------------------------


def test_build_cipher_with_empty_key_raises() -> None:
    """空の Fernet 鍵での生成は起動時バリデーションで ``FatalError`` (ADR-0012)。"""
    with pytest.raises(FatalError):
        build_cipher_from_key(SecretStr(""))


def test_build_cipher_with_invalid_key_raises() -> None:
    """base64 として不正な鍵は ``FatalError`` を送出する。"""
    with pytest.raises(FatalError):
        build_cipher_from_key(SecretStr("this-is-not-a-valid-fernet-key"))


def test_build_cipher_with_wrong_length_key_raises() -> None:
    """長さが 32 バイトでない (Fernet 規格外) 鍵は ``FatalError``。"""
    # 有効な base64 だが Fernet が要求する 32 バイトではない。
    import base64

    short_key = base64.urlsafe_b64encode(b"too-short").decode("ascii")
    with pytest.raises(FatalError):
        build_cipher_from_key(SecretStr(short_key))


def test_token_cipher_accepts_fernet_instance(fernet_key: str) -> None:
    """``TokenCipher`` は構築済み ``Fernet`` を受け取れる (DI 用)。"""
    fernet = Fernet(fernet_key.encode("ascii"))
    cipher = TokenCipher(fernet)

    assert cipher.decrypt(cipher.encrypt("v")) == "v"


# --- 設定からの生成 ---------------------------------------------------------------


def test_build_cipher_from_explicit_settings(fernet_key: str) -> None:
    """明示的に渡した ``Settings.fernet_key`` から生成できる。"""
    settings = Settings(fernet_key=SecretStr(fernet_key))
    cipher = build_cipher_from_settings(settings)

    assert cipher.decrypt(cipher.encrypt("tok")) == "tok"


def test_build_cipher_from_settings_uses_get_settings(
    monkeypatch: pytest.MonkeyPatch, fernet_key: str
) -> None:
    """``settings=None`` のとき ``get_settings()`` 経由で鍵を解決する。"""
    monkeypatch.setattr(
        "ymg_backend.core.security.get_settings",
        lambda: Settings(fernet_key=SecretStr(fernet_key)),
    )
    cipher = build_cipher_from_settings()

    assert cipher.decrypt(cipher.encrypt("tok")) == "tok"


def test_build_cipher_from_settings_invalid_key_raises() -> None:
    """設定の ``fernet_key`` が空のとき ``FatalError`` を送出する。"""
    settings = Settings(fernet_key=SecretStr(""))
    with pytest.raises(FatalError):
        build_cipher_from_settings(settings)


# --- Basic 認証 (ADR-0013, T024) --------------------------------------------------


@pytest.fixture
def auth_settings() -> Settings:
    """Basic 認証用の既知資格情報を持つ設定。"""
    return Settings(
        admin_username="admin",
        admin_password=SecretStr("s3cret"),
    )


def test_require_basic_auth_accepts_valid_credentials(auth_settings: Settings) -> None:
    """正しい資格情報なら認証済みユーザー名を返す。"""
    credentials = HTTPBasicCredentials(username="admin", password="s3cret")
    assert require_basic_auth(credentials, auth_settings) == "admin"


def test_require_basic_auth_rejects_missing_credentials(auth_settings: Settings) -> None:
    """資格情報未送信 (None) は 401 + WWW-Authenticate を返す。"""
    with pytest.raises(HTTPException) as exc_info:
        require_basic_auth(None, auth_settings)

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Basic"}


def test_require_basic_auth_rejects_wrong_password(auth_settings: Settings) -> None:
    """パスワード不一致は 401 を返す。"""
    credentials = HTTPBasicCredentials(username="admin", password="wrong")
    with pytest.raises(HTTPException) as exc_info:
        require_basic_auth(credentials, auth_settings)

    assert exc_info.value.status_code == 401


def test_require_basic_auth_rejects_wrong_username(auth_settings: Settings) -> None:
    """ユーザー名不一致は 401 を返す。"""
    credentials = HTTPBasicCredentials(username="intruder", password="s3cret")
    with pytest.raises(HTTPException):
        require_basic_auth(credentials, auth_settings)
