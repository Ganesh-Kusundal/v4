"""Tests for the MarketFeed live-tick bridge (runtime.market_feed)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.instruments import Equity, Index, Instrument
from tradex_domain.market import Depth, Quote
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.market_feed import FeedRegistry, MarketFeed


class TestDepthExchangeGate:
    """Depth is NSE-only: non-NSE depth requests fail loudly."""

    def test_subscribe_depth_gates_non_nse(self) -> None:
        feed, _, _ = _make()
        with pytest.raises(CapabilityNotSupportedError, match="NSE"):
            feed.subscribe([Equity.of("MCX", "CRUDEOIL")], depth="30")
        assert feed.active is False

    def test_start_depth_gates_non_nse(self) -> None:
        feed, _, _ = _make()
        with pytest.raises(CapabilityNotSupportedError, match="NSE"):
            feed.start([Equity.of("MCX", "CRUDEOIL")], depth="20")
        assert feed.active is False

    def test_quote_only_non_nse_allowed(self) -> None:
        feed, _, _ = _make()
        feed.subscribe([Equity.of("MCX", "CRUDEOIL")])
        assert feed.active is True

    def test_quote_only_bse_allowed(self) -> None:
        feed, _, _ = _make()
        feed.subscribe([Equity.of("BSE", "RELIANCE")])
        assert feed.active is True

    def test_depth_gates_bse_and_idx(self) -> None:
        feed, _, _ = _make()
        for inst in (Equity.of("BSE", "RELIANCE"), Index.of("IDX", "NIFTY")):
            with pytest.raises(CapabilityNotSupportedError, match="NSE"):
                feed.subscribe([inst], depth="30")
        assert feed.active is False


def _reliance() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _tcs() -> Instrument:
    return Equity.of("NSE", "TCS")


def _quote(instrument: Instrument, ltp: str = "1319.2") -> Quote:
    return Quote(instrument=instrument, ltp=Price(value=Decimal(ltp)))


def _depth(instrument: Instrument) -> Depth:
    return Depth(
        instrument=instrument,
        bids=((Price(value=Decimal("1319.0")), Quantity(value=Decimal("10"))),),
        asks=((Price(value=Decimal("1319.5")), Quantity(value=Decimal("5"))),),
    )


class _FakeBroker:
    """Records subscribe/unsubscribe calls; handlers can be fired manually.

    ``subscribe_depth_30`` only exists when ``has_depth_30`` is True — the
    feed selects it via ``getattr`` — so tests can exercise both the Upstox
    path (depth-30 present) and the Dhan fallback path (absent).
    """

    def __init__(self, *, has_depth_30: bool = False) -> None:
        self.has_depth_30 = has_depth_30
        self.quote_subs: list[tuple[list[Instrument], Any]] = []
        self.depth_subs: list[tuple[list[Instrument], Any]] = []
        self.depth_30_subs: list[tuple[list[Instrument], Any]] = []
        self.unsubscribed: list[object] = []
        self.unsubscribed_instruments_calls: list[list[Instrument]] = []
        self._quote_handlers: dict[str, Any] = {}
        self._depth_handlers: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name == "subscribe_depth_30" and self.has_depth_30:
            return self._subscribe_depth_30
        raise AttributeError(name)

    def subscribe_quotes(self, instruments: list[Instrument], handler: Any) -> str:
        self.quote_subs.append((instruments, handler))
        sub = f"q-{id(handler)}"  # real backends key by id(handler) — mirror them
        self._quote_handlers[sub] = handler
        return sub

    def subscribe_depth(self, instruments: list[Instrument], handler: Any) -> str:
        self.depth_subs.append((instruments, handler))
        sub = f"d-{id(handler)}"  # real backends key by id(handler) — mirror them
        self._depth_handlers[sub] = handler
        return sub

    def _subscribe_depth_30(self, instruments: list[Instrument], handler: Any) -> str:
        self.depth_30_subs.append((instruments, handler))
        sub = f"d30-{id(handler)}"  # real backends key by id(handler) — mirror them
        self._depth_handlers[sub] = handler
        return sub

    def unsubscribe(self, subscription: object) -> None:
        self.unsubscribed.append(subscription)
        # Real backends drop the handler on unsubscribe — mirror that so
        # handlers stop firing after stop().
        self._quote_handlers.pop(str(subscription), None)
        self._depth_handlers.pop(str(subscription), None)

    def unsubscribe_instruments(self, instruments: list[Instrument]) -> None:
        self.unsubscribed_instruments_calls.append(instruments)

    def emit_quote(self, quote: Quote) -> None:
        for h in tuple(self._quote_handlers.values()):
            h(quote)

    def emit_depth(self, depth: Depth) -> None:
        for h in tuple(self._depth_handlers.values()):
            h(depth)


def _make(broker: _FakeBroker | None = None) -> tuple[MarketFeed, _FakeBroker, ReactiveBus]:
    fake = broker or _FakeBroker()
    bus = ReactiveBus()
    feed = MarketFeed(broker=fake, bus=bus)
    return feed, fake, bus


class TestMarketFeedStart:
    def test_start_subscribes_quotes_and_depth(self) -> None:
        # Default fake has no subscribe_depth_30 -> Dhan fallback path.
        feed, fake, _ = _make()
        feed.start([_reliance()], depth="30")
        assert feed.active is True
        assert feed.depth_enabled is True
        assert len(fake.quote_subs) == 1
        assert len(fake.depth_subs) == 1
        assert len(fake.depth_30_subs) == 0
        assert fake.quote_subs[0][0] == [_reliance()]
        assert fake.depth_subs[0][0] == [_reliance()]

    def test_start_quotes_only_by_default(self) -> None:
        feed, fake, _ = _make()
        feed.start([_reliance()])
        assert feed.depth_enabled is False
        assert len(fake.quote_subs) == 1
        assert len(fake.depth_subs) == 0

    def test_prefers_depth_30_when_available(self) -> None:
        feed, fake, _ = _make(_FakeBroker(has_depth_30=True))
        feed.start([_reliance()], depth="30")
        assert len(fake.depth_30_subs) == 1
        assert len(fake.depth_subs) == 0

    def test_start_resets_previous_instruments(self) -> None:
        feed, fake, _ = _make()
        feed.start([_reliance()])
        feed.start([_tcs()])
        assert feed.instruments == {_tcs().instrument_id}
        assert len(fake.unsubscribed) == 1  # prior quote handler dropped

    def test_start_empty_instruments_is_noop(self) -> None:
        feed, fake, _ = _make()
        feed.start([])
        assert feed.active is False
        assert len(fake.quote_subs) == 0
        assert len(fake.depth_subs) == 0


class TestMarketFeedSubscribeUnsubscribe:
    def test_subscribe_is_additive_and_idempotent(self) -> None:
        feed, fake, _ = _make()
        feed.subscribe([_reliance()])
        feed.subscribe([_reliance()])
        feed.subscribe([_tcs()])
        assert feed.instruments == {
            _reliance().instrument_id,
            _tcs().instrument_id,
        }
        assert len(fake.quote_subs) == 2  # RELIANCE once, TCS once

    def test_unsubscribe_removes_instrument(self) -> None:
        feed, fake, _ = _make()
        feed.subscribe([_reliance(), _tcs()])
        feed.unsubscribe([_reliance()])
        assert feed.instruments == {_tcs().instrument_id}
        assert feed.active is True

    def test_unsubscribe_last_instrument_stops(self) -> None:
        feed, fake, _ = _make()
        feed.subscribe([_reliance()])
        feed.unsubscribe([_reliance()])
        assert feed.active is False
        assert len(fake.unsubscribed) == 1

    def test_unsubscribe_notifies_backend_wire_set(self) -> None:
        """Partial unsubscribe tells the broker backend to drop the instruments.

        Regression: the backend wire set never shrank, so reconnects
        resurrected ghost subscriptions and the socket kept streaming dropped
        instruments toward the broker's per-connection cap.
        """
        feed, fake, _ = _make()
        feed.subscribe([_reliance(), _tcs()])
        feed.unsubscribe([_reliance()])
        assert fake.unsubscribed_instruments_calls == [[_reliance()]]

    def test_partial_unsubscribe_drops_only_released_instruments(self) -> None:
        """At scale: 50 instruments subscribed, 10 released — the backend is
        told to drop exactly those 10; the other 40 keep streaming.
        """
        feed, fake, _ = _make()
        many = [Equity.of("NSE", f"SC{i:03d}") for i in range(50)]
        feed.subscribe(many)
        released = many[:10]
        feed.unsubscribe(released)
        # No subscription storm: the 50 went out in one subscribe call, and the
        # partial unsubscribe added no further subscribe (only a wire drop).
        assert len(fake.quote_subs) == 1
        assert fake.unsubscribed_instruments_calls == [released]
        assert feed.instruments == {i.instrument_id for i in many[10:]}
        assert feed.active is True

    def test_depth_toggled_later(self) -> None:
        feed, fake, _ = _make()
        feed.subscribe([_reliance()])
        assert feed.depth_enabled is False
        feed.subscribe([_reliance()], depth="30")
        assert feed.depth_enabled is True
        assert len(fake.depth_subs) == 1

    def test_depth_toggled_for_already_wanted_instrument_when_depth_already_on(self) -> None:
        """Requesting depth for an existing instrument works even when depth is active."""
        feed, fake, _ = _make()
        feed.subscribe([_reliance()])  # quote-only
        feed.subscribe([_tcs()], depth="30")  # depth on for tcs only
        assert feed.depth_enabled is True
        assert _reliance().instrument_id not in feed._depth_instruments
        # Explicit depth request for the already-wanted RELIANCE must not be
        # a silent no-op (regression: depth already on for another instrument).
        feed.subscribe([_reliance()], depth="30")
        assert _reliance().instrument_id in feed._depth_instruments
        assert len(fake.depth_subs) == 2

    def test_quote_only_subscribe_while_depth_on_does_not_pull_depth(self) -> None:
        """A new quote-only instrument added while depth is active stays quote-only."""
        feed, fake, _ = _make()
        feed.subscribe([_reliance()], depth="30")
        feed.subscribe([_tcs()])  # quote-only, depth already on
        assert feed.depth_enabled is True
        assert _tcs().instrument_id not in feed._depth_instruments

    def test_stop_depth_tears_down_depth_only(self) -> None:
        feed, fake, _ = _make()
        feed.subscribe([_reliance()], depth="30")
        feed.stop_depth()
        assert feed.depth_enabled is False
        assert feed.active is True  # quotes keep streaming
        assert len(fake.unsubscribed) == 1


class TestMarketFeedPublish:
    def test_quote_published_to_bus(self) -> None:
        feed, fake, bus = _make()
        feed.subscribe([_reliance()])
        received: list[Quote] = []
        bus.of_type(Quote).subscribe(received.append)
        fake.emit_quote(_quote(_reliance()))
        assert len(received) == 1
        assert received[0].ltp.value == Decimal("1319.2")

    def test_quote_for_unsubscribed_instrument_filtered(self) -> None:
        feed, fake, bus = _make()
        feed.subscribe([_reliance()])
        received: list[Quote] = []
        bus.of_type(Quote).subscribe(received.append)
        fake.emit_quote(_quote(_tcs()))
        assert received == []

    def test_depth_published_to_bus(self) -> None:
        feed, fake, bus = _make()
        feed.subscribe([_reliance()], depth="30")
        received: list[Depth] = []
        bus.of_type(Depth).subscribe(received.append)
        fake.emit_depth(_depth(_reliance()))
        assert len(received) == 1
        assert received[0].best_bid is not None
        assert received[0].best_bid.value == Decimal("1319.0")


class TestMarketFeedStop:
    def test_stop_unsubscribes_and_marks_inactive(self) -> None:
        feed, fake, _ = _make()
        feed.start([_reliance()], depth="30")
        feed.stop()
        assert feed.active is False
        assert len(fake.unsubscribed) == 2
        assert feed.subscription_count == 0

    def test_stop_without_start_is_noop(self) -> None:
        feed, fake, _ = _make()
        feed.stop()
        assert len(fake.unsubscribed) == 0

    def test_stop_prevents_further_publishes(self) -> None:
        feed, fake, bus = _make()
        feed.subscribe([_reliance()])
        feed.stop()
        received: list[Quote] = []
        bus.of_type(Quote).subscribe(received.append)
        fake.emit_quote(_quote(_reliance()))
        assert received == []


class TestMultiplexing:
    """Random subscriptions across clients multiplex onto ONE broker socket."""

    def test_multiple_subscribe_calls_share_one_handler(self) -> None:
        """Regressions: fresh bound methods per call must not fan out ticks N times."""
        feed, fake, bus = _make()
        feed.subscribe([_reliance()])
        feed.subscribe([_tcs()])  # second client/instrument via another call
        received: list[Quote] = []
        bus.of_type(Quote).subscribe(received.append)
        fake.emit_quote(_quote(_reliance()))
        fake.emit_quote(_quote(_tcs()))
        assert len(received) == 2  # one publish per tick, not one per handler

    def test_multiple_subscribe_calls_register_single_backend_handler(self) -> None:
        """Backends key handlers by id(handler); stable callbacks keep one entry."""
        from unittest.mock import MagicMock

        backend = MagicMock()
        backend.subscribe_quotes.return_value = "sub"
        broker = MagicMock()
        broker.subscribe_quotes = backend.subscribe_quotes
        feed = MarketFeed(broker=broker, bus=ReactiveBus())
        feed.subscribe([_reliance()])
        feed.subscribe([_tcs()])
        handlers = [call.args[1] for call in backend.subscribe_quotes.call_args_list]
        # The same callable object must be passed on every subscribe call so the
        # backend's id(handler) key collapses to a single registration.
        assert len({id(h) for h in handlers}) == 1

    def test_depth_multiplexing_single_handler(self) -> None:
        """Multiple depth subscribe calls must not fan depth ticks out N times."""
        feed, fake, bus = _make()
        feed.subscribe([_reliance()], depth="30")
        feed.subscribe([_tcs()], depth="30")
        received: list[Depth] = []
        bus.of_type(Depth).subscribe(received.append)
        fake.emit_depth(_depth(_reliance()))
        fake.emit_depth(_depth(_tcs()))
        assert len(received) == 2  # one publish per depth tick, not per handler

    def test_unsubscribe_resubscribe_keeps_single_handler(self) -> None:
        """The stable callback id survives a stop/unsubscribe cycle."""
        feed, fake, bus = _make()
        feed.subscribe([_reliance()])
        feed.unsubscribe([_reliance()])  # empties wanted set -> stop
        feed.subscribe([_tcs()])  # resubscribe
        assert len(fake.quote_subs) == 2
        received: list[Quote] = []
        bus.of_type(Quote).subscribe(received.append)
        fake.emit_quote(_quote(_tcs()))
        fake.emit_quote(_quote(_tcs()))
        assert len(received) == 2  # one publish per tick across the cycle

    def test_two_feeds_share_one_broker_socket(self) -> None:
        """Multiple feeds multiplex onto ONE backend handler per stream type.

        This is the real multiplexing guarantee: random subscriptions across
        clients share a single broker connection, with each feed filtering to
        its own wanted instruments.
        """
        fake = _FakeBroker()
        bus_a = ReactiveBus()
        bus_b = ReactiveBus()
        feed_a = MarketFeed(broker=fake, bus=bus_a)
        feed_b = MarketFeed(broker=fake, bus=bus_b)
        feed_a.subscribe([_reliance()])
        feed_b.subscribe([_tcs()])
        # One handler per feed (each filters to its own wanted set), sharing
        # the single broker socket; re-subscribes within a feed never add
        # entries, so the handler count stays exactly one per feed.
        assert len(fake._quote_handlers) == 2
        received_a: list[Quote] = []
        received_b: list[Quote] = []
        bus_a.of_type(Quote).subscribe(received_a.append)
        bus_b.of_type(Quote).subscribe(received_b.append)
        fake.emit_quote(_quote(_reliance()))
        fake.emit_quote(_quote(_tcs()))
        # Each feed publishes only its own instruments.
        assert len(received_a) == 1 and received_a[0].instrument == _reliance()
        assert len(received_b) == 1 and received_b[0].instrument == _tcs()
        # Growing a feed's wanted set reuses its stable callback — the handler
        # entry count on the shared broker stays unchanged.
        feed_a.subscribe([_tcs()])
        assert len(fake._quote_handlers) == 2
        fake.emit_quote(_quote(_tcs()))
        assert len(received_a) == 2 and len(received_b) == 2


class TestFeedCapabilities:
    """Streaming capability surface: depth level + per-connection cap."""

    def test_derives_capabilities_from_broker(self) -> None:
        from tradex_brokers.common.capabilities import dhan_capabilities

        fake = _FakeBroker()
        fake.capabilities = dhan_capabilities()
        feed, _, _ = _make(fake)
        assert feed.depth_levels == 20
        assert feed.max_stream_instruments == 1000

    def test_no_capabilities_means_no_limits(self) -> None:
        feed, _, _ = _make()  # fake broker has no capabilities table
        assert feed.depth_levels == 0
        assert feed.max_stream_instruments is None

    def test_subscribe_over_cap_raises_loudly(self) -> None:
        from tradex_domain.capabilities import BrokerCapabilities

        fake = _FakeBroker()
        fake.capabilities = BrokerCapabilities(max_stream_instruments=1)
        feed, _, _ = _make(fake)
        feed.subscribe([_reliance()])
        with pytest.raises(ValueError, match="cap exceeded"):
            feed.subscribe([_tcs()])
        assert feed.instruments == {_reliance().instrument_id}  # unchanged
        assert len(fake.quote_subs) == 1

    def test_feed_registry_rejects_over_cap_and_leaves_refs_untouched(self) -> None:
        from tradex_domain.capabilities import BrokerCapabilities

        fake = _FakeBroker()
        fake.capabilities = BrokerCapabilities(max_stream_instruments=1)
        feed, _, _ = _make(fake)
        reg = FeedRegistry(feed)
        reg.subscribe([_reliance()])
        with pytest.raises(ValueError, match="cap exceeded"):
            reg.subscribe([_tcs()])
        assert _tcs().instrument_id not in reg._counts


class TestFeedRegistry:
    def _make_registry(self) -> tuple[FeedRegistry, MarketFeed, _FakeBroker, ReactiveBus]:
        feed, fake, bus = _make(_FakeBroker(has_depth_30=True))
        return FeedRegistry(feed), feed, fake, bus

    def test_subscribe_refcounts_across_clients(self) -> None:
        reg, feed, fake, _ = self._make_registry()
        reg.subscribe([_reliance()])
        reg.subscribe([_reliance()])  # second client, same instrument
        assert feed.instruments == {_reliance().instrument_id}
        assert len(fake.quote_subs) == 1  # subscribed once

    def test_last_client_unsubscribe_drops_stream(self) -> None:
        reg, feed, fake, _ = self._make_registry()
        reg.subscribe([_reliance()])
        reg.subscribe([_reliance()])
        reg.unsubscribe([_reliance()])  # one client leaves
        assert feed.instruments == {_reliance().instrument_id}
        assert feed.active is True
        reg.unsubscribe([_reliance()])  # last client leaves
        assert feed.active is False
        assert len(fake.unsubscribed) == 1

    def test_depth_enabled_when_any_client_requests(self) -> None:
        reg, feed, fake, _ = self._make_registry()
        reg.subscribe([_reliance()])  # quote-only client
        reg.subscribe([_tcs()], depth="30")  # depth client
        assert feed.depth_enabled is True
        assert len(fake.depth_30_subs) == 1

    def test_depth_gates_non_nse_without_ref_leak(self) -> None:
        reg, feed, _, _ = self._make_registry()
        with pytest.raises(CapabilityNotSupportedError, match="NSE"):
            reg.subscribe([Equity.of("MCX", "CRUDEOIL")], depth="30")
        # Gate fires before any refcount mutation or feed subscription.
        assert reg.any_depth is False
        assert feed.instruments == frozenset()

    def test_depth_torn_down_when_last_depth_client_leaves(self) -> None:
        reg, feed, fake, _ = self._make_registry()
        reg.subscribe([_tcs()], depth="30")
        assert feed.depth_enabled is True
        reg.unsubscribe([_tcs()])
        assert reg.any_depth is False
        assert feed.depth_enabled is False

    def test_depth_kept_while_any_client_holds_depth(self) -> None:
        reg, feed, fake, _ = self._make_registry()
        reg.subscribe([_reliance()])
        reg.subscribe([_tcs()], depth="30")
        reg.unsubscribe([_tcs()])  # depth client leaves
        assert feed.depth_enabled is False
        reg.subscribe([_tcs()], depth="30")  # depth client returns
        assert feed.depth_enabled is True

    def test_release_is_connection_accounting_only(self) -> None:
        """Release never yanks a feed the app started for itself."""
        reg, feed, fake, _ = self._make_registry()
        # The app started the feed directly (not via the registry).
        feed.subscribe([_reliance()])
        reg.acquire()
        reg.release()  # the lone ws client leaves
        # Feed teardown belongs to the refcounts / session.stop() — the app's
        # instruments keep streaming.
        assert feed.active is True
        assert len(fake.unsubscribed) == 0

    def test_release_below_zero_is_safe(self) -> None:
        reg, feed, fake, _ = self._make_registry()
        reg.release()
        assert reg.connection_count == 0


class TestStaleFeed:
    def test_stale_feed_emitted_when_no_ticks(self) -> None:
        from datetime import UTC, datetime, timedelta

        from tradex_domain.events import StaleFeed

        fake = _FakeBroker()
        bus = ReactiveBus()
        feed = MarketFeed(broker=fake, bus=bus, stale_after=0.1)
        feed.subscribe([_reliance()])
        received: list[StaleFeed] = []
        bus.of_type(StaleFeed).subscribe(received.append)
        fake.emit_quote(_quote(_reliance()))
        # Make last tick appear stale
        iid = _reliance().instrument_id
        feed._last_tick[iid] = datetime.now(UTC) - timedelta(seconds=1)
        stale = feed.check_stale()
        assert len(stale) == 1
        assert len(received) == 1
        assert received[0].instrument == _reliance()
        assert received[0].age_seconds > 0.1
        assert received[0].last_timestamp is not None

    def test_stale_feed_not_emitted_when_fresh(self) -> None:
        from tradex_domain.events import StaleFeed

        fake = _FakeBroker()
        bus = ReactiveBus()
        feed = MarketFeed(broker=fake, bus=bus, stale_after=30.0)
        feed.subscribe([_reliance()])
        received: list[StaleFeed] = []
        bus.of_type(StaleFeed).subscribe(received.append)
        fake.emit_quote(_quote(_reliance()))
        stale = feed.check_stale()
        assert stale == []
        assert received == []

    def test_stale_feed_importable_from_runtime(self) -> None:
        from tradex_trading.runtime.market_feed import StaleFeed as MFStale

        from tradex_domain.events import StaleFeed as DomainStale

        assert MFStale is DomainStale
