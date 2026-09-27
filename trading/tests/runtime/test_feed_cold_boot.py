"""AC-20: a cold boot reaches READY because recovery ran, and refuses if it did not.

``MarketFeed.start()`` is the cold boot path. It used to stop, mark CONNECTING →
RESYNCHRONIZING, subscribe, and return — with no writer of READY anywhere on that
path, so the state machine ended terminal and ``supervisor.ready`` stayed false
for the life of the session. Every order was rejected with ``feed_not_ready``.

These tests boot the real live session (real ``boot()``, real ``MarketFeed``,
real ``FeedRecoveryCoordinator``, the same supervisor the execution gate reads)
and assert the three contract behaviours:

1. a cold start with a reconcilable history reaches READY, and READY is reached
   *through the history fetch* rather than because the socket opened;
2. a cold start whose recovery fails refuses, and names the reason;
3. a successful connect with no history reconciliation never produces READY.

The history source is patched at ``ParquetHistoryProvider.fetch_bars`` so the
decision under test is the coordinator's, not the clock's: the production
lookback window is one daily bucket, so a real lake read is empty on a weekend
and the result would depend on when the suite runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import (
    BrokerId,
    CorrelationId,
    Equity,
    OrderRequest,
    OrderSide,
    OrderType,
    Price,
    Quantity,
)
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.bar_aggregator import BarFrame
from tradex_runtime.feed_recovery import FeedRecoveryCoordinator, ParquetHistoryProvider
from tradex_runtime.feed_supervisor import FeedState, FeedSupervisor
from tradex_runtime.market_feed import FeedNotReadyError, MarketFeed
from trading.tests.risk_support import permissive_risk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INST = Equity.of("NSE", "RELIANCE")
_EPOCH = int(datetime(2026, 9, 24, 10, 0, tzinfo=UTC).timestamp())


def _fake_live_broker() -> MagicMock:
    """Broker that satisfies a live boot with no network.

    Same shape as ``test_session_readiness``: a plain MagicMock probes
    ``capabilities`` into a MagicMock that fails the ``BrokerCapabilities``
    isinstance check, so the feed reads it as capability-less.
    """
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.get_account.return_value = None
    return broker


def _boot_live(tmp_path, monkeypatch) -> Any:
    """Boot a real live session, offline."""
    import tradex_runtime.live as live_mod
    import tradex_runtime.startup as startup

    fake = _fake_live_broker()
    monkeypatch.setattr(
        live_mod, "build_broker_from_env", lambda _broker_id, **_kw: fake
    )
    return startup.boot(
        AppConfig(risk=permissive_risk(),
            broker_id=BrokerId.DHAN,
            mode="live",
            live_enabled=True,
            persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
        )
    )


class _RecordingHistory:
    """Patchable ``fetch_bars`` that records every call it is asked to serve.

    ``bars`` is public and reassignable, because a boot now cold starts the feed
    for the tape it is given: a test that wants to watch a *later* cold start
    fail lets the boot's own succeed, then swaps the answer out from under it.
    """

    def __init__(self, bars: Any = ()) -> None:
        self.bars = bars
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    def __call__(
        self, instrument: Any, timeframe: str, start: datetime, end: datetime
    ) -> Any:
        self.calls.append(
            (str(instrument.instrument_id), timeframe, start, end)
        )
        if isinstance(self.bars, Exception):
            raise self.bars
        return self.bars


def _bar(offset: int = 0, symbol: str = "RELIANCE") -> BarFrame:
    return BarFrame(
        instrument=f"NSE:{symbol}",
        timeframe="1m",
        time=_EPOCH + offset,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        closed=True,
    )


def _order(cid: str) -> OrderRequest:
    return OrderRequest(
        instrument=_INST,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        correlation_id=CorrelationId(value=cid),
    )


# ---------------------------------------------------------------------------
# 1. The cold boot test
# ---------------------------------------------------------------------------


def test_cold_boot_reaches_ready_because_history_reconciled(
    named_tape, tmp_path, monkeypatch,
) -> None:
    """A cold boot reaches READY because recovery ran and succeeded.

    Before the fix this assertion was impossible: the state machine ended at
    RESYNCHRONIZING and the order gate rejected everything with
    ``feed_not_ready``.

    ``boot()`` cold starts the feed itself now that an operator can name the
    tape, so this drives a *second* cold start through the session: the boot's
    proves the composition root (``test_boot_cold_start_wiring``), this one
    proves ``start_market_feed``. ``stop()`` is what ``start()`` does first, so
    calling it is the honest way back to the pre-start state — there is no
    longer a live session to be had with a feed nobody started.
    """
    history = _RecordingHistory([_bar(), _bar(60)])
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", history)
    session = _boot_live(tmp_path, monkeypatch)
    try:
        feed = session.market_feed
        assert feed is not None
        assert feed.supervisor.ready is True, (
            "the boot's own cold start must have proved the tape it was given"
        )

        feed.stop()
        assert feed.supervisor.ready is False, "precondition: not ready before start"

        session.start_market_feed([_INST])

        assert feed.supervisor.state is FeedState.READY
        assert feed.supervisor.ready is True
        # READY is a property of the same supervisor the execution gate reads,
        # so the order path opens with it.
        assert session.engine.feed_supervisor is feed.supervisor
        receipt = session.engine.submit(_order("cold-boot-ready"))
        assert receipt.message != "feed_not_ready"
        # The socket is not the evidence; the reconciled history is. Twice:
        # once for the boot's cold start and once for this one.
        assert len(history.calls) == 2
        assert (history.calls[-1][0], history.calls[-1][1]) == ("NSE:RELIANCE", "1m")
    finally:
        session.stop()


def test_cold_boot_recovery_failure_refuses_and_says_why(
    named_tape, tmp_path, monkeypatch,
) -> None:
    """A cold start whose recovery fails refuses to start, and says why.

    The boot's own cold start is allowed to succeed first, so the lake answers
    with bars and then stops answering: that is the only way to reach a session
    whose feed is halted, which is the state this asserts against. A boot whose
    *first* cold start fails is refused outright, and is covered in
    ``test_boot_cold_start_wiring``.
    """
    history = _RecordingHistory([_bar(), _bar(60)])
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", history)
    session = _boot_live(tmp_path, monkeypatch)
    history.bars = RuntimeError("datalake read failed")
    try:
        feed = session.market_feed
        with pytest.raises(FeedNotReadyError, match="datalake read failed"):
            session.start_market_feed([_INST])

        assert feed.supervisor.ready is False
        assert feed.supervisor.state is FeedState.HALTED
        assert history.calls, "recovery must have been attempted"
        # The refusal holds: orders are still gated, not silently admitted.
        assert session.engine.submit(_order("cold-boot-refused")).message == (
            "feed_not_ready"
        )
    finally:
        session.stop()


def test_cold_boot_refuses_when_history_returns_no_bars(
    named_tape, tmp_path, monkeypatch,
) -> None:
    """A cold boot with nothing to reconcile has proven nothing, so it refuses.

    Same two-phase shape as the failure case above, for the same reason: the
    boot's own cold start has to succeed to hand back a session at all.
    """

    history = _RecordingHistory([_bar(), _bar(60)])
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", history)
    session = _boot_live(tmp_path, monkeypatch)
    history.bars = []
    try:
        feed = session.market_feed
        with pytest.raises(FeedNotReadyError, match="no bars"):
            session.start_market_feed([_INST])
        assert feed.supervisor.ready is False
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# 2. No socket shortcut
# ---------------------------------------------------------------------------


class _QuoteOnlyBroker:
    def __init__(self) -> None:
        self.quote_subs: list[Any] = []
        self.connected = True  # the socket came up; nothing else is claimed

    def subscribe_quotes(self, instruments: list[Any], handler: Any) -> str:
        self.quote_subs.append(instruments)
        return f"q-{id(handler)}"

    def unsubscribe(self, sub: object) -> None:
        pass

    def unsubscribe_instruments(self, instruments: object) -> None:
        pass

    def set_reconnect_hook(self, cb: Any) -> None:
        self._hook = cb


def test_socket_connect_without_history_never_ready() -> None:
    """A live socket with no history source cannot prove continuity: refuse.

    The coordinator is real and unwired for history, so its own
    fail-closed path runs — this is the "socket alone never READY" invariant
    on the cold boot path, not just on reconnect.
    """
    broker = _QuoteOnlyBroker()
    feed = MarketFeed(broker=broker, bus=ReactiveBus())
    feed.set_recovery(FeedRecoveryCoordinator(feed))  # no history_provider

    with pytest.raises(FeedNotReadyError, match="history provider"):
        feed.start([_INST])

    assert broker.connected is True
    assert feed.supervisor.ready is False
    assert feed.supervisor.state is FeedState.HALTED


def test_start_without_recovery_coordinator_never_ready() -> None:
    """No coordinator at all: not READY, and no exception from a pure stream reset."""
    feed = MarketFeed(broker=_QuoteOnlyBroker(), bus=ReactiveBus())

    feed.start([_INST])

    assert feed.supervisor.ready is False
    assert feed.supervisor.state is FeedState.RESYNCHRONIZING


def test_failed_outcome_never_leaves_the_feed_ready() -> None:
    """A coordinator that fails without demoting the supervisor is overruled.

    The cheap path this guards against is a future ``start()`` that sets READY
    and *then* discovers the reconciliation failed.
    """
    feed = MarketFeed(broker=_QuoteOnlyBroker(), bus=ReactiveBus())

    class _LyingRecovery:
        """Fails, but leaves the supervisor READY behind its back."""

        def recover(self) -> Any:
            feed.supervisor.recovery_succeeded(
                last_event_at=None, recovered_through=None
            )
            return SimpleNamespace(ok=False, error="replay was a hole")

    feed.set_recovery(_LyingRecovery())

    with pytest.raises(FeedNotReadyError, match="replay was a hole"):
        feed.start([_INST])

    assert feed.supervisor.ready is False
    assert feed.supervisor.state is FeedState.HALTED


def test_coordinator_that_raises_refuses_rather_than_crashes() -> None:
    """A coordinator blowing up is a refusal with a reason, not a traceback."""
    feed = MarketFeed(broker=_QuoteOnlyBroker(), bus=ReactiveBus())

    class _ExplodingRecovery:
        def recover(self) -> Any:
            raise RuntimeError("coordinator is broken")

    feed.set_recovery(_ExplodingRecovery())

    with pytest.raises(FeedNotReadyError, match="coordinator is broken"):
        feed.start([_INST])

    assert feed.supervisor.ready is False
    assert feed.supervisor.state is FeedState.HALTED


def test_reconnect_after_cold_boot_still_demotes() -> None:
    """A cold boot that reached READY is demoted again by a later socket reopen."""
    broker = _QuoteOnlyBroker()
    supervisor = FeedSupervisor()
    feed = MarketFeed(broker=broker, bus=ReactiveBus(), supervisor=supervisor)
    feed.set_recovery(FeedRecoveryCoordinator(feed, supervisor=supervisor))
    with pytest.raises(FeedNotReadyError):
        feed.start([_INST])

    supervisor.recovery_succeeded(
        last_event_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
        recovered_through=datetime(2026, 9, 24, 10, 1, tzinfo=UTC),
    )
    assert supervisor.ready is True

    feed.notify_reconnect()

    assert supervisor.state is FeedState.RESYNCHRONIZING
    assert supervisor.ready is False


# ---------------------------------------------------------------------------
# 3. Unit level: what start() returns, and what it asked for
# ---------------------------------------------------------------------------


class _HistorySource:
    """Injectable history source that records the windows it was asked for."""

    def __init__(self, bars: Any = ()) -> None:
        self._bars = bars
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    def fetch_bars(
        self, instrument: Any, timeframe: str, start: datetime, end: datetime
    ) -> Any:
        self.calls.append(
            (str(getattr(instrument, "instrument_id", instrument)), timeframe, start, end)
        )
        if isinstance(self._bars, Exception):
            raise self._bars
        return self._bars


def test_start_returns_the_recovery_outcome() -> None:
    """A successful cold start hands the caller the coordinator's outcome."""
    feed = MarketFeed(broker=_QuoteOnlyBroker(), bus=ReactiveBus())
    feed.set_recovery(
        FeedRecoveryCoordinator(feed, history_provider=_HistorySource([_bar()]))
    )

    outcome = feed.start([_INST])

    assert outcome.ok is True
    assert outcome.bars_replayed == 1
    assert outcome.instruments == ("NSE:RELIANCE",)
    assert feed.supervisor.ready is True


def test_recovery_window_is_a_full_lookback_not_a_socket_tick() -> None:
    """The cold start asks for real history: a window ending now, not one tick."""
    feed = MarketFeed(broker=_QuoteOnlyBroker(), bus=ReactiveBus())
    history = _HistorySource([_bar()])
    feed.set_recovery(FeedRecoveryCoordinator(feed, history_provider=history))

    feed.start([_INST])

    assert feed.supervisor.ready is True
    _, timeframe, start, end = history.calls[0]
    assert timeframe == "1m"
    assert end - start == timedelta(days=1)
