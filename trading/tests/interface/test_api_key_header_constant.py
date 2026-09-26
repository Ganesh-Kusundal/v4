"""The legacy API-key header name is a wire contract.

``X-API-Key`` is spelled once as ``tradex_interfaces.auth.deps._API_KEY_HEADER``
and read by ``audit_identity``, ``require_auth`` and ``verify_api_key``.  A
typo'd or retyped header silently breaks every programmatic and loopback-dev
client, so the exact string is pinned here.

The frontend has two transports for the same credential (a header in
``apikey.ts`` and a ``?api_key=`` query param in ``feed.ts``).  Those are
TypeScript and cannot import this constant; the query-param one is a
deprecation candidate but removal could break external WS clients, so it is
tracked as a follow-up rather than changed here.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from tradex_interfaces.auth.deps import (  # noqa: E402
    _API_KEY_HEADER,
    audit_identity,
    require_auth,
    verify_api_key,
)

#: The literal that was hardcoded at all three read sites before the constant
#: existed.
LEGACY_LITERAL = "X-API-Key"


def test_constant_value_is_the_legacy_literal() -> None:
    assert _API_KEY_HEADER == LEGACY_LITERAL
    assert _API_KEY_HEADER == "X-API-Key"


def test_all_three_read_sites_use_the_constant() -> None:
    """No read site may retype the header — they must all reference the constant."""
    import inspect

    for fn in (audit_identity, require_auth, verify_api_key):
        source = inspect.getsource(fn)
        assert 'headers.get("X-API-Key")' not in source
        assert "headers.get(_API_KEY_HEADER)" in source


def test_header_is_read_case_insensitively_by_starlette() -> None:
    """The constant's canonical casing is what external clients send."""
    from starlette.datastructures import Headers

    assert Headers({_API_KEY_HEADER: "k"}).get("X-API-KEY") == "k"
    assert Headers({_API_KEY_HEADER: "k"}).get("x-api-key") == "k"
