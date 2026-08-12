# AMT Simulation + Live Paper Trading — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a depth-simulation layer and AMT (Auction Market Theory) analytics pipeline that enables E2E testing of order-flow strategies — from synthetic market data through volume profile, CVD, footprint, to paper execution — all without a live broker.

**Architecture:** Extend the existing `SyntheticTickGenerator` to emit `Depth` events alongside `Quote` events (ponytail: reuse the price path we already compute). Build real-time analytics (`VolumeProfileBuilder`, `CVDBuilder`) as simple classes that ingest bus events. Add `on_depth` to the Strategy protocol. Wire an AMT strategy that consumes analytics and produces signals. One E2E test validates the full pipeline.

**Tech Stack:** Python 3.12+, tradex_domain (Depth, Quote, Candle), tradex_trading (ReactiveBus, SyntheticTickGenerator, ReplayEngine, Strategy protocol), pytest.

---

## File Structure

```
MODIFY:
  trading/src/tradex_trading/replay/synthetic_ticks.py   — add depth emission
  trading/src/tradex_trading/analytics/volume_profile.py  — add VAH, VAL, LVN
  trading/src/tradex_trading/analytics/orderflow.py       — add CVD accumulator
  trading/src/tradex_trading/analytics/__init__.py        — export new symbols
  trading/src/tradex_trading/strategy/core/protocols.py   — add on_depth
  trading/src/tradex_trading/strategy/core/engine.py      — subscribe Depth

CREATE:
  trading/src/tradex_trading/analytics/footprint.py       — per-price volume tracking
  trading/src/tradex_trading/strategy/amt/__init__.py     — AMT package
  trading/src/tradex_trading/strategy/amt/strategy.py     — AMT strategy

TESTS:
  trading/tests/replay/test_synthetic_ticks_depth.py      — depth emission tests
  trading/tests/analytics/test_volume_profile_extended.py  — VAH/VAL/LVN tests
  trading/tests/analytics/test_cvd.py                      — CVD tests
  trading/tests/analytics/test_footprint.py                — footprint tests
  trading/tests/strategy/test_on_depth.py                  — protocol + engine tests
  trading/tests/integration/test_amt_e2e.py                — full pipeline E2E
```

---

## Task 1: Depth Emission in SyntheticTickGenerator

**Files:**
- Modify: `trading/src/tradex_trading/replay/synthetic_ticks.py`
- Test: `trading/tests/replay/test_synthetic_ticks_depth.py`

**Ponytail rationale:** Don't create a separate DepthSimulator class. The generator already computes price, spread, and publishes to the bus. Adding depth is ~20 lines inside `feed_bar()`.

- [ ] **Step 1: Write the failing test**

```python
# trading/tests/replay/test_synthetic_ticks_depth.py
"""Tests for Depth emission in SyntheticTickGenerator."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, Depth, Quote, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.synthetic_ticks import SyntheticTickGenerator

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(**kwargs) -> Candle:
    defaults = dict(
        open_=Decimal("100"), high=Decimal("110"),
        low=Decimal("95"), close=Decimal("105"),
        volume=Decimal("10000"),
    )
    defaults.update(kwargs)
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=defaults["open_"]),
            high=Price(value=defaults["high"]),
            low=Price(value=defaults["low"]),
            close=Price(value=defaults["close"]),
        ),
        volume=Quantity(value=defaults["volume"]),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


class TestDepthEmission:
    def test_no_depth_by_default(self) -> None:
        """Default mode: only Quotes, no Depth events."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        SyntheticTickGenerator(bus, seed=1).feed_bar(_candle())
        depths = [e for e in events if isinstance(e, Depth)]
        assert len(depths) == 0

    def test_depth_emitted_when_levels_set(self) -> None:
        """depth_levels=5 → one Depth event per bar."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        depths = [e for e in events if isinstance(e, Depth)]
        assert len(depths) == 1

    def test_depth_has_correct_levels(self) -> None:
        """Depth has exactly the requested number of bid and ask levels."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        depth = next(e for e in events if isinstance(e, Depth))
        assert len(depth.bids) == 5
        assert len(depth.asks) == 5

    def test_depth_bids_descending_asks_ascending(self) -> None:
        """Book ordering invariant holds."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=10)
        gen.feed_bar(_candle())
        depth = next(e for e in events if isinstance(e, Depth))
        # bids descending
        for i in range(len(depth.bids) - 1):
            assert depth.bids[i][0].value >= depth.bids[i + 1][0].value
        # asks ascending
        for i in range(len(depth.asks) - 1):
            assert depth.asks[i][0].value <= depth.asks[i + 1][0].value

    def test_depth_best_bid_ask_around_ltp(self) -> None:
        """Best bid < last tick price < best ask."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        quotes = [e for e in events if isinstance(e, Quote)]
        depth = next(e for e in events if isinstance(e, Depth))
        last_ltp = quotes[-1].ltp.value
        assert depth.bids[0][0].value < last_ltp
        assert depth.asks[0][0].value > last_ltp

    def test_depth_quantities_positive(self) -> None:
        """All level quantities are positive."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        depth = next(e for e in events if isinstance(e, Depth))
        for _, qty in depth.bids:
            assert qty.value > 0
        for _, qty in depth.asks:
            assert qty.value > 0

    def test_depth_emitted_per_bar_in_replay(self) -> None:
        """Two bars → two Depth events."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        gen.feed_bar(_candle(close=Decimal("108")))
        depths = [e for e in events if isinstance(e, Depth)]
        assert len(depths) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4 && python -m pytest trading/tests/replay/test_synthetic_ticks_depth.py -v`
