"""A recorded tape must actually be able to drive the two paper fill paths.

This is why lane 2g exists: AC-14 (lane 2d) compares the slippage-model fill
path against book matching on a captured session, and lane 1l compares the
backtest against the live path on the same. Both are assertions about fills, so
what has to be proven here is that a file written by :class:`TapeRecorder` and
read back by :func:`read_tape` produces the same fill prices, in the same
order, through both paths.

The negative control is that the expected prices are read from the tape file
itself rather than hard-coded. A reader that reordered, dropped or re-priced a
tick would change what the two paths were fed, and the two paths would still
agree with each other — so asserting only convergence would pass over exactly
the defect these two requirements depend on being absent.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tradex_brokers.paper.adapter import PaperBroker
from tradex_domain import OrderSide, Timeframe
from tradex_domain.enums import OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.fill_sources import PaperFillSource
from tradex_execution.trading_cache import TradingCache
from tradex_runtime.bar_aggregator import BarAggregator, BarFrame
from tradex_runtime.tape_recorder import (
    TAPE_RECORD_ENV_VAR,
    TapeRecorder,
    read_tape,
)

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: A session with a duplicate print and one tick that arrives out of order, so
#: a reader that normalised the sequence is caught here rather than in the
#: consuming lane's assertion.
SESSION = [
    ("10:15:00", "2500.50"),
    ("10:15:07", "2501.25"),
    ("10:15:03", "2499.75"),
    ("10:15:07", "2501.25"),
    ("10:15:11", "2502.00"),
]

#: Twenty significant digits — more than a binary float carries, so it is the
#: value that tells a decimal record from a JSON number.
OVER_PRECISE = "2500.1234567890123456789"


def _stamp(hms: str) -> datetime:
    hour, minute, second = (int(part) for part in hms.split(":"))
    return datetime(2026, 9, 24, hour, minute, second, tzinfo=IST)


def _quote(ts: datetime, ltp: str, *, volume: str | None = "1000") -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(ltp)),
        volume=None if volume is None else Quantity(value=Decimal(volume)),
        timestamp=ts,
        exchange="NSE",
        provider="dhan",
    )


def _write_tape(path: Path, rows: list[tuple[str, str]]) -> Path:
    with TapeRecorder(path, broker="dhan") as recorder:
        for hms, ltp in rows:
            recorder.record(_quote(_stamp(hms), ltp))
    return path


def _request(side: OrderSide = OrderSide.BUY) -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
    )


@pytest.fixture
def tape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A written tape, produced the way a gated production run produces one."""
    monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
    return _write_tape(tmp_path / "tapes" / "tape-dhan-20260924.jsonl", SESSION)


def test_read_tape_reproduces_the_recorded_session(tape: Path) -> None:
    """File order, the duplicate and the out-of-order tick included."""
    ticks = read_tape(tape)

    assert len(ticks) == len(SESSION)
    assert [t.ts_ist.strftime("%H:%M:%S") for t in ticks] == [s for s, _ in SESSION]
    assert [t.price for t in ticks] == [Decimal(p) for _, p in SESSION]


def test_one_tape_drives_both_paper_fill_paths_to_the_same_prices(tape: Path) -> None:
    """The unblock: one recorded file, two fill paths, the same fills in order.

    Path A is ``PaperFillSource`` (the reactive pipeline's fill path) and path B
    is ``PaperBroker``'s simulated book. Every recorded tick is fed to both, and
    both must fill at that tick's own recorded price.
    """
    expected = [tick.price for tick in read_tape(tape)]

    cache = TradingCache()
    pipeline_fills: list[Decimal] = []
    for tick in read_tape(tape):
        cache.update_quote(tick.to_quote())
        _, fill = PaperFillSource(cache=cache).submit(_request())
        assert fill is not None
        pipeline_fills.append(fill.price.value)

    broker = PaperBroker(auto_fill=True)
    book_fills: list[Decimal] = []
    for tick in read_tape(tape):
        quote = tick.to_quote()
        broker.set_quote(quote.instrument, ltp=quote.ltp)
        order = broker.get_order(broker.submit_order(_request()))
        assert order is not None
        book_fills.append(order.price.value)

    assert pipeline_fills == expected
    assert book_fills == expected
    assert pipeline_fills == book_fills


