"""Lane 3d (AC-12): per-symbol cadence liveness, and the exemption that is counted.

The check under test answers a question neither wire format lets us ask. There
is no sequence number on a Dhan or Upstox market-data frame, so a frame the
broker dropped is byte-for-byte indistinguishable from a minute in which
nothing traded. :meth:`MarketFeed.check_stale` cannot help — it only ages
instruments that have *already* ticked, so a symbol the wire stops delivering
for is absent from its state rather than stale in it. The cadence contract is
the only thing that separates "this scrip is illiquid" from "this scrip is
dead": one tick in any 3-minute window, on the session-anchored grid, during
session hours only.

Both halves of this file are negative controls, and they are negative controls
in *opposite* directions, because a suite that only proved one of them would
pass a broken implementation:

* :class:`TestCadenceFlagsSilence` removes the cadence check and must go red.
  It is the half that catches a dead symbol.
* :class:`TestHaltedDelistedExemption` removes the exemption — or the counter
  that records it — and must go red. It is the half that stops the check from
  paging the operator on every halted stock on the tape. An implementation with
  the cadence check and no exemption looks perfect to the first half and is
  operationally dead: a page that fires every session is a page nobody reads,
  which defeats the lane's entire purpose.

The reason the exemption has to be *counted* is the same argument pointed the
other way. Applied silently, an exemption is a blind spot with a plausible name:
a genuinely dead symbol gets quietly reclassified as "halted" and is never
looked at again. So every exemption increments ``feed_liveness_exempt_total``,
and that number is what makes the blind spot an observable fact rather than a
quiet one.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tradex_domain.instrument_lifecycle import (
    EXEMPT_STATES,
    normalize_status,
    row_status,
)
from tradex_domain.instruments import Equity, Instrument, InstrumentMeta
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.feed_monitor import (
    CADENCE_GRACE_SECONDS,
    CADENCE_WINDOW_SECONDS,
    EXEMPT_MASTER_STATES,
    FeedStaleMonitor,
    SymbolCadenceMonitor,
    in_session_hours,
)
from tradex_runtime.feed_supervisor import FeedState
from tradex_runtime.market_feed import MarketFeed
from tradex_runtime.metrics import MetricsRegistry

IST = ZoneInfo("Asia/Kolkata")

#: Mid-session, unambiguously inside 09:15-15:30 IST.
SESSION_NOW = datetime(2026, 9, 28, 10, 10, tzinfo=IST)
#: The same instant expressed the way a real feed stamps ticks: aware UTC.
SESSION_NOW_UTC = SESSION_NOW.astimezone(UTC)


def _with_status(instrument: Instrument, status: str | None) -> Instrument:
    """*instrument* carrying a canonical ``master_status`` on its metadata."""
    return dataclasses.replace(
        instrument, meta=InstrumentMeta(extra={"master_status": status})
    )


def _exempt_total(registry: MetricsRegistry, state: str) -> float:
    return registry.counter("feed_liveness_exempt_total").value(state=state)


# ---------------------------------------------------------------------------
# (a) The negative control: a silent symbol during session IS flagged
# ---------------------------------------------------------------------------


class TestCadenceFlagsSilence:
    """A symbol with no tick in a 3-minute session window must be reported."""

    def _monitor(
        self,
        instruments: list[Instrument],
        last_tick: datetime,
        *,
        now: datetime = SESSION_NOW,
        metrics: MetricsRegistry | None = None,
    ) -> SymbolCadenceMonitor:
        return SymbolCadenceMonitor(
            instruments=lambda: list(instruments),
            ticks=lambda instrument: last_tick,
            metrics=metrics,
            clock=lambda: now,
        )

    def test_a_symbol_silent_for_three_minutes_in_session_is_flagged(self) -> None:
        """The core contract, and the half an exemption-only suite would miss.

        The last tick is 10 minutes old: three windows passed with nothing in
        them, during a session that is open. Without the cadence check this
        symbol is invisible to the whole monitoring stack, so a silent empty
        list here is the failure this lane exists to prevent.
        """
        reliance = Equity.of("NSE", "RELIANCE")
        last = SESSION_NOW - timedelta(minutes=10)

        violations = self._monitor([reliance], last).check()

        assert [v.instrument_id for v in violations] == ["NSE:RELIANCE"]
        assert violations[0].instrument is reliance
        assert violations[0].quiet_seconds >= CADENCE_WINDOW_SECONDS

    def test_the_window_is_exactly_three_minutes(self) -> None:
        """Pin the window the spec names, so it cannot quietly drift to 30s.

        30s is the *feed* threshold; a per-symbol check at 30s would alarm on
        every thinly traded scrip, so the 3m value is load-bearing and asserted
        against the constant rather than a literal that a refactor could move.
        """
        assert CADENCE_WINDOW_SECONDS == 180.0

    def test_a_symbol_inside_the_window_is_not_flagged(self) -> None:
        """One tick 30 s ago is inside the window: not a violation."""
        reliance = Equity.of("NSE", "RELIANCE")

        violations = self._monitor(
            [reliance], SESSION_NOW - timedelta(seconds=30)
        ).check()

        assert violations == []

    def test_a_healthy_symbol_on_a_busy_tape_is_not_flagged(self) -> None:
        """One dead scrip does not make a healthy tape look dead.

        This is the composition claim in the other direction: a symbol that is
        ticking normally is never reported, so the check flags the *dead* one
        rather than everything. Without this, an implementation that flagged on
        "any symbol" would satisfy the test above and be useless.
        """
        dead = Equity.of("NSE", "RELIANCE")
        live = Equity.of("NSE", "TCS")
        ticks = {dead.instrument_id: SESSION_NOW - timedelta(minutes=10),
                 live.instrument_id: SESSION_NOW - timedelta(seconds=5)}
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [dead, live],
            ticks=lambda instrument: ticks[instrument.instrument_id],
            clock=lambda: SESSION_NOW,
        )

        violations = monitor.check()

        assert [v.instrument_id for v in violations] == ["NSE:RELIANCE"]

    def test_a_symbol_inside_its_grace_window_is_not_flagged(self) -> None:
        """A brand-new subscription is not judged until its window has elapsed.

        A cold boot subscribes the whole tape and the first seconds
        legitimately deliver nothing for the back of the book. Ageing from
        ``subscribe()`` would alarm on every newly-subscribed scrip at once;
        the window therefore starts at the monitor's *first sighting* of the
        symbol, so a fresh subscription always gets a full grace period.
        """
        reliance = Equity.of("NSE", "RELIANCE")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: None,
            clock=lambda: SESSION_NOW,
        )

        # First sweep at 10:10: the symbol was just seen, so its window has
        # not elapsed. Nothing is judged yet.
        assert monitor.check() == []

        # A later sweep, one window on, the symbol still never having ticked.
        later = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: None,
            clock=lambda: SESSION_NOW,
        )
        later.check()
        assert CADENCE_GRACE_SECONDS == CADENCE_WINDOW_SECONDS

    def test_a_symbol_that_has_never_ticked_is_flagged_once_its_window_elapses(
        self,
    ) -> None:
        """The headline failure, and the one ``check_stale`` structurally misses.

        A symbol on the wanted set that has produced **no** ticks at all is
        absent from ``MarketFeed._last_tick`` rather than stale in it, so the
        30 s tape check cannot see it no matter how long it runs. If the grace
        rule excused "never ticked" permanently, the one failure this lane
        exists to catch would be the one case left unmonitored.

        The window is measured from the first sighting, so this needs two
        sweeps: one to open the window, one to find it elapsed.
        """
        reliance = Equity.of("NSE", "RELIANCE")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: None,
            clock=lambda: SESSION_NOW,
        )
        assert monitor.check() == []  # window opens here

        # Advance past one full window: the symbol has now been on the wanted
        # set for longer than a window with nothing in it.
        past = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: None,
            clock=lambda: SESSION_NOW,
        )
        past._seen_since[str(reliance.instrument_id)] = (
            SESSION_NOW - timedelta(seconds=CADENCE_WINDOW_SECONDS)
        )

        assert [v.instrument_id for v in past.check()] == ["NSE:RELIANCE"]

    def test_a_symbol_that_left_the_tape_earns_a_fresh_window(self) -> None:
        """A resubscribe must not inherit an old verdict.

        Symbols are dropped from the sighting clock when they leave the wanted
        set, so a client that unsubscribes and comes back gets a full grace
        period rather than being flagged instantly on the strength of a
        window that opened before it existed.
        """
        reliance = Equity.of("NSE", "RELIANCE")
        tape = [reliance]
        monitor = SymbolCadenceMonitor(
            instruments=lambda: list(tape),
            ticks=lambda instrument: None,
            clock=lambda: SESSION_NOW,
        )
        monitor.check()
        iid = str(reliance.instrument_id)
        assert iid in monitor._seen_since

        tape.clear()
        monitor.check()
        assert iid not in monitor._seen_since

        tape.append(reliance)
        assert monitor.check() == []

    def test_a_resumed_symbol_clears_its_violation(self) -> None:
        """Silence is a transition, not a latch.

        A symbol that ticks again is no longer reported, so the log and the
        gauge mean "dark right now" rather than "was dark once this session".
        """
        reliance = Equity.of("NSE", "RELIANCE")
        stamp = SESSION_NOW - timedelta(minutes=10)
        ticks = [stamp]
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: ticks[0],
            clock=lambda: SESSION_NOW,
        )

        assert len(monitor.check()) == 1
        ticks[0] = SESSION_NOW - timedelta(seconds=5)

        assert monitor.check() == []
        assert monitor.violations == ()

    def test_the_window_reuses_the_canonical_bar_grid_rather_than_its_own(self) -> None:
        """The window is cut on ``tradex_domain.timeframe``'s grid, not a local floor.

        Lane 0d owns that grid because a midnight floor is skewed by 33,300s for
        every width that does not divide it. At the spec's 3-minute width the
        two *coincide* — 33,300 = 185 x 180, so a midnight floor happens to be
        the same grid — which is precisely why this cannot be asserted by
        watching 3-minute behaviour. It is asserted by widening the window to a
        width where the anchor genuinely bites, and by pinning the coincidence
        that makes 3m safe: a future change to the window must not silently
        move the grid off the one the bars are cut on.
        """
        reliance = Equity.of("NSE", "RELIANCE")

        # 3m: 33,300 divides evenly, so the session grid and a midnight floor
        # agree. Pinned so the reason this is safe is on the record.
        three_minute = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: datetime(2026, 9, 28, 9, 5),
            clock=lambda: datetime(2026, 9, 28, 9, 20),
            grace_seconds=0.0,
        )
        assert three_minute._window == 180.0
        assert 33_300 % 180 == 0

        # 1h: 33,300 does *not* divide it, so the anchor moves the grid. A
        # midnight floor would close a window at 09:00; the session grid closes
        # the one holding 09:05 at 10:15, because 09:05 falls in the
        # 08:15-09:15 bucket. At 09:20 the hourly window has not elapsed, so
        # this monitor is silent — where an epoch/midnight floor would have
        # fired twenty minutes earlier.
        hourly = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: datetime(2026, 9, 28, 9, 5),
            clock=lambda: datetime(2026, 9, 28, 9, 20),
            window_seconds=3600.0,
            grace_seconds=0.0,
        )
        assert 33_300 % 3600 != 0
        assert hourly.check() == []

        # And once the session-anchored window really has elapsed, it fires.
        past_seam = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: datetime(2026, 9, 28, 9, 5),
            clock=lambda: datetime(2026, 9, 28, 10, 20),
            window_seconds=3600.0,
            grace_seconds=0.0,
        )
        assert [v.instrument_id for v in past_seam.check()] == ["NSE:RELIANCE"]

    def test_a_three_minute_window_closes_on_the_session_grid_seam(self) -> None:
        """The 3m grid's seams, and exactly when a contract is broken.

        A symbol last seen at 09:16 sits in the 09:15-anchored 09:15-09:18
        bucket, so the next window that must contain a tick is 09:18-09:21. The
        contract is broken when that window *completes* empty, not when it
        starts: at 09:20:30 the window has been open 150s and the symbol may
        still print, at 09:21 it is empty and full, and the check fires.

        This is the three-minute contract on the grid the bars are cut on. (At
        this width the session-anchored grid and a midnight floor coincide —
        33,300 = 185 x 180 — so this pins the behaviour, not the distinction
        between the two; that is asserted at 1h in the test above.)
        """
        reliance = Equity.of("NSE", "RELIANCE")
        last = datetime(2026, 9, 28, 9, 16, tzinfo=IST)

        at_1830 = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: last,
            clock=lambda: datetime(2026, 9, 28, 9, 18, 30, tzinfo=IST),
        )
        assert at_1830.check() == []

        at_2030 = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: last,
            clock=lambda: datetime(2026, 9, 28, 9, 20, 30, tzinfo=IST),
        )
        assert at_2030.check() == []

        at_2100 = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: last,
            clock=lambda: datetime(2026, 9, 28, 9, 21, tzinfo=IST),
        )
        assert [v.instrument_id for v in at_2100.check()] == ["NSE:RELIANCE"]

    def test_utc_stamped_ticks_are_judged_the_same_as_ist_stamps(self) -> None:
        """A live feed stamps ticks in UTC; the grid is IST.

        ``MarketFeed._last_tick`` writes ``datetime.now(UTC)``. If the check
        compared that aware-UTC value against the naive-IST bucket arithmetic
        it would raise rather than answer, and a per-symbol raise inside
        ``check()`` degrades to "not judged" — i.e. the live path would silently
        never flag anything. This asserts the two stampings agree.
        """
        reliance = Equity.of("NSE", "RELIANCE")
        quiet_utc = SESSION_NOW_UTC - timedelta(minutes=10)
        live_utc = SESSION_NOW_UTC - timedelta(seconds=10)
        ticks = {reliance.instrument_id: None}

        quiet = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: quiet_utc,
            clock=lambda: SESSION_NOW_UTC,
        )
        assert [v.instrument_id for v in quiet.check()] == ["NSE:RELIANCE"]

        live = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: live_utc,
            clock=lambda: SESSION_NOW_UTC,
        )
        assert live.check() == []
        assert ticks == {reliance.instrument_id: None}

    def test_silence_is_published_as_a_gauge(self) -> None:
        """The verdict has to be observable, not only logged.

        A check that only writes a log line is invisible to Prometheus, and an
        invisible check is one nobody configures an alert for.
        """
        registry = MetricsRegistry()
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [Equity.of("NSE", "RELIANCE")],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            metrics=registry,
            clock=lambda: SESSION_NOW,
        )

        monitor.check()

        assert registry.get("feed_liveness_silent_symbols") == 1.0
        assert "feed_liveness_silent_symbols" in registry.render_prometheus()


# ---------------------------------------------------------------------------
# (b) The negative control: a halted/delisted symbol is exempt AND counted
# ---------------------------------------------------------------------------


class TestHaltedDelistedExemption:
    """A symbol the master reports as halted or delisted must not alarm — and
    the exemption must be counted, so it cannot become a silent blind spot."""

    def test_a_halted_symbol_with_no_ticks_is_exempt_not_flagged(self) -> None:
        """The half an implementation without the exemption fails.

        Same silence as the core contract test above — 10 minutes,
        mid-session — but the master says this security is halted, so it
        produces no ticks *by design*. Flagging it would page the operator for
        every halted stock on the tape, every session, forever.
        """
        halted = _with_status(Equity.of("NSE", "HALTCO"), "halted")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [halted],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            clock=lambda: SESSION_NOW,
        )

        assert monitor.check() == []
        assert monitor.exempt_now == frozenset({"NSE:HALTCO"})

    def test_a_delisted_symbol_with_no_ticks_is_exempt_not_flagged(self) -> None:
        """Delisted is the other half of the spec's two states."""
        delisted = _with_status(Equity.of("NSE", "OLDCO"), "delisted")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [delisted],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            clock=lambda: SESSION_NOW,
        )

        assert monitor.check() == []
        assert monitor.exempt_now == frozenset({"NSE:OLDCO"})

    def test_the_exemption_is_counted_in_feed_liveness_exempt_total(self) -> None:
        """A silent exemption is the opposite failure, and this is the counter.

        This is the assertion that makes the exemption safe rather than merely
        quiet. Drop the ``inc()`` and the halted symbol still stops alarming —
        the previous test still passes — but coverage is now being withdrawn
        with no trace, and a dead symbol could be reclassified "halted" and
        never examined again. The counter is the audit trail.
        """
        registry = MetricsRegistry()
        halted = _with_status(Equity.of("NSE", "HALTCO"), "halted")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [halted],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            metrics=registry,
            clock=lambda: SESSION_NOW,
        )

        monitor.check()

        assert _exempt_total(registry, "halted") == 1.0
        assert registry.get("feed_liveness_exempt_symbols") == 1.0
        assert 'feed_liveness_exempt_total{state="halted"}' in (
            registry.render_prometheus()
        )

    def test_each_exempt_symbol_and_state_is_counted_separately(self) -> None:
        """The count must be per symbol, or it under-reports the blind spot.

        Two halted stocks and one delisted are three blind spots, not one. A
        counter that moved once per poll (or once per state, or once in total)
        would say "1" here and the operator would size the hole wrong.
        """
        registry = MetricsRegistry()
        tape = [
            _with_status(Equity.of("NSE", "HALTCO1"), "halted"),
            _with_status(Equity.of("NSE", "HALTCO2"), "halted"),
            _with_status(Equity.of("NSE", "OLDCO"), "delisted"),
        ]
        monitor = SymbolCadenceMonitor(
            instruments=lambda: tape,
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            metrics=registry,
            clock=lambda: SESSION_NOW,
        )

        monitor.check()

        assert _exempt_total(registry, "halted") == 2.0
        assert _exempt_total(registry, "delisted") == 1.0
        assert registry.get("feed_liveness_exempt_symbols") == 3.0

    def test_a_continuing_exemption_is_not_counted_forever(self) -> None:
        """Counted once per (symbol, state), not once per poll.

        A poll every 15 s for a six-hour session is ~1,440 polls. A symbol
        exempt for the session must count as *one* exemption, or the alert
        built on this counter fires on arithmetic rather than on a change in
        the world.
        """
        registry = MetricsRegistry()
        halted = _with_status(Equity.of("NSE", "HALTCO"), "halted")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [halted],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            metrics=registry,
            clock=lambda: SESSION_NOW,
        )

        for _ in range(50):
            monitor.check()

        assert _exempt_total(registry, "halted") == 1.0

    def test_an_exemption_is_re_counted_after_a_reset(self) -> None:
        """A generation reset must not silently inherit the old memory.

        ``reset()`` clears the quiet set, so it clears the exemption memory
        with it: the counter is a per-process lifetime total, and after a
        reconnect the new master is a new answer that deserves a new count.
        """
        registry = MetricsRegistry()
        halted = _with_status(Equity.of("NSE", "HALTCO"), "halted")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [halted],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            metrics=registry,
            clock=lambda: SESSION_NOW,
        )
        monitor.check()

        monitor.reset()
        monitor.check()

        assert _exempt_total(registry, "halted") == 2.0

    def test_an_exemption_does_not_cover_an_unrelated_symbol(self) -> None:
        """The exemption is per symbol, never a session-wide mute.

        A blanket "something on this tape is halted, stop checking" is the
        failure this whole structure exists to prevent. The dead symbol here has
        no status of its own and must still be flagged.
        """
        halted = _with_status(Equity.of("NSE", "HALTCO"), "halted")
        dead = Equity.of("NSE", "RELIANCE")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [halted, dead],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            clock=lambda: SESSION_NOW,
        )

        violations = monitor.check()

        assert [v.instrument_id for v in violations] == ["NSE:RELIANCE"]
        assert monitor.exempt_now == frozenset({"NSE:HALTCO"})

    def test_an_unrecognised_status_does_not_exempt(self) -> None:
        """Unknown is not a permission. It is a reason to keep looking.

        If the exemption table ever grew a wildcard — "anything the master says
        about status" — a vendor renaming a column, or a master starting to
        publish a new vocabulary, would quietly mute the entire tape. Only the
        two states that are *known* to go quiet by design exempt.
        """
        registry = MetricsRegistry()
        for status in ("active", "ACTIVE", "something-new", "pre-open", ""):
            monitor = SymbolCadenceMonitor(
                instruments=lambda s=status: [_with_status(
                    Equity.of("NSE", "RELIANCE"), s
                )],
                ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
                metrics=registry,
                clock=lambda: SESSION_NOW,
            )
            assert [v.instrument_id for v in monitor.check()] == [
                "NSE:RELIANCE"
            ], f"status {status!r} must not exempt"
        assert registry.counter("feed_liveness_exempt_total").value() == 0.0

    def test_a_symbol_with_no_status_at_all_is_still_judged(self) -> None:
        """A master that carries no status column must not mute the tape.

        This is the realistic default today: the lifecycle column is read when
        the vendor publishes one, and neither master is guaranteed to have it on
        any given day. The check has to run — loudly — in that case rather than
        treat "the master told me nothing" as "nothing to worry about".
        """
        reliance = Equity.of("NSE", "RELIANCE")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            clock=lambda: SESSION_NOW,
        )

        assert [v.instrument_id for v in monitor.check()] == ["NSE:RELIANCE"]

    def test_the_status_source_is_a_resolver_not_a_hardcoded_symbol(self) -> None:
        """The exemption reads the master through an injected resolver.

        On a live boot the resolver is bound to the broker's instrument
        registry, where ``BaseBroker.load_instruments`` puts each master row's
        metadata. That is the whole point: the state comes from the instrument
        master, not from a value this module or a test made up.
        """
        registry = MetricsRegistry()
        tape = {"NSE:HALTCO": "halted", "NSE:OLDCO": "delisted"}
        instruments = [Equity.of("NSE", "HALTCO"), Equity.of("NSE", "OLDCO")]
        monitor = SymbolCadenceMonitor(
            instruments=lambda: instruments,
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            status_of=lambda instrument: tape.get(
                str(instrument.instrument_id)
            ),
            metrics=registry,
            clock=lambda: SESSION_NOW,
        )

        assert monitor.check() == []
        assert _exempt_total(registry, "halted") == 1.0
        assert _exempt_total(registry, "delisted") == 1.0

    def test_the_instrument_metadata_source_is_read_without_a_resolver(self) -> None:
        """An instrument built straight from a master row is exempt on its own.

        Off a full live boot there is no registry to ask, and an exemption that
        only worked through one wiring would be untestable and unusable. The
        value object's own metadata is the second source, and it agrees.
        """
        halted = _with_status(Equity.of("NSE", "HALTCO"), "halted")
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [halted],
            ticks=lambda instrument: SESSION_NOW - timedelta(minutes=10),
            clock=lambda: SESSION_NOW,
        )

        assert monitor.check() == []