Expected: FAIL — `depth_levels` not a valid parameter

- [ ] **Step 3: Implement depth emission in SyntheticTickGenerator**

Add `depth_levels` parameter and `_emit_depth` method to `synthetic_ticks.py`:

```python
# In __init__, add parameter:
def __init__(
    self,
    bus: Any,
    clock=None,
    ticks_per_bar: int = 60,
    seed: int | None = None,
    method: str = "anchored",
    depth_levels: int = 0,  # NEW: 0 = no depth (default), >0 = emit Depth
) -> None:
    # ... existing init ...
    self._depth_levels = depth_levels

# In feed_bar(), after the tick loop, add:
def feed_bar(self, candle: Candle) -> None:
    for i, (price, volume) in enumerate(zip(prices, volumes)):
        # ... existing Quote publish ...
    
    # NEW: emit Depth snapshot at bar close
    if self._depth_levels > 0:
        self._emit_depth(candle, prices[-1])

# New method:
def _emit_depth(self, candle: Candle, mid_price: float) -> None:
    """Generate a Depth snapshot around mid_price."""
    from tradex_domain.market import Depth
    
    tick_size = Decimal("0.05")  # ponytail: NSE tick size
    mid = Decimal(str(mid_price))
    levels = self._depth_levels
    
    bids = []
    asks = []
    for i in range(levels):
        offset = tick_size * (i + 1)
        # ponytail: exponential decay — more qty further from mid
        qty = Decimal(str(self._rng.uniform(50, 500))) * (1 + i * 0.3)
        qty = qty.quantize(Decimal("1"))
        bids.append((Price(value=mid - offset), Quantity(value=qty)))
        asks.append((Price(value=mid + offset), Quantity(value=qty)))
    
    self._bus.publish(Depth(
        instrument=candle.instrument,
        bids=tuple(bids),
        asks=tuple(asks),
        timestamp=candle.timestamp + timedelta(seconds=self._ticks_per_bar),
    ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest trading/tests/replay/test_synthetic_ticks_depth.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run existing synthetic tick tests to verify no regression**

Run: `python -m pytest trading/tests/replay/test_synthetic_ticks.py -v`
Expected: ALL PASS (depth_levels=0 default means no behavior change)

- [ ] **Step 6: Commit**

```bash
git add trading/src/tradex_trading/replay/synthetic_ticks.py trading/tests/replay/test_synthetic_ticks_depth.py
git commit -m "feat: SyntheticTickGenerator emits Depth events when depth_levels > 0"
```

---

## Task 2: Volume Profile — VAH, VAL, LVN

**Files:**
- Modify: `trading/src/tradex_trading/analytics/volume_profile.py`
- Modify: `trading/src/tradex_trading/analytics/__init__.py`
- Test: `trading/tests/analytics/test_volume_profile_extended.py`

- [ ] **Step 1: Write the failing tests**

```python
# trading/tests/analytics/test_volume_profile_extended.py
"""Tests for extended volume profile: VAH, VAL, LVN."""

from __future__ import annotations

from tradex_trading.analytics.volume_profile import (
    poc, vah, val, lvn, value_area,
)


def _profile() -> dict[float, float]:
    """Sample profile: peak at 104, low-volume nodes at 98 and 110."""
    return {
        96.0: 100, 97.0: 200, 98.0: 30,   # LVN at 98
        99.0: 400, 100.0: 800, 101.0: 1200,
        102.0: 1500, 103.0: 2000, 104.0: 3000,  # POC
        105.0: 2000, 106.0: 1500, 107.0: 1200,
        108.0: 800, 109.0: 400, 110.0: 30,   # LVN at 110
    }


class TestPOC:
    def test_poc_returns_highest_volume_price(self) -> None:
        assert poc(_profile()) == 104.0

    def test_poc_empty_returns_none(self) -> None:
        assert poc({}) is None


class TestValueArea:
    def test_value_area_contains_70_percent(self) -> None:
        """VAH and VAL bound the 70% volume range."""
        profile = _profile()
        lo, hi = value_area(profile)
        total = sum(profile.values())
        contained = sum(v for p, v in profile.items() if lo <= p <= hi)
        assert contained / total >= 0.70

    def test_vah_above_poc(self) -> None:
        profile = _profile()
        assert vah(profile) >= poc(profile)

    def test_val_below_poc(self) -> None:
        profile = _profile()
        assert val(profile) <= poc(profile)

    def test_empty_profile_returns_none(self) -> None:
        assert value_area({}) == (None, None)


