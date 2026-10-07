"""Feed recovery path: DEGRADED → RESYNCHRONIZING → READY.

``MarketFeed.check_stale()`` marks the supervisor DEGRADED when the tape goes
quiet. Before this lane nothing wrote READY on that path, so a stale feed stayed
DEGRADED for the life of the session and every order was refused with
``feed_not_ready``. The owned ``FeedStaleMonitor`` poll now triggers the
fail-closed ``FeedRecoveryCoordinator`` on the poll *after* the one that detected
staleness: the coordinator reconciles history and writes READY, or fails closed
and writes HALTED.

These are integration tests: a real ``MarketFeed``, a real ``ReactiveBus``, a
real ``FeedSupervisor`` and a real ``FeedRecoveryCoordinator``. Only the broker
transport and the history source are replaced at the process boundary — the
decision under test is the monitor's, not the network's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from tradex_domain.instruments import Equity, Instrument
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.bar_aggregator import BarFrame
from tradex_runtime.feed_monitor import FeedStaleMonitor
from tradex_runtime.feed_recovery import FeedRecoveryCoordinator
from tradex_runtime.feed_supervisor import FeedState
from tradex_runtime.market_feed import MarketFeed

_RELIANCE = Equity.of("NSE", "RELIANCE")
_EVENT_AT = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
_EPOCH = int(_EVENT_AT.timestamp())


class _FakeBroker:
    """Broker shape ``MarketFeed.subscribe`` needs, with no transport."""

    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        return object()

    def unsubscribe(self, subscription: object) -> None:
        return None

    def unsubscribe_instruments(self, instruments: object) -> None:
        return None

    def set_reconnect_hook(self, cb: object) -> None:
        return None


class _Provider:
    """History source that answers with *bars* or raises *error*."""

    def __init__(self, bars: object = ()) -> None:
        self.bars = bars
        self.calls: list[str] = []

    def fetch_bars(
        self, instrument: Instrument, timeframe: str, start: datetime, end: datetime
    ) -> object:
        self.calls.append(str(instrument.instrument_id))
        if isinstance(self.bars, Exception):
            raise self.bars
        return self.bars


def _bar(epoch: int) -> BarFrame:
    return BarFrame(
        instrument="NSE:RELIANCE",
        timeframe="1m",
        time=epoch,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        closed=True,
    )


def _ready_stale_feed() -> MarketFeed:
    """A feed that is READY, subscribed, and has a stale last tick."""
    feed = MarketFeed(broker=_FakeBroker(), bus=ReactiveBus(), stale_after=0.05)
    feed.subscribe([_RELIANCE])
    supervisor = feed.supervisor
    supervisor.connected()
    supervisor.recovery_started()
    supervisor.recovery_succeeded(
        last_event_at=_EVENT_AT, recovered_through=_EVENT_AT
    )
    feed._last_tick[_RELIANCE.instrument_id] = datetime.now(UTC) - timedelta(
        seconds=1
    )
    return feed


def test_degraded_feed_triggers_recovery_on_next_poll() -> None:
    feed = _ready_stale_feed()
    supervisor = feed.supervisor
    provider = _Provider([_bar(_EPOCH)])
    coordinator = FeedRecoveryCoordinator(
        feed, supervisor=supervisor, history_provider=provider
    )
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05, feed_recovery=coordinator)

    # Poll 1 detects staleness and demotes the feed; it does not yet recover.
    stale = monitor.poll_once()
    assert stale
    assert supervisor.state is FeedState.DEGRADED
    assert provider.calls == []

    # Poll 2 sees the DEGRADED feed and drives the fail-closed coordinator.
    monitor.poll_once()
    assert provider.calls == ["NSE:RELIANCE"]
    assert supervisor.state is FeedState.READY
    assert supervisor.ready is True


def test_failed_recovery_halts_feed() -> None:
    feed = _ready_stale_feed()
    supervisor = feed.supervisor
    provider = _Provider(RuntimeError("datalake read failed"))
    coordinator = FeedRecoveryCoordinator(
        feed, supervisor=supervisor, history_provider=provider
    )
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05, feed_recovery=coordinator)

    monitor.poll_once()
    assert supervisor.state is FeedState.DEGRADED

    monitor.poll_once()
    assert provider.calls == ["NSE:RELIANCE"]
    assert supervisor.state is FeedState.HALTED
    assert supervisor.ready is False


def test_no_recovery_coordinator_leaves_degraded() -> None:
    feed = _ready_stale_feed()
    supervisor = feed.supervisor
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05)  # no feed_recovery

    monitor.poll_once()
    assert supervisor.state is FeedState.DEGRADED

    monitor.poll_once()
    assert supervisor.state is FeedState.DEGRADED  # unchanged, orders stay gated


def test_live_boot_wires_the_coordinator_into_the_monitor(
    named_tape, tmp_path, monkeypatch
) -> None:
    """The poll trigger is on the production path, not only in this test.

    ``_compose_live_feed`` constructs the monitor and the coordinator. If the
    monitor is not handed the coordinator, this whole lane is dead code in the
    one mode that needs it — a stale live feed would stay DEGRADED forever.
    Asserted through a real ``boot()``, the same composition the live server
    uses.
    """
    import tradex_runtime.live as live_mod
    from tradex_config.schema import AppConfig, PersistenceConfig
    from tradex_domain import BrokerId
    from tradex_runtime.startup import boot
    from trading.tests.risk_support import permissive_risk

    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.get_account.return_value = None
    broker.master_loader = None
    broker.trade_book.return_value = []
    monkeypatch.setattr(
        live_mod, "build_broker_from_env", lambda _broker_id, **_kw: broker
    )

    session = boot(
        AppConfig(
            risk=permissive_risk(),
            broker_id=BrokerId.DHAN,
            mode="live",
            live_enabled=True,
            persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
        )
    )
    try:
        monitor = session.feed_monitor
        assert monitor is not None
        assert monitor._feed_recovery is session.feed_recovery
        assert session.feed_recovery is not None
    finally:
        session.stop()

