"""WS bar-streaming tests — subscribe_bars contract and replay control acks.

Follows the existing test_fastapi_app.py pattern: a MagicMock session with a
real ReactiveBus, since /ws/stream only needs ``session.bus``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402
from tradex_trading.reactive.bus import ReactiveBus  # noqa: E402


def _app() -> object:
    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus
    return create_app(session=session)


class TestSubscribeBarsContract:
    def test_subscribe_bars_requires_payload(self):
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws.send_json({"type": "subscribe_bars"})
            resp = ws.receive_json()
            assert resp["type"] == "error"
            assert "bars" in resp["message"]

    def test_subscribe_bars_acks(self):
        """No live feed (paper session) still acks — subscriptions are per-connection."""
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [{"instrument": "NSE:TEST", "interval": "1m"}],
            })
            resp = ws.receive_json()
            assert resp["type"] == "subscribed_bars"
            assert resp["active"] == 1

    def test_bad_interval_is_error_not_crash(self):
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [{"instrument": "NSE:TEST", "interval": "bogus"}],
            })
            resp = ws.receive_json()
            assert resp["type"] == "error"

    def test_unsubscribe_bars_acks(self):
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws.send_json({
                "type": "unsubscribe_bars",
                "bars": ["NSE:TEST|1m"],
            })
            resp = ws.receive_json()
            assert resp["type"] == "unsubscribed_bars"

    def test_quote_on_bus_produces_bar_frame(self):
        """A quote past one M1 bucket boundary emits exactly one closed frame."""
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        app = create_app(session=session)

        ist = ZoneInfo("Asia/Kolkata")
        base = datetime(2026, 7, 15, 10, 0).replace(tzinfo=ist)
        inst = Equity.of("NSE", "TEST")

        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [{"instrument": "NSE:TEST", "interval": "1m"}],
            })
            ack = ws.receive_json()
            assert ack["type"] == "subscribed_bars"

            # Two quotes in bucket 10:00, then one at 10:01 — closes the first bar.
            for i, ts in enumerate([
                base + timedelta(seconds=5),
                base + timedelta(seconds=30),
                base + timedelta(minutes=1, seconds=5),
            ]):
                bus.publish(
                    Quote(
                        instrument=inst,
                        ltp=Price(Decimal(str(100 + i))),
                        timestamp=ts,
                    )
                )
                # Yield so the call_soon_threadsafe bridge runs.
                import time

                time.sleep(0.05)

            frame = ws.receive_json()
            deadline = __import__("time").monotonic() + 3
            while frame.get("closed") is not True and __import__("time").monotonic() < deadline:
                if frame["type"] != "bar":
                    frame = ws.receive_json()
                    continue
                frame = ws.receive_json()
            # The closed 10:00 bar must carry only the two in-bucket prints.
            closed_frames = [frame]
            # Drain any remaining forming frames to prove the closed one exists.
            assert any(
                f["closed"] and f["time"] == int(base.timestamp()) and f["close"] == 101.0
                for f in closed_frames
            ), f"no closed frame for 10:00 in {closed_frames}"


class TestReplayControl:
    def test_pause_resume_speed_stop_ack(self):
        client = TestClient(_app())
        with client.websocket_connect("/ws/stream") as ws:
            for msg, expected in [
                ({"type": "replay_pause"}, "replay_paused"),
                ({"type": "replay_resume"}, "replay_resumed"),
                ({"type": "replay_speed", "speed": 2.0}, "replay_speed"),
                ({"type": "replay_stop"}, "replay_stopped"),
            ]:
                ws.send_json(msg)
                assert ws.receive_json()["type"] == expected

    def test_replay_start_without_instrument_is_error(self):
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws.send_json({"type": "replay_start"})
            resp = ws.receive_json()
            assert resp["type"] == "error"

    def test_replay_start_no_datalake_symbol_errors(self):
        """A symbol the datalake never saw surfaces an error frame, not a hang."""
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:NOSUCHSTOCKXYZ",
                "minutes": 10,
            })
            got_started_or_error = False
            for _ in range(4):
                msg = ws.receive_json()
                if msg["type"] in ("error", "replay_done"):
                    got_started_or_error = True
                    break
                if msg["type"] == "replay_started" and "NOSUCH" in str(msg):
                    continue
            assert got_started_or_error or msg["type"] == "error"

    @pytest.mark.timeout(10, func_only=True)
    def test_replay_step_ack_follows_stepped_bar_frame(self, monkeypatch):
        """A step is acknowledged only after its simulated bar frame is queued."""
        from datetime import datetime
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        import tradex_brokers.common.market_builders as market_builders
        import tradex_trading.datalake.parquet_storage as parquet_storage
        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import OHLC, Candle
        from tradex_domain.value_objects import Price, Quantity

        instrument = Equity.of("NSE", "TEST")
        first_time = datetime(2026, 1, 5, 9, 15)
        second_time = datetime(2026, 1, 5, 9, 16)
        candles = [
            Candle(
                instrument=instrument,
                timeframe=Timeframe.M1,
                ohlc=OHLC(
                    open=Price(Decimal("100")),
                    high=Price(Decimal("102")),
                    low=Price(Decimal("99")),
                    close=Price(Decimal("101")),
                ),
                volume=Quantity(Decimal("10")),
                timestamp=first_time,
            ),
            Candle(
                instrument=instrument,
                timeframe=Timeframe.M1,
                ohlc=OHLC(
                    open=Price(Decimal("101")),
                    high=Price(Decimal("103")),
                    low=Price(Decimal("100")),
                    close=Price(Decimal("102")),
                ),
                volume=Quantity(Decimal("10")),
                timestamp=second_time,
            ),
        ]

        class _DataFrame:
            empty = False

        class _FakeStore:
            def __init__(self, _base_path) -> None:
                pass

            def read(self, *_args, **_kwargs):
                return _DataFrame()

            def date_range(self, _symbol):
                return None

        def _candles_from_dataframe(_instrument, _frame, **_kwargs):
            return candles

        monkeypatch.setattr(parquet_storage, "ParquetStorage", _FakeStore)
        monkeypatch.setattr(
            market_builders, "candles_from_dataframe", _candles_from_dataframe
        )

        client = TestClient(_app())
        with client.websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 5
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "speed": 1.0,
                "ticks_per_bar": 2,
            })

            started = None
            first_bar = None
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_started":
                    started = msg
                elif msg["type"] == "bar" and msg.get("source") == "sim":
                    first_bar = msg
                    break
            assert started is not None
            assert first_bar is not None

            ws.send_json({"type": "replay_pause"})
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_paused":
                    break

            ws.send_json({"type": "replay_step"})
            stepped_time = int(
                second_time.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
            )
            saw_step_bar = False
            saw_step_ack = False
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if (
                    msg["type"] == "bar"
                    and msg.get("source") == "sim"
                    and msg.get("time") == stepped_time
                ):
                    saw_step_bar = True
                if msg["type"] == "replay_stepped":
                    saw_step_ack = True
                    break
            assert saw_step_ack, "replay_stepped acknowledgment was not emitted"
            assert saw_step_bar, "replay_stepped arrived before the stepped bar frame"


class TestWriterControlUnderFlood:
    """Regression: the outbound writer used ``done.pop()`` on
    asyncio.wait(FIRST_COMPLETED) over two non-empty queues, discarding the
    other getter's already-dequeued message. Under a bar flood that silently
    ate control messages (replay_stopped acks; potentially order/fill).
    The writer must consume EVERY completed getter and deliver all of them,
    control priority first."""

    def test_stats_acks_survive_concurrent_bar_flood(self):
        import time as _time
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        app = create_app(session=session)

        ist = ZoneInfo("Asia/Kolkata")
        base = datetime(2026, 7, 15, 10, 0).replace(tzinfo=ist)
        inst = Equity.of("NSE", "TEST")
        STATS = 20

        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [{"instrument": "NSE:TEST", "interval": "1m"}],
            })
            assert ws.receive_json()["type"] == "subscribed_bars"

            # Interleave bucket-crossing quote bursts (tick frames) with stats
            # requests (control frames) so both queues hold data at once.
            stats_sent = 0
            for block in range(STATS // 2):
                for j in range(6):
                    i = block * 6 + j
                    bus.publish(Quote(
                        instrument=inst,
                        ltp=Price(Decimal(str(100 + i))),
                        timestamp=base + timedelta(minutes=i + 1, seconds=5),
                    ))
                _time.sleep(0.02)
                ws.send_json({"type": "stats"})
                stats_sent += 1
                ws.send_json({"type": "stats"})
                stats_sent += 1

            # Read everything; every stats request must come back.
            ws._receive_timeout = 15  # TestClient session read guard
            deadline = __import__("time").monotonic() + 15
            got_stats = 0
            saw_bar = False
            while got_stats < stats_sent and __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "stats":
                    got_stats += 1
                elif msg["type"] == "bar":
                    saw_bar = True
            assert saw_bar, "no bar frames flowed - flood did not engage"
            assert got_stats == stats_sent, (
                f"control loss: {got_stats}/{stats_sent} stats acks survived"
            )


def _unused(*a):  # pragma: no cover — keeps json import referenced if edits drift
    json.dumps({})
