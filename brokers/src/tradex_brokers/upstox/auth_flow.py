"""Upstox authentication flows — OAuth refresh grant and TOTP self-mint.

Extracted from ``common/auth.py`` so broker-specific login logic lives
in the adapter directory, not in shared infrastructure.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from tradex_domain import AuthenticationError, RateLimitError

from tradex_brokers.common.auth import (
    _form_post,
    _is_rate_limit_message,
    _require_mapping,
    jwt_expiry,
)
from tradex_brokers.common.token_lifecycle import MintStrategy, TokenMintResult

_AuthFetch = Callable[[str, str, dict[str, str] | None, dict[str, str] | None], tuple[int, Any]]


# ---------------------------------------------------------------------------
# Upstox — OAuth refresh-token grant
# ---------------------------------------------------------------------------
# The initial authorization-code flow requires a browser/user consent, which a
# headless SDK cannot automate. We support the server-side refresh grant only;
# the initial refresh_token must be obtained once via the standard Upstox OAuth
# redirect (or TOTP flow) and stored in config/state.


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


# ---------------------------------------------------------------------------
# Upstox — TOTP self-mint (optional: requires ``upstox-totp`` package)
# ---------------------------------------------------------------------------
# Upstox does not expose a native TOTP API endpoint (unlike Dhan). The
# ``upstox-totp`` third-party library automates the full browser-like OAuth
# flow: login → OTP generation → TOTP verification → authorization code →
# token exchange. This is an optional dependency; when absent, the refresh-
# token grant (``upstox_refresh_mint``) remains the only mint strategy.


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
            expires_at=clock() + 86400.0,  # Upstox tokens expire in 24h
        )

    return mint
