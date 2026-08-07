"""Authentication helpers for broker-specific login flows.

Includes pure-Python utility functions (TOTP code generation, TOTP window
alignment, JWT expiry extraction, form-POST helpers, rate-limit message
detection) shared across broker adapters, plus stdlib-only mint factories for
Dhan (TOTP) and Upstox (OAuth refresh grant; optional ``upstox-totp``
self-mint).  All factories are fetch-injected and network-free at construction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from tradex_domain import AuthenticationError, RateLimitError

from tradex_brokers.common.token_lifecycle import MintStrategy, TokenMintResult

# ---------------------------------------------------------------------------
# Rate-limit message detection
# ---------------------------------------------------------------------------

# Phrasings providers use when a token mint is cooldown/rate-limit blocked.
# Matched case-insensitively so a differently-worded rejection still surfaces
# as the typed RateLimitError instead of a generic AuthenticationError.  Generic
# "try again" is deliberately excluded — a credential rejection ("Invalid PIN,
# please try again") must stay an AuthenticationError, never a rate limit.
_RATE_LIMIT_MARKERS = (
    "2 minute",
    "cooldown",
    "rate limit",
    "throttl",
    "too many",
    "udapi100500",
    "10 min",
    "maximum number",
    "generate an otp",
)


def _is_rate_limit_message(message: str) -> bool:
    """Return ``True`` if *message* matches a known rate-limit phrasing."""
    lowered = message.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}

# Local type alias for an HTTP fetch callable (method, url, ...) → (status, body).
_AuthFetch = Callable[..., tuple[int, Any]]


def _form_post(fetch: _AuthFetch, url: str, fields: dict[str, str]) -> tuple[int, Any]:
    """POST form-encoded fields (OAuth/token endpoints reject JSON bodies)."""
    return fetch("POST", url, data=urlencode(fields), headers=_FORM_HEADERS)


def _require_mapping(body: object, provider: str) -> dict[str, Any]:
    """Validate that a provider response body is a JSON object."""
    if not isinstance(body, dict):
        raise AuthenticationError(f"{provider} token mint returned a non-object response")
    return body


def _extract_message(data: dict[str, Any]) -> str:
    """Join the provider's human-readable error text across every key spelling
    Dhan/Upstox use (``message``, ``error``, ``error_message``, camelCase
    ``errorMessage``, OAuth ``error_description``/``errorCode``).  Without the
    camelCase variants a rejection like ``{"status": "error",
    "errorMessage": "Invalid TOTP"}`` degrades to the misleading generic
    "missing accessToken" — masking the real reason and the rate-limit type.
    """
    return " ".join(
        str(data.get(key) or "")
        for key in (
            "message",
            "error",
            "error_message",
            "errorMessage",
            "error_description",
            "errorCode",
        )
        if data.get(key)
    ).strip()


# ---------------------------------------------------------------------------
# RFC 6238 TOTP (stdlib-only; pyotp-compatible for standard 30s/6-digit SHA1)
# ---------------------------------------------------------------------------


def totp_code(secret: str, *, step: int = 30, digits: int = 6, at: float | None = None) -> str:
    """Compute an RFC 6238 TOTP code from a base32 secret."""
    raw = secret.upper().replace(" ", "")
    padding = "=" * ((8 - len(raw) % 8) % 8)
    try:
        key = base64.b32decode(raw + padding)
    except Exception as exc:  # noqa: BLE001 — bad base32 is a credential problem
        raise AuthenticationError("invalid TOTP secret (expected base32)") from exc
    counter = int((at if at is not None else time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return f"{code:0{digits}d}"


def totp_window_wait(
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
    *,
    settle: float = 5.0,
    min_remaining: float = 4.0,
    step: float = 30.0,
) -> None:
    """Align with the TOTP window before generating a code.

    Evidence from Dhan: codes sent early in a fresh 30s window were rejected
    ("Invalid TOTP") while one sent ~28s in succeeded — and codes are one-time
    use, so a rejected early code burns the mint slot.  Sleep until the current
    window has settled (>= *settle* seconds in); if too little remains before
    the window rolls over (< *min_remaining*), skip into the next window so the
    code can't expire in flight.  Injectable clock/sleeper keep tests instant.
    """
    phase = clock() % step
    if phase < settle:
        sleeper(settle - phase)
    elif phase > step - min_remaining:
        sleeper(step - phase + settle)


def jwt_expiry(token: str) -> float | None:
    """Best-effort JWT ``exp`` extraction; returns None when not decodable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — expiry is best-effort, not a contract
        return None


# ---------------------------------------------------------------------------
# Broker-specific mint factories
# ---------------------------------------------------------------------------


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
            # Only known message fields — never dump the raw body (it could echo
            # submitted PIN/TOTP fields back into an exception string).
            message = _extract_message(data)
            if _is_rate_limit_message(message):
                raise RateLimitError(f"Dhan token rate limited (HTTP {status}): {message}")
            raise AuthenticationError(f"Dhan token mint failed (HTTP {status}): {message}")
        # Dhan wraps rejections (rate limit, invalid TOTP) in HTTP 200 +
        # {"status": "error", "message": ...} — classify before token lookup.
        if data.get("status") == "error":
            message = _extract_message(data)
            if _is_rate_limit_message(message):
                raise RateLimitError(f"Dhan token rate limited: {message}")
            raise AuthenticationError(f"Dhan token mint rejected: {message}")
        raw_inner = data.get("data")
        inner = raw_inner if isinstance(raw_inner, dict) else {}
        # Dhan's success body is FLAT ({"accessToken": ..., "expiryTime": ...});
        # accept a nested "data" wrapper too, defensively.
        token = (
            data.get("accessToken")
            or data.get("access_token")
            or inner.get("accessToken")
            or inner.get("access_token")
        )
        if not token:
            # Dhan returns HTTP 200 + status:error for TOTP rate limits — never
            # mask that as a misleading "missing accessToken".
            message = _extract_message(data)
            if _is_rate_limit_message(message):
                raise RateLimitError(f"Dhan token rate limited: {message}")
            if message:
                raise AuthenticationError(f"Dhan token mint rejected: {message}")
            raise AuthenticationError("Dhan token mint response missing accessToken")
        token = str(token)
        return TokenMintResult(token=token, expires_at=jwt_expiry(token))

    return mint


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


__all__ = [
    "dhan_totp_mint",
    "jwt_expiry",
    "totp_code",
    "totp_window_wait",
    "upstox_refresh_mint",
    "upstox_totp_mint",
]
