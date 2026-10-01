"""F-39 / F-40: production wiring for bars and sizing, proven through ``boot()``.

The programme calls this the "extra proof" for Wave 0.5, and it is the test
that would have caught both defects. Neither was a wrong calculation: both were
**a correct component that production never called**, which is why a suite full
of unit tests on the aggregator and the sizer stayed green while paper/live
placed no bar-driven order at all.

So this suite deliberately does not construct the components. It boots the real
session through the composition root, drives the real bus, and asserts against
the real ``ReactiveStrategyEngine`` that ``boot()`` built.

Negative controls (each fails when its fix is removed):
  * ``test_boot_wires_a_bar_aggregator`` — fails if ``wire_bar_aggregation`` is
    dropped from the composition root, because no ``Candle`` is ever published.
  * ``test_boot_sizes_with_the_configured_sizer`` — fails if ``sizer=`` is
    dropped, because the engine then falls back to ``unconfigured_sizer``'s
    fixed ~Rs 95,000 notional.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from tradex_config.schema import AppConfig
from tradex_domain import Candle
from tradex_domain.enums import ExchangeId, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity
from tradex_runtime.startup import boot
from trading.tests.risk_support import app_config, permissive_risk

#: A real notional ceiling, far from the placeholder's 100,000, so the two
#: sizers cannot be confused for each other.
_CEILING = Decimal("25000")
_PRICE = 100.0
_INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")
_IST = timezone(timedelta(hours=5, minutes=30))
#: The 09:15 IST session open — bucket zero for every 1m bar that follows.
_SESSION_OPEN = datetime(2026, 9, 24, 9, 15, tzinfo=_IST)


def _ts(minutes_from_open: int) -> datetime:
    """A UTC timestamp for *minutes_from_open* past the 09:15 IST open."""
    return (_SESSION_OPEN + timedelta(minutes=minutes_from_open)).astimezone(UTC)


def _quote(index: int) -> Quote:
    return Quote(
        instrument=_INSTRUMENT,
        ltp=Price(value=Decimal(str(_PRICE + index))),
        volume=Quantity(value=Decimal("100")),
        timestamp=_ts(index),
    )


def _cfg() -> AppConfig:
    return app_config(
        mode="paper",
        # The tape is what the aggregator aggregates. An unnamed tape is a
        # legitimate state (nothing to trade), so the test must name one.
        feed_instruments=(_INSTRUMENT,),
        risk=permissive_risk(
            max_order_value=_CEILING, max_position_value=_CEILING
        ),
    )


class _RecordingStrategy:
    """A real ``Strategy``: counts the bars the engine hands it."""

    def __init__(self) -> None:
        self.strategy_id = "f39-smoke"
        self.version = "1.0.0"
        self.bars: list[Any] = []

    def on_bar(self, ctx: Any, candle: Any) -> None:
        self.bars.append(candle)

    def on_quote(self, ctx: Any, quote: Any) -> None:
        """The engine subscribes to three streams; two may be no-ops."""
        return None

    def on_depth(self, ctx: Any, depth: Any) -> None:
        return None

    def on_fill(self, ctx: Any, fill: Any) -> None:
        return None


def _boot_with_strategy() -> tuple[Any, _RecordingStrategy]:
    """Boot the real session, then register a strategy through the real engine.

    ``boot()`` takes no ``strategies=``; discovery is what registers strategies,
    so the test registers through ``session.strategy_engine`` — the same object
    and the same ``register()`` the composition root calls at
    ``startup.py:721``. The bus and the sizer under test are boot's, not ours.
    """
    session = boot(_cfg())
    strategy = _RecordingStrategy()
    session.strategy_engine.register(strategy)
    return session, strategy


def _drive(session: Any, count: int) -> None:
    """Push *count* real ``Quote`` objects through the real session bus."""
    for index in range(count):
        session.bus.publish(_quote(index))


# ---------------------------------------------------------------------------
# F-39 — bar aggregation is wired at the composition root
# ---------------------------------------------------------------------------


def test_boot_wires_a_bar_aggregator() -> None:
    """F-39 negative control: a boot that ignores the tape never makes a bar.

    Two 1m buckets' worth of quotes, published on the real bus. Only the
    aggregator bridges ``Quote`` to ``Candle``; nothing else in the session does.
    """
    session, strategy = _boot_with_strategy()
    try:
        _drive(session, 3)
        assert strategy.bars, (
            "no bar reached the strategy: wire_bar_aggregation is not called by "
            "the composition root, so on_bar never fires in paper/live"
        )
    finally:
        session.stop()


def test_boot_publishes_real_candles_onto_the_session_bus() -> None:
    """F-39: the bridge publishes on the *session* bus, not a private one."""
    session, _ = _boot_with_strategy()
    try:
        seen: list[Any] = []
        session.bus.of_type(Candle).subscribe(seen.append)
        _drive(session, 3)
        assert seen, "no Candle on the session bus"
    finally:
        session.stop()


def test_aggregated_bar_ohlc_is_internally_consistent() -> None:
    """F-39: a bar's own fields must not contradict each other.

    Cheap, and it is the assertion that would catch an aggregator wired with
    the wrong timeframe or a reversed bucket.
    """
    session, strategy = _boot_with_strategy()
    try:
        _drive(session, 3)
        # Assert non-emptiness first: a loop over an empty list passes
        # vacuously, so without this the assertion would be satisfied by the
        # exact defect it exists to catch (no bars reaching the strategy at all).
        assert strategy.bars, "no bar reached the strategy to be checked"
        for bar in strategy.bars:
            assert bar.ohlc.low.value <= bar.ohlc.high.value
            assert bar.ohlc.open.value <= bar.ohlc.high.value
            assert bar.ohlc.close.value <= bar.ohlc.high.value
    finally:
        session.stop()


def test_aggregated_bar_is_m1() -> None:
    """F-39: the wire carries M1, the grid every shipped indicator is built on."""
    session, strategy = _boot_with_strategy()
    try:
        _drive(session, 2)
        assert strategy.bars[0].timeframe in (Timeframe.M1, Timeframe.M1.value)
    finally:
        session.stop()


def test_a_bar_closes_only_when_the_next_bucket_starts() -> None:
    """F-39: closed-only, which is what makes live and backtest one stream.

    A strategy that acted on a forming bar would re-act on every print inside
    it — the look-ahead ``next_open`` exists to prevent. One quote in one
    bucket must not close a bar.
    """
    session, strategy = _boot_with_strategy()
    try:
        _drive(session, 1)
        assert strategy.bars == [], "a single quote closed a bar; forming bars leak"
        _drive(session, 2)  # advances into the next 1m bucket
        assert strategy.bars, "crossing a bucket boundary produced no closed bar"
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# F-40 — the configured sizer, not the placeholder, sizes live orders
# ---------------------------------------------------------------------------


def test_boot_builds_the_account_sizer() -> None:
    """F-40 negative control: no ``sizer=`` means the placeholder is in use.

    ``account_sizer`` labels the object ``"account"``; ``unconfigured_sizer``
    labels its own so an operator learns the policy is unconfigured. Asserting
    the label rather than a magic quantity keeps the control alive across a
    policy change.
    """
    session, _ = _boot_with_strategy()
    try:
        sizer = getattr(session.strategy_engine, "_sizer", None)
        assert sizer is not None
        assert sizer._label == "account", (
            "the engine holds a non-account sizer: the composition root did not "
            "pass sizer= to ReactiveStrategyEngine"
        )
    finally:
        session.stop()


def test_the_configured_notional_ceiling_reaches_the_sizer() -> None:
    """F-40: the sizer is built from ``cfg.risk``, the same source as the gate.

    If the ceiling were the constant 100,000 rather than the configured 25,000,
    the risk gate and the sizer would be sizing against different accounts.
    """
    session, _ = _boot_with_strategy()
    try:
        sizer = session.strategy_engine._sizer
        assert sizer._max_order_value == _CEILING
    finally:
        session.stop()


def test_backtest_and_live_are_handed_the_same_sizer_object() -> None:
    """F-40: the parity claim the factory exists to make.

    ``account_sizer``'s docstring: both engines are handed the *same object*.
    The backtest resolves the sizer it was given rather than building a second
    one, so one policy decides both modes.
    """
    from tradex_replay.backtest import BacktestEngine
    from tradex_strategy.core.sizing import account_sizer

    sizer = account_sizer(
        equity=Decimal("1000000"),
        max_order_value=_CEILING,
        risk_fraction=Decimal("0.01"),
    )
    assert BacktestEngine(sizer=sizer)._resolve_sizer() is sizer
