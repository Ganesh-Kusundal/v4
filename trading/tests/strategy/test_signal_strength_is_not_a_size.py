"""``Signal.strength`` survives as a field and must never come back as a size.

The data model forbids removing ``Signal.strength``, so it stays — documented as
signal *quality* and used for logging and ranking. That is exactly what makes
the bug dangerous the second time round: the field is present, plausible, and
typed as a number, so ``abs(signal.strength)`` compiles, reads as obvious
sizing code, and every shipped strategy (all of which emit ``strength=1.0``)
quietly trades one share.

Discouraging that is not enough, so this file does two things:

1. **A behavioural proof.** The size a signal gets is identical across a
   thousand-fold range of ``strength``. It fails the moment *any* code path
   reads ``strength`` as a size, whatever the reason and whatever the allowlist
   below says, because it never inspects source.
2. **A tree-wide gate.** No module on the Signal→Order path may *read*
   ``.strength``. Declaring it (``Signal(strength=1.0, …)``) and logging it are
   fine; turning it into a ``Quantity`` is not. A surviving field that still
   looks like a size is how this bug comes back, so the read has to fail a test
   rather than a review comment.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_domain import OrderSide, Quote, Signal
from tradex_domain.enums import AssetClass, ExchangeId
from tradex_domain.instruments import Equity, InstrumentId
from tradex_domain.value_objects import Price, Quantity
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.sizing import PositionSizer

from strategy._ac22_recorder import RecordingSubmit

REPO = Path(__file__).resolve().parents[3]  # <repo>/trading/tests/strategy/this_file.py

#: Packages that turn a ``Signal`` into an ``OrderRequest``. Every one of them is
#: a place the old one-liner could be retyped, so the gate below walks all of
#: them rather than the single file the bug was found in.
SIZING_SOURCES = sorted(
    path
    for package in ("strategy/src", "replay/src", "execution/src", "application/src")
    for path in (REPO / package).rglob("*.py")
    if "__pycache__" not in path.parts
)

#: The one file that still read ``signal.strength`` as a size, and the debt the
#: strict xfail below used to carry. **Empty, and the entry was paid in full**:
#: the backtest now builds its recording-only bridge's quantity from the same
#: ``PositionSizer`` its ``next_open`` queue uses. The dict is kept rather than
#: deleted because the test below is written against it — an allowance list with
#: no test of its own is how the next one gets added without a thought, and the
#: exemption set is subtracted from the gate's findings, so a stale entry here
#: would silently un-gate a file.
KNOWN_STRENGTH_AS_SIZE: dict[str, str] = {}

#: The file that carried the debt, asserted directly. ``test_no_module_on_the_
#: signal_to_order_path_reads_strength`` above would catch the read too, but it
#: reports it as one line among many; naming the file is what tells a reader
#: *which* debt was just paid.
RETIRED_STRENGTH_AS_SIZE: str = "replay/src/tradex_replay/backtest.py"

#: Files allowed to *declare* a strength (they construct ``Signal(strength=…)``).
#: A new entry is a deliberate act, not a side effect of adding a strategy.
STRENGTH_EMITTERS = {
    "strategy/src/tradex_strategy/core/buy_and_hold.py",
    "strategy/src/tradex_strategy/extensions/strategies/bracket_breakout.py",
    "strategy/src/tradex_strategy/extensions/strategies/mean_reversion.py",
    "strategy/src/tradex_strategy/extensions/strategies/multi_symbol_sma_cross.py",
    "strategy/src/tradex_strategy/extensions/strategies/sma_cross.py",
    "strategy/src/tradex_strategy/extensions/strategies/study_base.py",
}

_INSTRUMENT = Equity(
    instrument_id=InstrumentId.equity("NSE", "RELIANCE"),
    symbol="RELIANCE",
    exchange=ExchangeId.NSE,
    asset_class=AssetClass.EQUITY,
    lot_size=Decimal("1"),
    tick_size=Decimal("0.05"),
)

_SIZER = PositionSizer(
    max_order_value=Decimal("2000000"),
    risk_fraction=Decimal("0.01"),
    equity=Decimal("1000000"),
)


def _relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _strength_reads(path: Path) -> list[str]:
    """Every ``<something>.strength`` *read* in *path*, as ``file:line``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{_relative(path)}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "strength"
        and isinstance(node.ctx, ast.Load)
    ]