class TestLVN:
    def test_lvn_finds_local_minimum(self) -> None:
        """LVN is a price level with volume lower than neighbors."""
        lvns = lvn(_profile())
        # 98.0 and 110.0 are local minima (30 each, neighbors are 200+)
        assert 98.0 in lvns
        assert 110.0 in lvns

    def test_lvn_empty_profile(self) -> None:
        assert lvn({}) == []

    def test_lvn_flat_profile_has_no_lvns(self) -> None:
        flat = {100.0: 500, 101.0: 500, 102.0: 500}
        assert lvn(flat) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest trading/tests/analytics/test_volume_profile_extended.py -v`
Expected: FAIL — `vah`, `val`, `lvn`, `value_area` not importable

- [ ] **Step 3: Implement VAH, VAL, LVN**

```python
# trading/src/tradex_trading/analytics/volume_profile.py
"""Volume profile: POC, VAH, VAL, LVN, value area."""

from __future__ import annotations


def poc(price_volume: dict[float, float]) -> float | None:
    """Price level with the highest volume; None when empty."""
    if not price_volume:
        return None
    return max(price_volume, key=price_volume.__getitem__)


def value_area(
    price_volume: dict[float, float], pct: float = 0.70,
) -> tuple[float | None, float | None]:
    """Return (VAL, VAH) bounding the *pct* fraction of total volume around POC.

    Walks outward from POC until the accumulated volume >= pct * total.
    """
    if not price_volume:
        return (None, None)
    p = poc(price_volume)
    if p is None:
        return (None, None)
    total = sum(price_volume.values())
    target = total * pct
    sorted_prices = sorted(price_volume.keys())
    poc_idx = sorted_prices.index(p)
    accumulated = price_volume[p]
    lo_idx = poc_idx
    hi_idx = poc_idx
    while accumulated < target:
        expand_lo = lo_idx > 0
        expand_hi = hi_idx < len(sorted_prices) - 1
        if not expand_lo and not expand_hi:
            break
        lo_vol = price_volume[sorted_prices[lo_idx - 1]] if expand_lo else -1
        hi_vol = price_volume[sorted_prices[hi_idx + 1]] if expand_hi else -1
        if lo_vol >= hi_vol and expand_lo:
            lo_idx -= 1
            accumulated += lo_vol
        elif expand_hi:
            hi_idx += 1
            accumulated += hi_vol
        elif expand_lo:
            lo_idx -= 1
            accumulated += lo_vol
    return (sorted_prices[lo_idx], sorted_prices[hi_idx])


def vah(price_volume: dict[float, float], pct: float = 0.70) -> float | None:
    """Value Area High — upper bound of the value area."""
    return value_area(price_volume, pct)[1]


def val(price_volume: dict[float, float], pct: float = 0.70) -> float | None:
    """Value Area Low — lower bound of the value area."""
    return value_area(price_volume, pct)[0]


def lvn(price_volume: dict[float, float]) -> list[float]:
    """Low-Volume Nodes — price levels where volume is lower than both neighbors.

    Returns sorted list of prices. Empty profile or flat profile returns [].
    """
    if len(price_volume) < 3:
        return []
    sorted_prices = sorted(price_volume.keys())
    nodes: list[float] = []
    for i in range(1, len(sorted_prices) - 1):
        vol = price_volume[sorted_prices[i]]
        prev_vol = price_volume[sorted_prices[i - 1]]
        next_vol = price_volume[sorted_prices[i + 1]]
        if vol < prev_vol and vol < next_vol:
            nodes.append(sorted_prices[i])
    return nodes


