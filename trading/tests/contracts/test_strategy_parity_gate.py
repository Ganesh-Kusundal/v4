"""Strategy parity gate (Phase 10) — every registered strategy is parity-checked.

The extension contract tests (Phase 8) pin *where* a strategy may reach. This
gate pins *what must be true of every strategy the platform ships*: the parity
triangle's legs that apply to a strategy as a leaf artifact.

What is asserted, and why each is a real parity claim
-----------------------------------------------------
1. **Structural parity.** Every discovered strategy is a ``Strategy`` protocol
   instance with a unique, non-empty ``strategy_id`` and a semver ``version``.
   Every declared ``StrategySpec`` validates against the analytics registry
   (``validate_strategy_spec``) and names the strategy it belongs to. A strategy
   that cannot be described cannot be routed, versioned or replayed, so this is
   the precondition of every other leg.

2. **Historical ↔ replay parity.** Running the *same* strategy, freshly
   instantiated, through the backtest twice over one shared bar series produces
   the same economic fill stream (side/quantity/price). This is the replay leg:
   a historical run must be reproducible, or a golden result means nothing.

3. **Historical ↔ reactive (paper-shaped) parity.** Feeding the same bars to the
   strategy directly and through the production ``ReactiveStrategyEngine``
   produces the same signal sequence. The engine's context construction, routing
   and analytics wiring must not change what the strategy decides. This is the
   leg that catches an engine change silently altering strategy behaviour.

What is deliberately NOT here
-----------------------------
The *economic* backtest ↔ paper fill parity (same fills on both execution
sources, shared sizer, ``next_open`` reference) is a framework property and is
already gated by ``trading/tests/parity/test_backtest_live_tape_parity.py`` with
a purpose-built strategy and a controlled sizer. Re-deriving it per discovered
strategy here would confound the parity claim with risk/cash differences between
modes — the reason that suite exists in its own shape. This gate therefore
checks the legs that apply to a strategy *as an extension*, and leaves the
execution-model parity to the parity suite.

The gate iterates the public ``all_strategies`` / ``all_definitions`` discovery
surface, so a newly dropped-in extension is parity-checked the moment it is
registered, with no edit here.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.strategy import StrategyContext
from tradex_domain.value_objects import Price, Quantity
from tradex_reactive.bus import ReactiveBus
from tradex_replay.backtest import BacktestEngine
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.protocols import Strategy
from tradex_strategy.core.spec_validator import validate_strategy_spec
from tradex_strategy.extensions import all_definitions, all_strategies

_GOLDENS = Path(__file__).resolve().parents[1] / "analytics" / "goldens"
_INSTRUMENT = Equity.of("NSE", "RELIANCE")
_IST = timezone(timedelta(hours=5, minutes=30))

#: Backtest parameters for the replay leg. Both runs use the same values, so a
#: divergence is a non-determinism finding rather than a setup difference.
_INITIAL_CAPITAL = Decimal("100000")
_MAX_ORDER_VALUE = Decimal("100000")

#: Strategies that deliberately declare no ``StrategySpec``. The set is pinned
#: so a new spec-less strategy fails the gate instead of silently skipping the
#: spec-validation leg — the decision to ship without a spec is explicit.
_SPEC_LESS_STRATEGIES = frozenset({"multi_symbol_sma_cross"})

_SEMVER = re.compile(r"^\d+\.\d+\.\d+")


def _fixture_candles() -> list[Candle]:
    """The shared bar series every leg is driven over.

    Real bars from the analytics golden fixture, not a hand-written monotone
    ramp: every registered strategy emits at least one signal over it (asserted
    below), so the parity comparisons are not satisfied vacuously by a run that
    did nothing.
    """
    bars = json.loads((_GOLDENS / "fixtures.json").read_text())["bars"]
    return [
        Candle(
            instrument=_INSTRUMENT,
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(value=Decimal(str(open_))),
                high=Price(value=Decimal(str(high))),
                low=Price(value=Decimal(str(low))),
                close=Price(value=Decimal(str(close))),
            ),
            volume=Quantity(value=Decimal(str(volume))),
            timestamp=datetime.fromtimestamp(stamp, tz=_IST).replace(tzinfo=None),
        )
        for stamp, open_, high, low, close, volume in bars
    ]


_CANDLES = _fixture_candles()


def _fresh(strategy: Strategy) -> Any:
    """A new instance of *strategy*'s class, independent of the singleton.

    The discovered objects are module-level singletons that accumulate state
    across runs, so a parity comparison against one is only meaningful on a
    fresh instance. The constructor is inspected rather than hard-coded: the
    gate must work for the next strategy dropped in, whatever its signature.
    """
    cls = type(strategy)
    parameters = inspect.signature(cls.__init__).parameters
    kwargs: dict[str, Any] = {}
    if "strategy_id" in parameters:
        kwargs["strategy_id"] = f"{strategy.strategy_id}__gate"
    if "instrument" in parameters:
        kwargs["instrument"] = _INSTRUMENT
    return cls(**kwargs)


def _direct_signals(strategy: Any) -> list[str]:
    """Signal reasons from feeding the bars to the strategy directly."""
    context = StrategyContext()
    for candle in _CANDLES:
        strategy.on_bar(context, candle)
    return [signal.reason for signal in strategy.signals]


def _reactive_signals(strategy: Any) -> list[str]:
    """Signal reasons from feeding the bars through the reactive engine."""
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus=bus, submit=lambda request: None)
    engine.register(strategy)
    try:
        for candle in _CANDLES:
            bus.publish(candle)
    finally:
        engine.dispose_all()
    return [signal.reason for signal in strategy.signals]


def _economic_fills(result: Any) -> list[tuple[str, str, str]]:
    """The mode-invariant economic part of a backtest's fills.

    Order ids are random and timestamps are run-specific, so only the
    (side, quantity, price) triple is compared — exactly the projection the
    parity suite treats as the economic contract.
    """
    return [
        (fill.side.value, str(fill.quantity.value), str(fill.price.value))
        for fill in result.fill_records
    ]


def _run_backtest(strategy: Any) -> list[tuple[str, str, str]]:
    result = BacktestEngine(
        fill_source=None,
        initial_capital=_INITIAL_CAPITAL,
        max_order_value=_MAX_ORDER_VALUE,
    ).run(strategy, list(_CANDLES))
    return _economic_fills(result)


def test_discovery_is_non_empty() -> None:
    """Guard the whole gate against passing over an empty registry."""
    assert all_strategies, "no strategies discovered; the parity gate checks nothing"
    assert all_definitions, "no strategy definitions discovered"


def test_every_registered_strategy_is_a_valid_protocol_instance() -> None:
    for strategy in all_strategies:
        assert isinstance(strategy, Strategy), f"{strategy!r} is not a Strategy"
        assert strategy.strategy_id, f"{strategy!r} has an empty strategy_id"
        version = getattr(strategy, "version", None)
        assert isinstance(version, str) and _SEMVER.match(version), (
            f"{strategy.strategy_id}: version {version!r} is not semver"
        )


def test_strategy_ids_are_unique() -> None:
    ids = [strategy.strategy_id for strategy in all_strategies]
    duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
    assert not duplicates, f"duplicate strategy_id(s): {duplicates}"


def test_every_declared_spec_is_valid() -> None:
    """Every declared spec validates and names the strategy it describes."""
    problems: dict[str, list[str]] = {}
    for strategy in all_strategies:
        spec = getattr(strategy, "spec", None)
        if spec is None:
            continue
        assert spec.id == strategy.strategy_id, (
            f"{strategy.strategy_id}: spec.id {spec.id!r} does not match"
        )
        found = validate_strategy_spec(spec)
        if found:
            problems[strategy.strategy_id] = found
    for definition in all_definitions:
        assert definition.spec.id, "a StrategyDefinition has an empty spec.id"
        found = validate_strategy_spec(definition.spec)
        if found:
            problems[f"definition:{definition.spec.id}"] = found
    assert not problems, f"strategy specs failed validation: {problems}"


def test_spec_less_strategies_are_explicitly_allowlisted() -> None:
    actual = {
        strategy.strategy_id
        for strategy in all_strategies
        if getattr(strategy, "spec", None) is None
    }
    assert actual == set(_SPEC_LESS_STRATEGIES), (
        "the set of spec-less strategies changed; a strategy may ship without a "
        f"spec only if it is named in _SPEC_LESS_STRATEGIES (got {sorted(actual)})"
    )


def test_every_registered_strategy_passes_historical_replay_parity() -> None:
    """Two identical historical runs produce identical economic fills."""
    for strategy in all_strategies:
        first = _run_backtest(_fresh(strategy))
        second = _run_backtest(_fresh(strategy))
        assert first == second, (
            f"{strategy.strategy_id}: replaying the same bars produced a "
            f"different fill stream\n  first={first}\n  second={second}"
        )


def test_every_registered_strategy_passes_direct_reactive_parity() -> None:
    """The engine changes nothing the strategy decides.

    The same bars are fed directly and through the production reactive engine;
    the emitted signal sequence must be identical. The fixture is also proven to
    exercise the strategy — a strategy that emits nothing would pass this
    comparison vacuously.
    """
    for strategy in all_strategies:
        direct = _direct_signals(_fresh(strategy))
        assert direct, (
            f"{strategy.strategy_id}: the parity fixture produced no signals, so "
            "this strategy's parity is unverified — extend the fixture or the "
            "strategy's warmup/settings"
        )
        reactive = _reactive_signals(_fresh(strategy))
        assert direct == reactive, (
            f"{strategy.strategy_id}: direct and reactive signal sequences "
            f"differ\n  direct={direct}\n  reactive={reactive}"
        )
