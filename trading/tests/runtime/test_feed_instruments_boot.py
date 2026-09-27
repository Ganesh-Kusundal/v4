"""The operator names the tape, and a live boot refuses when nobody does.

Lane c1 closed the cold start into ``boot()`` and reported, as its one
load-bearing concern, that **nothing in the repository named a tape**: no config
field, no environment path, nothing on a strategy, artifact, scanner or CLI. So
a live boot reached a market feed it never started, logged an error naming
``boot(feed_instruments=...)``, and refused every order with ``feed_not_ready``
— the pre-fix behaviour, only louder. c1 priced every in-file derivation
(venue exposure, the local OMS cache, a scanner universe, the whole instrument
master) and rejected each for sound reasons; the decision is that an operator
names the tape instead.

These tests cover the composition root end of that decision:

1. ``AppConfig.feed_instruments`` reaches the cold start with no caller
   reaching into the session — the AFTER line, now driven by configuration;
2. **the environment variable alone** is enough, with no field passed, because
   ``AppConfig`` resolves it per call. That is the unattended operator's path,
   and it is the one ``TradingSession.live()`` and ``quick_start_live.py`` take
   without knowing the setting exists;
3. a live boot with an empty tape **fails, naming the environment variable**,
   rather than coming up as a server that looks alive and refuses every order;
4. an explicit empty tape is still expressible, and still refuses — the
   environment does not get to overrule a caller;
5. **paper is unchanged**: with the variable unset an order still fills, and
   with the variable *set* a paper session still constructs no feed, starts no
   recovery and still fills. This is the control that matters most, because
   paper is the mode the first phase of the programme exists to serve.
"""

from __future__ import annotations

from decimal import Decimal
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
    OrderStatus,
    OrderType,
    Price,
    Quantity,
)
from tradex_runtime.bar_aggregator import BarFrame
from tradex_runtime.feed_recovery import ParquetHistoryProvider
from tradex_runtime.feed_supervisor import FeedState
from tradex_runtime.market_feed import MarketFeed
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk

_VAR = "TRADEX_FEED_INSTRUMENTS"
_EPOCH = 1789946400  # 2026-09-24T10:00Z


def _order(instrument: Equity, cid: str) -> OrderRequest:
    return OrderRequest(
        instrument=instrument,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        correlation_id=CorrelationId(value=cid),
    )


def _bar(instrument: str = "NSE:RELIANCE") -> BarFrame:
    return BarFrame(
        instrument=instrument,
        timeframe="1m",
        time=_EPOCH,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        closed=True,
    )


class _RecordingHistory:
    """Patchable ``fetch_bars`` that records every window it is asked to serve."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, instrument: Any, timeframe: str, start: Any, end: Any) -> Any:
        self.calls.append(str(instrument.instrument_id))
        return [_bar(str(instrument.instrument_id))]


def _fake_live_broker() -> MagicMock:
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


def _live_config(tmp_path, **kwargs: Any) -> AppConfig:
    return AppConfig(risk=permissive_risk(),
        broker_id=BrokerId.DHAN,
        mode="live",
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
        **kwargs,
    )


def _boot_live(tmp_path, monkeypatch, **kwargs: Any) -> tuple[Any, MagicMock]:
    import tradex_runtime.live as live_mod

    fake = _fake_live_broker()
    monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _id, **_kw: fake)
    return boot(_live_config(tmp_path, **kwargs.pop("config_kwargs", {})), **kwargs), fake


# ---------------------------------------------------------------------------
# 1. A named tape on the config reaches the cold start
# ---------------------------------------------------------------------------


def test_a_tape_named_on_the_config_reaches_ready(tmp_path, monkeypatch) -> None:
    """The config field is enough: no caller touches the session.

    The assertion that matters is that ``boot()`` read the field and reconciled
    the history itself — READY is reached through ``fetch_bars``, not by the
    socket opening and not by a caller.
    """
    history = _RecordingHistory()
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", history)

    session, _broker = _boot_live(
        tmp_path,
        monkeypatch,
        config_kwargs={"feed_instruments": [Equity.of("NSE", "RELIANCE")]},
    )
    try:
        assert session.market_feed.supervisor.state is FeedState.READY
        assert history.calls == ["NSE:RELIANCE"]
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# 2. The environment variable alone names the tape
# ---------------------------------------------------------------------------


def test_the_env_var_alone_names_the_tape_for_a_live_boot(tmp_path, monkeypatch) -> None:
    """The unattended operator's path, and the one nothing has to opt into.

    ``TradingSession.live()`` and ``quick_start_live.py`` build their own
    ``AppConfig`` and pass no tape, so a setting that only worked when a caller
    passed the field explicitly would leave both of them unable to trade. The
    variable is resolved per ``AppConfig`` construction, the same convention as
    ``$TRADEX_RUNTIME_DIR``.
    """
    monkeypatch.setenv(_VAR, "NSE:RELIANCE,NSE:TCS")
    history = _RecordingHistory()
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", history)

    session, _broker = _boot_live(tmp_path, monkeypatch)
    try:
        assert session.market_feed.supervisor.ready is True
        assert history.calls == ["NSE:RELIANCE", "NSE:TCS"]
    finally:
        session.stop()


def test_an_explicit_argument_still_wins_over_the_environment(tmp_path, monkeypatch) -> None:
    """The kwarg is the override; the field is the default it falls back to."""
    monkeypatch.setenv(_VAR, "NSE:TCS")
    history = _RecordingHistory()
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", history)

    session, _broker = _boot_live(
        tmp_path, monkeypatch, feed_instruments=[Equity.of("NSE", "RELIANCE")]
    )
    try:
        assert history.calls == ["NSE:RELIANCE"]
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# 3. No tape named, live: refuse, and say which variable to set
# ---------------------------------------------------------------------------


def test_a_live_boot_with_no_tape_refuses_and_names_the_variable(
    tmp_path, monkeypatch
) -> None:
    """The failure mode this closes: a server that looks alive and trades nothing.

    c1 logged an error and handed back a session in ``READY`` with a feed that
    was not, so every order was refused with ``feed_not_ready`` and an operator
    reading a warning is an operator who trades on it. The refusal is an
    exception out of ``boot()`` and its message names the variable, because
    "refused" without the one thing to change teaches nothing.
    """
    monkeypatch.delenv(_VAR, raising=False)
    started: list[Any] = []
    monkeypatch.setattr(
        MarketFeed, "start", lambda self, *a, **kw: started.append(a) or None
    )

    with pytest.raises(ValueError) as excinfo:
        _boot_live(tmp_path, monkeypatch)

    assert _VAR in str(excinfo.value)
    assert started == [], "a boot with nothing named must not open a stream"


def test_a_malformed_tape_is_a_configuration_error_before_any_boot(
    tmp_path, monkeypatch
) -> None:
    """A typo names a tape the operator did not write, so it is never started."""
    monkeypatch.setenv(_VAR, "NSE:RELIANCE,RELIANCE")
    started: list[Any] = []
    monkeypatch.setattr(
        MarketFeed, "start", lambda self, *a, **kw: started.append(a) or None
    )

    with pytest.raises(ValueError) as excinfo:
        _boot_live(tmp_path, monkeypatch)

    assert _VAR in str(excinfo.value)
    assert started == []


def test_an_explicit_empty_tape_overrides_a_named_environment(
    tmp_path, monkeypatch
) -> None:
    """A caller that means "no tape" gets the refusal, not the environment's tape."""
    monkeypatch.setenv(_VAR, "NSE:TCS")

    with pytest.raises(ValueError, match=_VAR):
        _boot_live(tmp_path, monkeypatch, config_kwargs={"feed_instruments": ()})