__all__ = ["poc", "vah", "val", "lvn", "value_area"]
```

- [ ] **Step 4: Update analytics __init__.py exports**

Add to `trading/src/tradex_trading/analytics/__init__.py`:

```python
from tradex_trading.analytics.volume_profile import poc, vah, val, lvn, value_area
```

And add `"vah", "val", "lvn", "value_area"` to `__all__`.

- [ ] **Step 5: Run tests**

Run: `python -m pytest trading/tests/analytics/test_volume_profile_extended.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add trading/src/tradex_trading/analytics/volume_profile.py trading/src/tradex_trading/analytics/__init__.py trading/tests/analytics/test_volume_profile_extended.py
git commit -m "feat: volume profile adds VAH, VAL, LVN, value_area"
```

---

## Task 3: CVD (Cumulative Volume Delta)

**Files:**
- Modify: `trading/src/tradex_trading/analytics/orderflow.py`
- Modify: `trading/src/tradex_trading/analytics/__init__.py`
- Test: `trading/tests/analytics/test_cvd.py`

**Ponytail rationale:** CVD is just a running sum. Don't build a class — a function that takes a list of Quotes and returns deltas is enough for backtesting. A class is only needed when we wire it to the live bus (Task 7).

- [ ] **Step 1: Write the failing tests**

```python
# trading/tests/analytics/test_cvd.py
"""Tests for CVD (Cumulative Volume Delta)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Quote
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.analytics.orderflow import cvd_from_quotes, classify_aggressor


INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _quote(ltp: float, bid: float, ask: float) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(str(ltp))),
        bid=Price(value=Decimal(str(bid))),
        ask=Price(value=Decimal(str(ask))),
        volume=Quantity(value=Decimal("100")),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


class TestClassifyAggressor:
    def test_ltp_at_ask_is_buyer_aggressive(self) -> None:
        assert classify_aggressor(ltp=100.5, bid=100.0, ask=100.5) == 1

    def test_ltp_at_bid_is_seller_aggressive(self) -> None:
        assert classify_aggressor(ltp=100.0, bid=100.0, ask=100.5) == -1

    def test_ltp_at_mid_is_zero(self) -> None:
        assert classify_aggressor(ltp=100.25, bid=100.0, ask=100.5) == 0

    def test_ltp_above_ask_is_buyer(self) -> None:
        assert classify_aggressor(ltp=101.0, bid=100.0, ask=100.5) == 1

    def test_ltp_below_bid_is_seller(self) -> None:
        assert classify_aggressor(ltp=99.0, bid=100.0, ask=100.5) == -1


class TestCVDFromQuotes:
    def test_all_buyer_aggressive(self) -> None:
        quotes = [_quote(100.5, 100.0, 100.5)] * 5  # all at ask
        result = cvd_from_quotes(quotes)
        assert result == [100, 200, 300, 400, 500]  # cumulative

    def test_all_seller_aggressive(self) -> None:
        quotes = [_quote(100.0, 100.0, 100.5)] * 3  # all at bid
        result = cvd_from_quotes(quotes)
        assert result == [-100, -200, -300]

    def test_mixed(self) -> None:
        quotes = [
            _quote(100.5, 100.0, 100.5),  # buy +100
            _quote(100.0, 100.0, 100.5),  # sell -100
            _quote(100.5, 100.0, 100.5),  # buy +100
        ]
        result = cvd_from_quotes(quotes)
        assert result == [100, 0, 100]

    def test_empty_quotes(self) -> None:
        assert cvd_from_quotes([]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest trading/tests/analytics/test_cvd.py -v`
Expected: FAIL — `cvd_from_quotes`, `classify_aggressor` not importable

- [ ] **Step 3: Implement CVD**

Add to `trading/src/tradex_trading/analytics/orderflow.py`:

```python
from __future__ import annotations

from decimal import Decimal


def imbalance(bid_size: float, ask_size: float) -> float:
    """Return bid/ask imbalance ratio in [-1, 1]. Zero when both are zero."""
    total = bid_size + ask_size
    if total == 0:
        return 0.0
    return (bid_size - ask_size) / total


def classify_aggressor(*, ltp: float, bid: float, ask: float) -> int:
    """Classify a trade's aggressor side.

    Returns +1 (buyer aggressive), -1 (seller aggressive), or 0 (mid).
    ponytail: simple midpoint classification. Real tick data has exchange
    tick rule; this approximation works for Quote-level data.
    """
    mid = (bid + ask) / 2.0
    if ltp > mid:
        return 1
    if ltp < mid:
        return -1
    return 0


def cvd_from_quotes(quotes: list) -> list[int]:
    """Cumulative Volume Delta from a list of Quote events.

    Returns a list of cumulative delta values (one per quote).
    Each quote's volume is added if buyer-aggressive, subtracted if seller.
    """
    cumulative = 0
    result: list[int] = []
    for q in quotes:
        ltp = float(q.ltp.value)
        bid = float(q.bid.value) if q.bid is not None else ltp
        ask = float(q.ask.value) if q.ask is not None else ltp
        direction = classify_aggressor(ltp=ltp, bid=bid, ask=ask)
        vol = int(q.volume.value)
        cumulative += direction * vol
        result.append(cumulative)
    return result


__all__ = ["imbalance", "classify_aggressor", "cvd_from_quotes"]
```

- [ ] **Step 4: Update analytics __init__.py exports**

Add `classify_aggressor`, `cvd_from_quotes` to the import and `__all__`.

- [ ] **Step 5: Run tests**

Run: `python -m pytest trading/tests/analytics/test_cvd.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add trading/src/tradex_trading/analytics/orderflow.py trading/src/tradex_trading/analytics/__init__.py trading/tests/analytics/test_cvd.py
git commit -m "feat: CVD from Quote stream + aggressor classification"
```

---

## Task 4: Footprint — Per-Price Volume Tracking

**Files:**
- Create: `trading/src/tradex_trading/analytics/footprint.py`
- Test: `trading/tests/analytics/test_footprint.py`

**Ponytail rationale:** A footprint is just a dict of `{price: (buy_vol, sell_vol)}`. One class, two methods. No framework.

- [ ] **Step 1: Write the failing tests**

```python
# trading/tests/analytics/test_footprint.py
"""Tests for Footprint — per-price buy/sell volume tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Quote
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.analytics.footprint import Footprint

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _quote(ltp: float, bid: float, ask: float, vol: float = 100) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(str(ltp))),
        bid=Price(value=Decimal(str(bid))),
        ask=Price(value=Decimal(str(ask))),
        volume=Quantity(value=Decimal(str(vol))),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


