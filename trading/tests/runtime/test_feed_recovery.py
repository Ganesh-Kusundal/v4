"""Tests for fail-closed feed recovery (runtime.feed_recovery)."""

from __future__ import annotations

from datetime import UTC, datetime

from tradex_domain.events import FeedRecovered
from tradex_domain.instruments import Equity, Instrument

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.bar_aggregator import BarFrame
from tradex_trading.runtime.feed_recovery import FeedRecoveryCoordinator
from tradex_trading.runtime.feed_supervisor import FeedState, FeedSupervisor
from tradex_trading.runtime.metrics import MetricsRegistry

_EVENT_AT = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)
_BASE_EPOCH = int(_EVENT_AT.timestamp())


def _reliance() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _bar(epoch: int, *, symbol: str = "RELIANCE") -> BarFrame:
    return BarFrame(
        instrument=f"NSE:{symbol}",
        timeframe="1m",
        time=epoch,
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
        closed=True,
    )


class _FakeFeed:
    """Feed shape the coordinator needs (supervisor + instrument snapshot)."""

    def __init__(self, instruments: tuple[Instrument, ...]) -> None:
        self.supervisor = FeedSupervisor()
        self._instruments = instruments

    def snapshot_instruments(self) -> tuple[Instrument, ...]:
        return self._instruments


class _Provider:
    def __init__(self, bars: object = ()) -> None:
        self.bars = bars
        self.calls: list[tuple[str, str, object, object]] = []

    def fetch_bars(self, instrument, timeframe, start, end):  # noqa: ANN001
        self.calls.append((str(instrument.instrument_id), timeframe, start, end))
        if isinstance(self.bars, Exception):
            raise self.bars
        return self.bars


class _ExplodingProvider:
    def fetch_bars(self, instrument, timeframe, start, end):  # noqa: ANN001
        raise RuntimeError("history API down")


def _feed_ready_for_recovery() -> _FakeFeed:
    feed = _FakeFeed((_reliance(),))
    feed.supervisor.connected()
    feed.supervisor.recovery_started()
    feed.supervisor.recovery_succeeded(
        last_event_at=_EVENT_AT, recovered_through=_EVENT_AT
    )
    feed.supervisor.disconnected()  # the reconnect that triggered recovery
    return feed


def _coordinator(feed: _FakeFeed, **kwargs: object) -> FeedRecoveryCoordinator:
    return FeedRecoveryCoordinator(feed, supervisor=feed.supervisor, **kwargs)


def test_without_history_provider_recovery_fails_closed() -> None:
    feed = _feed_ready_for_recovery()
    coordinator = _coordinator(feed)

    outcome = coordinator.recover()

    assert outcome.ok is False
    assert "history provider" in str(outcome.error)
    assert feed.supervisor.state is FeedState.HALTED


def test_without_instruments_recovery_fails() -> None:
    feed = _FakeFeed(())
    feed.supervisor.connected()
    coordinator = _coordinator(feed, history_provider=_Provider([_bar(_BASE_EPOCH)]))

    outcome = coordinator.recover()

    assert outcome.ok is False
    assert outcome.error == "no instruments subscribed"
    assert feed.supervisor.state is FeedState.HALTED


def test_provider_failure_halts_and_counts() -> None:
    registry = MetricsRegistry()
    feed = _feed_ready_for_recovery()
    coordinator = _coordinator(
        feed,
        history_provider=_ExplodingProvider(),
        metrics=registry,
    )

    outcome = coordinator.recover()

    assert outcome.ok is False
    assert "history API down" in str(outcome.error)
    assert feed.supervisor.state is FeedState.HALTED
    assert registry.get("feed_recovery_failures_total") == 1


