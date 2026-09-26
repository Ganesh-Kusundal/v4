"""HTTP route modules for the TradeX API.

Each module owns one slice of routes. The contract is uniform:

- The module exposes a module-level ``router`` (a FastAPI ``APIRouter``).
- Route functions read the active session via :func:`get_session`, a
  FastAPI dependency that returns ``request.app.state.session``.

``create_app`` (in :mod:`tradex_interfaces.fastapi_app`) iterates
the :data:`ALL_ROUTES` list and ``include_router``s each one.
"""

from tradex_interfaces.routes import (
    account,
    extensions,
    health,
    market_data,
    orders,
    portfolio,
)

# ponytail: Order matters for OpenAPI doc grouping only; FastAPI is
# order-agnostic for routing itself.
ALL_ROUTES = [
    health.router,
    portfolio.router,
    orders.router,
    market_data.router,
    account.router,
    extensions.router,
]

__all__ = ["ALL_ROUTES"]
