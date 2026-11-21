# Top-Down Remediation — Pass 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two verified P0 correctness defects from `docs/audit/2026-11-21-top-down-module-analysis.md` (late-tick revisions destroyed by a type error; market-session gate reading host-local time) and add the two cheap guards that keep them from returning.

**Architecture:** No new layers, no new abstractions. Two surgical edits at existing seams (`SessionBarAggregator._frame_publisher`, `NSETradingCalendar.is_market_open`) plus one new domain event that lets a corrected bar travel without being mistaken for a new bar, and one guard test that calls an existing unused function. Everything else from the analysis is deliberately deferred (see *Out of scope*).

**Tech Stack:** Python 3.13, pytest, `tradex_domain` frozen dataclasses, RxPY bus (`tradex_reactive`).

**Spec:** `docs/audit/2026-11-21-top-down-module-analysis.md` (findings F1, F5, F8, F16) · `docs/audit/2026-11-21-principal-review-delta.md` (pipeline and parity rules).

## Global Constraints

- Real-money system: prefer correctness over cleverness; a silent wrong answer is worse than a loud failure.
- `pytest tests/test_guardrails.py` must stay green (17 passed at HEAD `da716483`).
- Do not touch `execution/engine.py` order pipeline, the event-store schema/hash chain, or `.pre-commit-config.yaml` ratchets.
- Deletion beats addition; no new dependency.
- Every task ends with its own test and its own commit on `analysis/top-down-review-2026-11-21`.
- Timeframes and session bounds come from `tradex_domain.timeframe` / `tradex_domain.market_calendar`; never re-derive either.

## Review Focus

1. **A late print arriving after a bar closed** — a consumer must see the corrected bar exactly once, and a strategy must not treat it as a new bar.
2. **A print arriving after a fill was booked against that bar** — interim behaviour is that the bar still revises (freeze not yet wired); it must not produce a second order.
3. **A process whose host clock is not IST** — the session gate must answer the same as on an IST host.
4. **A bar revised twice inside the window** — the second revision must build on the first, not on the pre-window bar.
5. **A fill that is currently rejected by cash gate but was already accepted by the venue** — out of scope here, but the fingerprint guard (Task 3) is what makes replayed dedup decisions identical across store/no-store.

---

## Task 1: Deliver late-tick revisions as revisions (F8, P0)

**Files:**
- Modify: `runtime/src/tradex_runtime/bar_aggregator.py:941-951` (`SessionBarAggregator._frame_publisher`)
- Modify: `domain/src/tradex_domain/events.py` (add `BarRevised`)
- Modify: `domain/src/tradex_domain/__init__.py` (export `BarRevised`)
- Test: `trading/tests/parity/test_sba_late_tick_parity.py` (replace the strict xfail)

**Interfaces:**
- Consumes: `RevisedBar` (`runtime/src/tradex_runtime/bar_aggregator.py:191`, fields `frame: BarFrame`, `reason: str`, `window_closes_at: float`, `bar_revisions: int`); `BarFrame`; `ReactiveBus.publish`.
- Produces: `tradex_domain.events.BarRevised` — `@dataclass(frozen=True, slots=True)` with `frame: Any`, `reason: str`, `timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))`, subclassing `DomainEvent` exactly as its siblings in that module do.

**Why not publish a `Candle`:** the existing parity test asserts the corrected bar appears in `bars_seen` (a `Candle` subscriber). But `ReactiveStrategyEngine._wrap_on_bar` (`strategy/src/tradex_strategy/core/engine.py:291-330`) has **no per-bar dedupe** — every `Candle` increments `_bar_count` and calls `strategy.on_bar`. Publishing a revision as a second `Candle` for the same timestamp would double-count bars, skew deferred-order aging (`deferred_at_bar`), and let a strategy emit a second order for a bar it already traded. So the revision travels as its own event: consumers that want the correction subscribe to `BarRevised`; strategies, subscribed to `Candle`, cannot double-fire. The parity test is updated to assert both halves — correction delivered **and** `Candle` count unchanged.

