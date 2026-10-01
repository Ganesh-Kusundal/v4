"""A quote that crosses the real feed must land on the tape.

:class:`~tradex_runtime.tape_recorder.TapeRecorder` was an island: nothing in
production constructed one, so the two requirements that consume a recorded
session (AC-14's two-paper-fill-path comparison, lane 1l's backtest/live parity
suite) had no way to obtain one. The wiring is the claim under test here, and it
is only worth anything if it is made by :meth:`MarketFeed._on_quote` - the one
place every live broker tick passes through, after ``row_to_quote()`` and before
``bus.publish``.

So the test drives a real ``Quote`` through a real ``MarketFeed`` (through the
broker fake's own registered handler, exactly as ``test_market_feed.py`` does)
and reads the file back with :func:`read_tape`. A test that called
``recorder.record()`` itself would prove the recorder works, which
``test_tape_recorder.py`` already does, and would keep passing if the feed
wiring were deleted.

The negative controls are that the gate off means no recorder is constructed and
no file appears, and that a quote for an instrument the feed does not hold is
not recorded - the second because the recorder sits inside the
``in self._instruments`` guard, and a recorder that ran before that guard would
record ticks no downstream consumer ever saw, quietly making the tape a
different session than the one that traded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tradex_domain.instruments import Equity, Instrument
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.market_feed import MarketFeed
from tradex_runtime.tape_recorder import (
    TAPE_RECORD_ENV_VAR,
    TapeRecorder,
    open_tape_recorder,
    read_tape,
)

IST = ZoneInfo("Asia/Kolkata")
RELIANCE = Equity.of("NSE", "RELIANCE")
TCS = Equity.of("NSE", "TCS")

TS = datetime(2026, 9, 24, 4, 45, 30, tzinfo=UTC)


class _FakeBroker:
    """The quote half of ``test_market_feed``'s fake: one handler, fired by hand.

    ``MarketFeed`` registers this handler through ``subscribe_quotes``, so
    ``emit_quote`` runs the real ``_on_quote`` - the same entry point a live
    broker's receive thread calls.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Any] = {}

    def subscribe_quotes(self, instruments: list[Instrument], handler: Any) -> str:
        sub = f"q-{id(handler)}"  # real backends key by id(handler) - mirror them
        self._handlers[sub] = handler
        return sub

    def unsubscribe(self, subscription: object) -> None:
        self._handlers.pop(str(subscription), None)

    def set_reconnect_hook(self, cb: object) -> None:
        return None

    def emit_quote(self, quote: Quote) -> None:
        for handler in tuple(self._handlers.values()):
            handler(quote)


def _quote(instrument: Instrument = RELIANCE, ltp: str = "2500.50") -> Quote:
    return Quote(
        instrument=instrument,
        ltp=Price(value=Decimal(ltp)),
        volume=Quantity(value=Decimal("1000")),
        timestamp=TS,
        exchange="NSE",
        provider="dhan",
    )


def _make(directory: Path) -> tuple[_FakeBroker, TapeRecorder | None]:
    """A subscribed feed whose recorder came from the gate, as ``boot()`` wires it."""
    broker = _FakeBroker()
    recorder = open_tape_recorder(broker="dhan", directory=directory)
    feed = MarketFeed(broker=broker, bus=ReactiveBus(), recorder=recorder)
    feed.subscribe([RELIANCE])
    return broker, recorder


def _tapes(directory: Path) -> list[Path]:
    """Every tape file in *directory*, or nothing when it was never created."""
    return sorted(directory.glob("*.jsonl")) if directory.is_dir() else []


class TestTapeAttachedToFeed:
    def test_a_quote_through_on_quote_is_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unblock: the feed is the thing that writes the tape."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        directory = tmp_path / "tapes"
        broker, recorder = _make(directory)
        assert recorder is not None

        broker.emit_quote(_quote())

        (path,) = _tapes(directory)
        (tick,) = read_tape(path)
        assert tick.price == Decimal("2500.50")
        assert tick.qty == Decimal("1000")
        assert tick.symbol == "NSE:RELIANCE"
        assert tick.broker == "dhan"
        assert tick.ts == TS.astimezone(IST)
        assert tick.ts_ist == datetime(2026, 9, 24, 10, 15, 30)

    def test_the_ticks_arrive_in_the_order_the_feed_took_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """File order IS the session for the consumers: a feed that recorded out
        of order would break their fill assertions rather than fail them."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        directory = tmp_path / "tapes"
        broker, recorder = _make(directory)
        assert recorder is not None

        for ltp in ("2500.50", "2501.25", "2502.00"):
            broker.emit_quote(_quote(ltp=ltp))

        (path,) = _tapes(directory)
        assert [t.price for t in read_tape(path)] == [
            Decimal("2500.50"),
            Decimal("2501.25"),
            Decimal("2502.00"),
        ]

    def test_a_quote_the_feed_does_not_hold_is_not_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard the recorder sits behind. A tick from an unsubscribed
        instrument reaching the tape is a record of a session that never traded
        it, and ``read_tape`` hands it back as evidence.

        The segment file itself exists from the moment the recorder opens (it is
        opened in append mode, so a restarted process keeps what it had), so the
        assertion is that it holds no ticks rather than that it is absent.
        """
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        directory = tmp_path / "tapes"
        broker, recorder = _make(directory)
        assert recorder is not None

        broker.emit_quote(_quote(TCS))

        assert [t.symbol for t in read_tape(_tapes(directory)[0])] == []


class TestGateOff:
    @pytest.mark.parametrize("value", [None, "0"])
    def test_no_recorder_and_no_file(
        self,
        value: str | None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unset and "0" mean the same thing: the factory returns None, so
        ``MarketFeed`` has nothing to record to and creates nothing - not the
        file, and not the directory."""
        if value is None:
            monkeypatch.delenv(TAPE_RECORD_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(TAPE_RECORD_ENV_VAR, value)
        directory = tmp_path / "tapes"
        broker, recorder = _make(directory)

        assert recorder is None
        broker.emit_quote(_quote())
        assert not directory.exists()
        assert list(tmp_path.iterdir()) == []
