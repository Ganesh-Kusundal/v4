"""Dhan authentication flow — TOTP-based token mint.

Extracted from ``common/auth.py`` so broker-specific login logic lives
in the adapter directory, not in shared infrastructure.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from tradex_domain import AuthenticationError, RateLimitError

from tradex_brokers.common.auth import (
    _extract_message,
    _form_post,
    _is_rate_limit_message,
    _require_mapping,
    jwt_expiry,
    totp_code,
    totp_window_wait,
)
from tradex_brokers.common.token_lifecycle import MintStrategy, TokenMintResult

_AuthFetch = Callable[[str, str, dict[str, str] | None, dict[str, str] | None], tuple[int, Any]]


def dhan_totp_mint(
    *,
    fetch: _AuthFetch,
    client_id: str,
    pin: str,
    totp_secret: str,
    token_url: str = "https://auth.dhan.co/app/generateAccessToken",
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    settle_seconds: float = 5.0,
) -> MintStrategy:
    """Build a Dhan token mint that POSTs client id + pin + TOTP (stdlib-only).

    Success body is FLAT: ``{"accessToken": ..., "expiryTime": ...}``;
    rejections arrive as HTTP 200 + ``{"status": "error", "message": ...}``.
    Waits for the TOTP window to settle before generating the code.
    """

    def mint() -> TokenMintResult:
        totp_window_wait(clock, sleeper, settle=settle_seconds)
        code = totp_code(totp_secret, at=clock())
        status, body = _form_post(
            fetch,
            token_url,
            {"dhanClientId": client_id, "pin": pin, "totp": code},
        )
        data = _require_mapping(body, "Dhan")
        if status not in {200, 201}:
            message = _extract_message(data)
            if _is_rate_limit_message(message):
                raise RateLimitError(f"Dhan token rate limited (HTTP {status}): {message}")
            raise AuthenticationError(f"Dhan token mint failed (HTTP {status}): {message}")
        if data.get("status") == "error":
            message = _extract_message(data)
            if _is_rate_limit_message(message):
                raise RateLimitError(f"Dhan token rate limited: {message}")
            raise AuthenticationError(f"Dhan token mint rejected: {message}")
        raw_inner = data.get("data")
        inner = raw_inner if isinstance(raw_inner, dict) else {}
        token = (
            data.get("accessToken")
            or data.get("access_token")
            or inner.get("accessToken")
            or inner.get("access_token")
        )
        if not token:
            message = _extract_message(data)
            if _is_rate_limit_message(message):
                raise RateLimitError(f"Dhan token rate limited: {message}")
            if message:
                raise AuthenticationError(f"Dhan token mint rejected: {message}")
            raise AuthenticationError("Dhan token mint response missing accessToken")
        token = str(token)
        return TokenMintResult(token=token, expires_at=jwt_expiry(token))

    return mint
