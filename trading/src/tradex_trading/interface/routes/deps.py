"""Shared FastAPI dependencies for the route modules."""

from __future__ import annotations

from typing import Any

from fastapi import Request


def get_session(request: Request) -> Any | None:
    """Return ``app.state.session`` for the current request.

    ``None`` when the app is bound without a TradingSession (e.g. test
    scaffolding). Route handlers that need a session should treat
    ``None`` as "no data" rather than raising.
    """
    return request.app.state.session
