"""``/orders`` — submit, list, modify, cancel orders against the bound session."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from tradex_domain.value_objects import CorrelationId, OrderId

from tradex_trading.execution.engine import (
    IdempotencyInflight,
    IdempotencyKeyReuseMismatch,
)
from tradex_trading.interface.auth import verify_api_key
from tradex_trading.interface.models import OrderResponse
from tradex_trading.interface.routes.deps import get_session

log = logging.getLogger(__name__)

router = APIRouter()



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


def _require_trading_mode(session: Any) -> None:
    """Reject order mutations while the session is in replay mode (N3).

    Tick-replay shows historical bars; orders must never execute against a
    live account while the chart is replaying. Interim server-side gate —
    the frontend mode pill is never trusted. The full replay-scoped
    session driver is a gated follow-on phase.
    """
    if getattr(session, "mode", None) == "replay":
        raise HTTPException(
            status_code=422,
            detail="orders are disabled during replay",
        )


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


@router.get("/orders", response_model=list[OrderResponse])
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


@router.get("/orders/{order_id}", response_model=OrderResponse)
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


@router.post("/orders", response_model=OrderResponse, dependencies=[Depends(verify_api_key)])
async def place_order(
    body: dict,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OrderResponse:
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session)
    if session is None:
        raise HTTPException(status_code=503, detail="no session bound")
    try:
        request = _build_order_request(body, session, correlation_id)
        receipt = session.engine.submit(request)
        # A completed idempotent replay returns the original OrderId from the
        # execution guard, while a first submission returns OrderReceipt.
        if isinstance(receipt, OrderId):
            return OrderResponse(
                order_id=receipt.value,
                status="SUBMITTED",
                message="idempotency_replay",
            )
        if getattr(receipt, "message", None) == "idempotency_in_flight":
            # The original request still owns the key (N5): 409, not 500.
            raise HTTPException(
                status_code=409,
                detail="idempotency key is being processed",
            )
        return OrderResponse(
            order_id=receipt.order_id.value,
            status=str(receipt.status),
            message=receipt.message,
        )
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


@router.post(
    "/orders/bracket",
    response_model=OrderResponse,
    dependencies=[Depends(verify_api_key)],
)
async def place_bracket_order(
    body: dict,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
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
    _require_trading_mode(session)
    if session is None:
        raise HTTPException(status_code=503, detail="no session bound")
    broker = getattr(session, "_broker", None) or getattr(session, "broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail="broker unavailable")
    caps = getattr(broker, "capabilities", None)
    if caps is None or not getattr(caps, "supports_super_order", False):
        raise HTTPException(status_code=422, detail="broker does not support super orders")
    try:
        request = _build_bracket_request(body, correlation_id)
        receipt = session.engine.submit(request)
    except HTTPException:
        raise
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyInflight as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("bracket order submission failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # A completed idempotent replay returns the original OrderId from the
    # execution guard, while a first submission returns OrderReceipt.
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


@router.put(
    "/orders/{order_id}",
    response_model=OrderResponse,
    dependencies=[Depends(verify_api_key)],
)
async def modify_order(
    order_id: str,
    body: dict,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OrderResponse:
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session)
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
                request = _build_bracket_modify_request(
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
            request = OrderRequest(
                instrument=instrument,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                time_in_force=time_in_force,
                correlation_id=correlation_id,
            )
        order = session.engine.modify(oid, request)
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


@router.delete(
    "/orders/{order_id}",
    response_model=OrderResponse,
    dependencies=[Depends(verify_api_key)],
)
async def cancel_order(
    order_id: str,
    session: Any | None = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OrderResponse:
    # Idempotency-Key is mandatory for mutations; the cancel's own key is
    # reserved/replayed by the engine (N2), not just validated here.
    correlation_id = _require_idempotency_key(idempotency_key)
    _require_trading_mode(session)
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        order = session.engine.cancel(oid, correlation_id=correlation_id)
        return OrderResponse(order_id=str(order.order_id), status="cancelled")
    except HTTPException:
        raise
    except IdempotencyKeyReuseMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInflight as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
