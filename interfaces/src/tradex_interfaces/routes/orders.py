"""``/orders`` — submit, list, modify, cancel orders against the bound session.

Route layer responsibility: parse HTTP input → call application handler → map to HTTP.
Business rules (broker capability, engine dispatch) live in tradex_trading.application.
Auth dependencies are preserved unchanged.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from tradex_domain.enums import ProductType
from tradex_domain.value_objects import CorrelationId, OrderId

from tradex_application.orders import (
    BrokerCapabilityError,
    cancel_order as app_cancel_order,
    modify_order as app_modify_order,
    submit_bracket_order as app_submit_bracket_order,
    submit_order as app_submit_order,
)
from tradex_execution.engine import (
    IdempotencyInflight,
    IdempotencyKeyReuseMismatch,
)
from tradex_interfaces.auth import (
    audit_identity,
    require_auth,
    require_csrf_if_session,
)
from tradex_interfaces.models import OrderResponse
from tradex_interfaces.replay_guard import ReplayGuard
from tradex_interfaces.routes.deps import get_session

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


def _parse_product(body: dict) -> ProductType:
    """Resolve ``body["product"]`` to a domain ``ProductType``.

    Uses the canonical ``ProductType.from_wire`` single source of truth.
    """
    raw = body.get("product")
    try:
        return ProductType.from_wire(raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"unknown product {raw!r}; expected one of MIS, CNC, NRML, MTF",
        ) from None


def _map_receipt(receipt: Any) -> OrderResponse:
    """Map an engine OrderReceipt (or idempotency-replay OrderId) → OrderResponse.

    The engine's submit() can return either:
    - ``OrderReceipt`` — first submission, or
    - ``OrderId``      — idempotency replay (duplicate key, same fingerprint).
    An in-flight receipt is surfaced as 409 before reaching this helper.
    """
    if isinstance(receipt, OrderId):
        return OrderResponse(
            order_id=receipt.value,
            status="SUBMITTED",
            message="idempotency_replay",
        )
    if getattr(receipt, "message", None) == "idempotency_in_flight":
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
            detail="Idempotency-Key header is required for order mutations",
        )
    if len(value.strip()) > 255:
        raise HTTPException(status_code=422, detail="Idempotency-Key is too long")
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
            detail="orders are disabled during replay",
        )
    if replay_guard is not None and replay_guard.active:
        raise HTTPException(
            status_code=422,
            detail="orders are disabled during replay",
        )


def _request_replay_guard(request: Request | None) -> ReplayGuard | None:
    if request is None:
        return None
    return getattr(request.state, "replay_guard", None)


def _price(value: Any, name: str) -> Any:
    """Parse a decimal ``Price`` from an HTTP body value."""
    from tradex_domain.value_objects import Price

    try:
        return Price(Decimal(str(value)))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def _build_order_request(
    body: dict, session: Any, correlation_id: CorrelationId | None = None,
) -> Any:
    """Parse an HTTP order body into a domain OrderRequest.

    Accepts either ``"instrument_id": "EXCHANGE:SYMBOL"`` or the pair
    ``"exchange"`` + ``"symbol"``. Raises ``HTTPException(422)`` on
    missing or malformed fields.
    """
    from tradex_domain.enums import OrderSide, OrderType, TimeInForce
    from tradex_domain.execution import OrderRequest
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import Price, Quantity

    instrument_id = body.get("instrument_id")
    if instrument_id:
        parts = instrument_id.split(":", 1)
        if len(parts) != 2:
            raise HTTPException(
                status_code=422,
                detail="instrument_id must be in 'EXCHANGE:SYMBOL' format",
            )
        instrument = Equity.of(parts[0], parts[1])
    else:
        exchange = body.get("exchange")
        symbol = body.get("symbol")
        if not exchange or not symbol:
            raise HTTPException(
                status_code=422,
                detail="provide either 'instrument_id' or both 'exchange' and 'symbol'",
            )
        instrument = Equity.of(exchange, symbol)

    side = OrderSide(body["side"])
    order_type = OrderType(body.get("order_type", "MARKET"))
    quantity = Quantity(Decimal(str(body["quantity"])))
    price = Price(Decimal(str(body["price"]))) if body.get("price") is not None else None
    trigger_price = (
        Price(Decimal(str(body["trigger_price"])))
        if body.get("trigger_price") is not None
        else None
    )
    time_in_force = (
        TimeInForce(body["time_in_force"])
        if body.get("time_in_force")
        else TimeInForce.DAY
    )
    return OrderRequest(
        instrument=instrument,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        trigger_price=trigger_price,
        time_in_force=time_in_force,
        product_type=_parse_product(body),
        correlation_id=correlation_id,
    )


def _build_bracket_request(
    body: dict, correlation_id: CorrelationId | None = None,
) -> Any:
    """Parse an HTTP body into a bracket (super) OrderRequest.

    Mirrors ``_build_order_request`` for instrument/entry fields, plus the
    protective legs ``stop_loss_price``/``target_price`` (required) and
    ``trailing_jump`` (optional, defaults 0). Raises ``KeyError`` on missing
    required fields and ``ValueError`` on malformed ones; the domain
    ``BracketOrderRequest`` validates protective-price ordering.
    """
    from tradex_domain.enums import OrderSide, OrderType, TimeInForce
    from tradex_domain.execution import BracketOrderRequest
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import Quantity

    instrument_id = body.get("instrument_id")
    if instrument_id:
        parts = instrument_id.split(":", 1)
        if len(parts) != 2:
            raise HTTPException(
                status_code=422,
                detail="instrument_id must be in 'EXCHANGE:SYMBOL' format",
            )
        instrument = Equity.of(parts[0], parts[1])
    else:
        exchange = body.get("exchange")
        symbol = body.get("symbol")
        if not exchange or not symbol:
            raise HTTPException(
                status_code=422,
                detail="provide either 'instrument_id' or both 'exchange' and 'symbol'",
            )
        instrument = Equity.of(exchange, symbol)

    side = OrderSide(body["side"])
    order_type = OrderType(body.get("order_type", "MARKET"))
    quantity = Quantity(Decimal(str(body["quantity"])))
    price = _price(body["price"], "price")
    time_in_force = (
        TimeInForce(body["time_in_force"])
        if body.get("time_in_force")
        else TimeInForce.DAY
    )
    trailing_jump = body.get("trailing_jump")
    return BracketOrderRequest(
        instrument=instrument,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        time_in_force=time_in_force,
        stop_loss_price=_price(body["stop_loss_price"], "stop_loss_price"),
        target_price=_price(body["target_price"], "target_price"),
        trailing_jump=(
            _price(trailing_jump, "trailing_jump")
            if trailing_jump is not None
            else _price("0", "trailing_jump")
        ),
        product_type=_parse_product(body),
        correlation_id=correlation_id,
    )


def _build_bracket_modify_request(
    existing: Any,
    body: dict,
    instrument: Any,
    correlation_id: CorrelationId,
) -> Any:
    """Build a ``BracketOrderRequest`` for PUT on an existing bracket order.

    Every mutable field defaults to the current order value so a partial
    update (e.g. only ``target_price``) is valid; protective legs stay
    mandatory — a bracket cannot be modified into a bare entry. The domain
    request validates side-aware protective ordering (422 on violation).
    """
    from tradex_domain.enums import OrderSide, OrderType, TimeInForce
    from tradex_domain.execution import BracketOrderRequest
    from tradex_domain.value_objects import Quantity

    entry = body.get("price")
    stop = body.get("stop_loss_price")
    target = body.get("target_price")
    trailing = body.get("trailing_jump")
    quantity_raw = body.get("quantity")
    side_raw = body.get("side")
    order_type_raw = body.get("order_type")
    tif_raw = body.get("time_in_force")

    return BracketOrderRequest(
        instrument=instrument,
        side=OrderSide(side_raw) if side_raw is not None else existing.side,
        order_type=(
            OrderType(order_type_raw)
            if order_type_raw is not None
            else existing.order_type
        ),
        quantity=(
            Quantity(Decimal(str(quantity_raw)))
            if quantity_raw is not None
            else existing.quantity
        ),
        price=(_price(entry, "price") if entry is not None else existing.price),
        stop_loss_price=(
            _price(stop, "stop_loss_price")
            if stop is not None
            else existing.stop_loss_price
        ),
        target_price=(
            _price(target, "target_price")
            if target is not None
            else existing.target_price
        ),
        trailing_jump=(
            _price(trailing, "trailing_jump")
            if trailing is not None
            else existing.trailing_jump
        ),
        time_in_force=(
            TimeInForce(tif_raw) if tif_raw else existing.time_in_force
        ),
        correlation_id=correlation_id,
    )


@_base_router.get("/orders", response_model=list[OrderResponse])
async def list_orders(
    status: str | None = None,
    limit: int = 50,
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


@_base_router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    session: Any | None = Depends(get_session),
) -> OrderResponse:
    if session is None:
        raise HTTPException(status_code=404, detail="No session bound")
    try:
        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        order = session.engine.get_order(oid)
        if order is None:
            # Engine has no record — fall back to the broker (orders placed
            # outside the session may live there).
            order = session.broker.get_order(oid)
        return OrderResponse(order_id=str(order.order_id), status=str(order.status))
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


async def place_order(
    body: dict,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request: Request = cast(Request, None),
) -> OrderResponse:
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session, _request_replay_guard(request))
    if session is None:
        raise HTTPException(status_code=503, detail="no session bound")
    try:
        order_request = _build_order_request(body, session, correlation_id)
        receipt = app_submit_order(session.engine, order_request)
        return _map_receipt(receipt)
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing required field: {exc}") from exc
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
    body: dict,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request: Request = cast(Request, None),
) -> OrderResponse:
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
        raise HTTPException(status_code=503, detail="no session bound")
    broker = getattr(session, "_broker", None) or getattr(session, "broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail="broker unavailable")
    try:
        order_request = _build_bracket_request(body, correlation_id)
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
    body: dict,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request: Request = cast(Request, None),
) -> OrderResponse:
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session, _request_replay_guard(request))
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        from tradex_domain.enums import OrderSide, OrderType, TimeInForce
        from tradex_domain.execution import OrderRequest
        from tradex_domain.value_objects import Price, Quantity

        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        correlation_id = _require_idempotency_key(idempotency_key)
        existing = session.engine.get_order(oid)
        if existing is None:
            raise HTTPException(status_code=422, detail="order not found")

        symbol = body.get("symbol")
        if symbol is not None:
            instrument = session.broker.search(symbol)[0]
        else:
            instrument = existing.instrument

        if existing.stop_loss_price is not None or existing.target_price is not None:
            # Modifying a bracket: the request must stay a full composite so
            # the engine dispatches to the venue's modify_super_order and the
            # protective legs project onto the OMS record. Domain-side
            # validation errors (malformed prices, broken protective
            # ordering) are client errors -> 422, matching POST /orders/bracket.
            try:
                order_request = _build_bracket_modify_request(
                    existing, body, instrument, correlation_id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        else:
            side = OrderSide(body["side"]) if "side" in body else existing.side
            order_type = OrderType(body.get("order_type", "LIMIT"))
            quantity = Quantity(Decimal(str(body["quantity"])))
            price = Price(Decimal(str(body["price"]))) if body.get("price") is not None else None
            time_in_force = (
                TimeInForce(body["time_in_force"])
                if body.get("time_in_force")
                else TimeInForce.DAY
            )
            order_request = OrderRequest(
                instrument=instrument,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                time_in_force=time_in_force,
                correlation_id=correlation_id,
            )
        order = app_modify_order(session.engine, oid, order_request)
        return OrderResponse(order_id=str(order.order_id), status="modified")
    except HTTPException:
        raise
    except IdempotencyKeyReuseMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInflight as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing required field: {exc}") from exc
    except IndexError as exc:
        raise HTTPException(status_code=422, detail="unknown symbol") from exc
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


async def cancel_order(
    order_id: str,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request: Request = cast(Request, None),
) -> OrderResponse:

    # Idempotency-Key is mandatory for mutations; the cancel's own key is
    # reserved/replayed by the engine (N2), not just validated here.
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session, _request_replay_guard(request))
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        order = app_cancel_order(session.engine, oid, correlation_id)
        return OrderResponse(order_id=str(order.order_id), status="cancelled")
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
    #   1. require_auth        — session cookie (new) or X-API-Key (legacy), fail-closed
    #   2. require_csrf_if_session — CSRF enforcement when authenticated via session
    #   3. _bind_replay_guard  — inject guard into request.state for mode checks
    #   4. _make_audit_dep     — structured audit log with caller identity
    def _mut_deps(action: str) -> list:
        return [
            Depends(require_auth),
            Depends(require_csrf_if_session),
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
