"""The FillSource contract must be declared, and the engine must fail toward unknown.

Defect (Critical, M-2): ``ExecutionEngine`` probed five members off the fill
source with ``getattr(..., <default>)``, and two of them were absent from the
``FillSource`` Protocol entirely:

* ``submission_boundary_crossed`` -- defaulted to ``False``. A live source
  that crossed into the venue and then raised a non-typed error was filed as a
  *definitive rejection*: the correlation id was released, the kill switch
  stayed untripped, and a client retry of its own "REJECTED" order placed a
  **second live order** under the same key.
* ``position_projection_owned`` -- 3 of 4 implementations declared neither,
  so the engine discovered a money-path-relevant capability by probing for an
  attribute that may or may not exist.

The invariant pinned here: a source that may have reached a venue is failed
toward UNKNOWN, and every implementation declares the capability explicitly
rather than relying on a probe default.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.fill_sources import (
    BrokerFillSource,
    FillSource,
    PaperFillSource,
    ReplayFillSource,
    SimulatedFillSource,
)

_INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Quantity(Decimal("10")),
        order_type=OrderType.MARKET,
        price=Price(Decimal("2500")),
    )


ALL_SOURCES = (SimulatedFillSource, PaperFillSource, ReplayFillSource)


@pytest.mark.parametrize("cls", ALL_SOURCES)
def test_offline_sources_declare_they_never_reach_a_venue(cls) -> None:
    """Backtest/paper/replay must say so explicitly, not rely on a probe default."""
    assert hasattr(cls, "reaches_venue"), f"{cls.__name__} must declare reaches_venue"
    assert cls.reaches_venue is False, (
        f"{cls.__name__} never contacts a venue; it must declare reaches_venue=False "
        "so a local failure is not mistaken for an unknown venue outcome"
    )


@pytest.mark.parametrize("cls", ALL_SOURCES)
def test_offline_sources_declare_projection_ownership(cls) -> None:
    """The engine projects for offline sources, so they must say they do not own it."""
    assert hasattr(cls, "position_projection_owned"), (
        f"{cls.__name__} must declare position_projection_owned"
    )
    assert cls.position_projection_owned is False


def test_broker_source_declares_both_members() -> None:
    assert hasattr(BrokerFillSource, "reaches_venue")
    assert BrokerFillSource.reaches_venue is True, (
        "a broker-backed source can be holding a live order; it must declare "
        "reaches_venue=True so failures default to UNKNOWN rather than REJECTED"
    )
    assert hasattr(BrokerFillSource, "position_projection_owned")
    assert hasattr(BrokerFillSource, "submission_boundary_crossed")


def test_protocol_declares_the_capabilities_the_engine_depends_on() -> None:
    """The engine calls these; the Protocol must declare them.

    ``@runtime_checkable`` only checks *method* presence, and a bare
    annotation creates no class attribute, so declaration is checked against
    the Protocol's annotations. This is how the probe defaults went unnoticed:
    the contract read by ``getattr`` was never written down.
    """
    declared = getattr(FillSource, "__annotations__", {})
    for member in ("reaches_venue", "position_projection_owned", "submission_boundary_crossed"):
        assert member in declared, (
            f"FillSource must declare {member}; the engine reads it"
        )


def test_a_live_source_that_raises_after_send_is_not_filed_as_a_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A venue-crossing failure with no latch set must still be UNKNOWN.

    This is the two-live-orders scenario: engine treats it as REJECTED, the
    caller retries the same correlation id, and the venue ends up holding the
    order twice.
    """
    from tradex_execution.engine import ExecutionEngine
    from tradex_reactive.bus import ReactiveBus

    class _SourceThatLiesByOmission:
        """A live source that never sets the boundary latch."""

        reaches_venue = True
        position_projection_owned = False

        def submit(self, request):  # type: ignore[no-untyped-def]
            raise ConnectionError("connection reset by peer after send")

        def cancel(self, order_id: object) -> None:
            return None

        def modify(self, order_id: object, request: object) -> None:
            return None

    engine = ExecutionEngine(ReactiveBus(), _SourceThatLiesByOmission())  # type: ignore[arg-type]

    from tradex_domain.errors import OrderSubmissionUnknownError

    with pytest.raises(OrderSubmissionUnknownError):
        engine.submit(_request())

    assert engine.kill_switch is True, (
        "a submission that may have reached the venue must halt admission; "
        "treating it as a rejection lets a retry place a second live order"
    )
