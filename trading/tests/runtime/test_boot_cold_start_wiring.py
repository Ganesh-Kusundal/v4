"""AC-20: the composition root cold starts the feed, and refuses when it cannot.

Lane 1m made ``MarketFeed.start()`` drive the fail-closed recovery coordinator
and gate on its verdict, and proved it by instrumenting a real boot. It also
reported, as its first concern, that **nothing in the tree calls ``start()``**:
``TradingSession.start_market_feed`` had no callers, so the fix was on no
production path and a live session rejected every order with ``feed_not_ready``
for the life of the process.

These tests cover the missing half, which is a ``startup.py`` change:

1. a live boot that has been told which tape to stream reaches ``READY``
   *through the history fetch*, with no caller reaching into the session;
2. a live boot whose recovery fails **refuses to start** — an exception out of
   ``boot()``, not a warning, and not a session left in ``READY``;
3. a live boot with nothing named to stream **refuses**, names the setting that
   would fix it, and opens no stream — the control against a "fix" that is
   really just a no-op, and against a boot that skips the cold start quietly
   and hands back a session which looks ready over a feed nobody started;
4. paper, backtest and replay construct no feed and start no recovery at all;
5. the live fill bridge counts its average-price fallbacks on the registry the
   ``/metrics`` endpoint serves (the second gap in the same closure lane).

Every test boots the real ``boot()``. A fake broker satisfies the live path
without a network; ``ParquetHistoryProvider.fetch_bars`` is patched at the
class level so the decision under test is the coordinator's, not the clock's.

Two decisions these tests pin, both of which a later change could quietly undo:

* **A refused cold start is an exception, not a warning and not a session left
  in ``NEW``.** ``reconcile_tripped`` (step 9) deliberately hands back an
  unstarted session so ``/health/ready`` can report why; the feed proof does
  not, because nothing downstream can be trusted once the tape is in question,
  and an operator who reads a warning is an operator who trades on it.
* **The tape is the operator's to name, and a live boot without one is
  refused.** Nothing in the repository names a watchlist, so an empty tape is
  the *default* live configuration and it is not a live configuration at all:
  there is nothing to reconcile, nothing to claim, and every order would be
  refused with ``feed_not_ready`` while the session reported itself ready. The
  tape is taken from ``$TRADEX_FEED_INSTRUMENTS`` or ``boot(feed_instruments=)``
  — never a default index, a scanner universe, or the account's own exposure.
  The other two ways to name one are covered in ``test_feed_instruments_boot``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from tradex_config.schema import FEED_INSTRUMENTS_ENV, AppConfig, ExecutionConfig, PersistenceConfig
from tradex_domain import (
    BrokerId,
    CorrelationId,
    Equity,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
)
from tradex_domain.value_objects import OrderId
from tradex_runtime.bar_aggregator import BarFrame
from tradex_runtime.feed_recovery import ParquetHistoryProvider
from tradex_runtime.feed_supervisor import FeedState
from tradex_runtime.live_fill_bridge import AVG_PRICE_FALLBACK_METRIC
from tradex_runtime.market_feed import FeedNotReadyError, MarketFeed
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk

INSTRUMENT = Equity.of("NSE", "RELIANCE")
_EPOCH = int(datetime(2026, 9, 24, 10, 0, tzinfo=UTC).timestamp())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_live_broker() -> MagicMock:
    """Broker that satisfies a live boot with no network.

    ``trade_book`` returns no rows, so the fill bridge takes its documented
    average-price fallback path (the path that must be counted). ``capabilities``
    stays a MagicMock, which fails the ``BrokerCapabilities`` isinstance check,
    so the feed reads this broker as capability-less.
    """
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.get_account.return_value = None
    broker.master_loader = None
    broker.trade_book.return_value = []
    return broker


def _live_config(tmp_path) -> AppConfig:
    return AppConfig(risk=permissive_risk(),
        broker_id=BrokerId.DHAN,
        mode="live",
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
        execution=ExecutionConfig(strategy_ids=("test_strategy",)),
    )


def _boot_live(tmp_path, monkeypatch, **kwargs) -> Any:
    """Boot a real live session offline."""
    import tradex_runtime.live as live_mod

    fake = _fake_live_broker()
    monkeypatch.setattr(
        live_mod, "build_broker_from_env", lambda _broker_id, **_kw: fake
    )
    return boot(_live_config(tmp_path), **kwargs), fake


class _RecordingHistory:
    """Patchable ``fetch_bars`` that records every window it is asked to serve."""

    def __init__(self, bars: Any = ()) -> None:
        self._bars = bars
        self.calls: list[tuple[str, str]] = []

    def __call__(self, instrument: Any, timeframe: str, start: Any, end: Any) -> Any:
        self.calls.append((str(instrument.instrument_id), timeframe))
        if isinstance(self._bars, Exception):
            raise self._bars
        return self._bars


def _bar(offset: int = 0) -> BarFrame:
    return BarFrame(
        instrument="NSE:RELIANCE",
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
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        correlation_id=CorrelationId(value=cid),
    )


def _spy_on_feed(monkeypatch) -> dict[str, list[Any]]:
    """Record every ``MarketFeed`` construction and every ``start()`` call."""
    calls: dict[str, list[Any]] = {"built": [], "started": []}
    original_init = MarketFeed.__init__
    original_start = MarketFeed.start

    def init_spy(self, *args: Any, **kwargs: Any) -> None:
        calls["built"].append(self)
        original_init(self, *args, **kwargs)

    def start_spy(self, *args: Any, **kwargs: Any) -> Any:
        calls["started"].append((args, kwargs))
        return original_start(self, *args, **kwargs)

    monkeypatch.setattr(MarketFeed, "__init__", init_spy)
    monkeypatch.setattr(MarketFeed, "start", start_spy)
    return calls


# ---------------------------------------------------------------------------
# 1. The cold boot reaches READY, through the composition root
# ---------------------------------------------------------------------------


def test_live_boot_reaches_ready_without_any_caller_starting_the_feed(
    tmp_path, monkeypatch
) -> None:
    """A live boot that knows its tape reaches READY because recovery ran.

    The assertion that matters is that ``boot()`` did it: nothing in this test
    touches ``session.start_market_feed``, and READY is nevertheless reached by
    the history fetch rather than by the socket opening.
    """
    history = _RecordingHistory([_bar(), _bar(60)])
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", history)

    session, _broker = _boot_live(
        tmp_path, monkeypatch, feed_instruments=[INSTRUMENT]
    )
    try:
        feed = session.market_feed
        assert feed is not None
        assert feed.supervisor.state is FeedState.READY
        assert feed.supervisor.ready is True
        # READY is a property of the same supervisor the execution gate reads,
        # so the order path is open with it (a bare AppConfig carries no risk
        # limits, so the next gate is what refuses — AC-10, lane 2f).
        assert session.engine.feed_supervisor is feed.supervisor
        assert session.engine.submit(_order("cold-boot-ready")).message != "feed_not_ready"
        # The socket is not the evidence; the reconciled history is.
        assert history.calls == [("NSE:RELIANCE", "1m")]
    finally:
        session.stop()


def test_boot_starts_the_feed_exactly_once_and_only_in_live_mode(
    tmp_path, monkeypatch
) -> None:
    """One feed, one cold start, and the instruments boot was told about."""
    monkeypatch.setattr(
        ParquetHistoryProvider, "fetch_bars", _RecordingHistory([_bar()])
    )
    calls = _spy_on_feed(monkeypatch)

    session, _broker = _boot_live(
        tmp_path, monkeypatch, feed_instruments=[INSTRUMENT]
    )
    try:
        assert len(calls["built"]) == 1
        assert len(calls["started"]) == 1
        started_instruments = calls["started"][0][0][0]
        assert [i.instrument_id for i in started_instruments] == [
            INSTRUMENT.instrument_id
        ]
    finally:
        session.stop()


def test_the_serve_shape_boot_cold_starts_too(tmp_path, monkeypatch) -> None:
    """The shape a live *server* boots in also cold starts.

    ``TradingSession.live`` — what ``tradex serve`` and ``serve_app`` call —
    passes ``wire_strategies=False``. The cold start must not hang off the
    strategy wiring, or the only production entry point keeps the defect.
    """
    history = _RecordingHistory([_bar()])
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", history)

    session, _broker = _boot_live(
        tmp_path, monkeypatch,
        wire_strategies=False,
        feed_instruments=[INSTRUMENT],
    )
    try:
        assert session.state == "READY"
        assert session.market_feed.supervisor.ready is True
        assert history.calls == [("NSE:RELIANCE", "1m")]
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# 2. The refusal — a refusal, not a warning
# ---------------------------------------------------------------------------


def test_live_boot_refuses_to_start_when_recovery_fails(tmp_path, monkeypatch) -> None:
    """A cold start that cannot prove continuity does not produce a session."""
    monkeypatch.setattr(
        ParquetHistoryProvider,
        "fetch_bars",
        _RecordingHistory(RuntimeError("datalake read failed")),
    )

    with pytest.raises(FeedNotReadyError, match="datalake read failed"):
        _boot_live(tmp_path, monkeypatch, feed_instruments=[INSTRUMENT])


def test_a_refused_boot_rolls_the_venue_and_lock_back(tmp_path, monkeypatch) -> None:
    """A refusal is a full rollback, not a half-started live session.

    ``boot`` only learns about the failure by raising out of the cold start, so
    the C3 rollback must still run: the broker closed and the single-writer lock
    released. Proven by booting again — while this process still holds nothing,
    a second live boot is refused while the first claim is live, so a successful
    second boot is evidence the first one rolled back.
    """
    import tradex_runtime.live as live_mod

    first = _fake_live_broker()
    monkeypatch.setattr(
        live_mod, "build_broker_from_env", lambda _broker_id, **_kw: first
    )
    monkeypatch.setattr(
        ParquetHistoryProvider,
        "fetch_bars",
        _RecordingHistory(RuntimeError("history replay returned no bars")),
    )
    with pytest.raises(FeedNotReadyError):
        boot(_live_config(tmp_path), feed_instruments=[INSTRUMENT])
    first.close.assert_called()

    # The lake answers this time: the same boot must now be able to take the
    # writer lock again, so the refused one really did release it.
    monkeypatch.setattr(
        ParquetHistoryProvider, "fetch_bars", _RecordingHistory([_bar()])
    )
    session, _broker = _boot_live(
        tmp_path, monkeypatch, feed_instruments=[INSTRUMENT]
    )
    try:
        assert session.market_feed.supervisor.ready is True
    finally:
        session.stop()


def test_a_cold_start_with_no_verdict_is_still_a_refusal(
    tmp_path, monkeypatch
) -> None:
    """A feed that returns without reaching READY refuses the boot.

    ``MarketFeed.start`` returns ``None`` rather than raising when no recovery
    coordinator is wired. At boot that is not a pass: nothing was proven, so the
    session must not start. This is the control that keeps a future
    "start() and hope" edit from being read as success.
    """
    monkeypatch.setattr(
        ParquetHistoryProvider, "fetch_bars", _RecordingHistory([_bar()])
    )
    monkeypatch.setattr(MarketFeed, "start", lambda self, *a, **kw: None)

    with pytest.raises(RuntimeError, match="READY"):
        _boot_live(tmp_path, monkeypatch, feed_instruments=[INSTRUMENT])


# ---------------------------------------------------------------------------
# 3. Nothing named to stream: nothing claimed
# ---------------------------------------------------------------------------


def test_live_boot_with_no_named_instruments_claims_no_ready_feed(
    no_tape, tmp_path, monkeypatch,
) -> None:
    """No tape named: no stream opened, and the boot refuses rather than lies.

    A boot that started the feed with an empty watchlist would fail closed for
    the wrong reason ("no instruments subscribed"). A boot that *skipped* the
    cold start quietly was the version of this that looked fine, and it was the
    bad one: the session came up ``READY`` over a feed nobody had started and
    every order was refused with ``feed_not_ready`` for the life of the process
    while ``/health/ready`` agreed with it. So the shipped answer is the third
    one — refuse, say which setting to set, and open nothing.

    ``started == []`` is the half a future "let it boot and warn" edit would
    break, and the raised ``ValueError`` is the half a future "let it boot and
    warn" edit would break first.
    """
    calls = _spy_on_feed(monkeypatch)

    with pytest.raises(ValueError, match=FEED_INSTRUMENTS_ENV):
        _boot_live(tmp_path, monkeypatch)

    assert calls["started"] == [], "a boot with nothing named must not open a stream"


# ---------------------------------------------------------------------------
# 4. Offline modes are untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["paper", "backtest", "replay"])
def test_offline_modes_construct_no_feed_and_start_no_recovery(
    tmp_path, monkeypatch, mode: str
) -> None:
    """Paper, backtest and replay build no MarketFeed and run no recovery."""
    calls = _spy_on_feed(monkeypatch)

    session = boot(
        AppConfig(risk=permissive_risk(), broker_id=BrokerId.PAPER, mode=mode),
        feed_instruments=[INSTRUMENT],
    )
    try:
        assert calls["built"] == []
        assert calls["started"] == []
        assert session.market_feed is None
        assert session.feed_recovery is None
        assert session.state == "READY"
    finally:
        session.stop()


def test_paper_mode_still_trades() -> None:
    """The paper path is byte for byte unchanged: an order still fills."""
    session = boot(AppConfig(risk=permissive_risk(), broker_id=BrokerId.PAPER, mode="paper"))
    try:
        receipt = session.engine.submit(_order("paper-unaffected"))
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# 5. The instrument set the cold start proves
# ---------------------------------------------------------------------------


def test_cold_start_instruments_cover_both_the_argument_and_the_live_wants() -> None:
    """A feed already carrying subscriptions is proven too, not reset away.

    ``MarketFeed.start`` stops before it subscribes, so an instrument somebody
    subscribed before the cold start would otherwise be dropped from the very
    set the boot is trying to prove.
    """
    from tradex_reactive.bus import ReactiveBus
    from tradex_runtime.startup import _cold_start_instruments

    other = Equity.of("NSE", "TCS")
    feed = MarketFeed(broker=MagicMock(), bus=ReactiveBus())
    feed.subscribe([other])

    assert _cold_start_instruments(feed, [INSTRUMENT]) == (INSTRUMENT, other)
    assert _cold_start_instruments(feed, None) == (other,)
    # A duplicate named twice is proven once.
    assert _cold_start_instruments(feed, [INSTRUMENT, INSTRUMENT]) == (
        INSTRUMENT,
        other,
    )


# ---------------------------------------------------------------------------
# 6. The live fill bridge counts on the registry /metrics serves
# ---------------------------------------------------------------------------


def _stream_update() -> Order:
    """A cumulative order-stream row with no per-trade detail behind it."""
    return Order(
        order_id=OrderId(value="dhan-order-1"),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("200")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.PARTIALLY_FILLED,
        correlation_id=CorrelationId(value="cid-1"),
        filled_quantity=Quantity(value=Decimal("100")),
        avg_price_traded=Price(value=Decimal("100")),
    )


def test_live_bridge_counts_avg_price_fallbacks_on_the_served_registry(
    named_tape, tmp_path, monkeypatch,
) -> None:
    """The fallback counter reaches the session's registry, not just the bridge.

    Asserted by driving one fallback fill through the bridge the boot built and
    reading the number back off ``session.metrics`` — the registry the
    ``/metrics`` endpoint renders. An unwired bridge leaves it at 0, so this
    cannot pass by booting successfully.
    """
    session, broker = _boot_live(tmp_path, monkeypatch)
    try:
        bridge = session._fill_bridge
        assert bridge is not None, "a live boot must build the fill bridge"
        assert session.metrics.get(AVG_PRICE_FALLBACK_METRIC) == 0

        backend = broker.stream_backend.return_value
        handler = backend.subscribe_orders.call_args[0][0]
        handler(_stream_update())

        assert session.metrics.get(AVG_PRICE_FALLBACK_METRIC) == 1
        # ...and it is on the exposition text, not only in the registry.
        assert "live_fill_avg_price_fallback_total 1.0" in (
            session.metrics.render_prometheus()
        )
    finally:
        session.stop()


def test_a_failed_session_start_stops_the_cold_started_feed(
    tmp_path, monkeypatch
) -> None:
    """A failed ``session.start()`` must not leave the proven feed streaming.

    The feed is cold started *before* ``session.start()``. ``TradingSession.stop``
    early-returns for a session that never reached READY, so without an explicit
    feed stop the rollback would leave the subscription live behind a boot that
    already failed.
    """
    from tradex_runtime.session_impl import TradingSession

    monkeypatch.setattr(
        ParquetHistoryProvider, "fetch_bars", _RecordingHistory([_bar()])
    )

    def _boom(self) -> None:
        raise RuntimeError("simulated session.start failure")

    monkeypatch.setattr(TradingSession, "start", _boom)
    calls = _spy_on_feed(monkeypatch)

    with pytest.raises(RuntimeError, match="simulated session.start failure"):
        _boot_live(tmp_path, monkeypatch, feed_instruments=[INSTRUMENT])

    assert len(calls["built"]) == 1
    feed = calls["built"][0]
    assert feed.snapshot_instruments() == (), (
        "the rollback must stop the cold-started feed"
    )
