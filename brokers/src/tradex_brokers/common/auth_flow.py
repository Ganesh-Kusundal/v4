"""Unified authentication flow — broker-specific token mint factories and a
single ``authenticate()`` entry point that dispatches to the right mint.

All factories are fetch-injected and network-free at construction.
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

_DEFAULT_REDIRECT_URI = "http://127.0.0.1:18080/callback"


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


def upstox_refresh_mint(
    *,
    fetch: _AuthFetch,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    refresh_token: str,
    token_url: str = "https://api.upstox.com/v2/login/authorization/token",
    clock: Callable[[], float] = time.time,
) -> MintStrategy:
    """Build an Upstox token mint that exchanges the refresh token for a fresh
    access token via ``grant_type=refresh_token`` (form-encoded POST)."""

    def mint() -> TokenMintResult:
        status, body = _form_post(
            fetch,
            token_url,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        data = _require_mapping(body, "Upstox")
        if status not in {200, 201}:
            message = str(data.get("error_description") or data.get("error") or data)
            if _is_rate_limit_message(message):
                raise RateLimitError(
                    f"Upstox token refresh rate limited (HTTP {status}): {message}"
                )
            raise AuthenticationError(f"Upstox token refresh failed (HTTP {status}): {message}")
        token = data.get("access_token")
        if not token:
            raise AuthenticationError("Upstox token refresh response missing access_token")
        expires_in = data.get("expires_in")
        expires_at = (
            clock() + float(expires_in)
            if isinstance(expires_in, (int, float))
            else jwt_expiry(str(token))
        )
        return TokenMintResult(
            token=str(token),
            expires_at=expires_at,
            refresh_token=str(data.get("refresh_token") or refresh_token),
        )

    return mint


def upstox_totp_mint(
    *,
    mobile: str,
    pin: str,
    totp_secret: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    clock: Callable[[], float] = time.time,
) -> MintStrategy:
    """Build an Upstox token mint that automates the full TOTP OAuth flow.

    Requires the ``upstox-totp`` package (``pip install upstox-totp``).
    Response shape: ``AccessTokenResponse.data.access_token``.
    """

    def mint() -> TokenMintResult:
        try:
            from upstox_totp import UpstoxTOTP
        except ImportError as exc:
            raise AuthenticationError(
                "Upstox TOTP self-mint requires the 'upstox-totp' package; "
                "install it with: pip install upstox-totp, or use the OAuth "
                "refresh-token flow instead (set UPSTOX_REFRESH_TOKEN)."
            ) from exc
        try:
            client = UpstoxTOTP(
                username=mobile,
                password=pin,
                pin_code=pin,
                totp_secret=totp_secret,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                debug=False,
            )
            response = client.app_token.get_access_token()
        except AuthenticationError:
            raise
        except Exception as exc:
            message = str(exc)
            if _is_rate_limit_message(message):
                raise RateLimitError(f"Upstox TOTP rate limited: {message}") from exc
            raise AuthenticationError(f"Upstox TOTP self-mint failed: {message}") from exc
        data = getattr(response, "data", None)
        if data is None or not getattr(data, "success", True):
            error = getattr(response, "error", None) or "no data in response"
            message = str(error)
            if _is_rate_limit_message(message):
                raise RateLimitError(f"Upstox TOTP rate limited: {message}")
            raise AuthenticationError(f"Upstox TOTP self-mint rejected: {message}")
        token = getattr(data, "access_token", None)
        if not token:
            raise AuthenticationError("Upstox TOTP response missing access_token")
        return TokenMintResult(
            token=str(token),
            expires_at=clock() + 86400.0,
        )

    return mint


def authenticate(
    broker: str,
    *,
    fetch: _AuthFetch,
    credentials: dict[str, str],
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> TokenMintResult:
    """Unified auth entry point — dispatch to the broker-specific mint.

    For ``"dhan"``: requires ``client_id``, ``pin``, ``totp_secret``.
    For ``"upstox"``: uses ``refresh_token`` when present (OAuth refresh
    grant), otherwise falls back to TOTP self-mint (``mobile``, ``pin``,
    ``totp_secret``). Both Upstox paths require ``client_id`` and
    ``client_secret``.
    """
    if broker == "dhan":
        mint = dhan_totp_mint(
            fetch=fetch,
            client_id=credentials["client_id"],
            pin=credentials["pin"],
            totp_secret=credentials["totp_secret"],
            clock=clock,
            sleeper=sleeper,
        )
    elif broker == "upstox":
        redirect_uri = credentials.get("redirect_uri", _DEFAULT_REDIRECT_URI)
        if credentials.get("refresh_token"):
            mint = upstox_refresh_mint(
                fetch=fetch,
                client_id=credentials["client_id"],
                client_secret=credentials["client_secret"],
                redirect_uri=redirect_uri,
                refresh_token=credentials["refresh_token"],
                clock=clock,
            )
        else:
            mint = upstox_totp_mint(
                mobile=credentials["mobile"],
                pin=credentials["pin"],
                totp_secret=credentials["totp_secret"],
                client_id=credentials["client_id"],
                client_secret=credentials["client_secret"],
                redirect_uri=redirect_uri,
                clock=clock,
            )
    else:
        raise AuthenticationError(f"unknown broker: {broker!r}")
    return mint()


__all__ = [
    "authenticate",
    "dhan_totp_mint",
    "upstox_refresh_mint",
    "upstox_totp_mint",
]
