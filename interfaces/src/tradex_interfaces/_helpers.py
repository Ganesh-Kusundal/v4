"""Helpers shared between ``fastapi_app`` and the route modules.

Lives here (not in ``fastapi_app``) so route modules can import these
without triggering a circular import. ``start_fastapi_server`` still
imports them from ``fastapi_app`` (re-export) for back-compat.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tradex_interfaces.models import HealthResponse, PositionResponse


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


def _feed_supervisor(session: Any) -> Any | None:
    values = getattr(session, "__dict__", {})
    if "_feed_supervisor" in values:
        return values["_feed_supervisor"]
    return values.get("feed_supervisor")


def readiness(session: Any | None) -> HealthResponse:
    """Readiness facts — single source of truth for /health/ready and the
    pre-bind probe in :func:`start_fastapi_server`.

    ``None`` session (no session bound) counts as ready, mirroring the
    pre-existing no-session behaviour of the route.
    """
    if session is None:
        return HealthResponse(status="ok", check="ready")
    supervisor = _feed_supervisor(session)
    feed_state = str(supervisor.state) if supervisor is not None else None
    feed_ready = bool(supervisor.ready) if supervisor is not None else None
    return HealthResponse(
        status="ok",
        check="ready",
        session_state=str(session.state),
        feed_state=feed_state,
        feed_ready=feed_ready,
    )


def is_ready(session: Any | None) -> bool:
    """True when the bound session (if any) is READY — the /health/ready gate."""
    if session is None:
        return True
    if str(getattr(session, "state", None)) != "READY":
        return False
    supervisor = _feed_supervisor(session)
    return supervisor is None or bool(supervisor.ready)


def feed_status_payload(session: Any | None) -> dict[str, Any]:
    """Build the ``feed_status`` WS message for a session.

    Single source of truth for the frame pushed by ``/ws/stream`` and the
    ``/health/ready`` gate: ``ready`` is what the UI must use to enable live
    order controls. A session with no feed supervisor (paper/backtest) reports
    ``ready=None`` and leaves live controls enabled, matching the pre-existing
    "no supervisor means no gate" behaviour.
    """
    supervisor = _feed_supervisor(session) if session is not None else None
    if supervisor is None:
        return {
            "type": "feed_status",
            "state": None,
            "ready": None,
            "generation": None,
            "last_event_at": None,
            "age_seconds": None,
            "live_orders_enabled": True,
        }
    last_event_at = getattr(supervisor, "last_event_at", None)
    age: float | None = None
    if isinstance(last_event_at, datetime):
        stamp = last_event_at if last_event_at.tzinfo else last_event_at.replace(tzinfo=UTC)
        age = max(0.0, (datetime.now(UTC) - stamp).total_seconds())
    ready = bool(getattr(supervisor, "ready", False))
    return {
        "type": "feed_status",
        "state": str(getattr(supervisor, "state", "")) or None,
        "ready": ready,
        "generation": int(getattr(supervisor, "generation", 0) or 0),
        "last_event_at": (
            last_event_at.isoformat()
            if isinstance(last_event_at, datetime)
            else None
        ),
        "age_seconds": age,
        "live_orders_enabled": ready,
    }


__all__ = [
    "feed_status_payload",
    "is_ready",
    "readiness",
    "serialize_position",
]