def test_a_sell_drives_both_paths_at_the_recorded_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same file drives the other side, so the unblock is not a buy-only
    artifact."""
    monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
    path = _write_tape(
        tmp_path / "tapes" / "tape-dhan-20260924.jsonl", [("10:15:00", "1500.75")]
    )
    (tick,) = read_tape(path)

    cache = TradingCache()
    cache.update_quote(tick.to_quote())
    _, fill = PaperFillSource(cache=cache).submit(_request(OrderSide.SELL))
    broker = PaperBroker(auto_fill=True)
    quote = tick.to_quote()
    broker.set_quote(quote.instrument, ltp=quote.ltp)
    order = broker.get_order(broker.submit_order(_request(OrderSide.SELL)))

    assert fill is not None
    assert order is not None
    assert fill.price.value == order.price.value == Decimal("1500.75")


def test_a_price_a_float_cannot_carry_survives_and_fills_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON number in the price field would truncate this at 16 digits, and
    the fill would then be asserted against a value the venue never sent."""
    monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
    path = _write_tape(
        tmp_path / "tapes" / "tape-dhan-20260924.jsonl", [("10:15:00", OVER_PRECISE)]
    )
    (tick,) = read_tape(path)

    assert str(float(Decimal(OVER_PRECISE))) != OVER_PRECISE
    assert str(tick.price) == OVER_PRECISE

    cache = TradingCache()
    cache.update_quote(tick.to_quote())
    _, fill = PaperFillSource(cache=cache).submit(_request())
    assert fill is not None
    assert str(fill.price.value) == OVER_PRECISE


def test_tick_to_quote_carries_the_recorded_clock_and_venue(tape: Path) -> None:
    """``PaperBroker.set_quote`` stamps its own wall clock, so the recorded time
    survives only where a consumer reads ``Quote.timestamp`` — the reactive path
    and the bar aggregator. The conversion has to be right for those, and the
    instrument has to come back as the same security."""
    tick = read_tape(tape)[0]
    quote = tick.to_quote()

    assert quote.timestamp == tick.ts == _stamp("10:15:00")
    assert quote.instrument == INSTRUMENT
    assert quote.provider == "dhan"
    assert quote.volume == Quantity(value=Decimal("1000"))
    assert quote.ltp == Price(value=Decimal("2500.50"))


def test_a_tick_buckets_on_its_own_recorded_minute(tape: Path) -> None:
    """The backtest leg of the unblock. ``BarAggregator.on_quote`` takes a
    tz-naive IST stamp, so a tick that lost or shifted its zone lands in the
    wrong minute — and the minute bars a parity assertion compares are built
    from it.
    """
    frames: list[BarFrame] = []
    aggregator = BarAggregator(
        str(INSTRUMENT.instrument_id),
        Timeframe.M1,
        on_frame=frames.append,
        wall_now=lambda: datetime(2026, 9, 24, 10, 15, 30),
    )

    for tick in read_tape(tape):
        aggregator.on_quote(tick.ts_ist, tick.price, tick.qty, source="sim")
    aggregator.flush()

    closed = [f for f in frames if f.closed]
    # One bar: the whole session falls in the 10:15 minute, and the duplicate
    # print and the out-of-order 10:15:03 tick are absorbed into it rather than
    # starting one or rewinding it.
    assert [f.time for f in closed] == [int(_stamp("10:15:00").timestamp())]
    (bar,) = closed
    assert (bar.open, bar.high, bar.low, bar.close) == (2500.50, 2502.00, 2499.75, 2502.00)
    assert bar.volume == 5000.0