def _strength_declarations(path: Path) -> int:
    """How many ``Signal(...)`` constructions in *path* pass ``strength=``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "Signal" and any(kw.arg == "strength" for kw in node.keywords):
            found += 1
    return found


def _signal(strength: float) -> Signal:
    return Signal(
        instrument=_INSTRUMENT,
        direction=OrderSide.BUY,
        strength=strength,
        reason="gate",
        metadata={"stop_loss_price": Decimal("2475"), "target_price": Decimal("2550")},
    )


# ---------------------------------------------------------------------------
# 1. behavioural proof — strength cannot move a size
# ---------------------------------------------------------------------------


def test_strength_cannot_move_the_size_by_a_single_unit() -> None:
    sizer_sizes = {
        _SIZER.size(_signal(value), reference_price=Decimal("2500")).quantity
        for value in (1.0, 0.0, -3.0, 3.5, 1000.0)
    }
    assert sizer_sizes == {Quantity(Decimal("400"))}

    order_sizes = set()
    for value in (1.0, 0.0, -3.0, 3.5, 1000.0):
        signal = _signal(value)
        bus = ReactiveBus()
        orders = RecordingSubmit()
        engine = ReactiveStrategyEngine(bus, orders, sizer=_SIZER, fill_reference="signal_close")

        class _Signaller:
            strategy_id = "gate"
            version = "1.0.0"

            def on_quote(self, context, quote):  # noqa: ANN001, ANN201
                return signal

        engine.register(_Signaller())
        bus.publish(Quote(instrument=_INSTRUMENT, ltp=Price(value=Decimal("2500"))))
        assert len(orders) == 1
        order_sizes.add(orders[0].quantity)

    assert order_sizes == {Quantity(Decimal("400"))}
    assert order_sizes != {Quantity(Decimal("1"))}


# ---------------------------------------------------------------------------
# 2. tree-wide gate — no module reads .strength
# ---------------------------------------------------------------------------


def test_no_module_on_the_signal_to_order_path_reads_strength() -> None:
    exempt = {
        line
        for relative in KNOWN_STRENGTH_AS_SIZE
        for line in _strength_reads(REPO / relative)
    }
    offenders = sorted({
        line for path in SIZING_SOURCES for line in _strength_reads(path)
    } - exempt)

    assert offenders == [], (
        "Signal.strength was read outside the domain declaration: "
        f"{offenders}. It is signal quality, never a size."
    )


@pytest.mark.parametrize("relative", [RETIRED_STRENGTH_AS_SIZE])
def test_the_known_strength_as_size_read_is_gone(relative: str) -> None:
    """The strict xfail this replaces, as a real assertion.

    It was ``xfail(strict=True)`` while the read was in ``backtest.py``, which is
    the right shape for an unpaid debt: it would XPASS — a *failure* — the moment
    the read disappeared, so the marker could not be forgotten after it was paid.
    Now that it is paid the same body is an ordinary assertion on the same file,
    and it is load bearing: reinstating ``abs(signal.strength)`` anywhere in the
    bridge puts a ``.strength`` read back in the AST and this goes red. (Its
    sibling behavioural proof, in
    ``trading/tests/replay/test_backtest_sizer_wiring.py``, fails on the *number*
    rather than on the source, so the two are independent.)
    """
    reads = _strength_reads(REPO / relative)
    assert not reads, (
        f"{relative} reads Signal.strength at {reads} — it is signal quality, "
        "never a size, and the backtest's bridge is sized by the run's sizer"
    )


def test_the_strength_exemption_list_is_empty() -> None:
    """Nothing is exempt from the gate above.

    The gate subtracts this dict's findings, so an entry here is a hole in it
    rather than a note about one. Asserting it is empty is what makes the next
    ``strength`` read a deliberate act that has to edit this file too.
    """
    assert KNOWN_STRENGTH_AS_SIZE == {}, (
        "Signal.strength is being read as a size again; remove the read rather "
        f"than adding an entry: {KNOWN_STRENGTH_AS_SIZE}"
    )


# ---------------------------------------------------------------------------
# 3. the emission allowlist
# ---------------------------------------------------------------------------


def test_the_strategies_that_declare_a_strength_are_the_recorded_ones() -> None:
    emitters = {
        _relative(path) for path in SIZING_SOURCES if _strength_declarations(path)
    }
    assert emitters == STRENGTH_EMITTERS
