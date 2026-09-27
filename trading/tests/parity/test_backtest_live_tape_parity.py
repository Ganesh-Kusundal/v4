"""Backtest/live parity on one recorded tape — the Wave 1 gate (lane 1l).

The claim under test: **the backtest and the live-shaped path agree on fills,
positions and profit and loss.** Everything else the two modes do differently
is deliberately excluded and named below.

How the two paths are made comparable
-------------------------------------
The natural objection is that the two paths do not fill alike by construction:
the backtest fills a signal at the **next bar's open**, while a live/paper path
with ``PaperFillSource`` fills **immediately at the cached LTP**. Rather than
invent a ``next_tick`` fill reference, both paths are driven through the SAME
``ReactiveStrategyEngine`` with ``fill_reference="next_open"``, fed the SAME M1
bars, and constructed with the SAME shared ``PositionSizer``. They therefore
share: identical strategy, identical sizer, identical strategy engine,
``protective_request`` / ``PlaceOrderCommand`` / ``ExecutionEngine`` /
``position_math.apply_fill``, and one ``FeeCalculator``. The only difference
between them is the fill source the ``ExecutionEngine`` was given — the
backtest's ``SimulatedFillSource`` versus the paper session's
``PaperFillSource``. That is the strongest parity claim available without a
production change.

**Residual asymmetry (stated, not hidden).** The live path in production fills
against a *tick* grid (whatever LTP the cache holds at submit time); this suite
feeds it the same *bar* grid the backtest consumes, so both fill at the next
bar's open. That removes the tick-vs-bar confound from the fill price — which
is the point, since a price difference here would be an artefact of the clock
and not a parity defect. What this suite therefore does **not** prove is that
a live path filling on ticks lands on the same price; proving that needs a
captured tick session and is lane 2d's job.

The bars are real
-----------------
The bars are not hand-written fixtures. They are produced by running a
``TapeRecorder``-written session through the production ``BarAggregator``, so
the parity is measured over the same bucketing the live feed uses and the
"the tape and the bars are the same session" reconciliation is a real guard, not
a tautology.

What is compared, and what is not
---------------------------------
Compared: the executed **fill stream** (side/quantity/price, via the shared
``execution_projection``), the **terminal position** (quantity exact, avg_price
within a paisa, realized_pnl within 0.05% of the oracle's net), the
FIFO **round trips** (count, side/quantity sequence, net within 0.05%), the
**filled-order count**, and both paths' **fees**.

Deliberately NOT compared (these genuinely differ by mode, and comparing them
would be comparing two different things):

* order ids (random/venue-assigned) and timestamps — omitted already by
  ``execution_projection``;
* fill latency and partial arrival (live books venue partials; the backtest
  books one fill per order) — hence round trips, not fill-by-fill;
* ``unrealized_pnl`` — ``position_math.apply_fill`` resets it to zero on every
  fill, while the backtest adds point-in-time MTM the reactive path does not;
* realized-PnL-vs-cash as an accounting base — the backtest debits cash, the
  reactive path debits the position's realized P&L, and the two coincide only
  for a flat run, so cash is reported but not asserted equal;
* the kill switch, persistence/event store, corporate actions (``None`` on
  both), and protective exits (this strategy declares no stop/target — lane 1k
  owns protective mechanics; the core parity here runs unprotected and the
  session ends on a deliberately non-flat position, asserted flatness is not
  assumed);
* the last-bar signal, which **both** paths drop — asserted as
  ``bt.num_unfilled == len(se.pending_snapshot())``, NOT as zero.

Non-vacuousness is itself a test case: the session is designed to produce
several round trips with mixed signs, a reversal, partial closes, a duplicate
print and an out-of-order tick, fees are asserted non-zero on both paths, and a
negative control proves the 0.05% PnL tolerance actually fails when a slippage
constant is wrong.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tradex_analytics.trade_metrics import Trade, round_trip_trades
from tradex_config.schema import AppConfig, ExecutionConfig
from tradex_domain import OHLC, Candle, Fill, OrderSide, Signal, Timeframe
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Position
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.fees import FeeCalculator
from tradex_execution.projection import execution_projection
from tradex_execution.slippage import PercentageSlippageModel
from tradex_reactive.bus import ReactiveBus
from tradex_replay.backtest import BacktestEngine as _BacktestEngine
from tradex_replay.backtest import BacktestResult
from tradex_runtime.bar_aggregator import BarAggregator, BarFrame
from tradex_runtime.session_impl import TradingSession
from tradex_runtime.tape_recorder import (
    TAPE_RECORD_ENV_VAR,
    TAPE_RECORD_ON,
    RecordedTick,
    TapeRecorder,
    read_tape,
)
from tradex_strategy import ReactiveStrategyEngine
from tradex_strategy.core.sizing import PositionSizer
from trading.tests.risk_support import permissive_risk

#: The backtest engine under test, imported under a private alias so the two
#: paths stay visually distinct in the helpers below.
BacktestEngine = _BacktestEngine

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: Opening capital for BOTH paths. The backtest seeds it; the paper session's
#: composition root reads the same figure from ``PaperBroker``'s balance, so the
#: two ledgers start equal and a divergence is a parity finding, not a setup
#: difference.
CASH = Decimal("100000")

#: 0.05% — the tolerance every P&L comparison in this module uses, expressed
#: against the oracle run's absolute net P&L.
PNL_TOLERANCE = Decimal("0.0005")

#: The one shared sizer policy. Large enough that every ``Signal.quantity`` in
#: this session is honoured verbatim by both paths, so sizes come from the
#: signal (never ``Signal.strength``) and the notional ceiling never binds.
SIZER_POLICY = {"max_order_value": Decimal("100000")}

#: A recorded session whose data is DELIBERATELY non-monotonic: it rises to
#: 106, falls to 101, then rises to 109. A monotone tape would make a wrong
#: fill price look correct. It carries a duplicate print and one out-of-order
#: tick (both inside the 10:18 minute) and, in the first tick of every minute,
#: the value that becomes that bar's OPEN — which is the price the previous
#: bar's signal fills at.
SESSION: tuple[tuple[str, str], ...] = (
    ("10:15:00", "100.00"), ("10:15:20", "100.40"), ("10:15:40", "100.20"),
    ("10:16:00", "102.00"), ("10:16:20", "102.60"), ("10:16:40", "101.80"),
    ("10:17:00", "104.00"), ("10:17:20", "104.50"), ("10:17:40", "103.60"),
    ("10:18:00", "106.00"), ("10:18:20", "106.80"), ("10:18:20", "106.80"),
    ("10:18:03", "105.20"), ("10:18:40", "105.60"),
    ("10:19:00", "103.00"), ("10:19:20", "103.40"), ("10:19:40", "102.60"),
    ("10:20:00", "101.00"), ("10:20:20", "100.60"), ("10:20:40", "100.20"),
    ("10:21:00", "105.00"), ("10:21:20", "105.40"), ("10:21:40", "104.60"),
    ("10:22:00", "107.00"), ("10:22:20", "107.40"), ("10:22:40", "106.60"),
    ("10:23:00", "109.00"), ("10:23:20", "109.40"), ("10:23:40", "108.60"),
)

#: What the strategy emits on each bar (1-indexed), as ``(side, quantity)``.
#: A signal on bar N fills at bar N+1's open, so the bar-9 signal has no bar
#: to fill against on EITHER path and is the shared "unfilled" case. Sizes
#: come from ``Signal.quantity``. This plan yields a trend leg (BUY 5, BUY 3),
#: two partial closes (SELL 4, SELL 4) that FIFO-decompose, a reversal into a
#: short and back (SELL 2, BUY 3), and a non-flat terminal position.
PLAN: dict[int, tuple[str, str]] = {
    1: ("BUY", "5"),
    2: ("BUY", "3"),
    3: ("SELL", "4"),
    4: ("SELL", "4"),
    5: ("SELL", "2"),
    6: ("BUY", "3"),
    7: ("BUY", "1"),
    8: ("BUY", "2"),
    9: ("BUY", "1"),
}


# ---------------------------------------------------------------------------
# Tape + bar construction (production writers only)
# ---------------------------------------------------------------------------


def _stamp(hms: str) -> datetime:
    hour, minute, second = (int(part) for part in hms.split(":"))
    return datetime(2026, 9, 24, hour, minute, second, tzinfo=IST)


def _quote(ts: datetime, ltp: str) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(ltp)),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
        exchange="NSE",
        provider="dhan",
    )


def _write_tape(path: Path) -> Path:
    """Record the session exactly the way a gated production run would."""
    with TapeRecorder(path, broker="dhan") as recorder:
        for hms, ltp in SESSION:
            recorder.record(_quote(_stamp(hms), ltp))
    return path


def _bucket_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _bars_from_tape(ticks: tuple[RecordedTick, ...]) -> tuple[Candle, ...]:
    """Aggregate the recorded tape into M1 bars via the production aggregator.

    Only ``closed`` frames are kept, in emission order, so the result is exactly
    the bars a live feed would have published: one per minute, closed when the
    next minute's first tick arrives and the last one closed by ``flush()``.
    ``BarFrame.time`` is the documented chart convention (UTC epoch seconds of
    the IST bucket start), so the Candle timestamp is that instant in UTC.
    """
    frames: list[BarFrame] = []
    aggregator = BarAggregator(
        str(INSTRUMENT.instrument_id),
        Timeframe.M1,
        on_frame=frames.append,
        # The aggregator's only clock is a monotonic throttle for FORMING
        # frames; there is no wall clock in a recorded session, so pin it. We
        # keep only closed frames, but pinning keeps this independent of the
        # host's speed exactly as the reference tape test does.
        now=lambda: 0.0,
    )
    for tick in ticks:
        aggregator.on_quote(tick.ts_ist, tick.price, tick.qty, source="sim")
    aggregator.flush()

    return tuple(
        Candle(
            instrument=INSTRUMENT,
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(value=Decimal(str(frame.open))),
                high=Price(value=Decimal(str(frame.high))),
                low=Price(value=Decimal(str(frame.low))),
                close=Price(value=Decimal(str(frame.close))),
            ),
            volume=Quantity(value=Decimal(str(frame.volume))),
            timestamp=datetime.fromtimestamp(frame.time, tz=ZoneInfo("UTC")),
        )
        for frame in frames
        if frame.closed
    )


# ---------------------------------------------------------------------------
# The strategy
# ---------------------------------------------------------------------------


class _TapeParityStrategy:
    """Emits the fixed PLAN on each bar. No stop/target declared (lane 1k)."""

    def __init__(self) -> None:
        self._bar = 0
        self.signals: list[Signal] = []

    @property
    def strategy_id(self) -> str:
        return "tape-parity-plan"

    def on_bar(self, context: Any, candle: Candle) -> Signal | None:
        self._bar += 1
        plan = PLAN.get(self._bar)
        if plan is None:
            return None
        side, shares = plan
        signal = Signal(
            instrument=candle.instrument,
            direction=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            strength=1.0,  # signal QUALITY, never the order size
            quantity=Quantity(value=Decimal(shares)),
            reason=f"bar{self._bar}",
            timestamp=candle.timestamp,
        )
        self.signals.append(signal)
        return signal

    def on_quote(self, context: Any, quote: Quote) -> None:
        return None

    def on_depth(self, context: Any, depth: Any) -> None:
        return None

    def on_fill(self, context: Any, fill: Fill) -> None:
        pass


# ---------------------------------------------------------------------------
# The two runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Run:
    """Everything one path produced, in comparable form."""

    label: str
    bars: tuple[Candle, ...]
    fills: tuple[Fill, ...]
    positions: tuple[Position, ...]
    cash: Decimal
    total_fees: Decimal
    pending: int
    filled_orders: int
    num_unfilled: int

    def position_for(self, instrument: Any) -> Position | None:
        for position in self.positions:
            if position.instrument.instrument_id == instrument.instrument_id:
                return position
        return None

    def net(self) -> Decimal:
        return sum((t.net_pnl for t in self.round_trips()), Decimal(0))

    def round_trips(self) -> list[Trade]:
        # No ``cost_by_order``: both paths book the SAME fills (asserted
        # elsewhere), hence identical fees, so the round-trip net is compared
        # on its price-derived part and the fee parity is asserted directly.
        # Comparing a gross number here avoids fabricating a per-order cost
        # map the backtest does not expose.
        return round_trip_trades(self.fills)


def _run_backtest(
    bars: tuple[Candle, ...],
    sizer: PositionSizer,
    *,
    slippage_model: Any = None,
) -> tuple[BacktestResult, PositionSizer]:
    """Run the backtest path. Returns (result, the sizer it was given)."""
    result = BacktestEngine(
        fill_source=None,  # engine builds its own SimulatedFillSource
        fee_calculator=FeeCalculator(),
        sizer=sizer,
        initial_capital=CASH,
        slippage_model=slippage_model,
        corporate_actions=None,
        risk_manager=None,
    ).run(_TapeParityStrategy(), list(bars))
    return result, sizer


def _run_live(
    bars: tuple[Candle, ...],
    sizer: PositionSizer,
) -> _Run:
    """Run the live-shaped path through a real paper session.

    ``TradingSession.paper`` supplies the production ``ExecutionEngine`` and its
    ``PaperFillSource``; the ``ReactiveStrategyEngine`` here is the same class
    the backtest uses, with the same ``next_open`` reference and the SAME sizer
    object, so the two paths differ only in the fill source.
    """
    bus = ReactiveBus()
    session = TradingSession.paper(
        bus=bus,
        config=AppConfig(risk=permissive_risk(), execution=ExecutionConfig(fees_enabled=True)),
    )
    strategy_engine = ReactiveStrategyEngine(
        session.bus, fill_reference="next_open", sizer=sizer,
    )
    strategy_engine.register(_TapeParityStrategy())
    fills: list[Fill] = []
    session.bus.of_type(OrderFilled).subscribe(lambda event: fills.append(event.fill))
    try:
        for candle in bars:
            bus.publish(candle)
        positions = tuple(session.engine.cache.all_positions())
        snapshot = session.engine.cash_snapshot()
        pending = len(strategy_engine.pending_snapshot())
        filled_orders = sum(
            1 for order in session.engine.cache.all_orders()
            if order.status.value == "FILLED"
        )
    finally:
        strategy_engine.dispose_all()
        session.stop()
    return _Run(
        label="live",
        bars=bars,
        fills=tuple(fills),
        positions=positions,
        cash=snapshot.cash,
        total_fees=snapshot.total_fees,
        pending=pending,
        filled_orders=filled_orders,
        num_unfilled=pending,  # reactive: the queue IS the unfilled set
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _tape_gate() -> Iterator[None]:
    """Open the recorder gate for the module, the way a gated run would."""
    previous = os.environ.get(TAPE_RECORD_ENV_VAR)
    os.environ[TAPE_RECORD_ENV_VAR] = TAPE_RECORD_ON
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(TAPE_RECORD_ENV_VAR, None)
        else:
            os.environ[TAPE_RECORD_ENV_VAR] = previous


@pytest.fixture(scope="module")
def tape(tmp_path_factory: pytest.TempPathFactory, _tape_gate: None) -> Path:
    directory = tmp_path_factory.mktemp("tapes")
    return _write_tape(directory / "tape-dhan-20260924.jsonl")


@pytest.fixture(scope="module")
def ticks(tape: Path) -> tuple[RecordedTick, ...]:
    return read_tape(tape)


@pytest.fixture(scope="module")
def bars(ticks: tuple[RecordedTick, ...]) -> tuple[Candle, ...]:
    return _bars_from_tape(ticks)


@pytest.fixture(scope="module")
def sizer() -> PositionSizer:
    """ONE sizer, constructed once, handed to BOTH paths."""
    return PositionSizer(label="parity", **SIZER_POLICY)


@pytest.fixture(scope="module")
def backtest(
    bars: tuple[Candle, ...], sizer: PositionSizer,
) -> tuple[BacktestResult, PositionSizer]:
    return _run_backtest(bars, sizer)


@pytest.fixture(scope="module")
def live(bars: tuple[Candle, ...], sizer: PositionSizer) -> _Run:
    return _run_live(bars, sizer)


@pytest.fixture(scope="module")
def bt_run(
    bars: tuple[Candle, ...], sizer: PositionSizer, backtest: Any,
) -> _Run:
    result, _ = backtest
    positions = tuple(
        _position_from_end(record) for record in result.end_positions
    )
    filled_orders = len(result.fill_records)
    return _Run(
        label="backtest",
        bars=bars,
        fills=tuple(result.fill_records),
        positions=positions,
        cash=Decimal(str(result.equity_curve[-1])),
        total_fees=Decimal(str(result.total_fees)),
        pending=result.num_unfilled,
        filled_orders=filled_orders,
        num_unfilled=result.num_unfilled,
    )


def _position_from_end(record: dict[str, Any]) -> Position:
    """Rebuild a minimal ``Position`` from a backtest ``end_positions`` row.

    The backtest reports its terminal book as plain dicts (float-quantised),
    while the live path hands back real ``Position`` objects. Both are reduced
    to Decimals here so the terminal comparison is apples-to-apples; the
    backtest row is the oracle for quantity/avg_price/realized.
    """
    return Position(
        instrument=INSTRUMENT,
        quantity=Quantity(value=Decimal(str(record["quantity"]))),
        avg_price=Price(value=Decimal(str(record["avg_price"]))),
        realized_pnl=Money(amount=Decimal(str(record["realized_pnl"])), currency="INR"),
        unrealized_pnl=Money(amount=Decimal(0), currency="INR"),
    )


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------


def _tolerance(scale: Decimal) -> Decimal:
    """0.05% of *scale*, or exact when there is no P&L to be relative to."""
    magnitude = abs(scale)
    return PNL_TOLERANCE * magnitude if magnitude > 0 else Decimal(0)


def _fill_rows(run: _Run) -> list[dict[str, object]]:
    """Deterministic economic fill rows (no order ids, no timestamps)."""
    return list(execution_projection(run.fills, run.positions)["fills"])


def _unmatched_report(bt: _Run, live: _Run) -> str:
    """Describe every fill present on one path but not the other, with a reason.

    Used only on failure: the two paths are expected to produce the SAME
    ordered fill stream, so any index where they differ is reported with both
    orders and a structural reason rather than a bare inequality.
    """
    bt_rows = _fill_rows(bt)
    live_rows = _fill_rows(live)
    lines: list[str] = []
    for index in range(max(len(bt_rows), len(live_rows))):
        b = bt_rows[index] if index < len(bt_rows) else None
        lv = live_rows[index] if index < len(live_rows) else None
        if b == lv:
            continue
        if b is None:
            lines.append(
                f"  fill #{index}: backtest has none, live={lv} "
                f"(reason: the live path booked a fill the backtest did not)"
            )
        elif lv is None:
            lines.append(
                f"  fill #{index}: live has none, backtest={b} "
                f"(reason: the backtest booked a fill the live path did not)"
            )
        else:
            lines.append(
                f"  fill #{index}: backtest={b} live={lv} "
                f"(reason: the two paths filled a different order at this step)"
            )
    if not lines:
        lines.append("  (fill streams match element-wise; a count/ordering issue)")
    return "\n".join(lines)


def _assert_fill_parity(bt: _Run, live: _Run) -> None:
    """The two paths must book the same ordered fills (side/qty/price)."""
    bt_rows = _fill_rows(bt)
    live_rows = _fill_rows(live)
    if bt_rows != live_rows:
        raise AssertionError(
            "fill streams differ between backtest and live:\n"
            + _unmatched_report(bt, live)
        )


def _assert_round_trip_parity(bt: _Run, live: _Run) -> None:
    """Same round trips, and the same net P&L within 0.05% of the oracle's.

    The backtest is the oracle. Comparison is on count, the (side, quantity)
    sequence, and the summed net (the fee-invariant, price-derived part — both
    runs book identical fills, so fees cancel; fee parity is asserted directly
    in its own case).
    """
    bt_trades = bt.round_trips()
    live_trades = live.round_trips()
    assert len(bt_trades) == len(live_trades), (
        f"round-trip count differs: backtest={len(bt_trades)} "
        f"live={len(live_trades)}\n{_unmatched_report(bt, live)}"
    )
    bt_seq = [(t.side, t.quantity) for t in bt_trades]
    live_seq = [(t.side, t.quantity) for t in live_trades]
    assert bt_seq == live_seq, (
        f"round-trip (side, quantity) sequences differ:\n"
        f"  backtest={bt_seq}\n  live={live_seq}"
    )
    bt_net = sum((t.net_pnl for t in bt_trades), Decimal(0))
    live_net = sum((t.net_pnl for t in live_trades), Decimal(0))
    tolerance = _tolerance(bt_net)
    assert abs(live_net - bt_net) <= tolerance, (
        f"round-trip net P&L differs beyond {PNL_TOLERANCE} of the oracle: "
        f"backtest={bt_net} live={live_net} tolerance={tolerance}"
    )


# ---------------------------------------------------------------------------
# The eight cases
# ---------------------------------------------------------------------------


def test_tape_and_bars_are_the_same_session(
    ticks: tuple[RecordedTick, ...], bars: tuple[Candle, ...],
) -> None:
    """The bars the parity runs consume are exactly the recorded tape.

    Reconciliation is exact, not approximate: the number of bars equals the
    number of distinct minutes in the tape, and each bar's OHLC equals the
    first/high/low/last of the ticks recorded in that minute in FILE order —
    which is what proves the aggregate, the duplicate print and the
    out-of-order tick all survived into the bars both paths are fed.
    """
    by_minute: dict[datetime, list[RecordedTick]] = {}
    for tick in ticks:
        by_minute.setdefault(_bucket_minute(tick.ts_ist), []).append(tick)

    assert len(bars) == len(by_minute) == 9

    for candle, minute in zip(bars, sorted(by_minute), strict=True):
        minute_ticks = by_minute[minute]
        prices = [t.price for t in minute_ticks]
        assert candle.ohlc.open.value == prices[0], f"open {minute}"
        assert candle.ohlc.high.value == max(prices), f"high {minute}"
        assert candle.ohlc.low.value == min(prices), f"low {minute}"
        assert candle.ohlc.close.value == prices[-1], f"close {minute}"
        # The bar's clock is the minute's start, in the chart's UTC convention.
        assert candle.timestamp == datetime(
            minute.year, minute.month, minute.day, minute.hour, minute.minute,
            tzinfo=IST,
        ).astimezone(ZoneInfo("UTC"))


def test_both_paths_reach_the_same_terminal_position(
    bt_run: _Run, live: _Run,
) -> None:
    """The headline: identical terminal position on both paths.

    Quantity is exact, avg_price within one paisa, and realized P&L within
    0.05% of the oracle's absolute net. The session deliberately ends NON-flat
    (long 4 @ ~107.5), so this is a real open-lot comparison, not a flat run
    where every number is trivially zero.
    """
    bt_pos = bt_run.position_for(INSTRUMENT)
    live_pos = live.position_for(INSTRUMENT)
    assert bt_pos is not None and live_pos is not None, "both paths hold a position"

    assert live_pos.quantity.value == bt_pos.quantity.value, (
        f"terminal quantity differs: backtest={bt_pos.quantity.value} "
        f"live={live_pos.quantity.value}"
    )
    avg_delta = abs(live_pos.avg_price.value - bt_pos.avg_price.value)
    assert avg_delta <= Decimal("0.01"), (
        f"terminal avg_price differs by {avg_delta} (> 1 paisa): "
        f"backtest={bt_pos.avg_price.value} live={live_pos.avg_price.value}"
    )
    scale = bt_run.net()
    realized_tolerance = _tolerance(scale)
    realized_delta = abs(
        live_pos.realized_pnl.amount - bt_pos.realized_pnl.amount
    )
    assert realized_delta <= realized_tolerance, (
        f"terminal realized_pnl differs by {realized_delta} "
        f"(> {PNL_TOLERANCE} of net {scale}): "
        f"backtest={bt_pos.realized_pnl.amount} live={live_pos.realized_pnl.amount}"
    )
    # Non-flat: this comparison is not vacuously satisfied by an all-zero book.
    assert bt_pos.quantity.value != 0


def test_both_paths_pair_into_the_same_round_trips(
    bt_run: _Run, live: _Run,
) -> None:
    """FIFO round trips agree in count, (side, quantity) sequence and net."""
    _assert_fill_parity(bt_run, live)
    _assert_round_trip_parity(bt_run, live)


def test_order_counts_match_and_unmatched_orders_are_listed(
    bt_run: _Run, live: _Run,
) -> None:
    """The same number of orders filled on both paths, and any mismatch is named.

    The two modes legitimately differ in order ids and in how a venue's
    partials arrive, so the comparison is on COUNT plus the economic fill
    stream. When they do not match, every unmatched order is reported with a
    reason so a parity failure is diagnosable rather than a bare inequality.
    """
    if bt_run.filled_orders != live.filled_orders or _fill_rows(bt_run) != _fill_rows(live):
        raise AssertionError(
            "filled-order streams differ between backtest and live:\n"
            f"  filled orders: backtest={bt_run.filled_orders} "
            f"live={live.filled_orders}\n"
            + _unmatched_report(bt_run, live)
        )
    assert bt_run.filled_orders == live.filled_orders
    assert len(bt_run.fills) == len(live.fills)


def test_both_paths_actually_traded(bt_run: _Run, live: _Run) -> None:
    """Guard against a vacuous all-empty pass.

    The suite proves nothing if both paths did nothing, so this asserts the
    session produced multiple round trips with a non-zero net P&L on BOTH
    paths, and a terminal position — the properties every other assertion in
    the file leans on.
    """
    for run in (bt_run, live):
        trades = run.round_trips()
        assert len(trades) >= 2, f"{run.label}: only {len(trades)} round trips"
        assert run.net() != 0, f"{run.label}: net P&L is zero"
        assert run.filled_orders >= 2, f"{run.label}: only {run.filled_orders} fills"
    # The nets are equal by construction here, but stating it keeps this case
    # from being satisfied by two runs that each traded but disagreed.
    assert bt_run.net() == live.net()


def test_fees_are_charged_on_both_paths(bt_run: _Run, live: _Run) -> None:
    """Both paths charge non-zero fees, and charge the SAME fees.

    A run that silently charged zero would make every P&L equality vacuous, so
    the suite refuses that. Both runs use the same ``FeeCalculator`` over the
    same fills, so the totals are equal to the paisa; the per-order cost is not
    compared because the two modes book it against different order ids.
    """
    assert bt_run.total_fees > 0, "backtest charged no fees — comparisons are vacuous"
    assert live.total_fees > 0, "live path charged no fees — comparisons are vacuous"
    fee_delta = abs(live.total_fees - bt_run.total_fees)
    assert fee_delta <= Decimal("0.01"), (
        f"total fees differ by {fee_delta}: "
        f"backtest={bt_run.total_fees} live={live.total_fees}"
    )


def test_neither_path_leaves_a_signal_unfilled_unexplained(
    bt_run: _Run, live: _Run,
) -> None:
    """Both paths drop exactly the same last-bar signal, and report it.

    A signal decided on the final bar has no next bar to fill against on
    EITHER path, so the expected unfilled count is one shared dropped signal —
    NOT zero. The assertion is that the backtest's count equals the reactive
    engine's still-pending queue length, proving neither path filled against a
    price the strategy could not have known and neither silently swallowed one.
    """
    assert bt_run.num_unfilled == live.pending, (
        f"unfilled-signal counts differ: backtest={bt_run.num_unfilled} "
        f"live pending={live.pending}"
    )
    # Exactly one: the bar-9 signal, present in PLAN and unfillable on both.
    assert bt_run.num_unfilled == 1


def test_wrong_slippage_constant_breaks_the_pnl_tolerance(
    bars: tuple[Candle, ...], sizer: PositionSizer, live: _Run,
) -> None:
    """NEGATIVE CONTROL: the 0.05% P&L tolerance is sensitive to a real error.

    A wrong slippage model is applied to the BACKTEST ONLY, leaving the live
    path unchanged. A suite that passes for any two runs would let a 1%-slipped
    backtest sail through; this proves the two comparisons the suite rests on
    both reject it:

    * the round-trip net P&L tolerance (0.05% of the oracle's net), and
    * the exact economic fill stream.

    The wrong constant is 1% — a hundred times the order of magnitude a real
    slippage model would use — and it moves the oracle's net P&L from a profit
    to a loss, so the divergence it produces is unambiguous rather than a
    near-miss.
    """
    wrong_slippage = PercentageSlippageModel(pct=Decimal("0.01"))  # 100x too big
    slipped_result, _ = _run_backtest(bars, sizer, slippage_model=wrong_slippage)
    slipped = _Run(
        label="backtest(slipped)",
        bars=bars,
        fills=tuple(slipped_result.fill_records),
        positions=tuple(
            _position_from_end(record) for record in slipped_result.end_positions
        ),
        cash=Decimal(0),
        total_fees=Decimal(str(slipped_result.total_fees)),
        pending=slipped_result.num_unfilled,
        filled_orders=len(slipped_result.fill_records),
        num_unfilled=slipped_result.num_unfilled,
    )

    # (a) The round-trip net P&L falls outside the 0.05% tolerance that test 3
    #     holds the two real paths to. Assert the tolerance is EXCEEDED.
    tolerance = _tolerance(slipped.net())
    divergence = abs(live.net() - slipped.net())
    assert divergence > tolerance, (
        f"a 1% slippage must push net P&L outside the {PNL_TOLERANCE} tolerance, "
        f"but live={live.net()} slipped={slipped.net()} tolerance={tolerance}"
    )
    # And the round-trip comparison itself must reject the two runs.
    with pytest.raises(AssertionError):
        _assert_round_trip_parity(slipped, live)

    # (b) The exact economic fill stream also differs (the slipped fills are
    #     priced off the market, the live fills off the bar open).
    with pytest.raises(AssertionError):
        _assert_fill_parity(slipped, live)