def test_successful_recovery_reaches_ready_and_publishes_event() -> None:
    bus = ReactiveBus()
    received: list[FeedRecovered] = []
    bus.of_type(FeedRecovered).subscribe(received.append)
    feed = _feed_ready_for_recovery()
    provider = _Provider([_bar(_BASE_EPOCH), _bar(_BASE_EPOCH + 60)])
    coordinator = _coordinator(feed, history_provider=provider, bus=bus)

    outcome = coordinator.recover()

    assert outcome.ok is True
    assert outcome.bars_replayed == 2
    assert outcome.missing_bars == 0
    assert outcome.generation == 1
    assert feed.supervisor.state is FeedState.READY
    assert feed.supervisor.ready is True
    assert len(received) == 1
    assert received[0].generation == 1
    assert received[0].bars_replayed == 2
    assert received[0].instruments == ("NSE:RELIANCE",)


def test_recovery_dedupes_replayed_bars() -> None:
    feed = _feed_ready_for_recovery()
    bar = _bar(_BASE_EPOCH)
    coordinator = _coordinator(feed, history_provider=_Provider([bar, bar]))

    outcome = coordinator.recover()

    assert outcome.ok is True
    assert outcome.bars_replayed == 1


def test_recovery_counts_missing_bars_from_last_closed_bar() -> None:
    feed = _feed_ready_for_recovery()
    coordinator = _coordinator(
        feed,
        history_provider=_Provider([_bar(_BASE_EPOCH + 180)]),
    )
    coordinator.record_closed_bar(_bar(_BASE_EPOCH))

    outcome = coordinator.recover()

    assert outcome.ok is True
    assert outcome.missing_bars == 2


def test_fetch_window_starts_at_last_closed_bar() -> None:
    feed = _feed_ready_for_recovery()
    provider = _Provider([_bar(_BASE_EPOCH + 60)])
    coordinator = _coordinator(feed, history_provider=provider)
    coordinator.record_closed_bar(_bar(_BASE_EPOCH))

    coordinator.recover()

    _, _, start, end = provider.calls[0]
    assert start == datetime.fromtimestamp(_BASE_EPOCH, tz=UTC)
    assert end >= start


def test_seeding_hook_receives_recovered_bars() -> None:
    feed = _feed_ready_for_recovery()
    seeded: list[tuple[object, ...]] = []
    coordinator = _coordinator(
        feed,
        history_provider=_Provider([_bar(_BASE_EPOCH + 60), _bar(_BASE_EPOCH)]),
        on_history=seeded.append,
    )

    coordinator.recover()

    assert len(seeded) == 1
    assert [bar.time for bar in seeded[0]] == [_BASE_EPOCH, _BASE_EPOCH + 60]


def test_seeding_failure_fails_closed() -> None:
    def _boom(bars: object) -> None:
        raise RuntimeError("aggregator unavailable")

    feed = _feed_ready_for_recovery()
    coordinator = _coordinator(
        feed,
        history_provider=_Provider([_bar(_BASE_EPOCH)]),
        on_history=_boom,
    )

    outcome = coordinator.recover()

    assert outcome.ok is False
    assert "history seeding failed" in str(outcome.error)
    assert feed.supervisor.state is FeedState.HALTED


def test_empty_replay_with_no_prior_event_fails_closed() -> None:
    feed = _FakeFeed((_reliance(),))
    feed.supervisor.connected()
    feed.supervisor.recovery_started()
    coordinator = _coordinator(feed, history_provider=_Provider([]))

    outcome = coordinator.recover()

    assert outcome.ok is False
    assert outcome.error == "history replay returned no bars"
    assert feed.supervisor.state is FeedState.HALTED


def test_empty_replay_with_prior_event_is_accepted() -> None:
    feed = _feed_ready_for_recovery()
    coordinator = _coordinator(feed, history_provider=_Provider([]))

    outcome = coordinator.recover()

    assert outcome.ok is True
    assert outcome.bars_replayed == 0
    assert feed.supervisor.state is FeedState.READY


def test_record_closed_bar_keeps_the_newest() -> None:
    feed = _feed_ready_for_recovery()
    coordinator = _coordinator(feed)

    coordinator.record_closed_bar(_bar(_BASE_EPOCH + 60))
    coordinator.record_closed_bar(_bar(_BASE_EPOCH))

    assert coordinator.last_closed_bar("NSE:RELIANCE").time == _BASE_EPOCH + 60