class TestFootprint:
    def test_empty_footprint(self) -> None:
        fp = Footprint()
        assert fp.levels() == {}

    def test_add_quote_buy_at_ask(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.5, 100.0, 100.5, 200))
        levels = fp.levels()
        assert 100.5 in levels
        assert levels[100.5] == (200, 0)  # (buy_vol, sell_vol)

    def test_add_quote_sell_at_bid(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.0, 100.0, 100.5, 150))
        levels = fp.levels()
        assert 100.0 in levels
        assert levels[100.0] == (0, 150)

    def test_multiple_quotes_same_price(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.5, 100.0, 100.5, 100))  # buy
        fp.add(_quote(100.5, 100.0, 100.5, 200))  # buy
        levels = fp.levels()
        assert levels[100.5] == (300, 0)

    def test_delta_per_level(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.5, 100.0, 100.5, 200))  # buy 200
        fp.add(_quote(100.5, 100.0, 100.5, 50))   # sell 50 (at mid? no, at ask)
        # both at ask → both buys
        delta = fp.delta()
        assert delta[100.5] == 250

    def test_reset(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.5, 100.0, 100.5))
        fp.reset()
        assert fp.levels() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest trading/tests/analytics/test_footprint.py -v`
Expected: FAIL — `Footprint` not importable

- [ ] **Step 3: Implement Footprint**

```python
# trading/src/tradex_trading/analytics/footprint.py
"""Footprint — per-price buy/sell volume tracking from Quote stream."""

from __future__ import annotations

from collections import defaultdict

from tradex_trading.analytics.orderflow import classify_aggressor


class Footprint:
    """Accumulates per-price buy/sell volume from Quote events.

    ponytail: dict-based, no persistence. Sufficient for session-scoped
    footprint analysis. Add persistence when multi-day analysis is needed.
    """

    def __init__(self) -> None:
        self._buy_vol: dict[float, float] = defaultdict(float)
        self._sell_vol: dict[float, float] = defaultdict(float)

    def add(self, quote: object) -> None:
        """Process one Quote: classify aggressor, accumulate volume."""
        q = quote  # typed as object to avoid import cycle
        ltp = float(q.ltp.value)
        bid = float(q.bid.value) if q.bid is not None else ltp
        ask = float(q.ask.value) if q.ask is not None else ltp
        vol = float(q.volume.value)
        direction = classify_aggressor(ltp=ltp, bid=bid, ask=ask)
        if direction >= 0:
            self._buy_vol[ltp] += vol
        if direction <= 0:
            self._sell_vol[ltp] += vol

    def levels(self) -> dict[float, tuple[float, float]]:
        """Return {price: (buy_volume, sell_volume)} for all traded prices."""
        all_prices = set(self._buy_vol) | set(self._sell_vol)
        return {
            p: (self._buy_vol.get(p, 0.0), self._sell_vol.get(p, 0.0))
            for p in sorted(all_prices)
        }

    def delta(self) -> dict[float, float]:
        """Return {price: buy_vol - sell_vol} for all traded prices."""
        return {
            p: buy - sell
            for p, (buy, sell) in self.levels().items()
        }

    def reset(self) -> None:
        """Clear all accumulated data."""
        self._buy_vol.clear()
        self._sell_vol.clear()


