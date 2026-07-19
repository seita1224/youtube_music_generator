"""RequestValidationError の機密 ``input`` 除去 (ADR-0038)。

FastAPI 既定ハンドラは Pydantic の ``errors()`` をそのまま返し、 ``api_key`` 等の
提出値が ``detail[].input`` に平文で載る。 本モジュールはその値だけを伏せ、
``loc`` / ``type`` / ``msg`` は通常フィールドと同じく有用なまま残す。

方針 (ADR-0038):
- エラー要素は常にディープコピーし、元の入れ子参照を共有しない。
- ``loc`` が機密フィールドを指すときは ``input`` 全体を置換する。
- 非機密 ``loc`` でも ``input`` / ``ctx`` 内の mapping・list を再帰走査し、
  秘密らしいキーの値を伏せる (例: provider 欠落時に body 全体が ``input`` になる経路)。
- ``loc`` 末端にフィールド名が無い (body ルート、配列インデックス等) とき、
  スカラー / list / 不明型はフィールド帰属不可のため ``input`` 全体を伏せる。
- mapping の非文字列・空キーは名前帰属不可のため値を伏せる。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, TypeGuard

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "REDACTED_INPUT",
    "is_sensitive_field_name",
    "register_validation_exception_handler",
    "sanitize_validation_errors",
    "sanitized_request_validation_exception_handler",
]

# 応答・ログに載せる置換マーカー。 提出バイト列そのものは残さない。
REDACTED_INPUT: Final[str] = "***"

_REQUEST_ROOT_LOCS: Final[frozenset[str]] = frozenset({"body", "query", "path", "header", "cookie"})

# 正規化後の完全一致 (snake_case + 圧縮別名)。
# 広すぎる ``*_key`` 接尾辞は foreign_key / cache_key 等の誤検知になるため使わない。
# 代わりに既知の資格情報名・複数形・provider 固有を明示する。
_SENSITIVE_EXACT: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "api_keys",
        "password",
        "passwd",
        "pass_word",
        "secret",
        "secret_key",
        "token",
        "tokens",
        "jwt",
        "authorization",
        "authorization_code",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "admin_password",
        "fernet_key",
        "credentials",
        "credential",
        "openai_key",
        "anthropic_key",
    }
)

# アンダースコア除去後の別名 (apikey / clientsecret 等)。
_SENSITIVE_COMPACT: Final[frozenset[str]] = frozenset(
    {
        "apikey",
        "apikeys",
        "password",
        "passwd",
        "secret",
        "secretkey",
        "token",
        "tokens",
        "jwt",
        "authorization",
        "authorizationcode",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "privatekey",
        "adminpassword",
        "fernetkey",
        "credentials",
        "credential",
        "openaikey",
        "anthropickey",
    }
)

# 接尾辞一致 (例: openai_api_key / nested_admin_password / refresh_token)。
# ``_key`` 単独は誤検知が多いので入れない。 provider 固有と複数形のみ追加。
_SENSITIVE_SUFFIXES: Final[tuple[str, ...]] = (
    "_api_key",
    "_api_keys",
    "_password",
    "_passwd",
    "_pass_word",
    "_secret",
    "_secret_key",
    "_token",
    "_tokens",
    "_jwt",
    "_credentials",
    "_credential",
    "_fernet_key",
    "_authorization",
    "_authorization_code",
    "_private_key",
    "_openai_key",
    "_anthropic_key",
)

_CAMEL_BOUNDARY_RE: Final[re.Pattern[str]] = re.compile(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])")
_SEPARATOR_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[_\-\s]+")


def _normalize_field_name(name: str) -> str:
    """snake / camel / hyphen / 連続区切りを防御的に正規化する。"""
    stripped = name.strip()
    if not stripped:
        return ""
    # camelCase / PascalCase / APIKey → 境界に _
    with_breaks = _CAMEL_BOUNDARY_RE.sub(
        lambda m: f"{m.group(1) or m.group(3)}_{m.group(2) or m.group(4)}",
        stripped,
    )
    lowered = with_breaks.lower()
    collapsed = _SEPARATOR_RUN_RE.sub("_", lowered).strip("_")
    return collapsed


def is_sensitive_field_name(name: str) -> bool:
    """フィールド名が秘密値 (api_key / password / secret / token 系) なら True。"""
    normalized = _normalize_field_name(name)
    if not normalized:
        return False
    if normalized in _SENSITIVE_EXACT:
        return True
    compact = normalized.replace("_", "")
    if compact in _SENSITIVE_COMPACT:
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _loc_is_sensitive(loc: Sequence[Any]) -> bool:
    return any(isinstance(part, str) and is_sensitive_field_name(part) for part in loc)


def _loc_terminal_field_name(loc: Sequence[Any]) -> str | None:
    """``loc`` 末端が名前付きフィールドならその文字列、さもなくば None。

    配列インデックス (int) や ``body`` ルートだけではスカラーをフィールドに
    帰属できない。 末端が文字列でも request root 名だけのときは帰属不可。
    """
    if not loc:
        return None
    terminal = loc[-1]
    if not isinstance(terminal, str):
        return None
    if terminal in _REQUEST_ROOT_LOCS:
        return None
    return terminal


def _is_field_unattributable(loc: Sequence[Any]) -> bool:
    """末端フィールド名が無く、スカラー / list を安全に残せる帰属が無い。"""
    return _loc_terminal_field_name(loc) is None


def _is_mapping(value: object) -> TypeGuard[Mapping[Any, Any]]:
    return isinstance(value, Mapping) and not isinstance(value, (str, bytes, bytearray))


def _is_sequence(value: object) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    )


def _mapping_key_attributable(key: object) -> TypeGuard[str]:
    """文字列かつ非空のキーだけ名前ヒューリスティックを適用できる。"""
    return isinstance(key, str) and bool(key.strip())


def _sanitize_tree(value: object) -> object:
    """mapping / list を再帰コピーし、秘密らしいキーの値を伏せる。

    非文字列・空キーはフィールド帰属不可のため値全体を伏せる。
    """
    if _is_mapping(value):
        out: dict[Any, Any] = {}
        for key, child in value.items():
            if not _mapping_key_attributable(key) or is_sensitive_field_name(key):
                out[key] = REDACTED_INPUT
            else:
                out[key] = _sanitize_tree(child)
        return out
    if _is_sequence(value):
        return [_sanitize_tree(item) for item in value]
    # スカラー・不明型はそのまま (イミュータブル想定)。 bytes はコピー。
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return value


def _sanitize_error_input(loc: Sequence[Any], value: object) -> object:
    """1 件の ``input`` を方針どおり伏せる。"""
    if _loc_is_sensitive(loc):
        return REDACTED_INPUT

    if _is_field_unattributable(loc):
        if _is_mapping(value):
            # body ルート等: 秘密キーと非文字列キーを再帰的に伏せる。
            return _sanitize_tree(value)
        # スカラー / list / 不明型: フィールド帰属不可のため全体を伏せる。
        return REDACTED_INPUT

    if _is_mapping(value) or _is_sequence(value):
        return _sanitize_tree(value)

    # 非機密フィールドへの明示帰属があるスカラーはデバッグ用に残す。
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return value


def _copy_loc(loc: object) -> tuple[Any, ...] | object:
    if _is_sequence(loc):
        return tuple(loc)
    return loc


def sanitize_validation_errors(
    errors: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """検証エラー配列をディープコピーし、機密 ``input`` / ``ctx`` を伏せる。

    - 機密 ``loc``: ``input`` を :data:`REDACTED_INPUT` に置換。
    - 非機密でも mapping/list の ``input`` は秘密キーを再帰的に伏せる。
    - フィールド帰属不可の ``loc`` (body ルート・配列インデックス末端等) の
      スカラー / list / 不明 ``input`` は全体を伏せる。
    - mapping の非文字列・空キーの値も伏せる。
    - ``ctx`` も同様に再帰サニタイズする。
    - ``type`` / ``loc`` / ``msg`` と、明示帰属のある非機密スカラー ``input`` は残す。
    """
    sanitized: list[dict[str, Any]] = []
    for raw in errors:
        item: dict[str, Any] = {}
        loc_raw = raw.get("loc", ())
        loc = _copy_loc(loc_raw)
        loc_seq: Sequence[Any] = loc if _is_sequence(loc) else ()

        for key, value in raw.items():
            if key == "loc":
                item["loc"] = loc
            elif key == "input":
                item["input"] = _sanitize_error_input(loc_seq, value)
            elif key == "ctx":
                item["ctx"] = _sanitize_tree(value)
            elif _is_mapping(value) or _is_sequence(value):
                # 未知の入れ子キーも共有参照を残さない。
                item[key] = _sanitize_tree(value)
            elif isinstance(value, (bytes, bytearray, memoryview)):
                item[key] = bytes(value)
            else:
                item[key] = value
        sanitized.append(item)
    return sanitized


async def sanitized_request_validation_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """``RequestValidationError`` を 422 で返し、 機密 ``input`` を伏せる。"""
    if not isinstance(exc, RequestValidationError):
        raise exc
    detail = sanitize_validation_errors(exc.errors())
    # 運用ログにも平文を載せない (type / loc / msg のみ。 input は含めない)。
    logger.bind(
        component="validation",
        path=str(request.url.path),
        error_count=len(detail),
        errors=[
            {
                "type": err.get("type"),
                "loc": list(err.get("loc", ())),
                "msg": err.get("msg"),
            }
            for err in detail
        ],
    ).warning("request_validation_failed")
    return JSONResponse(
        status_code=HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": jsonable_encoder(detail)},
    )


def register_validation_exception_handler(app: FastAPI) -> None:
    """``app`` に機密 ``input`` 除去付きの validation ハンドラを登録する。

    既存の他例外ハンドラは触らない。 ``RequestValidationError`` のみ上書きする
    (FastAPI 既定ハンドラの置換)。
    """
    app.add_exception_handler(
        RequestValidationError,
        sanitized_request_validation_exception_handler,
    )