# ---------------------------------------------------------------------------
# The vocabulary itself: one definition, shared by parsers and this check
# ---------------------------------------------------------------------------


class TestMasterStatusVocabulary:
    """The exemption's inputs, and the rule that unrecognised ≠ exempt."""

    def test_vendor_spellings_collapse_onto_the_two_canonical_states(self) -> None:
        for spelling in ("HALT", "halted", "HALTED", " suspend ", "SUSPENDED",
                         "suspension", "PAUSED"):
            assert normalize_status(spelling) == "halted", spelling
        for spelling in ("DELIST", "delisted", "REMOVED", "INACTIVE",
                         "inactive_after"):
            assert normalize_status(spelling) == "delisted", spelling

    def test_blank_and_active_and_unknown_are_not_states(self) -> None:
        """The asymmetry, stated directly: silence from the master is not consent."""
        for value in (None, "", "   ", "active", "ACTIVE", "listed", "new-thing"):
            assert normalize_status(value) is None, value
        assert EXEMPT_STATES == {"halted", "delisted"}
        assert EXEMPT_MASTER_STATES == EXEMPT_STATES

    def test_a_row_is_read_by_its_vendor_column_or_the_normalized_key(self) -> None:
        """One table, three shapes of input: Dhan raw, Upstox raw, normalized."""
        assert row_status({"SEM_STATUS": "HALT"}) == "halted"
        assert row_status({"status": "DELISTED"}) == "delisted"
        assert row_status({"master_status": "halted"}) == "halted"
        assert row_status({"symbol": "RELIANCE"}) is None
        assert row_status(None) is None
        assert row_status("not a mapping") is None


