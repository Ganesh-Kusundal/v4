"""Shared scaffolding for the runtime suite.

A live boot refuses to start unless an operator has named the tape this session
may trade — through ``$TRADEX_FEED_INSTRUMENTS`` or ``boot(feed_instruments=)``
— because nothing in the repository names one, so a live boot that did not would
hand back a session that looks ``READY`` over a feed nobody ever started and
refuse every order with ``feed_not_ready`` for the life of the process. The cold
start that then runs is ``READY`` only when the history reconciliation returns a
bar, and the lake is gitignored, so CI and a fresh clone have none.

So every live boot in this directory needs the same two things, and no test
should have to re-derive them: a tape named, and a history to reconcile against.
``named_tape`` does both, and is **opt-in** — a test that must prove the refusal
simply does not ask for one, and gets the empty tape the contract refuses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from tradex_config.schema import FEED_INSTRUMENTS_ENV
from tradex_domain import Equity
from tradex_runtime.bar_aggregator import BarFrame
from tradex_runtime.feed_recovery import ParquetHistoryProvider


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "no_test_strategy: skip the autouse test strategy registration"
    )

#: 2026-09-24T10:00Z — the stamp the synthetic bars across this suite use.
_EPOCH = 1789946400

#: The tape every live boot in this suite names unless it is about the refusal.
#: One cash equity, because a tape is the operator's to choose and the tests are
#: not about which one.
TAPE = (Equity.of("NSE", "RELIANCE"),)


def _reconcilable_bar(instrument_id: str) -> BarFrame:
    return BarFrame(
        instrument=instrument_id,
        timeframe="1m",
        time=_EPOCH,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        closed=True,
    )


class _ReconcilableHistory:
    """A ``fetch_bars`` that always reconciles, for any window it is asked for.

    An *instance*, not a function: ``fetch_bars`` is patched as a class
    attribute, and only a plain function would bind ``self`` to it and swallow
    the instrument the coordinator passes first.
    """

    def __call__(
        self, instrument: Any, timeframe: str, start: datetime, end: datetime
    ) -> list[BarFrame]:
        return [_reconcilable_bar(str(instrument.instrument_id))]


@pytest.fixture
def named_tape(monkeypatch: pytest.MonkeyPatch) -> tuple[Equity, ...]:
    """A tape a live boot may cold start on, offline and deterministically.

    Named through ``$TRADEX_FEED_INSTRUMENTS`` because that is the route the
    refusal itself points an operator at, and the one ``TradingSession.live()``
    and ``quick_start_live.py`` take without knowing a tape argument exists — so
    a live boot through them is booted the way an unattended operator's is.
    ``AppConfig`` resolves the variable on every construction, so this needs no
    cooperation from the config a test happens to build.

    ``fetch_bars`` is patched rather than the lake seeded so the decision under
    test stays the coordinator's and not the clock's: the production lookback
    window is one daily bucket, so a real read of an absent lake is empty on
    every run and on a weekend alike.
    """
    monkeypatch.setenv(
        FEED_INSTRUMENTS_ENV, ",".join(str(i.instrument_id) for i in TAPE)
    )
    monkeypatch.setattr(
        ParquetHistoryProvider, "fetch_bars", _ReconcilableHistory()
    )
    return TAPE


@pytest.fixture
def no_tape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the contract: an operator who named nothing.

    Named as its own fixture so a test proving the refusal says so in its
    signature, and so it cannot pass by inheriting a tape from the ambient
    environment a developer happens to export.
    """
    monkeypatch.delenv(FEED_INSTRUMENTS_ENV, raising=False)


class _TestStrategy:
    """A minimal strategy for tests that boot in live mode.

    Live mode requires ``TRADEX_STRATEGY_IDS`` (or ``execution.strategy_ids``)
    to name at least one known strategy. This class satisfies that contract
    without running any real logic.
    """

    strategy_id = "test_strategy"
    version = "0.0.0"

    def on_bar(self, context, candle):
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        return None


@pytest.fixture(autouse=True)
def _register_test_strategy(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    """Register ``_TestStrategy`` so live boots may name it.

    Patches ``tradex_runtime.startup.all_strategies`` and
    ``all_definitions`` at the module level so every test in this directory
    that boots in live mode has a known strategy to select. Tests must still
    set ``execution.strategy_ids=("test_strategy",)`` in their AppConfig.

    Tests that specifically test strategy discovery or filtering opt out by
    setting the ``no_test_strategy`` marker.
    """
    # Skip patching for tests that need to see the real strategy list
    if request.node.get_closest_marker("no_test_strategy"):
        return

    import tradex_runtime.startup as startup

    monkeypatch.setattr(startup, "all_strategies", (_TestStrategy(),))
    monkeypatch.setattr(startup, "all_definitions", ())
