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
# Broker-specific mint factories (re-exported from adapter modules)
# ---------------------------------------------------------------------------


def __getattr__(name: str) -> object:
    """Backward-compat: re-export broker mint factories from their new homes."""
    if name == "dhan_totp_mint":
        from tradex_brokers.dhan.auth_flow import dhan_totp_mint
        return dhan_totp_mint
    if name == "upstox_refresh_mint":
        from tradex_brokers.upstox.auth_flow import upstox_refresh_mint
        return upstox_refresh_mint
    if name == "upstox_totp_mint":
        from tradex_brokers.upstox.auth_flow import upstox_totp_mint
        return upstox_totp_mint
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "dhan_totp_mint",
    "jwt_expiry",
    "totp_code",
    "totp_window_wait",
    "upstox_refresh_mint",
    "upstox_totp_mint",
]
