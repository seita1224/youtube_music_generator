"""RequestValidationError の機密 input 除去 (ADR-0038)。"""

from __future__ import annotations

import json
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from ymg_backend.core.validation_errors import (
    REDACTED_INPUT,
    is_sensitive_field_name,
    register_validation_exception_handler,
    sanitize_validation_errors,
)

_USERNAME = "admin"
_PASSWORD = "s3cret"
_AUTH = (_USERNAME, _PASSWORD)

# テスト専用ダミー。 アサーションは「応答・ログに含まれない」ことのみ。
_DUMMY_OVERSIZED_KEY = "sk-probe-" + ("K" * 2100)
_DUMMY_NESTED_SECRET = "nested-secret-value-should-not-echo"
_DUMMY_ADMIN_PASSWORD = "admin-password-should-not-echo"
_DUMMY_ARRAY_SECRET = "array-body-secret-should-not-echo"
_DUMMY_SCALAR_SECRET = "scalar-body-secret-should-not-echo"
_DUMMY_MISSING_PROVIDER_SECRET = "missing-provider-secret-should-not-echo"
_DUMMY_CTX_SECRET = "ctx-secret-should-not-echo"
_DUMMY_CAMEL_SECRET = "camelCase-secret-should-not-echo"
_DUMMY_LIST_INDEX_SECRET = "list-index-scalar-should-not-echo"
_DUMMY_INT_KEY_SECRET = "int-key-mapping-should-not-echo"
_DUMMY_PROVIDER_KEY_SECRET = "provider-key-should-not-echo"

pytestmark = pytest.mark.unit


class _NonSensitiveProbeIn(BaseModel):
    window_hours: int = Field(ge=1)
    reason: str = Field(min_length=4)


class _NestedAuthProbe(BaseModel):
    admin_password: str = Field(min_length=8)
    client_secret: str = Field(min_length=8)


class _NestedSecretProbeIn(BaseModel):
    auth: _NestedAuthProbe


class _CamelAliasProbeIn(BaseModel):
    api_key: str = Field(min_length=20, alias="apiKey")
    model_config = {"populate_by_name": True}


class _ListItemProbe(BaseModel):
    api_key: str = Field(min_length=20)
    label: str = Field(min_length=1)


class _ListBodyProbeIn(BaseModel):
    items: list[_ListItemProbe]


class _CtxSecretProbeIn(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _inject_ctx(cls, value: object) -> object:
        raise PydanticCustomError(
            "probe_ctx",
            "probe",
            {"api_key": _DUMMY_CTX_SECRET, "max_length": 1},
        )


class _CredentialLikeIn(BaseModel):
    provider: Literal["openai", "anthropic"]
    api_key: str = Field(min_length=1, max_length=2048)



def _assert_no_leak(haystack: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in haystack


def _probe_app_with_model(model: type[BaseModel], path: str = "/probe") -> TestClient:
    app = FastAPI()
    register_validation_exception_handler(app)

    async def _endpoint(payload: BaseModel) -> dict[str, str]:
        return {"ok": "1"}

    # 実行時の具象モデルを注釈に載せ、 FastAPI が body として扱うようにする。
    _endpoint.__annotations__["payload"] = model
    app.add_api_route(path, _endpoint, methods=["POST"])
    return TestClient(app)


# --- unit: name detection / sanitizer -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("api_key", True),
        ("API-Key", True),
        ("apiKey", True),
        ("apikey", True),
        ("api__key", True),
        ("api_keys", True),
        ("APIKeys", True),
        ("openai_api_key", True),
        ("openai_key", True),
        ("openaiKey", True),
        ("anthropic_key", True),
        ("anthropicKey", True),
        ("x_openai_key", True),
        ("password", True),
        ("pass_word", True),
        ("PassWord", True),
        ("admin_password", True),
        ("adminPassword", True),
        ("client_secret", True),
        ("clientSecret", True),
        ("secret", True),
        ("secret_key", True),
        ("SecretKey", True),
        ("refresh_token", True),
        ("tokens", True),
        ("access_tokens", True),
        ("jwt", True),
        ("JWT", True),
        ("session_jwt", True),
        ("authorization_code", True),
        ("fernet_key", True),
        ("credentials", True),
        # 通常フィールド・過広接尾辞の誤検知回避
        ("provider", False),
        ("model", False),
        ("window_hours", False),
        ("reason", False),
        ("foreign_key", False),
        ("cache_key", False),
        ("jwt_algorithm", False),
        ("api_key_length", False),
        ("token_count", False),
    ],
)
def test_is_sensitive_field_name(name: str, expected: bool) -> None:
    assert is_sensitive_field_name(name) is expected


