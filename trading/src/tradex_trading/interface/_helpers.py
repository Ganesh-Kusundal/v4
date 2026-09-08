"""Helpers shared between ``fastapi_app`` and the route modules.

Lives here (not in ``fastapi_app``) so route modules can import these
without triggering a circular import. ``start_fastapi_server`` still
imports them from ``fastapi_app`` (re-export) for back-compat.
"""

from __future__ import annotations

from typing import Any

from tradex_trading.interface.models import HealthResponse, PositionResponse


def serialize_position(pos: Any) -> PositionResponse:
    """Serialize a Position domain object to a PositionResponse."""
    return PositionResponse(
        instrument=str(pos.instrument.instrument_id),
        quantity=str(pos.quantity.value),
        avg_price=str(pos.avg_price.value),
        realized_pnl=str(pos.realized_pnl.amount),
        unrealized_pnl=str(pos.unrealized_pnl.amount),
        total_pnl=str(pos.total_pnl.amount),
        is_long=pos.is_long,
        is_short=pos.is_short,
        mark_price=(str(pos.mark_price.value) if pos.mark_price is not None else None),
        marked_at=(pos.marked_at.isoformat() if pos.marked_at is not None else None),
        mark_source=pos.mark_source,
        mark_stale=(pos.mark_price is None or pos.marked_at is None),
    )


def readiness(session: Any | None) -> HealthResponse:
    """Readiness facts — single source of truth for /health/ready and the
    pre-bind probe in :func:`start_fastapi_server`.

    ``None`` session (no session bound) counts as ready, mirroring the
    pre-existing no-session behaviour of the route.
    """
    if session is None:
        return HealthResponse(status="ok", check="ready")
    return HealthResponse(status="ok", check="ready", session_state=str(session.state))


def is_ready(session: Any | None) -> bool:
    """True when the bound session (if any) is READY — the /health/ready gate."""
    if session is None:
        return True
    return str(getattr(session, "state", None)) == "READY"


__all__ = ["is_ready", "readiness", "serialize_position"]
