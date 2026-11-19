"""``require_session(code)``: the one home of the 'no session bound' HTTP policy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from tradex_domain.constants import NO_SESSION_BOUND
from tradex_interfaces.routes.deps import require_session


def _req(session):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session=session)))


@pytest.mark.parametrize("code", [400, 404])
def test_missing_session_raises_configured_status(code: int) -> None:
    with pytest.raises(HTTPException) as ei:
        require_session(code)(_req(None))
    assert ei.value.status_code == code and ei.value.detail == NO_SESSION_BOUND


def test_bound_session_is_returned() -> None:
    s = object()
    assert require_session(404)(_req(s)) is s