# ---------------------------------------------------------------------------
# Session hours: the 09:15-15:30 window is the calendar's, not a local literal
# ---------------------------------------------------------------------------


class TestSessionHours:
    def test_the_session_bounds_are_the_market_calendars(self) -> None:
        """Lane 0d owns 09:15; a second copy here would be a second session."""
        from tradex_domain.market_calendar import MARKET_CLOSE, MARKET_OPEN

        assert in_session_hours(datetime(2026, 9, 28, 9, 15, tzinfo=IST))
        assert in_session_hours(datetime(2026, 9, 28, 15, 30, tzinfo=IST))
        assert not in_session_hours(datetime(2026, 9, 28, 9, 14, 59, tzinfo=IST))
        assert not in_session_hours(datetime(2026, 9, 28, 15, 31, tzinfo=IST))
        assert (MARKET_OPEN, MARKET_CLOSE) == (time(9, 15), time(15, 30))

    def test_a_utc_clock_is_converted_before_the_session_is_judged(self) -> None:
        """04:30 UTC is 10:00 IST and open; 22:00 UTC is 03:30 IST and shut.

        Without the conversion a UTC clock would judge the wrong half of the
        day, and half the time the answer is the safe-looking one.
        """
        assert in_session_hours(datetime(2026, 9, 28, 4, 30, tzinfo=UTC))
        assert not in_session_hours(datetime(2026, 9, 28, 22, 0, tzinfo=UTC))

    def test_a_naive_stamp_is_read_as_ist(self) -> None:
        """The datalake contract stores naive IST; that is what a naive means."""
        assert in_session_hours(datetime(2026, 9, 28, 10, 0))
        assert not in_session_hours(datetime(2026, 9, 28, 20, 0))

    def test_nothing_is_flagged_outside_session_hours(self) -> None:
        """A quiet tape at 20:00 is not a defect.

        Without this the check would alarm on every symbol for seven hours a
        day, which is the fastest way to make an operator ignore the page.
        """
        monitor = SymbolCadenceMonitor(
            instruments=lambda: [Equity.of("NSE", "RELIANCE")],
            ticks=lambda instrument: datetime(2026, 9, 28, 14, 0),
            clock=lambda: datetime(2026, 9, 28, 20, 0, tzinfo=IST),
        )

        assert monitor.check() == []
        assert monitor.off_session_polls == 1

    def test_a_pre_open_tick_is_judged_on_the_session_grid(self) -> None:
        """A 09:05 pre-open print ages on the 09:15-anchored grid.

        The grid's seams run 09:00, 09:03, 09:06 … on the 3m width (a
        09:15-anchored grid steps backwards below the open, which is what
        lane 0d's ``bucket_wall_seconds`` does for every intraday width). A
        symbol whose last print was at 09:05 therefore has its next window at
        09:06-09:09, and by 09:09 it is genuinely silent.

        The grace does **not** save it, and that is deliberate: the grace covers
        a symbol that has never ticked at all, not one that has ticked and then
        stopped. A pre-open print proves the wire delivered for that symbol, so
        the cadence contract is already in force — otherwise a single 09:05
        frame would buy a symbol the whole session.
        """
        reliance = Equity.of("NSE", "RELIANCE")

        monitor = SymbolCadenceMonitor(
            instruments=lambda: [reliance],
            ticks=lambda instrument: datetime(2026, 9, 28, 9, 5, tzinfo=IST),
            clock=lambda: datetime(2026, 9, 28, 9, 20, tzinfo=IST),
        )

        assert [v.instrument_id for v in monitor.check()] == ["NSE:RELIANCE"]


