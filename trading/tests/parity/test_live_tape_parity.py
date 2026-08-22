"""Live-tape parity — prove the LIVE pipeline produces the same OMS state as
the paper spine for identical economic events.

Method: recorded-shape provider order-update tapes (real Dhan/Upstox field
names as mapped by each client's ``_stream_order_from_row``) are replayed
through the production chain —

    provider row → adapter mapping → LiveFillBridge → OrderFilled on the bus
                 → ExecutionEngine OMS/PositionManager

— and the resulting engine state is compared against (a) golden expectations
derived from the tape itself and (b) the PaperBroker simulating the same
fills through the shared FillModel. This is the recorded-evidence leg of live
parity that scripted-transport tests cannot provide: it exercises the real
provider mapping code, the real bridge delta logic, and the real engine.

Known mapping gap surfaced by this harness (documented, not hidden): Upstox's
status map has no partial-fill representation — ``open`` maps to ACK even when
``filled_quantity`` is non-zero — so Upstox partials collapse into a single
fill at terminal status. Dhan partials (PART_TRADED) map correctly. Final
state parity holds for both; fill *granularity* currently only does for Dhan.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.dhan.client import DhanApiClient
from tradex_brokers.paper.adapter import PaperBroker
from tradex_brokers.upstox.adapter import UpstoxBroker
from tradex_brokers.upstox.client import UpstoxApiClient
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import PaperFillSource
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.sdk.live_fill_bridge import LiveFillBridge

_RELIANCE = Equity.of("NSE", "RELIANCE")
_CORR = "strat-tape-1"
_QTY = Decimal("10")
_LIMIT = Decimal("2500")
_TRADED = Decimal("2500.5")

# ---------------------------------------------------------------------------
# Recorded-shape tapes (provider-native field names)
# ---------------------------------------------------------------------------

_DHAN_ACK = {
    "orderId": "prov-1", "securityId": "2885", "transactionType": "BUY",
    "orderType": "LIMIT", "quantity": "10", "price": "2500",
    "orderStatus": "TRANSIT", "filledQty": "0", "correlationId": _CORR,
}
_DHAN_PARTIAL = {
    **_DHAN_ACK,
    "orderStatus": "PART_TRADED", "filledQty": "4", "tradedPrice": str(_TRADED),
}
_DHAN_FINAL = {
    **_DHAN_ACK,
    "orderStatus": "TRADED", "filledQty": "10", "tradedPrice": str(_TRADED),
}

_UPSTOX_ACK = {
    "order_id": "prov-1", "tradingsymbol": "NSE_EQ|RELIANCE",
    "transaction_type": "BUY", "order_type": "LIMIT", "quantity": 10,
    "price": "2500", "status": "open", "filled_quantity": 0, "tag": _CORR,
}
_UPSTOX_PARTIAL = {
    **_UPSTOX_ACK,
    "filled_quantity": 4, "average_price": str(_TRADED),
}
_UPSTOX_FINAL = {
    **_UPSTOX_ACK,
    "status": "complete", "filled_quantity": 10, "average_price": str(_TRADED),
}


def _seeded_engine() -> tuple[ExecutionEngine, ReactiveBus, TradingCache]:
    """Engine + bus with the locally-placed order already in the OMS cache
    (as it would be after a real submit), carrying the strategy correlation id
    the broker echoes back."""
    bus = ReactiveBus()
    cache = TradingCache()
    engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource(), cache=cache)
    cache.update_order(
        Order(
            order_id=OrderId(value="prov-1"),
            instrument=_RELIANCE,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=_QTY),
            price=Price(value=_LIMIT),
            time_in_force=TimeInForce.DAY,
            correlation_id=CorrelationId(value=_CORR),
            status=OrderStatus.ACK,
        )
    )
    return engine, bus, cache


def _replay_tape(mapped_orders: list[Order]) -> tuple[list, list]:
    """Push cumulative stream updates through LiveFillBridge → bus → engine.

    Returns (engine, received OrderFilled events).
    """
    from tradex_domain.events import OrderFilled

    engine, bus, _ = _seeded_engine()
    received: list[OrderFilled] = []
    bus.of_type(OrderFilled).subscribe(received.append)

    pushed: list = []

    def subscribe_orders(handler):  # noqa: ANN001 — backend seam
        pushed.append(handler)
        return "sub"

    bridge = LiveFillBridge(bus=bus, engine=engine, subscribe_orders=subscribe_orders)
    handler = pushed[0]
    for order in mapped_orders:
        handler(order)  # broker receive-thread equivalent (sequential)
    bridge.close()
    return engine, received


# ---------------------------------------------------------------------------
# Provider mapping + bridge + engine, per recorded tape
# ---------------------------------------------------------------------------


def _dhan_mapped_tape() -> list[Order]:
    broker = DhanBroker.from_fetch(
        fetch=lambda method, url, **kw: {"data": {}},
        client_id="client",
        access_token="token",
    )
    iid = _RELIANCE.instrument_id
    broker.registry.register(iid, {"key": "NSE_EQ|RELIANCE", "security_id": "2885"})
    broker.registry.add_alias("2885", iid)
    mapper = broker._transport._stream_order_from_row  # noqa: SLF001 — harness seam
    return [mapper(row) for row in (_DHAN_ACK, _DHAN_PARTIAL, _DHAN_FINAL)]


def _upstox_mapped_tape() -> list[Order]:
    broker = UpstoxBroker.from_fetch(
        fetch=lambda method, url, **kw: {"data": {}},
        access_token="token",
    )
    broker.registry.register(_RELIANCE.instrument_id, {"key": "NSE_EQ|RELIANCE"})
    mapper = broker._transport._stream_order_from_row  # noqa: SLF001 — harness seam
    return [mapper(row) for row in (_UPSTOX_ACK, _UPSTOX_PARTIAL, _UPSTOX_FINAL)]


def test_dhan_tape_fills_granular_and_matches_golden() -> None:
    engine, fills = _replay_tape(_dhan_mapped_tape())
    order = engine.cache.get_order("prov-1")
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity.value == _QTY
    # Dhan partials map: two delta fills (4 then 6) at the tape traded price.
    assert [f.fill.quantity.value for f in fills] == [Decimal("4"), Decimal("6")]
    assert all(f.fill.price.value == _TRADED for f in fills)
    position = engine.cache.all_positions()
    assert len(position) == 1
    assert position[0].quantity.value == _QTY
    assert position[0].avg_price.value == _TRADED


def test_upstox_tape_reaches_identical_final_state() -> None:
    engine, fills = _replay_tape(_upstox_mapped_tape())
    order = engine.cache.get_order("prov-1")
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity.value == _QTY
    # Mapping gap (documented): partials collapse — one terminal delta of 10.
    assert [f.fill.quantity.value for f in fills] == [_QTY]
    assert fills[0].fill.price.value == _TRADED
    position = engine.cache.all_positions()
    assert position[0].quantity.value == _QTY
    assert position[0].avg_price.value == _TRADED


# ---------------------------------------------------------------------------
# Cross-provider parity: identical tape economics → identical engine state
# ---------------------------------------------------------------------------


def test_live_tape_parity_across_providers() -> None:
    dhan_engine, _ = _replay_tape(_dhan_mapped_tape())
    upstox_engine, _ = _replay_tape(_upstox_mapped_tape())

    d = dhan_engine.cache.get_order("prov-1")
    u = upstox_engine.cache.get_order("prov-1")
    assert (d.status, d.filled_quantity.value) == (u.status, u.filled_quantity.value)

    d_pos = dhan_engine.cache.all_positions()[0]
    u_pos = upstox_engine.cache.all_positions()[0]
    assert d_pos.quantity.value == u_pos.quantity.value == _QTY
    assert d_pos.avg_price.value == u_pos.avg_price.value == _TRADED


# ---------------------------------------------------------------------------
# Live ↔ paper parity: the same fills through the paper spine produce the
# same position (shared FillModel / position math).
# ---------------------------------------------------------------------------


def test_live_tape_position_matches_paper_spine() -> None:
    engine, _ = _replay_tape(_dhan_mapped_tape())
    live_pos = engine.cache.all_positions()[0]

    paper = PaperBroker(auto_fill=True)
    paper.set_quote(_RELIANCE, ltp=Price(value=_TRADED))
    from tradex_domain.execution import OrderRequest

    paper.submit_order(
        OrderRequest(
            instrument=_RELIANCE,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=_QTY),
        )
    )
    paper_pos = paper.get_positions()[0]

    assert paper_pos.quantity.value == live_pos.quantity.value == _QTY
    assert paper_pos.avg_price.value == live_pos.avg_price.value == _TRADED