def test_a_refused_boot_for_want_of_a_tape_rolls_back(tmp_path, monkeypatch) -> None:
    """The refusal is a full rollback: no venue left connected, no lock held.

    Proven by booting again — a same-process writer lock is refused while the
    first claim is live, so a second boot succeeding is evidence the first one
    released it.
    """
    import tradex_runtime.live as live_mod

    monkeypatch.delenv(_VAR, raising=False)
    first = _fake_live_broker()
    monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _id, **_kw: first)

    with pytest.raises(ValueError):
        boot(_live_config(tmp_path))
    first.close.assert_called()

    monkeypatch.setenv(_VAR, "NSE:RELIANCE")
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", _RecordingHistory())
    session, _broker = _boot_live(tmp_path, monkeypatch)
    try:
        assert session.market_feed.supervisor.ready is True
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# 4. Paper is completely unchanged — the control that matters most
# ---------------------------------------------------------------------------


def test_paper_with_no_variable_is_unchanged() -> None:
    """The mode the first phase targets: no feed, no recovery, an order fills."""
    session = boot(AppConfig(risk=permissive_risk(), broker_id=BrokerId.PAPER, mode="paper"))
    try:
        assert session.market_feed is None
        assert session.feed_recovery is None
        assert session.state == "READY"
        receipt = session.engine.submit(
            _order(Equity.of("NSE", "RELIANCE"), "paper-unset")
        )
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
    finally:
        session.stop()


def test_paper_with_a_named_tape_is_still_unchanged(monkeypatch) -> None:
    """A tape named for a live session must not change what paper does.

    Paper constructs no ``MarketFeed``, so there is nothing for the tape to
    stream and nothing to reconcile. The risk this pins is a paper session
    reaching for the lake — or refusing to boot — because an operator left the
    variable set in the environment the paper run inherited.
    """
    monkeypatch.setenv(_VAR, "NSE:RELIANCE")
    built: list[Any] = []
    started: list[Any] = []
    monkeypatch.setattr(
        MarketFeed, "__init__",
        lambda self, *a, **kw: built.append(self) or MagicMock.__init__(self),
    )
    monkeypatch.setattr(
        MarketFeed, "start", lambda self, *a, **kw: started.append(a) or None
    )

    session = boot(AppConfig(risk=permissive_risk(), broker_id=BrokerId.PAPER, mode="paper"))
    try:
        assert session.market_feed is None
        assert built == [] and started == []
        assert session.state == "READY"
        receipt = session.engine.submit(
            _order(Equity.of("NSE", "RELIANCE"), "paper-named")
        )
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
    finally:
        session.stop()
