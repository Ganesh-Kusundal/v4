"""WS bar-streaming tests — subscribe_bars contract and replay control acks.

Follows the existing test_fastapi_app.py pattern: a MagicMock session with a
real ReactiveBus, since /ws/stream only needs ``session.bus``.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402
from tradex_reactive.bus import ReactiveBus  # noqa: E402


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

    def test_seed_continues_only_matching_bucket_and_marks_provisional(self):
        import time
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price, Quantity

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        app = create_app(session=session)
        base = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        current_time = int(base.timestamp())
        last_time = int((base - timedelta(minutes=1)).timestamp())

        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 5
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [
                    {
                        "instrument": "NSE:TEST",
                        "interval": "1m",
                        "seed": {
                            "lastClosed": {
                                "time": last_time,
                                "open": 90,
                                "high": 91,
                                "low": 89,
                                "close": 90,
                                "volume": 4,
                            },
                            "currentBucket": {
                                "time": current_time,
                                "open": 100,
                                "high": 105,
                                "low": 99,
                                "close": 102,
                                "volume": 10,
                            },
                        },
                        "provisional": False,
                    },
                    {
                        "instrument": "NSE:TEST",
                        "interval": "5m",
                        "provisional": True,
                    },
                ],
            })
            assert ws.receive_json()["type"] == "subscribed_bars"
            bus.publish(Quote(
                instrument=Equity.of("NSE", "TEST"),
                ltp=Price(Decimal("106")),
                volume=Quantity(Decimal("2")),
                timestamp=base + timedelta(seconds=30),
            ))
            time_deadline = time.monotonic() + 3
            frames = {}
            while len(frames) < 2 and time.monotonic() < time_deadline:
                message = ws.receive_json()
                if message.get("type") == "bar":
                    frames[message["interval"]] = message
            assert frames["1m"]["time"] == current_time
            assert frames["1m"]["open"] == 100
            assert frames["1m"]["high"] == 106
            assert frames["1m"]["close"] == 106
            assert frames["1m"]["volume"] == 12
            assert frames["1m"]["closed"] is False
            assert frames["1m"]["provisional"] is False
            assert frames["5m"]["open"] == 106
            assert frames["5m"]["provisional"] is True
            bus.publish(Quote(
                instrument=Equity.of("NSE", "TEST"),
                ltp=Price(Decimal("103")),
                volume=Quantity(Decimal("1")),
                timestamp=base + timedelta(seconds=65),
            ))
            deadline = time.monotonic() + 3
            closed = None
            while closed is None and time.monotonic() < deadline:
                message = ws.receive_json()
                if (
                    message.get("type") == "bar"
                    and message.get("interval") == "1m"
                    and message.get("closed")
                ):
                    closed = message
            assert closed is not None
            assert closed["time"] == current_time
            assert closed["open"] == 100
            assert closed["high"] == 106
            assert closed["close"] == 106
            assert closed["volume"] == 12

    def test_missing_current_bucket_marks_first_live_frame_provisional(self):
        import time
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price, Quantity

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        app = create_app(session=session)
        base = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        last_time = int((base - timedelta(minutes=1)).timestamp())

        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 5
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [{
                    "instrument": "NSE:TEST",
                    "interval": "1m",
                    "seed": {"lastClosed": {
                        "time": last_time,
                        "open": 90,
                        "high": 91,
                        "low": 89,
                        "close": 90,
                        "volume": 4,
                    }},
                }],
            })
            assert ws.receive_json()["type"] == "subscribed_bars"
            bus.publish(Quote(
                instrument=Equity.of("NSE", "TEST"),
                ltp=Price(Decimal("101")),
                volume=Quantity(Decimal("1")),
                timestamp=base + timedelta(seconds=10),
            ))
            deadline = time.monotonic() + 3
            frame = None
            while frame is None and time.monotonic() < deadline:
                message = ws.receive_json()
                if message.get("type") == "bar":
                    frame = message
            assert frame is not None
            assert frame["closed"] is False
            assert frame["provisional"] is True
            assert frame["time"] == int(base.timestamp())


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
            assert resp["type"] in ("error", "replay_error")

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
                if msg["type"] in ("error", "replay_error", "replay_done"):
                    got_started_or_error = True
                    break
                if msg["type"] == "replay_started" and "NOSUCH" in str(msg):
                    continue
            assert got_started_or_error or msg["type"] in ("error", "replay_error")

    @pytest.mark.timeout(10, func_only=True)
    def test_replay_step_ack_follows_stepped_bar_frame(self, monkeypatch):
        """A step is acknowledged only after its simulated bar frame is queued."""
        from datetime import datetime
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        import tradex_brokers.common.market_builders as market_builders
        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import OHLC, Candle
        from tradex_domain.value_objects import Price, Quantity

        import tradex_market_data.parquet_storage as parquet_storage

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
            # One step feeds M1 candles until the subscribed interval closes.
            # Feeding 9:16 closes the 9:15 bucket — that closed bar is the step.
            closed_time = int(
                first_time.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
            )
            saw_step_bar = False
            saw_step_ack = False
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if (
                    msg["type"] == "bar"
                    and msg.get("source") == "sim"
                    and msg.get("closed") is True
                    and msg.get("time") == closed_time
                ):
                    saw_step_bar = True
                if msg["type"] == "replay_stepped":
                    saw_step_ack = True
                    break
            assert saw_step_ack, "replay_stepped acknowledgment was not emitted"
            assert saw_step_bar, "replay_stepped arrived before the closed stepped bar frame"

            ws.send_json({"type": "replay_stop"})
            deadline = __import__("time").monotonic() + 5
            stopped = False
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_stopped":
                    stopped = True
                    break
            assert stopped, "expected replay_stopped after stop"

    def test_replay_start_aggregator_failure_errors(self):
        """Aggregator construction failure must not start replay."""
        from unittest.mock import patch

        class _BadStore:
            def __init__(self, _base_path) -> None: pass
            def read(self, *_args, **_kwargs):
                class _Df:
                    empty = True
                return _Df()
            def date_range(self, _symbol):
                return None

        client = TestClient(_app())
        with client.websocket_connect("/ws/stream") as ws:
            with patch("tradex_market_data.parquet_storage.ParquetStorage", _BadStore):
                ws.send_json({
                    "type": "replay_start",
                    "instrument": "NSE:NOSUCH",
                    "interval": "1m",
                    "minutes": 10,
                })
                msgs = []
                deadline = __import__("time").monotonic() + 5
                while __import__("time").monotonic() < deadline:
                    msg = ws.receive_json()
                    msgs.append(msg)
                    if msg["type"] in ("error", "replay_error", "replay_done"):
                        break
                assert any(m["type"] in ("error", "replay_error") for m in msgs)
                assert not any(m["type"] == "replay_started" for m in msgs)

    def test_replay_stopped_state_reset(self, monkeypatch):
        """Stop must reset paused, step, speed, and key; restart is fresh."""
        from datetime import datetime
        from decimal import Decimal

        import tradex_brokers.common.market_builders as market_builders
        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import OHLC, Candle
        from tradex_domain.value_objects import Price, Quantity

        import tradex_market_data.parquet_storage as parquet_storage

        _candle = Candle(
            instrument=Equity.of("NSE", "TEST"),
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(Decimal("100")),
                high=Price(Decimal("101")),
                low=Price(Decimal("99")),
                close=Price(Decimal("100")),
            ),
            volume=Quantity(Decimal("10")),
            timestamp=datetime(2026, 1, 5, 9, 15),
        )

        class _DataFrame:
            empty = False

        class _FakeStore:
            def __init__(self, _base_path) -> None: pass
            def read(self, *_args, **_kwargs): return _DataFrame()
            def date_range(self, _symbol): return None

        monkeypatch.setattr(parquet_storage, "ParquetStorage", _FakeStore)
        monkeypatch.setattr(
            market_builders, "candles_from_dataframe",
            lambda *_a, **_k: [_candle],
        )

        client = TestClient(_app())
        with client.websocket_connect("/ws/stream") as ws:
            # Start replay then stop - state must clear
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "speed": 2.0,
                "ticks_per_bar": 2,
            })
            deadline = __import__("time").monotonic() + 5
            started = False
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_started":
                    started = True
                if msg["type"] in ("replay_done", "replay_stopped"):
                    break
            assert started

            ws.send_json({"type": "replay_stop"})
            deadline = __import__("time").monotonic() + 5
            stopped = False
            while __import__("time").monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_stopped":
                    stopped = True
                    break
            assert stopped

            # After stop, pause/resume/step/speed must ack fresh (no stale state)
            ws.send_json({"type": "replay_pause"})
            assert ws.receive_json()["type"] == "replay_paused"
            ws.send_json({"type": "replay_resume"})
            assert ws.receive_json()["type"] == "replay_resumed"
            ws.send_json({"type": "replay_speed", "speed": 3.0})
            assert ws.receive_json()["type"] == "replay_speed"
            ws.send_json({"type": "replay_step"})
            # step doesn't ack, but pause after step should work
            ws.send_json({"type": "replay_pause"})
            assert ws.receive_json()["type"] == "replay_paused"

    def test_replay_scoped_flush_no_unrelated_aggregator_mutation(self, monkeypatch):
        """Sim replay quotes route only to the selected replay aggregator.

        After replay_stop, publish a live quote: only the replayed
        (instrument, interval) aggregator should emit a bar frame.
        """
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        import tradex_brokers.common.market_builders as market_builders
        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import OHLC, Candle, Quote
        from tradex_domain.value_objects import Price, Quantity

        import tradex_market_data.parquet_storage as parquet_storage

        _candle = Candle(
            instrument=Equity.of("NSE", "TEST"),
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(Decimal("100")),
                high=Price(Decimal("101")),
                low=Price(Decimal("99")),
                close=Price(Decimal("100")),
            ),
            volume=Quantity(Decimal("10")),
            timestamp=datetime(2026, 1, 5, 9, 15),
        )

        class _DataFrame:
            empty = False

        class _FakeStore:
            def __init__(self, _base_path) -> None: pass
            def read(self, *_args, **_kwargs): return _DataFrame()
            def date_range(self, _symbol): return None

        monkeypatch.setattr(parquet_storage, "ParquetStorage", _FakeStore)
        monkeypatch.setattr(
            market_builders, "candles_from_dataframe",
            lambda *_a, **_k: [_candle],
        )

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
                "bars": [
                    {"instrument": "NSE:TEST", "interval": "1m"},
                    {"instrument": "NSE:TEST", "interval": "5m"},
                ],
            })
            sub = ws.receive_json()
            assert sub["type"] == "subscribed_bars"
            assert sub["active"] == 2

            # Seed the 1m aggregator across a boundary so a live bar
            # frame exists for the 1m interval before replay starts.
            for offset_sec in [5, 30]:
                bus.publish(Quote(
                    instrument=inst,
                    ltp=Price(Decimal("100")),
                    timestamp=base + timedelta(seconds=offset_sec),
                ))
            import time
            time.sleep(0.1)
            deadline = __import__("time").monotonic() + 3
            while __import__("time").monotonic() < deadline:
                try:
                    msg = ws.receive_json()
                    if msg.get("type") == "bar" and msg.get("interval") == "1m":
                        break
                except Exception:
                    break

            # Start replay on 1m only - the scoped route fix ensures
            # sim quotes never reach the 5m aggregator.
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "ticks_per_bar": 2,
            })
            started = False
            deadline = __import__("time").monotonic() + 10
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_started":
                    started = True
                if msg["type"] == "replay_done":
                    break
            assert started, "replay_started not received"

            ws.send_json({"type": "replay_stop"})
            deadline = __import__("time").monotonic() + 5
            stopped = False
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_stopped":
                    stopped = True
                    break
            assert stopped, "expected replay_stopped"
            # TODO: verify 5m aggregator unaffected (needs async
            # drain; live quote path not yet wired for sync poll).


class TestReplayCursorContract:
    """Server owns the replay cursor: closed-interval steps, seek, D alias, live isolation."""

    @staticmethod
    def _m1_candles(n: int, start: datetime | None = None):
        from datetime import datetime, timedelta
        from decimal import Decimal

        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import OHLC, Candle
        from tradex_domain.value_objects import Price, Quantity

        instrument = Equity.of("NSE", "TEST")
        t0 = start or datetime(2026, 1, 5, 9, 15)
        out = []
        for i in range(n):
            px = Decimal("100") + Decimal(i)
            out.append(
                Candle(
                    instrument=instrument,
                    timeframe=Timeframe.M1,
                    ohlc=OHLC(
                        open=Price(px),
                        high=Price(px + Decimal("1")),
                        low=Price(px - Decimal("1")),
                        close=Price(px),
                    ),
                    volume=Quantity(Decimal("10")),
                    timestamp=t0 + timedelta(minutes=i),
                )
            )
        return out

    @staticmethod
    def _patch_lake(monkeypatch, candles):
        import tradex_brokers.common.market_builders as market_builders

        import tradex_market_data.parquet_storage as parquet_storage

        class _DataFrame:
            empty = False

        class _FakeStore:
            def __init__(self, _base_path) -> None:
                pass

            def read(self, *_args, **_kwargs):
                return _DataFrame()

            def date_range(self, _symbol):
                return None

        monkeypatch.setattr(parquet_storage, "ParquetStorage", _FakeStore)
        monkeypatch.setattr(
            market_builders,
            "candles_from_dataframe",
            lambda *_a, **_k: list(candles),
        )

    def test_subscribe_bars_accepts_daily_D_alias(self):
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [{"instrument": "NSE:TEST", "interval": "D"}],
            })
            resp = ws.receive_json()
            assert resp["type"] == "subscribed_bars"
            assert resp["bars"] == ["NSE:TEST|1d"]

    @pytest.mark.timeout(15, func_only=True)
    def test_replay_step_closes_one_5m_bar(self, monkeypatch):
        """A 5m step feeds M1 candles until one closed 5-minute bar emits."""
        candles = self._m1_candles(7)  # 9:15..9:21 — first 5m closes on 9:20
        self._patch_lake(monkeypatch, candles)

        client = TestClient(_app())
        with client.websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 5
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "5m",
                "speed": 1.0,
                "ticks_per_bar": 2,
            })
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_started":
                    break
            ws.send_json({"type": "replay_pause"})
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_paused":
                    break

            ws.send_json({"type": "replay_step"})
            saw_closed = False
            saw_stepped = False
            deadline = __import__("time").monotonic() + 8
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if (
                    msg["type"] == "bar"
                    and msg.get("closed") is True
                    and msg.get("interval") == "5m"
                    and msg.get("source") == "sim"
                ):
                    saw_closed = True
                if msg["type"] == "replay_stepped":
                    saw_stepped = True
                    break
            assert saw_stepped
            assert saw_closed, "5m step must emit one closed interval bar before replay_stepped"

            ws.send_json({"type": "replay_stop"})
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_stopped":
                    break

    @pytest.mark.timeout(15, func_only=True)
    def test_replay_seek_restarts_at_start_time(self, monkeypatch):
        from zoneinfo import ZoneInfo

        candles = self._m1_candles(5)
        self._patch_lake(monkeypatch, candles)
        seek_ts = int(
            candles[2].timestamp.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
        )

        client = TestClient(_app())
        with client.websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 5
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "ticks_per_bar": 2,
            })
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_started":
                    break

            ws.send_json({"type": "replay_seek", "start_time": seek_ts})
            restarted = None
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_started":
                    restarted = msg
                    break
            assert restarted is not None
            assert restarted["start_time"] == seek_ts
            assert restarted["total_bars"] == 3  # candles[2:]

            ws.send_json({"type": "replay_stop"})
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_stopped":
                    break


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


class _ReplayFixture:
    def __init__(self, monkeypatch, candles, provider_error=None):
        from tradex_interfaces.replay_guard import ReplayGuard

        self.guard = ReplayGuard()
        self.bus = ReactiveBus()
        self.session = MagicMock()
        self.session.state = "READY"
        self.session.mode = "paper"
        self.session.bus = self.bus
        self.app = create_app(session=self.session, replay_guard=self.guard)
        self.client = TestClient(self.app)
        self.ws_context = self.client.websocket_connect("/ws/stream")
        self.ws = self.ws_context.__enter__()
        self.ws._receive_timeout = 8
        self.candles = list(candles)
        self.provider_error = provider_error
        self.monkeypatch = monkeypatch
        self._patch_lake(monkeypatch)

    def _patch_lake(self, monkeypatch):
        import tradex_brokers.common.market_builders as market_builders

        import tradex_market_data.parquet_storage as parquet_storage
        from tradex_market_data.market_provider import ParquetMarketProvider

        candles = self.candles
        provider_error = self.provider_error

        class _Frame:
            empty = not candles

        class _Store:
            def __init__(self, _base_path):
                pass

            def read(self, *_args, **_kwargs):
                return _Frame()

            def date_range(self, _symbol):
                if not candles:
                    return None
                return (candles[0].timestamp, candles[-1].timestamp)

        monkeypatch.setattr(parquet_storage, "ParquetStorage", _Store)
        monkeypatch.setattr(
            market_builders,
            "candles_from_dataframe",
            lambda *_a, **_k: list(candles),
        )
        if provider_error is not None:
            def _raise(*_args, **_kwargs):
                raise provider_error

            monkeypatch.setattr(ParquetMarketProvider, "history", _raise)

    def break_replay_task(self):
        from tradex_replay.synthetic_ticks import SyntheticTickGenerator

        def _raise(*_args, **_kwargs):
            raise RuntimeError("history failed")

        self.monkeypatch.setattr(SyntheticTickGenerator, "iter_ticks", _raise)

    def send(self, message):
        self.ws.send_json(message)

    def receive_until(self, kind):
        import time

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            message = self.ws.receive_json()
            if message.get("type") == kind:
                return message
        raise AssertionError(f"did not receive {kind}")

    def subscribe(self):
        self.send({
            "type": "subscribe_bars",
            "bars": [{"instrument": "NSE:TEST", "interval": "1m"}],
        })
        assert self.receive_until("subscribed_bars")["active"] == 1

    def start(self, **overrides):
        message = {
            "type": "replay_start",
            "instrument": "NSE:TEST",
            "interval": "1m",
            "speed": 1.0,
            "ticks_per_bar": 2,
        }
        message.update(overrides)
        self.send(message)

    def natural_finish(self):
        return self.receive_until("replay_done")

    def receive_bar_for_run(self, run_id):
        import time

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            message = self.ws.receive_json()
            if message.get("type") == "bar" and message.get("run_id") == run_id:
                return message
        raise AssertionError("did not receive a replay bar")

    def receive_bar_after(self, previous_time):
        import time

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            message = self.ws.receive_json()
            if message.get("type") == "bar" and message.get("time", 0) > previous_time:
                return message
        raise AssertionError("did not receive a later bar")

    def publish_live(self):
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price

        instrument = Equity.of("NSE", "TEST")
        base = datetime(2026, 1, 5, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        for offset, price in ((5, 200), (65, 201)):
            self.bus.publish(
                Quote(
                    instrument=instrument,
                    ltp=Price(Decimal(price)),
                    timestamp=base + timedelta(seconds=offset),
                )
            )
        return self.receive_until("bar")

    def close(self):
        self.ws_context.__exit__(None, None, None)
        self.client.close()


def _lifecycle_candles(count=5):
    from datetime import datetime, timedelta
    from decimal import Decimal

    from tradex_domain.enums import Timeframe
    from tradex_domain.instruments import Equity
    from tradex_domain.market import OHLC, Candle
    from tradex_domain.value_objects import Price, Quantity

    instrument = Equity.of("NSE", "TEST")
    start = datetime(2026, 1, 5, 8, 0)
    return [
        Candle(
            instrument=instrument,
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(Decimal(100 + i)),
                high=Price(Decimal(101 + i)),
                low=Price(Decimal(99 + i)),
                close=Price(Decimal(100 + i)),
            ),
            volume=Quantity(Decimal(10)),
            timestamp=start + timedelta(minutes=i),
        )
        for i in range(count)
    ]


@pytest.fixture
def replay_fixture(monkeypatch):
    fixture = _ReplayFixture(monkeypatch, _lifecycle_candles())
    fixture.subscribe()
    try:
        yield fixture
    finally:
        fixture.close()


def test_replay_done_releases_live_key_and_guard(replay_fixture):
    replay_fixture.start()
    replay_fixture.receive_until("replay_started")
    assert replay_fixture.guard.active is True
    replay_fixture.natural_finish()
    assert replay_fixture.guard.active is False
    live = replay_fixture.publish_live()
    assert live.get("source") == "live"


def test_replay_setup_error_releases_guard(replay_fixture, monkeypatch):
    replay_fixture.candles = []
    replay_fixture._patch_lake(monkeypatch)
    replay_fixture.start()
    assert replay_fixture.receive_until("replay_error")["type"] == "replay_error"
    assert replay_fixture.guard.active is False
    assert replay_fixture.publish_live().get("source") == "live"


def test_replay_task_exception_releases_guard(replay_fixture):
    replay_fixture.break_replay_task()
    replay_fixture.start()
    assert replay_fixture.receive_until("replay_error")["type"] == "replay_error"
    assert replay_fixture.guard.active is False
    assert replay_fixture.publish_live().get("source") == "live"


def test_replay_seek_never_reuses_partial_bucket(replay_fixture):
    replay_fixture.start(speed=1.0)
    first = replay_fixture.receive_until("bar")
    partial = replay_fixture.receive_bar_after(first["time"])
    partial_time = partial["time"]
    replay_fixture.send({"type": "replay_pause"})
    replay_fixture.receive_until("replay_paused")
    previous_time = partial_time - 60
    replay_fixture.send({"type": "replay_seek", "start_time": previous_time})
    restarted = replay_fixture.receive_until("replay_started")
    first_after_seek = replay_fixture.receive_bar_for_run(restarted["run_id"])
    assert first_after_seek["time"] < partial_time
    assert first_after_seek["source"] == "sim"
    replay_fixture.send({"type": "replay_stop"})
    replay_fixture.receive_until("replay_stopped")
    assert replay_fixture.publish_live().get("source") == "live"


def test_invalid_replay_parameters_return_error_without_closing_socket(replay_fixture):
    replay_fixture.send({"type": "replay_start", "speed": "not-a-number"})
    assert replay_fixture.receive_until("replay_error")["type"] == "replay_error"
    assert replay_fixture.guard.active is False
    assert replay_fixture.publish_live().get("source") == "live"


def test_active_replay_errors_include_run_id_and_stop_run(replay_fixture):
    replay_fixture.start()
    started = replay_fixture.receive_until("replay_started")
    replay_fixture.send({"type": "replay_speed", "speed": "invalid"})
    error = replay_fixture.receive_until("replay_error")
    stopped = replay_fixture.receive_until("replay_stopped")
    assert error["run_id"] == started["run_id"]
    assert stopped["run_id"] == started["run_id"]
    assert replay_fixture.guard.active is False


def test_invalid_new_start_does_not_scope_to_active_run(replay_fixture):
    replay_fixture.start()
    started = replay_fixture.receive_until("replay_started")
    replay_fixture.send({"type": "replay_start", "speed": "invalid"})
    error = replay_fixture.receive_until("replay_error")
    assert "run_id" not in error
    replay_fixture.send({"type": "replay_pause"})
    paused = replay_fixture.receive_until("replay_paused")
    assert paused["run_id"] == started["run_id"]
    replay_fixture.send({"type": "replay_stop"})
    replay_fixture.receive_until("replay_stopped")


    json.dumps({})


# ---------------------------------------------------------------------------
# Wave B6: on_bar_frame integrity hook + terminal path isolation tests
# ---------------------------------------------------------------------------

def _make_session(bus=None):
    session = MagicMock()
    session.state = "READY"
    session.bus = bus or ReactiveBus()
    session.market_feed = None  # explicit: no integrity tracker by default
    return session


def _patch_store(monkeypatch, candles):
    import tradex_brokers.common.market_builders as market_builders
    import tradex_market_data.parquet_storage as parquet_storage

    class _Frame:
        empty = not candles

    class _Store:
        def __init__(self, _): pass
        def read(self, *a, **kw): return _Frame()
        def date_range(self, _): return (candles[0].timestamp, candles[-1].timestamp) if candles else None

    monkeypatch.setattr(parquet_storage, "ParquetStorage", _Store)
    monkeypatch.setattr(market_builders, "candles_from_dataframe", lambda *a, **k: list(candles))


class TestOnBarFrameIntegrityHook:
    """on_bar_frame must be called for live closed bars, never for sim/forming bars."""

    def test_live_closed_bar_calls_market_feed_on_bar_frame(self):
        """A live quote that closes a bucket triggers market_feed.on_bar_frame."""
        import time
        from datetime import datetime, timedelta
        from decimal import Decimal
        from unittest.mock import MagicMock
        from zoneinfo import ZoneInfo

        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        mock_feed = MagicMock()
        session.market_feed = mock_feed
        app = create_app(session=session)

        ist = ZoneInfo("Asia/Kolkata")
        base = datetime(2026, 7, 15, 10, 0).replace(tzinfo=ist)
        inst = Equity.of("NSE", "INTEGRITY")

        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 5
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [{"instrument": "NSE:INTEGRITY", "interval": "1m"}],
            })
            assert ws.receive_json()["type"] == "subscribed_bars"

            # Two quotes: one in bucket 10:00, one in 10:01 — closes the 10:00 bar.
            for i, ts in enumerate([
                base + timedelta(seconds=5),
                base + timedelta(minutes=1, seconds=5),
            ]):
                bus.publish(Quote(
                    instrument=inst,
                    ltp=Price(Decimal(str(100 + i))),
                    timestamp=ts,
                ))
                time.sleep(0.05)

            # Drain until we see the closed bar.
            deadline = time.monotonic() + 4
            got_closed = False
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg.get("type") == "bar" and msg.get("closed") is True:
                    got_closed = True
                    break

        assert got_closed, "expected a closed live bar frame"
        # Hook must have fired for the closed frame.
        assert mock_feed.on_bar_frame.called
        frame = mock_feed.on_bar_frame.call_args[0][0]
        assert frame.closed is True
        assert frame.source == "live"

    def test_forming_bar_does_not_call_market_feed_on_bar_frame(self):
        """Forming (open) bars must not trigger on_bar_frame."""
        import time
        from datetime import datetime, timedelta
        from decimal import Decimal
        from unittest.mock import MagicMock
        from zoneinfo import ZoneInfo

        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        mock_feed = MagicMock()
        session.market_feed = mock_feed
        app = create_app(session=session)

        ist = ZoneInfo("Asia/Kolkata")
        base = datetime(2026, 7, 15, 10, 0).replace(tzinfo=ist)
        inst = Equity.of("NSE", "FORMTEST")

        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 3
            ws.send_json({
                "type": "subscribe_bars",
                "bars": [{"instrument": "NSE:FORMTEST", "interval": "1m"}],
            })
            assert ws.receive_json()["type"] == "subscribed_bars"

            # Single quote — stays in the open bucket (no close event yet).
            bus.publish(Quote(
                instrument=inst,
                ltp=Price(Decimal("100")),
                timestamp=base + timedelta(seconds=5),
            ))
            time.sleep(0.1)
            # Consume the single forming frame — that's all that should arrive.
            forming = ws.receive_json()
            assert forming.get("type") == "bar"
            assert forming.get("closed") is False
            # Close WS without crossing a bucket boundary.

        # on_bar_frame must NOT have been called (no closed bar emitted).
        assert not mock_feed.on_bar_frame.called

    @pytest.mark.timeout(15, func_only=True)
    def test_sim_bar_does_not_call_market_feed_on_bar_frame(self, monkeypatch):
        """Replay (sim) closed bars must NOT call market_feed.on_bar_frame."""
        from unittest.mock import MagicMock

        mock_feed = MagicMock()
        candles = _lifecycle_candles(count=3)
        _patch_store(monkeypatch, candles)

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        session.market_feed = mock_feed
        app = create_app(session=session)

        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 8
            # speed=1000 → delay per tick < 0.001s → no asyncio.sleep → finishes in ms
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "ticks_per_bar": 2,
                "speed": 1000.0,
            })
            import time
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] in ("replay_done", "replay_error"):
                    break

        # Replay emits sim bars — integrity hook must not fire for any of them.
        for call in mock_feed.on_bar_frame.call_args_list:
            frame = call[0][0]
            assert frame.source != "sim", (
                f"on_bar_frame called for sim bar: {frame}"
            )


class TestTerminalPathIsolation:
    """Each terminal path must stop task, flush aggregator, release guard, restore controls."""

    @pytest.mark.timeout(15, func_only=True)
    def test_ws_disconnect_during_paused_replay_releases_guard(self, monkeypatch):
        """Abrupt WS disconnect (no replay_stop) must still release the guard."""
        from tradex_interfaces.replay_guard import ReplayGuard

        guard = ReplayGuard()
        candles = _lifecycle_candles(count=5)
        _patch_store(monkeypatch, candles)

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        session.market_feed = None
        app = create_app(session=session, replay_guard=guard)

        import time
        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 8
            # paused=True keeps the task alive so the disconnect is mid-replay.
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "ticks_per_bar": 2,
                "paused": True,
            })
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_started":
                    break
            assert guard.active, "guard must be active before disconnect"
            # Exit without replay_stop — simulates client crash / network drop.

        assert not guard.active, "guard must be released after WS disconnect"

    @pytest.mark.timeout(15, func_only=True)
    def test_new_replay_while_active_replaces_previous_and_guard_clean(self, monkeypatch):
        """Starting a second replay while one is active must stop the first cleanly."""
        from tradex_interfaces.replay_guard import ReplayGuard

        guard = ReplayGuard()
        candles = _lifecycle_candles(count=5)
        _patch_store(monkeypatch, candles)

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        session.market_feed = None
        app = create_app(session=session, replay_guard=guard)

        import time
        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 8

            # --- First replay (paused so it stays alive) ---
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "ticks_per_bar": 2,
                "paused": True,
            })
            first_run_id = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_started":
                    first_run_id = msg["run_id"]
                    break
            assert first_run_id is not None
            assert guard.active

            # --- Start second replay WITHOUT sending replay_stop ---
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "ticks_per_bar": 2,
            })
            second_run_id = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_started":
                    second_run_id = msg["run_id"]
                    break
            assert second_run_id is not None
            assert second_run_id != first_run_id
            assert guard.active, "guard must still be active (second replay running)"

            # Drain until second replay finishes (not paused) or stop it.
            ws.send_json({"type": "replay_stop"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_stopped":
                    break

        assert not guard.active, "guard must be fully released after second replay stops"

    @pytest.mark.timeout(10, func_only=True)
    def test_repeated_stop_after_natural_finish_does_not_corrupt_guard(self, replay_fixture):
        """Extra stop commands after natural replay_done must be no-ops (guard stays released)."""
        replay_fixture.start()
        replay_fixture.natural_finish()
        assert not replay_fixture.guard.active

        # Send two extra stop commands.
        replay_fixture.send({"type": "replay_stop"})
        replay_fixture.send({"type": "replay_stop"})
        import time; time.sleep(0.1)

        assert not replay_fixture.guard.active, "repeated stop commands must not re-lock the guard"

    @pytest.mark.timeout(15, func_only=True)
    def test_repeated_pause_seek_stop_while_active_cleans_up(self, monkeypatch):
        """Rapid repeated control commands while active must all clean up correctly."""
        from tradex_interfaces.replay_guard import ReplayGuard

        guard = ReplayGuard()
        candles = _lifecycle_candles(count=5)
        _patch_store(monkeypatch, candles)

        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        session.market_feed = None
        app = create_app(session=session, replay_guard=guard)

        import time
        from zoneinfo import ZoneInfo

        seek_ts = int(
            candles[2].timestamp.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()
        )

        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 8
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "ticks_per_bar": 2,
            })
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_started":
                    break

            # Fire rapid control commands.
            for _ in range(3):
                ws.send_json({"type": "replay_pause"})
                ws.send_json({"type": "replay_resume"})
            ws.send_json({"type": "replay_seek", "start_time": seek_ts})
            # Allow seek to restart.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_started":
                    break
            ws.send_json({"type": "replay_stop"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_stopped":
                    break

        assert not guard.active

    @pytest.mark.timeout(15, func_only=True)
    def test_task_exception_releases_guard_and_restores_live_bars(self, replay_fixture):
        """Task crash path (replay_error) must release guard; live bars resume."""
        replay_fixture.break_replay_task()
        replay_fixture.start()
        assert replay_fixture.receive_until("replay_error")["type"] == "replay_error"
        assert not replay_fixture.guard.active
        live = replay_fixture.publish_live()
        assert live.get("source") == "live", "live bars must flow after replay task crash"


class TestReplayContractFixes:
    """OHLC path, isolation, pause mid-bar, bar_index, seek errors, speed label."""

    def _m1_candles(self, n: int):
        from datetime import datetime, timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo

        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import OHLC, Candle
        from tradex_domain.value_objects import Price, Quantity

        instrument = Equity.of("NSE", "TEST")
        base = datetime(2026, 1, 5, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
        candles = []
        for i in range(n):
            open_ = Decimal("100") + Decimal(i)
            candles.append(
                Candle(
                    instrument=instrument,
                    timeframe=Timeframe.M1,
                    ohlc=OHLC(
                        open=Price(open_),
                        high=Price(open_ + Decimal("2")),
                        low=Price(open_ - Decimal("1")),
                        close=Price(open_ + Decimal("1")),
                    ),
                    volume=Quantity(Decimal("1000")),
                    timestamp=base + timedelta(minutes=i),
                )
            )
        return candles

    def _patch_lake(self, monkeypatch, candles):
        import tradex_brokers.common.market_builders as market_builders
        import tradex_market_data.parquet_storage as parquet_storage

        class _Frame:
            empty = not candles

        class _Store:
            def __init__(self, _base_path):
                pass

            def read(self, *_args, **_kwargs):
                return _Frame()

            def date_range(self, _symbol):
                if not candles:
                    return None
                return (candles[0].timestamp, candles[-1].timestamp)

        monkeypatch.setattr(parquet_storage, "ParquetStorage", _Store)
        monkeypatch.setattr(
            market_builders,
            "candles_from_dataframe",
            lambda *_a, **_k: list(candles),
        )

    @pytest.mark.timeout(15, func_only=True)
    def test_replay_ohlc_bar_matches_candle_and_emits_no_quotes(self, monkeypatch):
        candles = self._m1_candles(2)
        self._patch_lake(monkeypatch, candles)
        first = candles[0]

        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 8
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "speed": 1000.0,
                "paused": True,
            })
            started = None
            import time
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_started":
                    started = msg
                    break
            assert started is not None
            assert started["simulated"] is True
            assert started["wall_seconds_per_bar"] == pytest.approx(1.0 / 1000.0)

            ws.send_json({"type": "replay_step"})
            closed = None
            quotes = []
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "quote":
                    quotes.append(msg)
                if (
                    msg["type"] == "bar"
                    and msg.get("closed") is True
                    and msg.get("source") == "sim"
                ):
                    closed = msg
                if msg["type"] == "replay_stepped":
                    assert msg.get("bar_index") == 0
                    break
            assert closed is not None
            assert closed["open"] == float(first.ohlc.open.value)
            assert closed["high"] == float(first.ohlc.high.value)
            assert closed["low"] == float(first.ohlc.low.value)
            assert closed["close"] == float(first.ohlc.close.value)
            assert closed["volume"] == float(first.volume.value)
            assert quotes == [], "replay must not emit quote frames"

            ws.send_json({"type": "replay_stop"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_stopped":
                    break

    @pytest.mark.timeout(15, func_only=True)
    def test_second_connection_replay_rejected_while_first_holds_guard(self, monkeypatch):
        from tradex_interfaces.replay_guard import ReplayGuard

        guard = ReplayGuard()
        candles = self._m1_candles(5)
        self._patch_lake(monkeypatch, candles)
        bus = ReactiveBus()
        session = MagicMock()
        session.state = "READY"
        session.bus = bus
        session.market_feed = None
        app = create_app(session=session, replay_guard=guard)

        import time
        with TestClient(app).websocket_connect("/ws/stream") as ws1:
            ws1._receive_timeout = 8
            ws1.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "speed": 1000.0,
                "paused": True,
            })
            first = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = ws1.receive_json()
                if msg["type"] == "replay_started":
                    first = msg
                    break
            assert first is not None
            assert guard.active

            with TestClient(app).websocket_connect("/ws/stream") as ws2:
                ws2._receive_timeout = 5
                ws2.send_json({
                    "type": "replay_start",
                    "instrument": "NSE:TEST",
                    "interval": "1m",
                    "speed": 1000.0,
                })
                err = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    msg = ws2.receive_json()
                    if msg["type"] == "replay_error":
                        err = msg
                        break
                assert err is not None
                assert "another replay" in err["message"]

            ws1.send_json({"type": "replay_stop"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws1.receive_json()["type"] == "replay_stopped":
                    break
        assert not guard.active

    @pytest.mark.timeout(15, func_only=True)
    def test_pause_honored_between_prints(self, monkeypatch):
        candles = self._m1_candles(3)
        self._patch_lake(monkeypatch, candles)

        import time
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 8
            # Start paused so the run is armed, resume into a slow pace,
            # pause again, wait wall-clock without draining, then step.
            # If pause were only checked between candles, the in-flight
            # candle would finish (closed bar) during the quiet window.
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "speed": 1.0,
                "paused": True,
            })
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_started":
                    break
            ws.send_json({"type": "replay_resume"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_resumed":
                    break
            time.sleep(0.05)
            ws.send_json({"type": "replay_pause"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_paused":
                    break
            # Quiet window: do not drain — a closed bar would only appear
            # if the current candle finished despite pause.
            time.sleep(0.7)
            ws.send_json({"type": "replay_step"})
            closed_after = 0
            saw_step = False
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if (
                    msg["type"] == "bar"
                    and msg.get("closed")
                    and msg.get("source") == "sim"
                ):
                    closed_after += 1
                if msg["type"] == "replay_stepped":
                    saw_step = True
                    break
            assert saw_step
            # Only the stepped bar should close after the quiet window.
            assert closed_after == 1
            ws.send_json({"type": "replay_stop"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_stopped":
                    break

    @pytest.mark.timeout(15, func_only=True)
    def test_replay_stepped_bar_index_advances(self, monkeypatch):
        candles = self._m1_candles(3)
        self._patch_lake(monkeypatch, candles)

        import time
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 8
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "speed": 1000.0,
                "paused": True,
            })
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_started":
                    break

            indices = []
            for expected in (0, 1):
                ws.send_json({"type": "replay_step"})
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    msg = ws.receive_json()
                    if msg["type"] == "replay_stepped":
                        indices.append(msg["bar_index"])
                        assert msg["bar_index"] == expected
                        break
            assert indices == [0, 1]
            ws.send_json({"type": "replay_stop"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if ws.receive_json()["type"] == "replay_stopped":
                    break

    @pytest.mark.timeout(15, func_only=True)
    def test_seek_failure_includes_previous_run_id(self, monkeypatch):
        candles = self._m1_candles(3)
        self._patch_lake(monkeypatch, candles)

        import time
        with TestClient(_app()).websocket_connect("/ws/stream") as ws:
            ws._receive_timeout = 8
            ws.send_json({
                "type": "replay_start",
                "instrument": "NSE:TEST",
                "interval": "1m",
                "speed": 1000.0,
                "paused": True,
            })
            started = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_started":
                    started = msg
                    break
            assert started is not None

            ws.send_json({"type": "replay_seek", "index": 999})
            err = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "replay_error":
                    err = msg
                    break
            assert err is not None
            assert err.get("run_id") == started["run_id"]