def test_sanitize_redacts_api_key_keeps_non_sensitive() -> None:
    errors = [
        {
            "type": "string_too_long",
            "loc": ("body", "api_key"),
            "msg": "String should have at most 2048 characters",
            "input": _DUMMY_OVERSIZED_KEY,
        },
        {
            "type": "greater_than_equal",
            "loc": ("body", "window_hours"),
            "msg": "Input should be greater than or equal to 1",
            "input": 0,
        },
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == REDACTED_INPUT
    assert out[0]["loc"] == ("body", "api_key")
    assert out[0]["type"] == "string_too_long"
    assert "at most 2048" in out[0]["msg"]
    assert out[1]["input"] == 0
    assert out[1]["loc"] == ("body", "window_hours")
    _assert_no_leak(json.dumps(out), _DUMMY_OVERSIZED_KEY)


def test_sanitize_redacts_nested_password_and_secret() -> None:
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", "auth", "admin_password"),
            "msg": "String should have at least 8 characters",
            "input": "short",
        },
        {
            "type": "missing",
            "loc": ("body", "nested", "client_secret"),
            "msg": "Field required",
            "input": {"client_secret": _DUMMY_NESTED_SECRET},
        },
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == REDACTED_INPUT
    assert out[1]["input"] == REDACTED_INPUT
    blob = json.dumps(out)
    assert out[0]["input"] != "short"
    assert '"input": "short"' not in blob
    _assert_no_leak(blob, _DUMMY_NESTED_SECRET, _DUMMY_ADMIN_PASSWORD)


