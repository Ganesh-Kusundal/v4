"""``/orders`` — submit, list, modify, cancel orders against the bound session.

Route layer responsibility: parse HTTP input → call application handler → map to HTTP.
Business rules (broker capability, engine dispatch) live in tradex_application.orders.
Auth dependencies are preserved unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from tradex_application.orders import (
    BrokerCapabilityError,
    build_modify_request,
    build_order_request,
    lookup_order,
    parse_product,
)
from tradex_application.orders import (
    build_bracket_request as app_build_bracket_request,
)
from tradex_application.orders import (
    cancel_order as app_cancel_order,
)
from tradex_application.orders import (
    modify_order as app_modify_order,
)
from tradex_application.orders import (
    submit_bracket_order as app_submit_bracket_order,
)
from tradex_application.orders import (
    submit_order as app_submit_order,
)
from tradex_domain.constants import (
    BROKER_UNAVAILABLE_MESSAGE,
    CANCELLED_STATUS,
    DEFAULT_LIMIT,
    IDEMPOTENCY_IN_FLIGHT,
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_KEY_REQUIRED_MESSAGE,
    IDEMPOTENCY_KEY_TOO_LONG_MESSAGE,
    MISSING_REQUIRED_FIELD_MESSAGE,
    MODIFIED_STATUS,
    NO_SESSION_BOUND,
    ORDER_NOT_FOUND_MESSAGE,
    ORDERS_DISABLED_DURING_REPLAY_MESSAGE,
    UNKNOWN_SYMBOL_MESSAGE,
)
from tradex_domain.enums import ProductType
from tradex_domain.execution import OrderReceipt
from tradex_domain.value_objects import CorrelationId, OrderId
from tradex_execution.engine import (
    IdempotencyInflight,
    IdempotencyKeyReuseMismatch,
)

from tradex_interfaces.auth import (
    audit_identity,
)
from tradex_interfaces.contracts import (
    BracketOrderRequestDto,
    OrderUpdateRequest,
    PlaceOrderRequest,
)
from tradex_interfaces.models import OrderResponse
from tradex_interfaces.replay_guard import ReplayGuard
from tradex_interfaces.routes.deps import get_session, require_session

log = logging.getLogger(__name__)

_base_router = APIRouter()

#: Wire product strings (what the shell's MIS/CNC/NRML selector sends) ->
#: domain ``ProductType``. Explicit and closed on purpose: the product drives
#: margin treatment, so an unrecognised value must never fall back to the
#: ``INTRADAY`` default. ``COVER_ORDER`` has no wire form and is intentionally
#: absent — no client may request it through this endpoint.
_PRODUCT_BY_WIRE: dict[str, ProductType] = {
    "MIS": ProductType.INTRADAY,
    "CNC": ProductType.DELIVERY,
    "NRML": ProductType.MARGIN,
    "MTF": ProductType.MTF,
}


def _parse_product(body: Any) -> ProductType:
    """HTTP wrapper over the application parser: unknown product -> 422."""
    try:
        return parse_product(body.get("product"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _map_receipt(receipt: OrderReceipt) -> OrderResponse:
    """Map an engine ``OrderReceipt`` → ``OrderResponse``.

    ``submit()`` returns one type for a first submission and for an
    idempotency replay (the recorded receipt); an in-flight reservation is
    surfaced as 409 before reaching this helper.
    """
    if receipt.message == IDEMPOTENCY_IN_FLIGHT:
        raise HTTPException(status_code=409, detail="idempotency key is being processed")
    return OrderResponse(
        order_id=receipt.order_id.value,
        status=str(receipt.status),
        message=receipt.message,
    )



def _require_idempotency_key(value: str | None) -> CorrelationId:
    """Validate the server-owned mutation idempotency key."""
    if value is None or not value.strip():
        raise HTTPException(
            status_code=422,
            detail=IDEMPOTENCY_KEY_REQUIRED_MESSAGE,
        )
    if len(value.strip()) > 255:
        raise HTTPException(status_code=422, detail=IDEMPOTENCY_KEY_TOO_LONG_MESSAGE)
    return CorrelationId(value=value.strip())


def _require_trading_mode(
    session: Any,
    replay_guard: ReplayGuard | None = None,
) -> None:
    """Reject order mutations while the session or replay guard is active."""
    mode = str(getattr(session, "mode", "")).strip().lower()
    if mode == "replay":
        raise HTTPException(
            status_code=422,
            detail=ORDERS_DISABLED_DURING_REPLAY_MESSAGE,
        )
    if replay_guard is not None and replay_guard.active:
        raise HTTPException(
            status_code=422,
            detail=ORDERS_DISABLED_DURING_REPLAY_MESSAGE,
        )


def _request_replay_guard(request: Request | None) -> ReplayGuard | None:
    if request is None:
        return None
    return getattr(request.state, "replay_guard", None)


def _live_orders_enabled(request: Request | None, session: Any) -> bool:
    app = getattr(request, "app", None) if request is not None else None
    state = getattr(app, "state", None) if app is not None else None
    if state is not None:
        if hasattr(state, "live_orders_enabled"):
            return bool(state.live_orders_enabled)
        cfg = getattr(state, "config", None)
        if cfg is not None and hasattr(cfg, "live_orders_enabled"):
            return bool(cfg.live_orders_enabled)
    return bool(
        getattr(session, "_live_orders_enabled", getattr(session, "live_orders_enabled", True))
    )


@_base_router.get(
    "/orders", response_model=list[OrderResponse]
)
async def list_orders(
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    session: Any | None = Depends(get_session),
) -> list[OrderResponse]:
    if session is None:
        return []
    orders = session.engine.all_orders()
    result = [
        OrderResponse(order_id=str(o.order_id), status=str(o.status))
        for o in orders
    ]
    if status is not None:
        result = [o for o in result if o.status.lower() == status.lower()]
    return result[offset : offset + limit]


@_base_router.get(
    "/orders/{order_id}", response_model=OrderResponse
)
async def get_order(
    order_id: str,
    session: Any = Depends(require_session(404)),
) -> OrderResponse:
    try:
        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        order = lookup_order(session.engine, session.broker, oid)
        return OrderResponse(order_id=str(order.order_id), status=str(order.status))
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


async def place_order(
    body: PlaceOrderRequest,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
    request: Request = cast(Request, None),
) -> OrderResponse:
    body = body.model_dump()
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session, _request_replay_guard(request))
    if session is None:
        raise HTTPException(status_code=503, detail=NO_SESSION_BOUND)
    if str(getattr(session, "mode", "")).strip().lower() == "live" and not _live_orders_enabled(request, session):
        raise HTTPException(status_code=503, detail="live_orders_disabled")
    try:
        order_request = build_order_request(body, correlation_id)
        receipt = app_submit_order(session.engine, order_request)
        return _map_receipt(receipt)
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(
            status_code=422, detail=f"{MISSING_REQUIRED_FIELD_MESSAGE}: {exc}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyKeyReuseMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInflight as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("order submission failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def place_bracket_order(
    body: BracketOrderRequestDto,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
    request: Request = cast(Request, None),
) -> OrderResponse:
    body = body.model_dump()  # type: ignore[assignment]
    """Place a bracket (super) order — entry + protective stop/target legs.

    Capability-gated like ``/orders`` is session-bound; the submission itself
    goes through the ``ExecutionEngine`` pipeline (idempotency → risk → fill
    → OMS) as a composite ``BracketOrderRequest`` — brackets are NOT exempt
    from dedup and risk checks. The live fill source forwards the composite
    to the broker's super-order endpoint; an unsupported broker returns 422
    before the engine is reached.
    """
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session, _request_replay_guard(request))
    if session is None:
        raise HTTPException(status_code=503, detail=NO_SESSION_BOUND)
    if str(getattr(session, "mode", "")).strip().lower() == "live" and not _live_orders_enabled(request, session):
        raise HTTPException(status_code=503, detail="live_orders_disabled")
    broker = getattr(session, "_broker", None) or getattr(session, "broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail=BROKER_UNAVAILABLE_MESSAGE)
    try:
        order_request = app_build_bracket_request(body, correlation_id)
        receipt = app_submit_bracket_order(session.engine, broker, order_request)
        return _map_receipt(receipt)
    except BrokerCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyInflight as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("bracket order submission failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def modify_order(
    order_id: str,
    body: OrderUpdateRequest,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
    request: Request = cast(Request, None),
) -> OrderResponse:
    body = body.model_dump()  # type: ignore[assignment]
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session, _request_replay_guard(request))
    if session is None:
        raise HTTPException(status_code=400, detail=NO_SESSION_BOUND)
    try:
        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        correlation_id = _require_idempotency_key(idempotency_key)
        existing = session.engine.get_order(oid)
        if existing is None:
            raise HTTPException(status_code=422, detail=ORDER_NOT_FOUND_MESSAGE)

        symbol = body.get("symbol")
        if symbol is not None:
            instrument = session.broker.search(symbol)[0]
        else:
            instrument = existing.instrument

        try:
            order_request = build_modify_request(existing, body, instrument, correlation_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        order = app_modify_order(session.engine, oid, order_request)
        return OrderResponse(order_id=str(order.order_id), status=MODIFIED_STATUS)
    except HTTPException:
        raise
    except IdempotencyKeyReuseMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInflight as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=422, detail=f"{MISSING_REQUIRED_FIELD_MESSAGE}: {exc}"
        ) from exc
    except IndexError as exc:
        raise HTTPException(status_code=422, detail=UNKNOWN_SYMBOL_MESSAGE) from exc
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


async def cancel_order(
    order_id: str,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
    request: Request = cast(Request, None),
) -> OrderResponse:

    # Idempotency-Key is mandatory for mutations; the cancel's own key is
    # reserved/replayed by the engine (N2), not just validated here.
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session, _request_replay_guard(request))
    if session is None:
        raise HTTPException(status_code=400, detail=NO_SESSION_BOUND)
    try:
        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        order = app_cancel_order(session.engine, oid, correlation_id)
        return OrderResponse(order_id=str(order.order_id), status=CANCELLED_STATUS)
    except HTTPException:
        raise
    except IdempotencyKeyReuseMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInflight as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _bind_replay_guard(replay_guard: ReplayGuard):
    def bind(request: Request) -> None:
        request.state.replay_guard = replay_guard

    return bind


def _make_audit_dep(action: str):
    """Return a FastAPI dependency that emits a structured audit log line.

    Called once per mutation route; the action string is fixed at bind time
    so the log entry always identifies the exact money command.
    """

    def _audit(request: Request) -> None:
        identity = audit_identity(request)
        log.info(
            "audit.order action=%s identity=%s path=%s",
            action,
            identity,
            request.url.path,
        )

    return _audit


def create_orders_router(
    *,
    replay_guard: ReplayGuard | None = None,
) -> APIRouter:
    guard = replay_guard if replay_guard is not None else ReplayGuard()
    app_router = APIRouter()
    app_router.include_router(_base_router)

    # Mutation dependencies for every state-changing order route:
    #   1. require_csrf_if_session — CSRF enforcement when authenticated via session
    #   2. _bind_replay_guard  — inject guard into request.state for mode checks
    #   3. _make_audit_dep     — structured audit log with caller identity
    def _mut_deps(action: str) -> list:
        return [
            Depends(_bind_replay_guard(guard)),
            Depends(_make_audit_dep(action)),
        ]

    app_router.add_api_route(
        "/orders",
        place_order,
        methods=["POST"],
        response_model=OrderResponse,
        dependencies=_mut_deps("place_order"),
    )
    app_router.add_api_route(
        "/orders/bracket",
        place_bracket_order,
        methods=["POST"],
        response_model=OrderResponse,
        dependencies=_mut_deps("place_bracket_order"),
    )
    app_router.add_api_route(
        "/orders/{order_id}",
        modify_order,
        methods=["PUT"],
        response_model=OrderResponse,
        dependencies=_mut_deps("modify_order"),
    )
    app_router.add_api_route(
        "/orders/{order_id}",
        cancel_order,
        methods=["DELETE"],
        response_model=OrderResponse,
        dependencies=_mut_deps("cancel_order"),
    )
    return app_router


router = create_orders_router()


__all__ = ["ReplayGuard", "create_orders_router", "router"]
