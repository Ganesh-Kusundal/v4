"""Tests for auth utility functions ported from v3.

Covers ``totp_code``, ``jwt_expiry``, ``_form_post``, ``_extract_message``,
``_is_rate_limit_message``, and ``_require_mapping``.  The broker-specific
mint stubs (``dhan_totp_mint``, ``upstox_refresh_mint``, ``upstox_totp_mint``)
are not tested here — they always raise ``NotImplementedError``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from tradex_domain import AuthenticationError

from tradex_brokers.common.auth import (
    _extract_message,
    _form_post,
    _is_rate_limit_message,
    _require_mapping,
    jwt_expiry,
    totp_code,
)

# ---------------------------------------------------------------------------
# totp_code — RFC 6238
# ---------------------------------------------------------------------------


def test_totp_code_produces_six_digit_string() -> None:
    # Well-known test vector: base32("12345678901234567890") = "GEZDGNBVGY3TQOJQ"
    code = totp_code("GEZDGNBVGY3TQOJQ", at=59.0)
    assert len(code) == 6
    assert code.isdigit()


def test_totp_code_is_deterministic_for_same_time_step() -> None:
    code1 = totp_code("JBSWY3DPEHPK3PXP", at=1000000000.0)
    code2 = totp_code("JBSWY3DPEHPK3PXP", at=1000000000.0)
    assert code1 == code2


def test_totp_code_changes_across_steps() -> None:
    code1 = totp_code("JBSWY3DPEHPK3PXP", at=1000000000.0)
    code2 = totp_code("JBSWY3DPEHPK3PXP", at=1000000030.0)  # next 30s step
    assert code1 != code2


def test_totp_code_custom_step_and_digits() -> None:
    code = totp_code("JBSWY3DPEHPK3PXP", step=60, digits=8, at=1000000000.0)
    assert len(code) == 8
    assert code.isdigit()


def test_totp_code_invalid_base32_raises_authentication_error() -> None:
    with pytest.raises(AuthenticationError, match="invalid TOTP secret"):
        totp_code("!!!not-base32!!!")


def test_totp_code_handles_lowercase_and_spaces() -> None:
    # Should normalise to upper-case and strip spaces before decoding
    code = totp_code("jbswy3dp ehpk3pxp", at=1000000000.0)
    assert len(code) == 6


# ---------------------------------------------------------------------------
# jwt_expiry
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict[str, Any]) -> str:
    """Build a minimal unsigned JWT with the given payload."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = "fakesig"
    return f"{header}.{body}.{sig}"


def test_jwt_expiry_extracts_exp() -> None:
    token = _make_jwt({"exp": 1700000000, "sub": "user1"})
    assert jwt_expiry(token) == 1700000000.0


def test_jwt_expiry_returns_none_when_no_exp() -> None:
    token = _make_jwt({"sub": "user1"})
    assert jwt_expiry(token) is None


def test_jwt_expiry_returns_none_for_garbage() -> None:
    assert jwt_expiry("not.a.jwt") is None
    assert jwt_expiry("") is None
    assert jwt_expiry("single-segment") is None


# ---------------------------------------------------------------------------
# _is_rate_limit_message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Too many requests, please wait 2 minute",
        "TOTP cooldown active",
        "rate limit exceeded",
        "Request throttled",
        "too many attempts",
        "UDAPI100500: limit reached",
        "Please wait 10 min before retry",
        "Maximum number of TOTP attempts exceeded",
        "Please generate an OTP after some time",
    ],
)
def test_rate_limit_markers_detected(message: str) -> None:
    assert _is_rate_limit_message(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "Invalid PIN, please try again",
        "Session expired",
        "Network error",
        "",
    ],
)
def test_non_rate_limit_messages(message: str) -> None:
    assert _is_rate_limit_message(message) is False


# ---------------------------------------------------------------------------
# _extract_message
# ---------------------------------------------------------------------------


def test_extract_message_from_standard_keys() -> None:
    assert _extract_message({"message": "Invalid TOTP"}) == "Invalid TOTP"
    assert _extract_message({"error": "bad request"}) == "bad request"
    assert _extract_message({"error_message": "field missing"}) == "field missing"


def test_extract_message_from_camel_case_keys() -> None:
    assert _extract_message({"errorMessage": "Invalid TOTP"}) == "Invalid TOTP"


def test_extract_message_from_oauth_keys() -> None:
    assert (
        _extract_message({"error_description": "bad client", "errorCode": "AUTH001"})
        == "bad client AUTH001"
    )


def test_extract_message_joins_multiple_keys() -> None:
    result = _extract_message({"message": "hello", "error": "world"})
    assert "hello" in result
    assert "world" in result


def test_extract_message_empty_when_no_keys_present() -> None:
    assert _extract_message({"status": "error"}) == ""


# ---------------------------------------------------------------------------
# _require_mapping
# ---------------------------------------------------------------------------


def test_require_mapping_accepts_dict() -> None:
    body = {"access_token": "tok"}
    assert _require_mapping(body, "Test") is body


def test_require_mapping_rejects_non_dict() -> None:
    with pytest.raises(AuthenticationError, match="non-object response"):
        _require_mapping("not a dict", "Test")
    with pytest.raises(AuthenticationError, match="non-object response"):
        _require_mapping([1, 2, 3], "Test")
    with pytest.raises(AuthenticationError, match="non-object response"):
        _require_mapping(None, "Test")


# ---------------------------------------------------------------------------
# _form_post
# ---------------------------------------------------------------------------


def test_form_post_sends_urlencoded_data() -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        captured["method"] = method
        captured["url"] = url
        captured["data"] = kwargs.get("data")
        captured["headers"] = kwargs.get("headers")
        return 200, {"ok": True}

    status, body = _form_post(
        fake_fetch,
        "https://example.com/token",
        {"client_id": "abc", "pin": "1234"},
    )
    assert status == 200
    assert body == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://example.com/token"
    assert "client_id=abc" in captured["data"]
    assert "pin=1234" in captured["data"]
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