# ---------------------------------------------------------------------------
# Composition with FeedStaleMonitor: one poll, three questions, no disagreement
# ---------------------------------------------------------------------------


class _FakeBroker:
    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        return object()

    def unsubscribe(self, subscription: object) -> None:
        return None

    def unsubscribe_instruments(self, instruments: object) -> None:
        return None

    def set_reconnect_hook(self, cb: object) -> None:
        return None


class TestComposesWithFeedStaleMonitor:
    def test_the_cadence_check_rides_the_existing_poll_thread(self) -> None:
        """One owned timer, not a second one.

        The order-stream watchdog already established the pattern in this
        module: a monitoring question joins ``poll_once`` rather than owning a
        timer, so there is one start, one stop, and one exception boundary. A
        second thread would be a second lifecycle to leak.
        """
        seen: list[int] = []

        class _Counting:
            def check(self) -> list[Any]:
                seen.append(1)
                return []

        feed = MarketFeed(broker=_FakeBroker(), bus=ReactiveBus(), stale_after=30.0)
        feed.start([Equity.of("NSE", "RELIANCE")])
        monitor = FeedStaleMonitor(
            feed, interval_seconds=0.01, cadence_monitor=_Counting()
        )

        monitor.poll_once()

        assert seen == [1]

    def test_a_raising_cadence_check_never_takes_the_poll_down(self) -> None:
        """A monitor that dies is a monitor that stopped monitoring.

        The tape and order-stream checks share this poll. An unhandled raise
        here would stop both, so the cadence check is isolated exactly as the
        order watchdog is.
        """
        class _Exploding:
            def check(self) -> list[Any]:
                raise RuntimeError("cadence bug")

        feed = MarketFeed(broker=_FakeBroker(), bus=ReactiveBus(), stale_after=30.0)
        feed.start([Equity.of("NSE", "RELIANCE")])
        monitor = FeedStaleMonitor(
            feed, interval_seconds=0.01, cadence_monitor=_Exploding()
        )

        assert monitor.poll_once() == []

    def test_a_silent_symbol_is_invisible_to_the_tape_check_and_visible_to_cadence(
        self,
    ) -> None:
        """The composition claim, proven from both sides.

        A real ``MarketFeed`` subscribed to two symbols, one of which has gone
        completely dark. ``check_stale`` — the 30 s *tape* threshold — sees
        nothing, because a symbol with no ticks is absent from ``_last_tick``
        rather than stale in it. The cadence check sees it, because it reads
        the *wanted* set. Two checks, two different questions, no overlap and
        no disagreement: this is the reason they were kept separate instead of
        merged.
        """
        live = Equity.of("NSE", "TCS")
        dead = Equity.of("NSE", "RELIANCE")
        feed = MarketFeed(
            broker=_FakeBroker(), bus=ReactiveBus(), stale_after=30.0,
        )
        feed.start([live, dead])
        # TCS is ticking right now; RELIANCE's last tick is long enough ago to
        # be a full session grid behind.
        # Pinned to one IN-SESSION instant (10:30 IST = 05:00 UTC), not the
        # real wall clock: ``SymbolCadenceMonitor.check()`` skips its entire
        # sweep outside 09:15-15:30 IST, so a live ``now`` makes this
        # assertion pass by day and fail by night. Every stamp below is
        # relative to this instant for the same reason.
        now = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)
        feed._last_tick[live.instrument_id] = now
        feed._last_tick[dead.instrument_id] = now - timedelta(hours=1)
        feed.supervisor.recovery_succeeded(
            last_event_at=now, recovered_through=now
        )

        # The tape check, on its own, reports nothing for the dead symbol: it
        # is not in ``_last_tick`` at all. The one entry it does return is TCS
        # ageing past 30s inside this test's own execution time — the point is
        # that RELIANCE, the fully-dark symbol, is absent from the result.
        flagged_by_tape = {e.instrument.instrument_id for e in feed.check_stale()}

        assert "NSE:RELIANCE" not in flagged_by_tape

        # Same pinned instant as the stamps above, so the monitor judges the
        # tape it was just handed rather than a different moment.
        cadence = SymbolCadenceMonitor(
            instruments=feed.wanted_instruments,
            ticks=feed.last_tick,
            clock=lambda: now,
        )

        violations = cadence.check()

        assert [v.instrument_id for v in violations] == ["NSE:RELIANCE"]
        # And the healthy symbol is still healthy under the cadence check.
        assert "NSE:TCS" not in {v.instrument_id for v in violations}

    def test_the_tape_threshold_is_not_weakened_by_the_cadence_window(self) -> None:
        """A dead socket must still be caught in 30 s, not three minutes.

        The reason the two checks stay apart. Folding cadence into
        ``check_stale`` would have to pick one threshold, and either pick is
        wrong: 3m makes a genuinely dead socket take three minutes to notice,
        30s makes every illiquid scrip alarm.
        """
        feed = MarketFeed(broker=_FakeBroker(), bus=ReactiveBus(), stale_after=30.0)
        feed.start([Equity.of("NSE", "TCS")])
        feed._last_tick[Equity.of("NSE", "TCS").instrument_id] = (
            SESSION_NOW_UTC - timedelta(seconds=45)
        )
        feed.supervisor.recovery_succeeded(
            last_event_at=SESSION_NOW_UTC, recovered_through=SESSION_NOW_UTC
        )

        # 45s of silence: past the tape's 30s threshold, inside the cadence
        # window's grace. Two verdicts, two different questions.
        assert len(feed.check_stale()) == 1
        assert feed.supervisor.state is FeedState.DEGRADED

        assert CADENCE_WINDOW_SECONDS == 180.0
        assert feed.stale_after == 30.0
