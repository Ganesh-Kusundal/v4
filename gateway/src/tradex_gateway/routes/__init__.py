"""REST and WebSocket route modules for the TradeX gateway.

Each module owns one slice of the gateway surface. The contract is uniform:

- The module exposes a module-level ``router`` (a FastAPI ``APIRouter``).
- Route functions read services from ``request.app.state``.

``create_app`` (in :mod:`tradex_gateway.app`) mounts each router.
"""

from tradex_gateway.routes import (
    chains,
    depth,
    health,
    history,
    instruments,
    metrics,
    quotes,
    stream,
)

__all__ = [
    "chains",
    "depth",
    "health",
    "history",
    "instruments",
    "metrics",
    "quotes",
    "stream",
]
