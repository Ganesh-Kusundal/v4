"""``/orders`` — submit, list, modify, cancel orders against the bound session."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from tradex_domain.value_objects import OrderId

from tradex_trading.interface.auth import verify_api_key
from tradex_trading.interface.models import OrderResponse
from tradex_trading.interface.routes.deps import get_session

log = logging.getLogger(__name__)

router = APIRouter()



def _build_order_request(body: dict, session: Any) -> Any:
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
    trigger_price = Price(Decimal(str(body["trigger_price"]))) if body.get("trigger_price") is not None else None
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
    )


def _build_bracket_request(body: dict) -> Any:
    """Parse an HTTP body into a bracket (super) OrderRequest.

    Mirrors ``_build_order_request`` for instrument/entry fields, plus the
    protective legs ``stop_loss_price``/``target_price`` (required) and
    ``trailing_jump`` (optional, defaults 0). Raises ``KeyError`` on missing
    required fields and ``ValueError`` on malformed ones; the broker facade
    validates protective-price ordering.
    """
    from tradex_domain.enums import OrderSide, OrderType, TimeInForce
    from tradex_domain.execution import OrderRequest
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import Price, Quantity

    def _price(value: Any, name: str) -> Price:
        try:
            return Price(Decimal(str(value)))
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"invalid {name}: {value!r}") from exc

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
    return OrderRequest(
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
            else Price(Decimal("0"))
        ),
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
) -> OrderResponse:
    if session is None:
        raise HTTPException(status_code=503, detail="no session bound")
    try:
        request = _build_order_request(body, session)
        receipt = session.engine.submit(request)
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
    except Exception as exc:
        log.exception("order submission failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/orders/bracket", dependencies=[Depends(verify_api_key)])
async def place_bracket_order(
    body: dict, session: Any | None = Depends(get_session)
) -> dict:
    """Place a bracket (super) order — entry + protective stop/target legs.

    Delegates to ``session.broker.submit_super_order``. Capability-gated;
    a missing session returns 503 (matching ``/orders``), an unsupported
    broker 422. Non-bracket orders keep using ``/orders``.
    """
    if session is None:
        raise HTTPException(status_code=503, detail="no session bound")
    broker = getattr(session, "_broker", None) or getattr(session, "broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail="broker unavailable")
    caps = getattr(broker, "capabilities", None)
    if caps is None or not getattr(caps, "supports_super_order", False):
        raise HTTPException(status_code=422, detail="broker does not support super orders")
    try:
        request = _build_bracket_request(body)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        order_id = broker.submit_super_order(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"order_id": str(order_id)}


@router.put(
    "/orders/{order_id}",
    response_model=OrderResponse,
    dependencies=[Depends(verify_api_key)],
)
async def modify_order(
    order_id: str,
    body: dict,
    session: Any | None = Depends(get_session),
) -> OrderResponse:
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        from tradex_domain.enums import OrderSide, OrderType, TimeInForce
        from tradex_domain.execution import OrderRequest
        from tradex_domain.value_objects import Price, Quantity

        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        existing = session.engine.get_order(oid)
        if existing is None:
            raise HTTPException(status_code=422, detail="order not found")

        symbol = body.get("symbol")
        if symbol is not None:
            instrument = session.broker.search(symbol)[0]
        else:
            instrument = existing.instrument

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
        )
        order = session.engine.modify(oid, request)
        return OrderResponse(order_id=str(order.order_id), status="modified")
    except HTTPException:
        raise
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
) -> OrderResponse:
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        oid = order_id if isinstance(order_id, OrderId) else OrderId(value=str(order_id))
        order = session.engine.cancel(oid)
        return OrderResponse(order_id=str(order.order_id), status="cancelled")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