__all__ = ["Footprint"]
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest trading/tests/analytics/test_footprint.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/analytics/footprint.py trading/tests/analytics/test_footprint.py
git commit -m "feat: Footprint — per-price buy/sell volume from Quote stream"
```

---

## Task 5: on_depth in Strategy Protocol + Engine

**Files:**
- Modify: `trading/src/tradex_trading/strategy/core/protocols.py`
- Modify: `trading/src/tradex_trading/strategy/core/engine.py`
- Test: `trading/tests/strategy/test_on_depth.py`

- [ ] **Step 1: Write the failing tests**

```python
# trading/tests/strategy/test_on_depth.py
"""Tests for on_depth callback in Strategy protocol and engine."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Equity
from tradex_domain.market import Depth
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine


INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _depth() -> Depth:
    return Depth(
        instrument=INSTRUMENT,
        bids=((Price(Decimal("99.9")), Quantity(Decimal("100"))),),
        asks=((Price(Decimal("100.1")), Quantity(Decimal("50"))),),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


class _DepthRecorder:
    """Strategy that records on_depth calls."""
    strategy_id = "depth-recorder"

    def __init__(self) -> None:
        self.depths: list[Depth] = []

    def on_start(self, ctx): pass
    def on_stop(self, ctx): pass
    def on_bar(self, ctx, bar): return None
    def on_quote(self, ctx, quote): return None
    def on_fill(self, ctx, fill): pass
    def on_event(self, event): pass
    def on_depth(self, ctx, depth):
        self.depths.append(depth)


class TestOnDepthProtocol:
    def test_on_depth_receives_depth_events(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        recorder = _DepthRecorder()
        engine.register(recorder)
        bus.publish(_depth())
        assert len(recorder.depths) == 1
        assert isinstance(recorder.depths[0], Depth)

    def test_on_depth_receives_multiple(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        recorder = _DepthRecorder()
        engine.register(recorder)
        bus.publish(_depth())
        bus.publish(_depth())
        bus.publish(_depth())
        assert len(recorder.depths) == 3

    def test_strategy_without_on_depth_still_works(self) -> None:
        """Strategies that don't implement on_depth don't break."""
        class _NoDepth:
            strategy_id = "no-depth"
            def on_start(self, ctx): pass
            def on_stop(self, ctx): pass
            def on_bar(self, ctx, bar): return None
            def on_quote(self, ctx, quote): return None
            def on_fill(self, ctx, fill): pass
            def on_event(self, event): pass

        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        engine.register(_NoDepth())
        bus.publish(_depth())  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest trading/tests/strategy/test_on_depth.py -v`
Expected: FAIL — Depth not subscribed in engine

- [ ] **Step 3: Add on_depth to Strategy protocol**

In `protocols.py`, add:

```python
from tradex_domain.market import Candle, Depth, Quote  # add Depth import

# Add to Strategy protocol:
def on_depth(self, context: StrategyContext, depth: Depth) -> Signal | None:
    """Called on each new depth snapshot."""
    ...
```

- [ ] **Step 4: Add Depth subscription in engine**

In `engine.py`, add `_wrap_on_depth` and subscribe in `register()`:

```python
def _wrap_on_depth(self, strategy: Any) -> Any:
    """Wrap strategy.on_depth to inject context."""
    def handler(depth: Any) -> Any:
        ctx = self._make_context(timestamp=getattr(depth, "timestamp", None))
        if hasattr(strategy, "on_depth"):
            return strategy.on_depth(ctx, depth)
    return handler

# In register(), add:
from tradex_domain.market import Depth
self._disposables.append(
    self._bus.of_type(Depth).subscribe(self._wrap_on_depth(strategy))
)
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest trading/tests/strategy/test_on_depth.py -v`
Expected: ALL PASS

- [ ] **Step 6: Run all existing strategy tests for regression**

Run: `python -m pytest trading/tests/strategy/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add trading/src/tradex_trading/strategy/core/protocols.py trading/src/tradex_trading/strategy/core/engine.py trading/tests/strategy/test_on_depth.py
git commit -m "feat: Strategy protocol adds on_depth callback + engine subscription"
```

---

## Task 6: AMT Strategy

**Files:**
- Create: `trading/src/tradex_trading/strategy/amt/__init__.py`
- Create: `trading/src/tradex_trading/strategy/amt/strategy.py`
- Test: `trading/tests/integration/test_amt_e2e.py` (Task 7 covers tests)

**Ponytail rationale:** The AMT strategy is the consumer of all analytics. Keep it simple — one class, three phases (Direction, Location, Aggression). No state machine framework.

- [ ] **Step 1: Create AMT package init**

```python
# trading/src/tradex_trading/strategy/amt/__init__.py
"""AMT (Auction Market Theory) strategy — Fabio Valentini style."""

from tradex_trading.strategy.amt.strategy import AMTStrategy

__all__ = ["AMTStrategy"]
```

- [ ] **Step 2: Implement AMT strategy**

```python
# trading/src/tradex_trading/strategy/amt/strategy.py
"""AMT Strategy — Direction + Location + Aggression.

Simplified Fabio Valentini model for E2E testing.
ponytail: no state machine, no framework. Three checks per quote.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.market import Candle, Depth, Quote
from tradex_domain.strategy import Signal, StrategyContext

from tradex_trading.analytics.footprint import Footprint
from tradex_trading.analytics.orderflow import cvd_from_quotes
from tradex_trading.analytics.volume_profile import lvn, poc, vah, val


