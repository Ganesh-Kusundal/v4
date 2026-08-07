"""TradeX v4 TUI — simple text-based diagnostics display.

No external dependencies (no rich, no curses).
"""

from __future__ import annotations

from typing import Any

from tradex_trading.sdk.session import TradingSession


class TUI:
    """Simple text-based diagnostics display."""

    def __init__(self, session: TradingSession) -> None:
        self._session = session

    def render(self) -> str:
        """Render the TUI as a string."""
        lines = []
        lines.append("=" * 60)
        lines.append("TradeX v4 — Paper Trading")
        lines.append("=" * 60)
        lines.append(f"State: {self._session.state}")
        lines.append(f"Broker: {self._session.broker_id}")

        from tradex_trading.runtime.health import check_health

        health = check_health(self._session)
        lines.append(f"Health: {health.status}")

        positions = self._session.portfolio.positions()
        lines.append(f"Positions: {len(positions)}")

        lines.append("=" * 60)
        return "\n".join(lines)

    def display(self) -> None:
        """Print the TUI to stdout."""
        print(self.render())


def diagnose(session: TradingSession | Any) -> dict[str, object]:
    """Connectivity probe over a TradingSession or runtime context.

    Parameters
    ----------
    session : TradingSession | Any
        The session or runtime context to probe.

    Returns
    -------
    dict[str, object]
        Diagnostic information.
    """
    if isinstance(session, TradingSession):
        from tradex_trading.runtime.health import check_health

        health = check_health(session)
        positions = session.portfolio.positions()
        return {
            "broker_connected": health.status == "ok",
            "broker_reachable": health.status != "unhealthy",
            "session_state": session.state.value,
            "environment": session.broker_id.value,
            "orders": 0,  # Would need engine access
            "positions": len(positions),
        }

    # Fallback for runtime context (v3-style)
    broker = getattr(session, "broker", None)
    connected = bool(getattr(broker, "_connected", False))
    try:
        if broker and hasattr(broker, "get_orderbook"):
            broker.get_orderbook()
        reachable = True
    except Exception:  # noqa: BLE001 — probe must not raise
        reachable = False

    sess = getattr(session, "session", None)
    config = getattr(session, "config", None)
    engine = getattr(session, "engine", None)
    cache = getattr(session, "cache", None)

    return {
        "broker_connected": connected,
        "broker_reachable": reachable,
        "session_state": sess.state.value if sess else "UNKNOWN",
        "environment": config.environment if config else "UNKNOWN",
        "orders": len(engine.all_orders()) if engine else 0,
        "positions": len(cache.all_positions()) if cache else 0,
    }


def render_status(session: TradingSession | Any) -> str:
    """Render a one-line status string.

    Parameters
    ----------
    session : TradingSession | Any
        The session or runtime context.

    Returns
    -------
    str
        Formatted status string.
    """
    report = diagnose(session)
    return (
        f"tradex-v4 [{report['environment']}] "
        f"session={report['session_state']} "
        f"broker_connected={report['broker_connected']} "
        f"orders={report['orders']} positions={report['positions']}"
    )


__all__ = ["TUI", "diagnose", "render_status"]