- [ ] **Step 1: Write the failing test** — replace the body of `trading/tests/parity/test_sba_late_tick_parity.py` from `@pytest.mark.xfail(...)` to the end with:

```python
def test_sba_late_tick_parity(bus: ReactiveBus) -> None:
    from tradex_domain.events import BarRevised

    aggregator = wire_bar_aggregation(bus, [INSTRUMENT], now=lambda: 0.0)
    bars_seen: list[Candle] = []
    revisions_seen: list[BarRevised] = []
    bus.of_type(Candle).subscribe(bars_seen.append)
    bus.of_type(BarRevised).subscribe(revisions_seen.append)

    bus.publish(_quote(_stamp("10:15:00"), "100.00"))
    bus.publish(_quote(_stamp("10:15:20"), "100.40"))
    bus.publish(_quote(_stamp("10:16:00"), "102.00"))

    closed_bars = len(bars_seen)
    assert any(c.ohlc.close.value == Decimal("100.40") for c in bars_seen)

    bus.publish(_quote(_stamp("10:15:10"), "103.00"))

    assert len(bars_seen) == closed_bars, "a revision must not look like a new bar"
    assert revisions_seen, "revised bar did not reach any consumer (A4)"
    assert revisions_seen[-1].frame.close == pytest.approx(103.0)
    assert revisions_seen[-1].reason == "late_tick"
```

- [ ] **Step 2: Run it and confirm both failure modes**

Run: `.venv/bin/python -m pytest trading/tests/parity/test_sba_late_tick_parity.py -q`
Expected: FAIL at `assert revisions_seen` (today the `RevisedBar` raises `AttributeError` inside `publish`, swallowed by bus isolation, so nothing is emitted).

- [ ] **Step 3: Add `BarRevised` to `domain/src/tradex_domain/events.py`**

Place it beside the other bus events, following their exact style (frozen, slots, `timestamp` default `datetime.now(UTC)`):

```python
@dataclass(frozen=True, slots=True)
class BarRevised(DomainEvent):
    """A closed bar was corrected by a print that arrived late.

    Its own event, not a second ``Candle``: a strategy already acted on the
    original bar, so a duplicate ``Candle`` would be counted as a new bar and
    could fire a second signal. Consumers that want the correction subscribe
    here; ``Candle`` subscribers keep seeing one bar per timestamp.
    """

    frame: Any
    reason: str
```

Then add `"BarRevised"` to the `__all__` in `domain/src/tradex_domain/__init__.py` and to the `from tradex_domain.events import (...)` block there.

- [ ] **Step 4: Unwrap in `_frame_publisher`** — replace lines 941-951 with:

```python
    def _frame_publisher(self, key: str) -> Callable[[Any], None]:
        """The ``on_frame`` callback for one instrument's aggregator."""
        def publish(frame: Any) -> None:
            # A late tick revises an already-closed bar and arrives wrapped in
            # RevisedBar, which has no ``closed``/``instrument`` of its own —
            # reading them here was an AttributeError swallowed by bus
            # isolation, so every correction vanished.
            revised = frame.frame if isinstance(frame, RevisedBar) else frame
            if self._on_bar_frame is not None:
                self._on_bar_frame(revised)
            if not revised.closed and not self._publish_forming:
                return
            if isinstance(frame, RevisedBar):
                from tradex_domain.events import BarRevised

                self._bus.publish(
                    BarRevised(frame=revised, reason=frame.reason)
                )
                return
            self._bus.publish(self._candle(revised))
        return publish
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest trading/tests/parity/test_sba_late_tick_parity.py -q`
Expected: PASS (1 passed).

- [ ] **Step 6: Run the neighbouring suites that consume bar frames**

Run: `.venv/bin/python -m pytest trading/tests/parity trading/tests/runtime/test_bar_aggregator.py trading/tests/runtime/test_bar_aggregator_late_ticks.py -q`
Expected: PASS. If any test asserts the old swallowing behaviour, fix the test to assert the new contract — do not re-add the `xfail`.

