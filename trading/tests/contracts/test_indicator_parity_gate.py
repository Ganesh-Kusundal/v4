"""Indicator parity gate (Phase 10) — every registered indicator is parity-checked.

An indicator is parity-checked on three surfaces, and this gate pins all three
for the *whole* registry rather than for whichever fixture someone remembered to
add:

1. **Golden parity.** Every registered indicator has a golden fixture and an
   entry in the golden harness's parameter/plot maps, so the TypeScript-derived
   ground truth actually exercises it. ``test_golden_parity.py`` iterates the
   *goldens*, which means an indicator added to the registry without one is
   invisible to it; the coverage assertions here are what close that gap. A
   representative subset is then run through the harness end to end, proving the
   harness the coverage claim points at still works.

2. **Batch parity.** The indicator computes over the full bar series through
   ``compute_indicator`` without error and with a registered compute function.

3. **Stream parity.** The same indicator, read through the production
   ``AnalyticsRuntime`` view (bars ingested one at a time into the shared
   ``BarStore``), returns exactly the batch result. This is the seam a strategy
   reads; a divergence means the engine's view and the REST/golden path compute
   different numbers from the same bars.

The registry is the public discovery surface (``REGISTRY.all()``), so a newly
registered indicator is gated the moment its module is imported.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tradex_analytics.indicators import REGISTRY, compute_indicator, get_indicator_spec
from tradex_analytics.runtime import AnalyticsRuntime
from tradex_domain import OHLC, Candle, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from trading.tests.analytics.test_golden_parity import (
    PARAM_MAP,
    PLOT_MAP,
    _check_plots,
    _compute,
    _golden_ids,
    _load,
)

_GOLDENS = Path(__file__).resolve().parents[1] / "analytics" / "goldens"
_INSTRUMENT = Equity.of("NSE", "RELIANCE")
_IST = timezone(timedelta(hours=5, minutes=30))

#: One indicator per family, run through the golden harness end to end. The
#: full harness already runs every golden in CI; this subset exists so the gate
#: proves the harness it leans on is functional, without duplicating the whole
#: expensive comparison.
_GOLDEN_SUBSET = (
    "sma",
    "ema",
    "rsi",
    "macd",
    "bollinger",
    "atr",
    "stochastic",
    "supertrend",
    "vwap",
    "adx",
    "ichimoku",
    "kama",
)


def _fixture_candles() -> list[Candle]:
    """The shared bar series the batch/stream leg runs over.

    Built from the analytics golden fixture, so the "stream" view and the
    "batch" call are fed identical real bars rather than a synthetic ramp that
    could hide an indexing or alignment defect.
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


def _registry_ids() -> set[str]:
    return {spec.id for spec in REGISTRY.all()}


def test_registry_is_non_empty() -> None:
    """Guard the whole gate against passing over an empty registry."""
    assert REGISTRY.all(), "no indicators registered; the parity gate checks nothing"


def test_every_registered_indicator_has_a_compute_fn() -> None:
    for spec in REGISTRY.all():
        assert callable(getattr(spec, "fn", None)), (
            f"indicator {spec.id!r} is registered without a compute function"
        )


def test_every_registered_indicator_has_a_golden_fixture() -> None:
    missing = sorted(_registry_ids() - set(_golden_ids()))
    assert not missing, (
        "registered indicators have no golden fixture, so nothing checks them "
        f"against the reference implementation: {missing}"
    )


def test_no_orphan_golden_fixtures() -> None:
    orphan = sorted(set(_golden_ids()) - _registry_ids())
    assert not orphan, (
        "golden fixtures exist for indicators that are not registered; the "
        f"parity harness would compute them but nothing serves them: {orphan}"
    )


def test_every_registered_indicator_is_covered_by_the_golden_harness() -> None:
    """The harness has a parameter and plot map for every registered id.

    A golden with no ``PARAM_MAP`` entry cannot be computed (the harness indexes
    the map directly) and a golden with no ``PLOT_MAP`` entry has plots the
    comparison cannot account for. Either way the indicator is present in the
    fixture set but not actually compared.
    """
    registry = _registry_ids()
    missing_params = sorted(registry - set(PARAM_MAP))
    missing_plots = sorted(registry - set(PLOT_MAP))
    assert not missing_params, f"no golden PARAM_MAP entry for: {missing_params}"
    assert not missing_plots, f"no golden PLOT_MAP entry for: {missing_plots}"


def test_golden_subset_is_registered() -> None:
    """The representative subset must not drift out of the registry."""
    unknown = sorted(set(_GOLDEN_SUBSET) - _registry_ids())
    assert not unknown, f"golden subset names unregistered indicators: {unknown}"


def test_every_registered_indicator_passes_batch_stream_parity() -> None:
    """The shared analytics view returns the batch computation exactly.

    Bars are ingested one at a time into the production ``AnalyticsRuntime``
    (the stream shape a live feed has), then each indicator is read through the
    view and compared to the same indicator computed over the whole series in
    one call. Any difference is a defect in the view/store seam, not in the
    indicator math, which is identical on both sides.
    """
    runtime = AnalyticsRuntime()
    for candle in _CANDLES:
        runtime.ingest(candle)
    view = runtime.view(_INSTRUMENT.instrument_id, Timeframe.M1)

    mismatches = [
        spec.id
        for spec in REGISTRY.all()
        if compute_indicator(spec.id, _CANDLES, None) != view.indicator(spec.id)
    ]
    assert not mismatches, (
        "indicator batch and stream computations disagree for: " + ", ".join(mismatches)
    )


def test_representative_indicators_pass_golden_parity() -> None:
    """Run a family-spanning subset through the golden harness end to end."""
    for indicator_id in _GOLDEN_SUBSET:
        spec = get_indicator_spec(indicator_id)
        assert spec is not None, f"{indicator_id}: registered spec disappeared"
        golden = _load(indicator_id)
        got = _compute(indicator_id, golden)
        _check_plots(indicator_id, golden, spec, got)
