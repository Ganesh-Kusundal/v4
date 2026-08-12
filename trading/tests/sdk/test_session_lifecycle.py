"""Session lifecycle tests — services, state transitions, kill switch.

Ported from v3 ``test_followup_execution_session.py``.

v4 API differences:
- ``TradingSession(broker, bus, engine, cache, broker_id)`` requires all components
- Services are properties (not methods) that check READY state
- ``SessionStateError`` raised when accessing services outside READY
- ``stop()`` replaces v3 ``close()``
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain import BrokerId
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import PaperFillSource, SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.startup import boot
from tradex_trading.sdk.session import SessionState, TradingSession


def _make_equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_request() -> OrderRequest:
    return OrderRequest(
        instrument=_make_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500.00")),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


# ---------------------------------------------------------------------------
# Session service accessors require READY state
# ---------------------------------------------------------------------------


class TestSessionServiceAccessors:
    """Services raise SessionStateError when session is not READY."""

    def test_services_raise_in_new_state(self) -> None:
        session = boot()
        # boot() returns READY, so create a raw session in NEW state
        bus = ReactiveBus()
        engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource())
        raw = TradingSession(
            broker=session._broker,
            bus=bus,
            engine=engine,
            cache=engine.cache,
            broker_id=BrokerId.PAPER,
        )
        # raw is in NEW state
        from tradex_domain.errors import SessionStateError
        with pytest.raises(SessionStateError):
            _ = raw.trade
        session.stop()

    def test_services_raise_after_stop(self) -> None:
        session = boot()
        session.stop()
        from tradex_domain.errors import SessionStateError
        with pytest.raises(SessionStateError):
            _ = session.trade

    def test_all_services_accessible_in_ready(self) -> None:
        session = boot()
        assert session.market is not None
        assert session.trade is not None
        assert session.portfolio is not None
        assert session.stream is not None
        assert session.scanner is not None
        assert session.extension is not None
        session.stop()


# ---------------------------------------------------------------------------
# Session lifecycle: NEW → READY → STOPPED
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """Session state transitions."""

    def test_boot_returns_ready_session(self) -> None:
        session = boot()
        assert session.state == SessionState.READY
        session.stop()

    def test_stop_transitions_to_stopped(self) -> None:
        session = boot()
        session.stop()
        assert session.state == SessionState.STOPPED

    def test_double_stop_is_safe(self) -> None:
        session = boot()
        session.stop()
        session.stop()  # should not raise

    def test_broker_id_is_paper_by_default(self) -> None:
        session = boot()
        assert session.broker_id == BrokerId.PAPER
        session.stop()

    def test_mode_is_paper_by_default(self) -> None:
        session = boot()
        assert session.mode == "paper"
        session.stop()

    def test_stop_closes_broker_sockets(self) -> None:
        """stop() tears down the broker so WebSocket loops/reconnect die with it.

        Regression: nothing ever closed the broker's market WebSocket — after
        stop() the daemon receive loop and reconnect machinery stayed armed,
        minting fresh tokens forever.
        """
        session = boot()
        broker = session._broker
        assert broker._connected is True
        session.stop()
        assert broker._connected is False


# ---------------------------------------------------------------------------
# Execution engine without risk manager reaches fill source
# ---------------------------------------------------------------------------


class TestExecutionEngineWithoutRisk:
    """ExecutionEngine works without a risk manager."""

    def test_submit_reaches_fill_source(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus=bus,
            fill_source=SimulatedFillSource(),
            risk_manager=None,
        )
        receipt = engine.submit(_make_request())
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)

    def test_submit_with_default_risk(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus=bus,
            fill_source=PaperFillSource(),
        )
        receipt = engine.submit(_make_request())
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)


# ---------------------------------------------------------------------------
# Session capabilities
# ---------------------------------------------------------------------------


class TestSessionCapabilities:
    """Session capabilities and broker access."""

    def test_capabilities_accessible(self) -> None:
        session = boot()
        caps = session.capabilities
        assert caps is not None
        assert caps.supports_market_order is True
        session.stop()

    def test_bus_is_reactive(self) -> None:
        session = boot()
        from tradex_trading.reactive.bus import ReactiveBus
        assert isinstance(session.bus, ReactiveBus)
        session.stop()