- [ ] **Step 7: Commit**

```bash
git add runtime/src/tradex_runtime/bar_aggregator.py domain/src/tradex_domain/events.py domain/src/tradex_domain/__init__.py trading/tests/parity/test_sba_late_tick_parity.py
git commit -m "fix(runtime): deliver late-tick bar revisions instead of swallowing them"
```

---

## Task 2: Make the session gate host-clock independent (F1, P0)

**Files:**
- Modify: `domain/src/tradex_domain/market_calendar.py:116-126` (`NSETradingCalendar.is_market_open`)
- Test: `domain/tests/test_market_calendar.py` (append)

**Interfaces:**
- Consumes: `IST` (already defined in that module, `:26`), `MARKET_OPEN`/`MARKET_CLOSE`.
- Produces: no signature change — `is_market_open(dt: datetime | None = None) -> bool` keeps its shape so the one production caller (`brokers/src/tradex_brokers/paper/adapter.py:1013`) and existing tests are untouched.

- [ ] **Step 1: Write the failing test** — append to `domain/tests/test_market_calendar.py`:

```python
def test_is_market_open_default_is_ist_not_host_time(monkeypatch) -> None:
    """On a non-IST host the default must still read IST wall clock."""
    from datetime import UTC, datetime

    from tradex_domain import market_calendar

    class _HostClock(datetime):
        @classmethod
        def now(cls, tz=None):
            # 04:30 UTC == 10:00 IST: open. A naive read would compare 04:30.
            instant = datetime(2026, 8, 6, 4, 30, tzinfo=UTC)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(market_calendar, "datetime", _HostClock)
    cal = market_calendar.NSETradingCalendar()
    assert cal.is_market_open() is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest domain/tests/test_market_calendar.py::test_is_market_open_default_is_ist_not_host_time -v`
Expected: FAIL — returns `False` (04:30 naive is before `MARKET_OPEN`).

- [ ] **Step 3: Fix the default** — in `is_market_open`, change

```python
        if dt is None:
            dt = datetime.now()
```

to

```python
        if dt is None:
            # Session bounds are IST wall clock; a naive ``datetime.now()``
            # answers in the host's zone, so a UTC container gates 5h30m off.
            dt = datetime.now(IST)
```

- [ ] **Step 4: Run the test and the calendar suite**

Run: `.venv/bin/python -m pytest domain/tests/test_market_calendar.py trading/tests/runtime/test_calendar.py -q`
Expected: PASS. Existing tests pass naive datetimes explicitly — those are documented as already-IST and are unaffected.

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/market_calendar.py domain/tests/test_market_calendar.py
git commit -m "fix(domain): read IST wall clock in is_market_open default"
```

---

## Task 3: Pin the two fill-fingerprint implementations together (F5, P1)

**Files:**
- Test: `execution/tests/test_fill_fingerprints_agree.py` (create)
- No production change.

**Interfaces:**
- Consumes: `tradex_execution.idempotency.fingerprints_agree() -> bool` (`idempotency.py:320`), which already compares `FillDedup.fingerprint` against `sqlite_event_store.fill_fingerprint` on a sample and has **zero callers** today.
- Produces: nothing for later tasks.

- [ ] **Step 1: Write the test**

```python
"""The in-process and durable fill fingerprints must be one rule, not two."""

from tradex_execution.idempotency import fingerprints_agree


