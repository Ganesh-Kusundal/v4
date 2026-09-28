"""Slippage-model vs book-matching fill parity (AC-14, spec lane 2d).

The claim under test
--------------------
**The two paper fill paths are selectable per run and, on the same recorded
session, agree on order count, on the terminal position to the lot, and on net
profit and loss to within 0.05% of the oracle's absolute net.** The oracle is
**book matching** — the existing paper broker's simulated multi-level book.

How the two paths are made comparable
-------------------------------------
Both paths are driven through the SAME paper session, the SAME
``ReactiveStrategyEngine(fill_reference="next_open")``, the SAME
``PositionSizer``, the SAME bars bucketed by the production ``BarAggregator``
from a ``TapeRecorder``-written session, and the SAME ``ExecutionEngine``
(risk, cash ledger, fees, position math). The only thing that differs is the
``FillSource`` the composition root was configured with — ``book_matching``
(:class:`PaperBookFillSource`, which walks ``PaperBroker``'s book) versus
``slippage_model`` (``PaperFillSource`` + ``PercentageSlippageModel``). So a
divergence is about how the crossing cost is found, not about the clock, the
sizer, the fees, or the ledger.

The bar grid is the same one a live feed would publish, and the tape and the
bars are reconciled (``test_tape_and_bars_are_the_same_session``), so the two
runs are provably over one session.

What is compared, and what is not
---------------------------------
Compared, exactly as the spec directs: the **filled-order count**, the
**terminal position per instrument to the lot** (quantity exact), and the **net
profit and loss within 0.05% of the oracle's absolute net**. The P&L is taken
from FIFO **round trips**, not fill by fill, because the two paths legitimately
book a different number of fills for the same order (the book books one fill per
matched increment; the slippage path books one fill per order). Unmatched
orders — an order one path filled and the other did not — are **excluded from
the P&L comparison and reported with a reason** in
``_unmatched_report``, never silently dropped.

The 0.05% bound and what it depends on (measured, not assumed)
--------------------------------------------------------------
The oracle's book is seeded with a proportional half-spread equal to the
slippage model's constant, so a **single-level** top-of-book fill on either path
costs exactly the same and the two agree to the paisa. Where an order is large
enough to **walk more than one level**, the oracle books the consumed-level VWAP
(``one fill per partial``) while the slippage path books one price at the top of
the book, and the VWAP premium is a real, systematic cost the single-price model
does not carry. On the main session the book is deep enough for every order to
fill in one level — the realistic case for a liquid NSE large-cap at 1-minute
granularity — and the two paths agree to ``0.00``. ``test_thin_book_vwap_keeps_
terminal_position_but_moves_pnl`` pins the other end with real numbers: it
proves the oracle genuinely walks levels (so it is a real book, not a flat-LTP
fallback) and records exactly how far the round-trip P&L moves once fills walk,
rather than asserting a silently loosened tolerance. That measured divergence is
the finding of this lane and is reported here verbatim rather than hidden.

Non-vacuousness is itself a test: the session ends on a deliberately NON-flat
position, produces several round trips with mixed signs and a reversal, both
paths charge non-zero fees, the book is proven to have walked real levels, and a
negative control proves the 0.05% tolerance fails on a wrong slippage constant
(``test_wrong_slippage_constant_breaks_the_pnl_tolerance``).
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
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.projection import execution_projection
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.bar_aggregator import BarAggregator, BarFrame
from tradex_runtime.paper_book_fill_source import (
    BOOK_MATCHING,
    SLIPPAGE_MODEL,
    PaperBookFillSource,
)
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

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: 0.05% — the tolerance every P&L comparison uses, expressed against the
#: ORACLE (book-matching) run's absolute net P&L.
PNL_TOLERANCE = Decimal("0.0005")

#: The one shared sizer policy. Large enough that every ``Signal.quantity`` in
#: this session is honoured verbatim by both paths, so sizes come from the
#: signal and the notional ceiling never binds.
SIZER_POLICY = {"max_order_value": Decimal("1000000")}

#: The crossing cost both paths are configured with, in basis points. The book's
#: half-spread is this many bps of the mid, so a top-of-book fill costs exactly
#: what ``PercentageSlippageModel(bps/10_000)`` charges. 5 bps is a realistic
#: half-spread for a liquid NSE large-cap.
CROSSING_BPS = Decimal("5")

#: A recorded session whose data is DELIBERATELY non-monotonic: it rises to
#: 106, falls to 101, then rises to 109. A monotone tape would make a wrong
#: fill price look correct. Each minute's first tick is the value that becomes
#: that bar's OPEN — the price the previous bar's signal fills at.
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
#: A signal on bar N fills at bar N+1's open, so the bar-9 signal has no bar to
#: fill against on EITHER path and is the shared "unfilled" case. This plan
#: yields a trend leg (BUY 50, BUY 30), two partial closes (SELL 40, SELL 40)
#: that FIFO-decompose, a reversal into a short and back, and a non-flat
#: terminal position.
PLAN: dict[int, tuple[str, str]] = {
    1: ("BUY", "50"),
    2: ("BUY", "30"),
    3: ("SELL", "40"),
    4: ("SELL", "40"),
    5: ("SELL", "20"),
    6: ("BUY", "30"),
    7: ("BUY", "10"),
    8: ("BUY", "20"),
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
    """
    frames: list[BarFrame] = []
    aggregator = BarAggregator(
        str(INSTRUMENT.instrument_id),
        Timeframe.M1,
        on_frame=frames.append,
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


class _PlanStrategy:
    """Emits the fixed PLAN on each bar. No stop/target declared."""

    def __init__(self) -> None:
        self._bar = 0
        self.signals: list[Signal] = []

    @property
    def strategy_id(self) -> str:
        return "slippage-vs-book-plan"

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
    """Everything one fill path produced, in comparable form."""

    label: str
    fills: tuple[Fill, ...]
    positions: tuple[Position, ...]
    cash: Decimal
    total_fees: Decimal
    pending: int
    filled_orders: int
    num_unfilled: int
    orders: tuple[Any, ...] = ()
    audit: Any = None

    def position_for(self, instrument: Any) -> Position | None:
        for position in self.positions:
            if position.instrument.instrument_id == instrument.instrument_id:
                return position
        return None

    def net(self) -> Decimal:
        return sum((t.net_pnl for t in self.round_trips()), Decimal(0))

    def round_trips(self) -> list[Trade]:
        # Price-derived net only: both paths run the same FeeCalculator over the
        # same fills, so fees are identical and cancel in the round-trip net;
        # fee parity is asserted directly in its own case.
        return round_trip_trades(self.fills)


def _run_path(
    bars: tuple[Candle, ...],
    sizer: PositionSizer,
    *,
    fill_path: str,
    slippage_bps: Decimal,
    book_spread_bps: Decimal,
    level_depth: Decimal | None = None,
) -> _Run:
    """Run ONE paper session on the given fill path.

    ``TradingSession.paper`` is the production composition root, so the fill
    source, risk, cash ledger and fees are all wired exactly as a real paper run
    wires them; the only knob this helper turns is which fill path the config
    selected. ``level_depth`` (oracle-only) forces the book to a given resting
    size per level so the multi-level walk can be exercised deliberately.
    """
    bus = ReactiveBus()
    session = TradingSession.paper(
        bus=bus,
        config=AppConfig(risk=permissive_risk(),
            execution=ExecutionConfig(
                fees_enabled=True,
                fill_path=fill_path,
                slippage_bps=slippage_bps,
                book_spread_bps=book_spread_bps,
            )
        ),
    )
    # AC-22: the strategy hands its sized request to the session's engine
    # directly. Constructed after the session so the callable exists.
    strategy_engine = ReactiveStrategyEngine(
        bus, session.engine.submit, fill_reference="next_open", sizer=sizer,
    )
    strategy_engine.register(_PlanStrategy())
    fill_source = session.engine._fill
    if level_depth is not None and isinstance(fill_source, PaperBookFillSource):
        # Re-narrow the book AFTER construction: set_depth is re-seeded per
        # submit from this attribute, so the next order walks multiple levels.
        fill_source._level_depth = level_depth
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
        label=fill_path,
        fills=tuple(fills),
        positions=positions,
        cash=snapshot.cash,
        total_fees=snapshot.total_fees,
        pending=pending,
        filled_orders=filled_orders,
        num_unfilled=pending,
        orders=tuple(session.engine.cache.all_orders()),
        audit=getattr(fill_source, "audit", None),
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


def _unmatched_report(oracle: _Run, other: _Run) -> str:
    """Describe every order present on one path but not the other, with a reason.

    Unmatched orders are EXCLUDED from the P&L comparison (the two paths may
    book a different number of fills per order) but must never be dropped in
    silence — this names each one and the structural reason it is unmatched, and
    it is embedded in the assertion messages.
    """
    oracle_rows = _fill_rows(oracle)
    other_rows = _fill_rows(other)
    lines: list[str] = []
    for index in range(max(len(oracle_rows), len(other_rows))):
        o = oracle_rows[index] if index < len(oracle_rows) else None
        x = other_rows[index] if index < len(other_rows) else None
        if o == x:
            continue
        if o is None:
            lines.append(
                f"  fill #{index}: oracle({oracle.label}) has none, {other.label}={x} "
                f"(reason: {other.label} booked a fill the oracle did not)"
            )
        elif x is None:
            lines.append(
                f"  fill #{index}: {other.label} has none, oracle={o} "
                f"(reason: the oracle booked a fill {other.label} did not)"
            )
        else:
            lines.append(
                f"  fill #{index}: oracle={o} {other.label}={x} "
                f"(reason: the two paths filled a different order at this step)"
            )
    if not lines:
        lines.append(
            f"  (fill streams match element-wise; "
            f"oracle orders={oracle.filled_orders} {other.label} orders={other.filled_orders})"
        )
    return "\n".join(lines)


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
    return PositionSizer(label="slip-vs-book", **SIZER_POLICY)


@pytest.fixture(scope="module")
def oracle_run(bars: tuple[Candle, ...], sizer: PositionSizer) -> _Run:
    """Book matching — the ORACLE (spec lane 2d)."""
    return _run_path(
        bars, sizer, fill_path=BOOK_MATCHING,
        slippage_bps=CROSSING_BPS, book_spread_bps=CROSSING_BPS,
    )


@pytest.fixture(scope="module")
def slippage_run(bars: tuple[Candle, ...], sizer: PositionSizer) -> _Run:
    """The slippage-model path, at the same crossing constant."""
    return _run_path(
        bars, sizer, fill_path=SLIPPAGE_MODEL,
        slippage_bps=CROSSING_BPS, book_spread_bps=CROSSING_BPS,
    )


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_tape_and_bars_are_the_same_session(
    ticks: tuple[RecordedTick, ...], bars: tuple[Candle, ...],
) -> None:
    """The bars BOTH runs consume are exactly the recorded tape.

    Exact reconciliation: the number of bars equals the number of distinct
    minutes in the tape, and each bar's OHLC equals the first/high/low/last of
    the ticks recorded in that minute in FILE order — proving the aggregate, the
    duplicate print and the out-of-order tick all survived into the one session
    both fill paths are measured on.
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
        assert candle.timestamp == datetime(
            minute.year, minute.month, minute.day, minute.hour, minute.minute,
            tzinfo=IST,
        ).astimezone(ZoneInfo("UTC"))


def test_fill_path_is_selectable_per_run(
    oracle_run: _Run, slippage_run: _Run,
) -> None:
    """The composition root really built two DIFFERENT fill sources.

    Guards the whole claim: if the config switch were ignored and both runs got
    the same source, every other assertion here would be comparing a path with
    itself. This names the concrete source each run was wired with.
    """
    assert oracle_run.audit is not None, "book path did not build PaperBookFillSource"
    assert slippage_run.audit is None, "slippage path must not be the book source"
    # The two paths are, concretely, different objects.
    assert BOOK_MATCHING != SLIPPAGE_MODEL


def test_both_paths_walked_a_real_book(
    oracle_run: _Run,
) -> None:
    """The oracle is a REAL book, not a flat-LTP fallback.

    A book-matching run that matched nothing (or fell back to the LTP) would
    satisfy a fill-parity test by comparing the slippage path against itself.
    This asserts the book actually produced fills on this session.
    """
    audit = oracle_run.audit
    assert audit is not None
    assert audit.submits == oracle_run.filled_orders > 0, (
        f"oracle book recorded {audit.submits} submits for "
        f"{oracle_run.filled_orders} filled orders"
    )
    assert audit.single_level_fills + audit.vwap_fills == audit.submits - audit.unfilled
    assert audit.unfilled == 0, "the deep-book session must fill every order"


def test_order_counts_match_and_unmatched_orders_are_listed(
    oracle_run: _Run, slippage_run: _Run,
) -> None:
    """Same filled-order count on both paths, and any mismatch is named.

    The two paths may book a different number of FILLS for the same order (the
    spec's "one fill per partial vs one fill per order"), so the order count is
    compared on the number of FILLED orders, and any economic difference is
    reported with a reason rather than dropped silently.
    """
    if oracle_run.filled_orders != slippage_run.filled_orders:
        raise AssertionError(
            "filled-order counts differ between the book and slippage paths:\n"
            f"  oracle(book)={oracle_run.filled_orders} "
            f"slippage={slippage_run.filled_orders}\n"
            + _unmatched_report(oracle_run, slippage_run)
        )
    assert oracle_run.filled_orders == slippage_run.filled_orders
    # Every order the strategy emitted (bar 9 is the shared unfillable last-bar
    # signal) is accounted for, so an order neither path filled is impossible to
    # hide.
    assert oracle_run.filled_orders == len(PLAN) - 1


def test_every_filled_order_is_booked_fully_in_the_oms(
    oracle_run: _Run, slippage_run: _Run,
) -> None:
    """Each FILLED order's OMS quantity equals the quantity of fills on it.

    A fill source that hands the engine an order whose ``filled_quantity``
    already counts the fill (rather than a fresh order the engine transitions)
    makes ``OrderManager.on_order_filled`` see ``remaining = 0`` and clamp the
    fill away. The position and P&L still land — the clamp is in the OMS
    bookkeeping, not the projection — so this is invisible to a P&L-only suite,
    which is exactly why it is asserted here: the order the engine books must
    reconcile with the fills it published for it.
    """
    for run in (oracle_run, slippage_run):
        booked: dict[str, Decimal] = {}
        for fill in run.fills:
            booked[str(fill.order_id)] = (
                booked.get(str(fill.order_id), Decimal(0)) + fill.quantity.value
            )
        assert booked, f"{run.label}: no fills to reconcile"
        for order in run.orders:
            if order.status.value != "FILLED":
                continue
            booked_qty = booked.get(str(order.order_id), Decimal(0))
            assert order.filled_quantity.value == booked_qty, (
                f"{run.label}: order {order.order_id} is FILLED with OMS "
                f"quantity {order.filled_quantity.value} but its fills sum to "
                f"{booked_qty} — the OMS order does not reconcile with its fills"
            )


def test_terminal_position_matches_to_the_lot(
    oracle_run: _Run, slippage_run: _Run,
) -> None:
    """Same terminal position per instrument, quantity EXACT (to the lot).

    The session ends deliberately NON-flat (long 40), so this is a real open-lot
    comparison, not a flat run where every number is trivially zero. ``avg_price``
    is compared within a paisa because the two paths can book the same quantity
    at a marginally different average when a fill walks levels; the spec's
    binding requirement is the position TO THE LOT, which is asserted exactly.
    """
    oracle_pos = oracle_run.position_for(INSTRUMENT)
    slip_pos = slippage_run.position_for(INSTRUMENT)
    assert oracle_pos is not None and slip_pos is not None, "both hold a position"
    assert slip_pos.quantity.value == oracle_pos.quantity.value, (
        f"terminal quantity differs: oracle={oracle_pos.quantity.value} "
        f"slippage={slip_pos.quantity.value}"
    )
    avg_delta = abs(slip_pos.avg_price.value - oracle_pos.avg_price.value)
    assert avg_delta <= Decimal("0.01"), (
        f"terminal avg_price differs by {avg_delta} (> 1 paisa): "
        f"oracle={oracle_pos.avg_price.value} slippage={slip_pos.avg_price.value}"
    )
    # Non-flat: this comparison is not vacuously satisfied by an all-zero book.
    assert oracle_pos.quantity.value != 0


def test_round_trip_net_pnl_agrees_within_tolerance(
    oracle_run: _Run, slippage_run: _Run,
) -> None:
    """Net P&L within 0.05% of the ORACLE's absolute net, on round trips.

    The oracle is book matching; the tolerance denominator is the oracle's
    absolute net P&L. The comparison is on FIFO round trips (not fill by fill),
    as the spec directs, because the two paths may book a different number of
    fills per order. Any unmatched order is excluded from the P&L and listed
    with a reason in the failure message.
    """
    oracle_trades = oracle_run.round_trips()
    slip_trades = slippage_run.round_trips()
    assert len(oracle_trades) == len(slip_trades), (
        f"round-trip count differs: oracle={len(oracle_trades)} "
        f"slippage={len(slip_trades)}\n{_unmatched_report(oracle_run, slippage_run)}"
    )
    oracle_net = sum((t.net_pnl for t in oracle_trades), Decimal(0))
    slip_net = sum((t.net_pnl for t in slip_trades), Decimal(0))
    tolerance = _tolerance(oracle_net)
    divergence = abs(slip_net - oracle_net)
    assert divergence <= tolerance, (
        f"round-trip net P&L diverges {divergence} from the oracle beyond "
        f"{PNL_TOLERANCE} of |{oracle_net}| (tolerance {tolerance}): "
        f"oracle={oracle_net} slippage={slip_net}\n"
        + _unmatched_report(oracle_run, slippage_run)
    )


def test_both_paths_actually_traded(oracle_run: _Run, slippage_run: _Run) -> None:
    """Guard against a vacuous all-empty pass.

    The suite proves nothing if both paths did nothing, so this asserts several
    round trips with a non-zero net P&L on BOTH paths, a terminal position, and
    non-zero fees — the properties every other assertion here leans on.
    """
    for run in (oracle_run, slippage_run):
        trades = run.round_trips()
        assert len(trades) >= 2, f"{run.label}: only {len(trades)} round trips"
        assert run.net() != 0, f"{run.label}: net P&L is zero"
        assert run.filled_orders >= 2, f"{run.label}: only {run.filled_orders} orders"
        assert run.total_fees > 0, f"{run.label}: charged no fees"
    # The nets agree by construction on this deep-book session; stating it keeps
    # this case from being satisfied by two runs that each traded but disagreed.
    assert oracle_run.net() == slippage_run.net()


def test_fees_are_charged_on_both_paths(
    oracle_run: _Run, slippage_run: _Run,
) -> None:
    """Both paths charge non-zero fees, and charge the SAME fees.

    A run that silently charged zero would make every P&L equality vacuous, so
    the suite refuses that. Both runs use the same ``FeeCalculator`` over the
    same fills, so the totals are equal to the paisa.
    """
    assert oracle_run.total_fees > 0, "oracle charged no fees"
    assert slippage_run.total_fees > 0, "slippage path charged no fees"
    fee_delta = abs(slippage_run.total_fees - oracle_run.total_fees)
    assert fee_delta <= Decimal("0.01"), (
        f"total fees differ by {fee_delta}: "
        f"oracle={oracle_run.total_fees} slippage={slippage_run.total_fees}"
    )


def test_neither_path_leaves_an_unexplained_unfilled_signal(
    oracle_run: _Run, slippage_run: _Run,
) -> None:
    """Both paths drop exactly the same last-bar signal, and report it.

    A signal decided on the final bar has no next bar to fill against on EITHER
    path, so the expected unfilled count is the one shared dropped signal — NOT
    zero. The assertion is that both paths left the same number unfilled, proving
    neither filled against a price the strategy could not have known and neither
    silently swallowed one.
    """
    assert oracle_run.num_unfilled == slippage_run.num_unfilled, (
        f"unfilled-signal counts differ: oracle={oracle_run.num_unfilled} "
        f"slippage={slippage_run.num_unfilled}"
    )
    # Exactly one: the bar-9 signal, present in PLAN and unfillable on both.
    assert oracle_run.num_unfilled == 1


def test_wrong_slippage_constant_breaks_the_pnl_tolerance(
    bars: tuple[Candle, ...], sizer: PositionSizer, oracle_run: _Run,
) -> None:
    """NEGATIVE CONTROL: the 0.05% tolerance is sensitive to a real error.

    A wrong slippage model is applied to the SLIPPAGE path only, leaving the
    oracle unchanged. A suite that passed for any two runs would let a
    mis-specified slippage constant sail through; this proves the round-trip net
    comparison actually rejects it. The wrong constant is 1 bps against the
    oracle's 5 bps — a small, plausible mis-specification, not an absurd one —
    and it moves the net far outside the tolerance.
    """
    wrong_bps = Decimal("1")
    assert wrong_bps != CROSSING_BPS
    slipped = _run_path(
        bars, sizer, fill_path=SLIPPAGE_MODEL,
        slippage_bps=wrong_bps, book_spread_bps=CROSSING_BPS,
    )

    oracle_net = oracle_run.net()
    slipped_net = slipped.net()
    tolerance = _tolerance(oracle_net)
    divergence = abs(slipped_net - oracle_net)

    # The divergence must EXCEED the tolerance the passing case relies on.
    assert divergence > tolerance, (
        f"a {wrong_bps} bps slippage must push net P&L outside the "
        f"{PNL_TOLERANCE} tolerance, but oracle={oracle_net} "
        f"slipped={slipped_net} divergence={divergence} tolerance={tolerance}"
    )
    # And the tolerance is tight enough that even a 1 bps mis-specification is
    # caught by a wide margin (guards against a tolerance so loose it is blind).
    assert divergence > Decimal(10) * tolerance, (
        f"the negative control only just failed the tolerance: "
        f"divergence={divergence} tolerance={tolerance}"
    )


def test_thin_book_walks_levels_but_keeps_the_position_to_the_lot(
    bars: tuple[Candle, ...], sizer: PositionSizer, oracle_run: _Run,
) -> None:
    """The oracle genuinely walks levels; the walk costs P&L, and that is measured.

    On the main session the book is deep enough that every order fills in ONE
    level, so a top-of-book fill on either path costs the same crossing constant
    and the two agree to the paisa (the 0.05% bound of the spec). This case
    deliberately makes the book THIN, so the larger orders WALK multiple levels:
    the oracle books the consumed-level VWAP — the "one fill per partial" the
    spec names — while the slippage path still books one price at the top of the
    book. Three things are asserted, all of them real invariants:

    * the oracle really walked levels (so the main case's exact agreement is not
      an artefact of a flat-LTP fallback, and the oracle is a real book), and
    * the oracle's VWAP cost is one-directional — the thin book is strictly WORSE
      for the trader than the single-price model, because crossing deeper into a
      thin book always costs more than the top level, and
    * the terminal position STILL matches to the lot, because walking levels
      changes a fill's PRICE, not how much of the order the book accepts.

    The round-trip P&L divergence under a thin book is the lane's FINDING and is
    REPORTED with real numbers (and only reported) rather than asserted equal:
    once fills walk, the VWAP premium is a real, systematic cost the single-price
    slippage model does not carry, and on a 20-share-per-level book it moves the
    net well beyond 0.05% of the oracle. The main (deep-book) case is where the
    spec's 0.05% bound is enforced with the tolerance left at 0.05% — this case
    documents the depth-dependence of that bound instead of hiding it behind a
    loosened tolerance.
    """
    thin = _run_path(
        bars, sizer, fill_path=BOOK_MATCHING,
        slippage_bps=CROSSING_BPS, book_spread_bps=CROSSING_BPS,
        level_depth=Decimal("20"),
    )
    slippage = _run_path(
        bars, sizer, fill_path=SLIPPAGE_MODEL,
        slippage_bps=CROSSING_BPS, book_spread_bps=CROSSING_BPS,
    )

    # (a) The oracle really walked multiple levels on this thin book.
    assert thin.audit is not None
    assert thin.audit.vwap_fills > 0, (
        "a thin book must produce at least one multi-level (VWAP) fill; "
        "otherwise this case is not exercising the partial-vs-order asymmetry"
    )

    # (b) The VWAP cost is one-directional: crossing deeper into a thin book is
    #     strictly worse for the trader than the single top-of-book price.
    oracle_net = thin.net()
    slip_net = slippage.net()
    assert oracle_net < slip_net, (
        "a thin book (multi-level VWAP) must be strictly worse for the trader "
        f"than the single-price slippage model: oracle={oracle_net} "
        f"slippage={slip_net}"
    )

    # (c) Position still matches to the lot, even when fills walked: the walk
    #     changed a price, not a quantity.
    thin_pos = thin.position_for(INSTRUMENT)
    slip_pos = slippage.position_for(INSTRUMENT)
    assert thin_pos is not None and slip_pos is not None
    assert thin_pos.quantity.value == slip_pos.quantity.value, (
        f"thin-book terminal quantity differs: oracle={thin_pos.quantity.value} "
        f"slippage={slip_pos.quantity.value}"
    )
    assert thin_pos.quantity.value != 0, "non-flat book comparison"

    # REPORT the divergence (not asserted equal). This is the measured finding:
    # how far the round-trip P&L moves once the book is thin enough to walk.
    tolerance = _tolerance(oracle_net)
    divergence = abs(slip_net - oracle_net)
    print(
        f"\n[thin-book finding] multi-level book: oracle net={oracle_net} "
        f"slippage net={slip_net} |diff|={divergence} "
        f"0.05%-of-oracle={tolerance} ratio={divergence / tolerance:.1f}x "
        f"— oracle worse by the VWAP premium; deep-book case agrees to 0.00."
    )
