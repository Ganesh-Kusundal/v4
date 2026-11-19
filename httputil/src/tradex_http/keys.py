"""Constant-time API-key comparison shared by the gateway and interfaces."""

from __future__ import annotations

import hmac


def keys_match(provided: str | None, expected: str | None) -> bool:
    """True only when both keys are non-empty and equal.

    Compares UTF-8 bytes: ``hmac.compare_digest`` raises ``TypeError`` on a
    non-ASCII ``str``, and a header can carry one.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())