def test_lru_and_store_fingerprints_agree() -> None:
    assert fingerprints_agree(), (
        "FillDedup.fingerprint and sqlite_event_store.fill_fingerprint have "
        "drifted: the same fill would read as fresh in the LRU and duplicate "
        "in the store (or the reverse), so dedup depends on whether a store "
        "happens to be configured."
    )
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest execution/tests/test_fill_fingerprints_agree.py -q`
Expected: PASS. If it fails, that is a real drift bug — stop and report it, do not adjust the assertion.

- [ ] **Step 3: Commit**

```bash
git add execution/tests/test_fill_fingerprints_agree.py
git commit -m "test(execution): pin LRU and durable fill fingerprints to one rule"
```

---

## Task 4: Delete the unused durable-bus mode (F16, P2 — confirm first)

**Files:**
- Delete: `reactive/src/tradex_reactive/event_log.py`
- Delete: `reactive/tests/test_bus_replay_sql.py`
- Modify: `reactive/src/tradex_reactive/{bus.py,thread_safe_bus.py}` (drop the `event_log=` parameter and its docstring branches)

**Interfaces:**
- Consumes: nothing.
- Produces: `ReactiveBus()` / `ThreadSafeReactiveBus()` lose the `event_log` keyword. **Gate this task on a repo-wide check returning no callers outside tests** (verified at plan time: none).

- [ ] **Step 1: Re-verify zero production callers**

Run: `grep -rn "SQLEventLog\|event_log=" --include="*.py" . | grep -v "/.venv/" | grep -v -e "^./reactive/src/tradex_reactive/event_log.py" -e test`
Expected: no output. If anything appears, **skip this task** and report the caller instead.

- [ ] **Step 2: Delete and de-wire, then run the reactive and guard suites**

Run: `rm reactive/src/tradex_reactive/event_log.py reactive/tests/test_bus_replay_sql.py`
Run: `.venv/bin/python -m pytest reactive/tests tests/test_guardrails.py -q`
Expected: PASS. `bus.py:150`'s comment mentioning `SQLEventLog` must be rewritten to describe the deque-only path.

- [ ] **Step 3: Commit**

```bash
git add -A reactive/
git commit -m "refactor(reactive): delete the unused durable bus mode"
```

---

## Out of scope — checked and deliberately deferred

| Item | Why not now |
|---|---|
| Collapse `market_calendar.IST` onto `timezones.IST_ZONE` | **My own analysis was wrong to call this a defect.** `market_calendar.py:17-26` documents that `ZoneInfo.utcoffset(None)` returns `None`, and session-grid arithmetic depends on an offset that answers without a datetime. It is two representations of one zone with a stated reason, not drift. Leave it, and fix the analysis doc's F2 wording. |
| Typed `SessionServices` for the 14 `object`-typed `TradingSession` params | Type-safety only, zero runtime behaviour change, and a wide diff across `startup.py` + tests. Defer until a wiring bug actually bites. |
| Move the indicator stream engine out of `routes/stream_indicators.py` | No frontend exists, so no consumer is harmed today; a 959-LOC move with no user-visible outcome is the opposite of the smallest complete change. |
| Wire `SessionBarAggregator.mark_filled` (traded-bar freeze) | Task 1 already prevents a revision from being counted as a new bar, which was the double-signal risk. Wiring the freeze needs a `frame_for(instrument, timestamp)` accessor on the aggregator and a composition-root decision about which frame a fill was booked from — a real task, not a step. Next plan. |
| Grow `tradex_application`, consolidate `replay_controller`'s five `flush()` sites, close remaining batch-fallback indicators | Hygiene with no correctness payoff; queue behind the above. |

## Self-review

- **Spec coverage:** F8 → Task 1; F1 → Task 2; F5 → Task 3; F16 → Task 4. F2 explicitly retracted above. F9/F11/F13/F14/F17 recorded as deferred with reasons.
- **Step scan:** each step names one action, one command, and the expected result. Test code carries the spec's exact values (`100.40`, `103.00`, 04:30 UTC ↔ 10:00 IST).
- **Type consistency:** `BarRevised.frame` is the unwrapped `BarFrame` in both the producer (Task 1 Step 4) and the test (Step 1); `reason` is `frame.reason` from `RevisedBar`.
- **Risk:** Task 1 changes what a `Candle` subscriber can expect (one bar per timestamp, revisions elsewhere). Any out-of-tree consumer that relied on receiving revisions as `Candle` would see a behavioural change — there is none in this repo, verified by grep.
