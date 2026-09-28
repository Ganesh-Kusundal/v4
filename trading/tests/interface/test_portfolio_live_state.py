"""Lane 3i — the portfolio routes must serve the live session book.

The single observed fact that put this lane on the programme: the routes
served a book the running session never wrote. ``/positions`` read
``session.engine.cache.all_positions()`` (the right object) but with no
account half, and ``/holdings`` read ``session.broker.get_holdings()`` — for
a paper session that is the broker's *own* position dict, which the session's
fill path never writes: ``PaperFillSource.position_projection_owned is False``
(``execution/src/tradex_execution/fill_sources.py:206``), so the engine books
the OMS cache and the broker's dict stays empty. A session holding a filled
position reported a flat account on ``/holdings``. That is the stub.

The live source of truth is the OMS cache reached at
``session.engine.cache.all_positions()``
(``execution/src/tradex_execution/trading_cache.py:107``), exposed here through
``get_session_portfolio`` in ``interfaces/src/tradex_interfaces/routes/deps.py``.

The tests below are built on a *real* boot (``TradingSession.paper``), not a
mocked session, because the point is exactly that the route reaches the object
the engine wrote to. Negative controls mutate the live object in place and
assert the route output moves — a route returning a constant or a stub cannot
pass them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from starlette.testclient import TestClient
from tradex_config.schema import AppConfig, RiskConfig
from tradex_domain import UNBOUNDED
from tradex_domain.enums import OrderSide, OrderType, ProductType
from tradex_domain.execution import OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_interfaces.fastapi_app import create_app
from tradex_runtime.session import TradingSession

_INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: A complete, deliberately huge risk policy. The two P&L guards opt out with
#: ``UNBOUNDED`` — the documented opt-out, not an omission — because a test
#: session has no quote-driven mark service and the guards fail closed without
#: one, which is not what these tests are about.
def _risk() -> RiskConfig:
    return RiskConfig(
        max_order_value=Decimal("10000000"),
        max_position_value=Decimal("10000000"),
        max_orders_per_minute=1000,
        max_daily_loss_amt=UNBOUNDED,
        max_drawdown_pct=UNBOUNDED,
        reject_unknown_market_value=False,
        require_fresh_marks=False,
        cash_provider=lambda: Decimal("1000000"),
    )


@pytest.fixture
def session() -> TradingSession:
    """A real booted paper session, stopped by the fixture."""
    session = TradingSession.paper(config=AppConfig(mode="paper", risk=_risk()))
    session.start()
    yield session  # type: ignore[misc]
    session.stop()


@pytest.fixture
def client(session: TradingSession) -> TestClient:
    """The app bound to the live session (no API key — the paper posture)."""
    return TestClient(create_app(session=session, api_key=None))


def _seed_quote(session: TradingSession) -> None:
    """Seed the OMS cache quote and the paper tape behind one MARKET order.

    The risk gate prices a MARKET order off the cache quote
    (``RiskManager._mark_price`` → ``cache.get_quote``), and the paper fill
    source fills off the same cache LTP, so both halves need one quote.
    """
    from tradex_domain.market import Quote

    quote = Quote(
        instrument=_INSTRUMENT,
        ltp=Price(value=Decimal("100.25")),
        bid=Price(value=Decimal("100.20")),
        ask=Price(value=Decimal("100.30")),
        timestamp=datetime.now(UTC),
    )
    session.engine.cache.update_quote(quote)


def _buy_market(session: TradingSession, qty: Decimal) -> None:
    session.engine.submit(
        OrderRequest(
            instrument=_INSTRUMENT,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=qty),
            product_type=ProductType.INTRADAY,
        )
    )


# ---------------------------------------------------------------------------
# The route reaches the live book
# ---------------------------------------------------------------------------


def test_positions_reflects_a_real_fill(session: TradingSession, client: TestClient):
    """A fill that lands in the live book is what ``/positions`` returns."""
    assert client.get("/positions").json() == []
    _seed_quote(session)
    _buy_market(session, Decimal("10"))

    body = client.get("/positions").json()
    assert len(body) == 1
    assert body[0]["instrument"] == "NSE:RELIANCE"
    assert body[0]["quantity"] == "10"
    # The canonical economic projection is what the parity suite pins, and the
    # route must not drift from it.
    assert body[0]["avg_price"] == "100.25"


def test_holdings_no_longer_reports_a_flat_account_for_a_filled_position(
    session: TradingSession, client: TestClient
):
    """The defect: ``broker.get_holdings()`` saw the broker's own empty dict.

    The session booked the fill into the OMS cache and nowhere else, so the
    old route answered ``[]`` for a session holding 10 lots. This is the
    single assertion the lane exists for.
    """
    assert session.broker.get_holdings() == []
    _seed_quote(session)
    _buy_market(session, Decimal("10"))

    assert client.get("/holdings").json() == [
        {"instrument_id": "NSE:RELIANCE", "quantity": 10.0}
    ]


# ---------------------------------------------------------------------------
# Negative controls — mutate the live object, assert the route output moves
# ---------------------------------------------------------------------------


def test_positions_moves_when_the_live_book_moves(session: TradingSession, client: TestClient):
    """Control: the route reads the live object, not a constant.

    Writes a distinct position directly into the OMS cache the engine uses and
    asserts the route's response changes with it. A route serving a stub or a
    hardcoded list cannot pass, and neither can one that reads the broker's
    own dict (which this write does not touch).
    """
    before = client.get("/positions").json()
    assert before == []

    live = session.engine.cache
    marker = Position(
        instrument=_INSTRUMENT,
        quantity=Quantity(value=Decimal("17")),
        avg_price=Price(value=Decimal("250.5")),
        realized_pnl=_money(Decimal("3")),
        unrealized_pnl=_money(Decimal("4")),
    )
    live.update_position(marker)

    after = client.get("/positions").json()
    assert len(after) == 1
    assert after[0]["instrument"] == "NSE:RELIANCE"
    assert after[0]["quantity"] == "17"
    # The marker's economic values come back through the canonical projection,
    # so this also proves the projection is fed the live rows and not a copy
    # captured at boot.
    assert after[0]["avg_price"] == "250.5"
    assert after[0]["realized_pnl"] == "3"
    assert after[0]["unrealized_pnl"] == "4"


def test_holdings_moves_when_the_live_book_moves(session: TradingSession, client: TestClient):
    """Control: the holdings route reads the same live object as ``/positions``."""
    assert client.get("/holdings").json() == []

    session.engine.cache.update_position(
        Position(
            instrument=_INSTRUMENT,
            quantity=Quantity(value=Decimal("23")),
            avg_price=Price(value=Decimal("180.0")),
            realized_pnl=_money(Decimal("0")),
            unrealized_pnl=_money(Decimal("0")),
        )
    )

    assert client.get("/holdings").json() == [
        {"instrument_id": "NSE:RELIANCE", "quantity": 23.0}
    ]


def test_a_flat_position_is_not_reported_as_a_holding(
    session: TradingSession, client: TestClient
):
    """A zero-quantity row is not a holding — the UI would show an exited symbol.

    A stub serving a static list cannot pass this, and neither can one that
    forgets to filter: a closed broker holding row is exactly the case the
    filter exists for (see ``_copy_portfolio`` in the scanner, same rule).
    """
    session.engine.cache.update_position(
        Position(
            instrument=_INSTRUMENT,
            quantity=Quantity(value=Decimal("0")),
            avg_price=Price(value=Decimal("180.0")),
            realized_pnl=_money(Decimal("0")),
            unrealized_pnl=_money(Decimal("0")),
        )
    )
    assert client.get("/holdings").json() == []


# ---------------------------------------------------------------------------
# The accessor is the single source the routes read
# ---------------------------------------------------------------------------


def test_the_accessor_reads_the_oms_book_and_the_account(session: TradingSession):
    """``get_session_portfolio`` is the live object, not a reconstruction."""
    from tradex_interfaces.routes.deps import get_session_portfolio

    assert get_session_portfolio(None).positions == []

    _seed_quote(session)
    _buy_market(session, Decimal("10"))
    snapshot = get_session_portfolio(session)

    (position,) = snapshot.positions
    assert str(position.instrument.instrument_id) == "NSE:RELIANCE"
    assert position.quantity.value == Decimal("10")
    # The account half is the broker's, and it is the same Money the
    # ``/account`` route already serves — one read, book and money.
    assert snapshot.account is not None
    assert snapshot.account.balance is not None
    assert snapshot.account.balance.amount == Decimal("100000")


def test_a_broker_that_cannot_read_its_account_still_serves_the_book(
    session: TradingSession, client: TestClient
):
    """A funds-call failure must not turn a working book into an empty one.

    ``/account`` answers the broker's account directly and so fails with the
    broker — the money really is unknown, and that route's contract is to say
    so rather than fabricate a zero. ``/positions`` shares none of that
    dependency: it reads the live book and only *degrades* the account half
    of its snapshot, which is the point of building the two halves
    independently in ``get_session_portfolio``.
    """
    _seed_quote(session)
    _buy_market(session, Decimal("10"))

    def _boom() -> None:
        raise RuntimeError("funds endpoint down")

    session.broker.get_account = _boom  # type: ignore[method-assign]

    # The account read genuinely fails, and that is the right outcome — the
    # route reports the failure instead of inventing a balance. (Not asserted
    # as a status code: whether the framework surfaces it as 500 or 502 is the
    # framework's business, and pinning it would be pinning an accident.)
    with pytest.raises(RuntimeError, match="funds endpoint down"):
        client.get("/account")

    body = client.get("/positions").json()
    assert [row["instrument"] for row in body] == ["NSE:RELIANCE"]
    assert body[0]["quantity"] == "10"

    # The book is still served when the account is unreadable, and the
    # account half of the snapshot is dropped rather than reported as zero.
    from tradex_interfaces.routes.deps import get_session_portfolio

    snapshot = get_session_portfolio(session)
    assert snapshot.account is None
    assert [str(p.instrument.instrument_id) for p in snapshot.positions] == ["NSE:RELIANCE"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _money(amount: Decimal):
    from tradex_domain.value_objects import Money

    return Money(amount=amount, currency="INR")