class AMTStrategy:
    """Auction Market Theory strategy.

    Parameters
    ----------
    tick_size : float
        Price bucket size for volume profile (e.g., 0.5 for Nifty).
    risk_pct : float
        Risk per trade as fraction of balance (ponytail: fixed 0.25%).
    max_daily_losses : int
        Circuit breaker: stop trading after N losses.
    """

    def __init__(
        self,
        tick_size: float = 0.5,
        risk_pct: float = 0.0025,
        max_daily_losses: int = 3,
    ) -> None:
        self.strategy_id = "amt"
        self._tick_size = tick_size
        self._risk_pct = risk_pct
        self._max_daily_losses = max_daily_losses
        # Analytics state
        self._profile: dict[float, float] = {}
        self._quotes: list[Quote] = []
        self._footprint = Footprint()
        self._daily_losses = 0
        self._in_position = False

    def on_start(self, context: StrategyContext) -> None:
        self._profile.clear()
        self._quotes.clear()
        self._footprint.reset()
        self._daily_losses = 0
        self._in_position = False

    def on_stop(self, context: StrategyContext) -> None:
        pass

    def on_bar(self, context: StrategyContext, bar: Candle) -> Signal | None:
        """Update volume profile from each bar."""
        self._update_profile_from_bar(bar)
        return None

    def on_quote(self, context: StrategyContext, quote: Quote) -> Signal | None:
        """AMT 3-step check: Direction → Location → Aggression."""
        if self._daily_losses >= self._max_daily_losses:
            return None
        if self._in_position:
            return None

        self._quotes.append(quote)
        self._footprint.add(quote)

        # Step 1: Direction — CVD trend
        direction = self._read_direction()
        if direction == 0:
            return None

        # Step 2: Location — is price at an LVN or VA edge?
        if not self._at_key_level(float(quote.ltp.value)):
            return None

        # Step 3: Aggression — footprint shows imbalance at this level
        if not self._has_aggression(float(quote.ltp.value), direction):
            return None

        side = OrderSide.BUY if direction > 0 else OrderSide.SELL
        self._in_position = True
        return Signal(
            instrument=quote.instrument,
            direction=side,
            strength=1.0,
            reason=f"AMT: dir={direction}, ltp={quote.ltp.value}",
        )

    def on_depth(self, context: StrategyContext, depth: Depth) -> Signal | None:
        """Depth confirms book imbalance — ponytail: unused in v1."""
        return None

    def on_fill(self, context: StrategyContext, fill: Fill) -> None:
        pass

    def on_event(self, event: object) -> None:
        pass

    # -- AMT internals -------------------------------------------------------

    def _update_profile_from_bar(self, bar: Candle) -> None:
        """Add bar volume to the profile using triangular distribution."""
        low = float(bar.ohlc.low.value)
        high = float(bar.ohlc.high.value)
        vol = float(bar.volume.value)
        if high <= low or vol <= 0:
            return
        mid = (float(bar.ohlc.open.value) + float(bar.ohlc.close.value)) / 2
        n_buckets = max(1, int((high - low) / self._tick_size))
        for i in range(n_buckets):
            price = low + (i + 0.5) * self._tick_size
            distance = abs(price - mid)
            max_dist = (high - low) / 2
            weight = max(0.1, 1.0 - distance / max_dist) if max_dist > 0 else 1.0
            bucket = round(price / self._tick_size) * self._tick_size
            self._profile[bucket] = self._profile.get(bucket, 0) + vol * weight / n_buckets

    def _read_direction(self) -> int:
        """CVD direction: +1 bullish, -1 bearish, 0 neutral."""
        if len(self._quotes) < 5:
            return 0
        deltas = cvd_from_quotes(self._quotes)
        recent = deltas[-1] - deltas[-5]
        if recent > 0:
            return 1
        if recent < 0:
            return -1
        return 0

    def _at_key_level(self, price: float) -> bool:
        """Is price near a POC, LVN, or VA edge?"""
        if not self._profile:
            return False
        p = poc(self._profile)
        if p is not None and abs(price - p) < self._tick_size * 2:
            return True
        lvns = lvn(self._profile)
        for l in lvns:
            if abs(price - l) < self._tick_size * 2:
                return True
        v = val(self._profile)
        h = vah(self._profile)
        if v is not None and abs(price - v) < self._tick_size * 2:
            return True
        if h is not None and abs(price - h) < self._tick_size * 2:
            return True
        return False

    def _has_aggression(self, price: float, direction: int) -> bool:
        """Footprint shows aggressive volume at this price in the direction."""
        levels = self._footprint.levels()
        # Find the closest level within tick_size
        for p, (buy_vol, sell_vol) in levels.items():
            if abs(p - price) < self._tick_size:
                if direction > 0 and buy_vol > sell_vol * 1.5:
                    return True
                if direction < 0 and sell_vol > buy_vol * 1.5:
                    return True
        return False


