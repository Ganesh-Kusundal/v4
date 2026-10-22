"""HTTP route modules for the TradeX API.

Each module owns one slice of routes. The contract is uniform:

- The module exposes a module-level ``router`` (a FastAPI ``APIRouter``).
- Route functions read the active session via :func:`get_session`, a
  FastAPI dependency that returns ``request.app.state.session``.

``create_app`` (in :mod:`tradex_interfaces.fastapi_app`) imports each router
module directly.
"""

from tradex_interfaces.routes import (
    account,
    extensions,
    health,
    market_data,
    orders,
    portfolio,
)

__all__ = ["account", "extensions", "health", "market_data", "orders", "portfolio"]