def test_sanitize_deep_copies_and_does_not_share_nested_refs() -> None:
    ctx = {"api_key": _DUMMY_CTX_SECRET, "max_length": 1}
    nested_input = {"api_key": _DUMMY_MISSING_PROVIDER_SECRET, "provider": "openai"}
    errors = [
        {
            "type": "missing",
            "loc": ("body", "provider"),
            "msg": "Field required",
            "input": nested_input,
            "ctx": ctx,
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] is not nested_input
    assert out[0]["ctx"] is not ctx
    assert nested_input["api_key"] == _DUMMY_MISSING_PROVIDER_SECRET
    assert ctx["api_key"] == _DUMMY_CTX_SECRET
    assert out[0]["input"]["api_key"] == REDACTED_INPUT
    assert out[0]["ctx"]["api_key"] == REDACTED_INPUT
    assert out[0]["ctx"]["max_length"] == 1


def test_sanitize_array_body_root_redacts_entire_input() -> None:
    errors = [
        {
            "type": "model_attributes_type",
            "loc": ("body",),
            "msg": "Input should be a valid dictionary",
            "input": [{"api_key": _DUMMY_ARRAY_SECRET}],
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == REDACTED_INPUT
    _assert_no_leak(json.dumps(out), _DUMMY_ARRAY_SECRET)


def test_sanitize_scalar_body_root_redacts_entire_input() -> None:
    errors = [
        {
            "type": "model_attributes_type",
            "loc": ("body",),
            "msg": "Input should be a valid dictionary",
            "input": _DUMMY_SCALAR_SECRET,
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == REDACTED_INPUT
    _assert_no_leak(json.dumps(out), _DUMMY_SCALAR_SECRET)


def test_sanitize_empty_loc_scalar_redacts() -> None:
    errors = [
        {
            "type": "model_type",
            "loc": (),
            "msg": "Input should be a valid dictionary",
            "input": _DUMMY_SCALAR_SECRET,
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == REDACTED_INPUT


def test_sanitize_non_sensitive_loc_mapping_redacts_secret_keys() -> None:
    """provider 欠落など非機密 loc でも body 全体 input 内の秘密キーを伏せる。"""
    errors = [
        {
            "type": "missing",
            "loc": ("body", "provider"),
            "msg": "Field required",
            "input": {
                "api_key": _DUMMY_MISSING_PROVIDER_SECRET,
                "extra": "keep-me",
            },
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"]["api_key"] == REDACTED_INPUT
    assert out[0]["input"]["extra"] == "keep-me"
    _assert_no_leak(json.dumps(out), _DUMMY_MISSING_PROVIDER_SECRET)


def test_sanitize_ctx_secret_like_mapping() -> None:
    errors = [
        {
            "type": "probe_ctx",
            "loc": ("body", "name"),
            "msg": "probe",
            "input": "safe",
            "ctx": {
                "api_key": _DUMMY_CTX_SECRET,
                "clientSecret": _DUMMY_CTX_SECRET,
                "max_length": 1,
            },
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == "safe"
    assert out[0]["ctx"]["api_key"] == REDACTED_INPUT
    assert out[0]["ctx"]["clientSecret"] == REDACTED_INPUT
    assert out[0]["ctx"]["max_length"] == 1
    _assert_no_leak(json.dumps(out), _DUMMY_CTX_SECRET)


def test_sanitize_alias_camel_case_loc_redacts() -> None:
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", "apiKey"),
            "msg": "String should have at least 20 characters",
            "input": _DUMMY_CAMEL_SECRET,
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == REDACTED_INPUT
    _assert_no_leak(json.dumps(out), _DUMMY_CAMEL_SECRET)


def test_sanitize_list_index_scalar_unattributable_redacts() -> None:
    """loc 末端が配列インデックスのスカラーは帰属不可のため全体を伏せる。"""
    errors = [
        {
            "type": "list_type",
            "loc": ("body", 0),
            "msg": "Input should be a valid dictionary",
            "input": _DUMMY_LIST_INDEX_SECRET,
        },
        {
            "type": "model_type",
            "loc": ("body", "items", 0),
            "msg": "Input should be a valid dictionary",
            "input": _DUMMY_LIST_INDEX_SECRET,
        },
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == REDACTED_INPUT
    assert out[1]["input"] == REDACTED_INPUT
    _assert_no_leak(json.dumps(out), _DUMMY_LIST_INDEX_SECRET)


def test_sanitize_list_index_list_unattributable_redacts() -> None:
    """loc 末端が配列インデックスの list input も全体を伏せる。"""
    errors = [
        {
            "type": "list_type",
            "loc": ("body", "items", 0),
            "msg": "Input should be a valid list",
            "input": [_DUMMY_LIST_INDEX_SECRET, "keep-looking"],
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == REDACTED_INPUT
    _assert_no_leak(json.dumps(out), _DUMMY_LIST_INDEX_SECRET, "keep-looking")


def test_sanitize_list_index_mapping_redacts_secret_keys() -> None:
    """インデックス末端でも mapping なら秘密キーのみ伏せ、非機密は残す。"""
    errors = [
        {
            "type": "model_type",
            "loc": ("body", "items", 0),
            "msg": "Input should be a valid dictionary",
            "input": {
                "api_key": _DUMMY_LIST_INDEX_SECRET,
                "label": "visible-label",
            },
        }
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"]["api_key"] == REDACTED_INPUT
    assert out[0]["input"]["label"] == "visible-label"
    _assert_no_leak(json.dumps(out), _DUMMY_LIST_INDEX_SECRET)


def test_sanitize_body_root_non_string_keys_redact_values() -> None:
    """body ルート mapping の非文字列・空キーは帰属不可のため値を伏せる。"""
    errors = [
        {
            "type": "model_attributes_type",
            "loc": ("body",),
            "msg": "Input should be a valid dictionary",
            "input": {
                1: _DUMMY_INT_KEY_SECRET,
                "": _DUMMY_INT_KEY_SECRET,
                "api_key": _DUMMY_INT_KEY_SECRET,
                "keep": "ok-non-secret",
            },
        }
    ]
    out = sanitize_validation_errors(errors)
    inp = out[0]["input"]
    assert isinstance(inp, dict)
    assert inp[1] == REDACTED_INPUT
    assert inp[""] == REDACTED_INPUT
    assert inp["api_key"] == REDACTED_INPUT
    assert inp["keep"] == "ok-non-secret"
    _assert_no_leak(json.dumps(out, default=str), _DUMMY_INT_KEY_SECRET)


def test_sanitize_provider_specific_and_plural_loc_redacts() -> None:
    """openai_key / jwt / api_keys 等の loc も全体を伏せる。"""
    secrets = {
        "openai_key": _DUMMY_PROVIDER_KEY_SECRET + "-openai",
        "anthropic_key": _DUMMY_PROVIDER_KEY_SECRET + "-anthropic",
        "jwt": _DUMMY_PROVIDER_KEY_SECRET + "-jwt",
        "api_keys": _DUMMY_PROVIDER_KEY_SECRET + "-apikeys",
        "tokens": _DUMMY_PROVIDER_KEY_SECRET + "-tokens",
    }
    errors = [
        {
            "type": "string_too_short",
            "loc": ("body", field),
            "msg": "too short",
            "input": value,
        }
        for field, value in secrets.items()
    ]
    out = sanitize_validation_errors(errors)
    for item in out:
        assert item["input"] == REDACTED_INPUT
    blob = json.dumps(out)
    for value in secrets.values():
        _assert_no_leak(blob, value)


def test_sanitize_attributed_non_sensitive_scalar_preserved() -> None:
    """末端フィールド名がある非機密スカラーはデバッグ用に残す。"""
    errors = [
        {
            "type": "greater_than_equal",
            "loc": ("body", "window_hours"),
            "msg": "Input should be greater than or equal to 1",
            "input": 0,
        },
        {
            "type": "string_too_short",
            "loc": ("body", "items", 0, "label"),
            "msg": "String should have at least 1 character",
            "input": "",
        },
    ]
    out = sanitize_validation_errors(errors)
    assert out[0]["input"] == 0
    assert out[1]["input"] == ""


def test_non_sensitive_validation_keeps_useful_input() -> None:
    """通常フィールドの 422 では input が残り、 デバッグに使える。"""
    probe = _probe_app_with_model(_NonSensitiveProbeIn, "/probe-nonsensitive")
    resp = probe.post(
        "/probe-nonsensitive",
        json={"window_hours": 0, "reason": "abcd"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    hours_errs = [
        e for e in detail if isinstance(e, dict) and "window_hours" in list(e.get("loc", []))
    ]
    assert hours_errs, f"unexpected detail={detail!r}"
    assert hours_errs[0].get("input") == 0
    assert "type" in hours_errs[0] and "msg" in hours_errs[0]


def test_nested_secret_like_body_redacts_via_handler() -> None:
    """ネストした admin_password / client_secret も handler 経由で伏せる。"""
    probe = _probe_app_with_model(_NestedSecretProbeIn, "/probe-nested-secret")
    resp = probe.post(
        "/probe-nested-secret",
        json={
            "auth": {
                "admin_password": "short",
                "client_secret": "tiny",
            }
        },
    )
    assert resp.status_code == 422
    raw = resp.text
    for err in resp.json()["detail"]:
        if any(part in ("admin_password", "client_secret") for part in err.get("loc", [])):
            assert err.get("input") == REDACTED_INPUT
    assert '"input": "short"' not in raw
    assert '"input": "tiny"' not in raw
    _assert_no_leak(raw, _DUMMY_ADMIN_PASSWORD, _DUMMY_NESTED_SECRET)


def test_camel_alias_endpoint_redacts_secret() -> None:
    probe = _probe_app_with_model(_CamelAliasProbeIn, "/probe-camel")
    short = "too-short"
    resp = probe.post("/probe-camel", json={"apiKey": short})
    assert resp.status_code == 422
    for err in resp.json()["detail"]:
        if any(is_sensitive_field_name(str(part)) for part in err.get("loc", [])):
            assert err.get("input") == REDACTED_INPUT
    assert short not in resp.text
    _assert_no_leak(resp.text, _DUMMY_CAMEL_SECRET)


def test_nested_list_secret_redacts_via_handler() -> None:
    probe = _probe_app_with_model(_ListBodyProbeIn, "/probe-list")
    short = "too-short"
    resp = probe.post(
        "/probe-list",
        json={"items": [{"api_key": short, "label": "x"}]},
    )
    assert resp.status_code == 422
    for err in resp.json()["detail"]:
        if any(is_sensitive_field_name(str(part)) for part in err.get("loc", [])):
            assert err.get("input") == REDACTED_INPUT
        inp = err.get("input")
        if isinstance(inp, dict) and "api_key" in inp:
            assert inp["api_key"] == REDACTED_INPUT
    assert short not in resp.text
    _assert_no_leak(resp.text, _DUMMY_ARRAY_SECRET)


def test_ctx_secret_mapping_redacted_via_handler() -> None:
    probe = _probe_app_with_model(_CtxSecretProbeIn, "/probe-ctx")
    resp = probe.post("/probe-ctx", json={"name": "anything"})
    assert resp.status_code == 422
    for err in resp.json()["detail"]:
        ctx = err.get("ctx")
        if isinstance(ctx, dict) and "api_key" in ctx:
            assert ctx["api_key"] == REDACTED_INPUT
            assert ctx.get("max_length") == 1
    _assert_no_leak(resp.text, _DUMMY_CTX_SECRET)


def test_credential_like_array_and_scalar_on_probe_endpoint() -> None:
    """実 FastAPI エンドポイントで配列 / スカラー body の回帰。"""
    probe = _probe_app_with_model(_CredentialLikeIn, "/probe-cred")
    array_resp = probe.post(
        "/probe-cred",
        content=json.dumps([{"api_key": _DUMMY_ARRAY_SECRET}]).encode(),
        headers={"content-type": "application/json"},
    )
    assert array_resp.status_code == 422
    for err in array_resp.json()["detail"]:
        assert err.get("input") == REDACTED_INPUT
    _assert_no_leak(array_resp.text, _DUMMY_ARRAY_SECRET)

    scalar_resp = probe.post(
        "/probe-cred",
        content=json.dumps(_DUMMY_SCALAR_SECRET).encode(),
        headers={"content-type": "application/json"},
    )
    assert scalar_resp.status_code == 422
    for err in scalar_resp.json()["detail"]:
        assert err.get("input") == REDACTED_INPUT
    _assert_no_leak(scalar_resp.text, _DUMMY_SCALAR_SECRET)