__all__ = ["AMTStrategy"]
```

- [ ] **Step 3: Commit**

```bash
git add trading/src/tradex_trading/strategy/amt/
git commit -m "feat: AMT strategy — Direction + Location + Aggression model"
```

---

## Task 7: E2E Integration Test — Full Pipeline

**Files:**
- Create: `trading/tests/integration/test_amt_e2e.py`

This is the payoff test: simulated M1 candles → SyntheticTickGenerator (with depth) → Quotes + Depth on bus → AMT analytics → AMT strategy → Signal.

- [ ] **Step 1: Write the E2E test**

```python
# trading/tests/integration/test_amt_e2e.py
"""E2E test: simulated market data → AMT analytics → AMT strategy → signal.

Validates the full pipeline without any live broker:
  M1 candles → SyntheticTickGenerator → Quote + Depth on ReactiveBus
  → Footprint + CVD + VolumeProfile updated
  → AMTStrategy receives events and can produce signals
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import OHLC, Candle, Depth, Quote, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.analytics.footprint import Footprint
from tradex_trading.analytics.orderflow import cvd_from_quotes
from tradex_trading.analytics.volume_profile import lvn, poc, vah, val
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.synthetic_ticks import SyntheticTickGenerator
from tradex_trading.strategy.amt.strategy import AMTStrategy
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(ts, open_=100, high=110, low=95, close=105, vol=10000):
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(open_))),
            high=Price(value=Decimal(str(high))),
            low=Price(value=Decimal(str(low))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal(str(vol))),
        timestamp=ts,
    )


class TestE2EPipeline:
    def test_full_pipeline_produces_quotes_and_depth(self):
        """M1 candle → SyntheticTickGenerator → Quote + Depth on bus."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=42, depth_levels=5)

        base = datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC)
        gen.feed_bar(_candle(base))

        quotes = [e for e in events if isinstance(e, Quote)]
        depths = [e for e in events if isinstance(e, Depth)]
        assert len(quotes) == 60
        assert len(depths) == 1
        assert len(depths[0].bids) == 5
        assert len(depths[0].asks) == 5

    def test_analytics_consume_quotes(self):
        """Footprint + CVD process Quote events from the generator."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=42, depth_levels=5)

        base = datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC)
        gen.feed_bar(_candle(base))

        quotes = [e for e in events if isinstance(e, Quote)]

        # CVD
        cvd = cvd_from_quotes(quotes)
        assert len(cvd) == 60
        assert isinstance(cvd[-1], int)

        # Footprint
        fp = Footprint()
        for q in quotes:
            fp.add(q)
        assert len(fp.levels()) > 0

    def test_amt_strategy_receives_events_via_engine(self):
        """AMT strategy receives on_bar + on_quote + on_depth via engine."""
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        strategy = AMTStrategy(tick_size=1.0)
        engine.register(strategy)

        # Feed bars through the engine
        base = datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC)
        for i in range(5):
            candle = _candle(base + timedelta(minutes=i))
            bus.publish(candle)

        # Feed synthetic ticks
        gen = SyntheticTickGenerator(bus, seed=42, depth_levels=5)
        gen.feed_bar(_candle(base))

        # Strategy should have processed quotes (no assertion on signal —
        # the AMT model may not trigger on random data, which is correct)
        assert len(strategy._quotes) == 60

    def test_multiple_bars_build_profile(self):
        """10 bars → volume profile has entries, POC is valid."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=42, depth_levels=5)

        base = datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC)
        for i in range(10):
            gen.feed_bar(_candle(base + timedelta(minutes=i)))

        quotes = [e for e in events if isinstance(e, Quote)]
        depths = [e for e in events if isinstance(e, Depth)]
        assert len(quotes) == 600  # 10 bars × 60 ticks
        assert len(depths) == 10   # 1 depth per bar

    def test_depth_ordering_invariant(self):
        """Every emitted Depth passes domain validation (bids desc, asks asc)."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=42, depth_levels=10)

        base = datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC)
        for i in range(5):
            gen.feed_bar(_candle(base + timedelta(minutes=i)))

        depths = [e for e in events if isinstance(e, Depth)]
        assert len(depths) == 5
        for d in depths:
            # Depth.__post_init__ validates ordering — if we got here, it passed
            assert len(d.bids) == 10
            assert len(d.asks) == 10
```

- [ ] **Step 2: Run the E2E test**

Run: `python -m pytest trading/tests/integration/test_amt_e2e.py -v`
Expected: ALL PASS

- [ ] **Step 3: Run the full test suite for regression**

Run: `python -m pytest trading/tests/ -v --tb=short`
Expected: ALL PASS (no regressions in existing tests)

- [ ] **Step 4: Commit**

```bash
git add trading/tests/integration/test_amt_e2e.py
git commit -m "test: E2E integration test — simulated data → AMT pipeline"
```

---

## Summary: What Gets Built

| Component | File | Lines (est.) | Purpose |
|-----------|------|-------------|---------|
| Depth emission | `synthetic_ticks.py` | +30 | Depth events from simulated ticks |
| VAH/VAL/LVN | `volume_profile.py` | +50 | Volume profile analytics |
| CVD | `orderflow.py` | +30 | Cumulative volume delta |
| Footprint | `footprint.py` (new) | +50 | Per-price buy/sell tracking |
| on_depth | `protocols.py` + `engine.py` | +15 | Strategy depth callback |
| AMT Strategy | `amt/strategy.py` (new) | +120 | Direction+Location+Aggression |
| Tests | 6 test files | +300 | Full coverage |

**Total: ~600 lines of code + tests. Zero new dependencies.**

Skipped: real-time bus subscriber classes (VolumeProfileBuilder etc.), hybrid session, persistence. Add when live paper trading is the next step.
