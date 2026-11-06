from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

from gateway_support import FakeBroker, FakeWindowLoader, equity, gateway_config, make_candle
from tradex_domain.market import HistoricalSeries
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.history import HistoryService
from tradex_gateway.router import MarketDataRouter


def _service(
    loader: FakeWindowLoader, *brokers: FakeBroker, executor: object | None = None
) -> HistoryService:
    runtimes = {broker.name: BrokerRuntime(broker.name, broker) for broker in brokers}
    directory = InstrumentDirectory(runtimes)
    router = MarketDataRouter(gateway_config(), runtimes)
    return HistoryService(
        directory, router, gateway_config(), window_loader=loader, executor=executor
    )


def test_datalake_hit_serves_without_broker():
    moment = datetime(2026, 11, 6, 9, 15, tzinfo=UTC)
    loader = FakeWindowLoader((make_candle(equity(), moment),))
    service = _service(loader, FakeBroker("dhan"), FakeBroker("upstox"))
    result = service.history(
        "NSE:RELIANCE",
        "1m",
        moment,
        moment + timedelta(minutes=1),
    )
    assert result["source"] == "datalake"
    assert result["broker"] is None
    assert result["bars"][0]["t"] == int(moment.timestamp() * 1000)
    assert loader.calls == 1


def test_empty_datalake_falls_through_to_broker_and_caches():
    moment = datetime(2026, 11, 6, 9, 15, tzinfo=UTC)
    upstox = FakeBroker("upstox")
    upstox.history_impl = lambda instrument, tf, start, end: HistoricalSeries(
        instrument=instrument,
        timeframe=tf,
        candles=[make_candle(instrument, end)],
        start=start,
        end=end,
    )
    loader = FakeWindowLoader(())
    service = _service(loader, FakeBroker("dhan"), upstox)
    start, end = moment, moment + timedelta(minutes=1)
    first = service.history("NSE:RELIANCE", "1m", start, end)
    second = service.history("NSE:RELIANCE", "1m", start, end)
    assert first["source"] == "broker"
    assert first["broker"] == "upstox"
    assert second["cached"] is True
    assert sum(1 for call in upstox.calls if call[0] == "history") == 1


def test_unsupported_timeframe_resamples_from_fallback():
    moment = datetime(2026, 11, 6, 9, 15, tzinfo=UTC)

    def history(instrument, tf, start, end):
        if str(tf) == "30m":
            raise ValueError("unsupported timeframe")
        return HistoricalSeries(
            instrument=instrument,
            timeframe=tf,
            candles=[
                make_candle(instrument, moment),
                make_candle(instrument, moment + timedelta(minutes=15)),
            ],
            start=start,
            end=end,
        )

    upstox = FakeBroker("upstox")
    upstox.history_impl = history
    service = _service(FakeWindowLoader(()), FakeBroker("dhan"), upstox)
    result = service.history("IDX:NIFTY", "30m", moment, moment + timedelta(minutes=30))
    assert result["timeframe"] == "30m"
    assert any(call[2] == "15m" for call in upstox.calls if call[0] == "history")


def test_fill_gaps_merges_broker_tail():
    moment = datetime(2026, 11, 6, 9, 15, tzinfo=UTC)
    end = moment + timedelta(minutes=15)
    loader = FakeWindowLoader((make_candle(equity(), moment),))

    def history(instrument, tf, start, end):
        return HistoricalSeries(
            instrument=instrument,
            timeframe=tf,
            candles=[make_candle(instrument, end)],
            start=start,
            end=end,
        )

    upstox = FakeBroker("upstox")
    upstox.history_impl = history
    service = _service(loader, FakeBroker("dhan"), upstox)
    result = service.history("NSE:RELIANCE", "1m", moment, end, fill_gaps=True)
    assert result["source"] == "datalake"
    assert len(result["bars"]) == 2


def test_broker_bars_are_clipped_to_the_requested_window():
    ist = timezone(timedelta(hours=5, minutes=30))
    start = datetime(2026, 11, 3, 14, 59, tzinfo=ist)
    end = datetime(2026, 11, 3, 15, 0, tzinfo=ist)

    def history(instrument, tf, window_start, window_end):
        return HistoricalSeries(
            instrument=instrument,
            timeframe=tf,
            candles=[
                make_candle(instrument, datetime(2026, 11, 3, 9, 15, tzinfo=ist)),
                make_candle(instrument, datetime(2026, 11, 3, 14, 55, tzinfo=ist)),
                make_candle(instrument, datetime(2026, 11, 3, 15, 0, tzinfo=ist)),
            ],
            start=window_start,
            end=window_end,
        )

    upstox = FakeBroker("upstox")
    upstox.history_impl = history
    service = _service(FakeWindowLoader(()), FakeBroker("dhan"), upstox)
    result = service.history("IDX:NIFTY", "5m", start, end)
    times = [bar["t"] for bar in result["bars"]]
    assert times == [
        int(datetime(2026, 11, 3, 14, 55, tzinfo=ist).timestamp() * 1000),
        int(datetime(2026, 11, 3, 15, 0, tzinfo=ist).timestamp() * 1000),
    ]


def test_batch_naive_timestamps_are_rejected():
    service = _service(FakeWindowLoader(()), FakeBroker("dhan"), FakeBroker("upstox"))
    results = service.batch(
        [
            {
                "instrument_id": "IDX:NIFTY",
                "from_": "2026-11-06T09:15:00",
                "to": "2026-11-06T09:16:00",
            }
        ]
    )
    assert results[0]["error"]["code"] == "invalid_request"
    assert "UTC offset" in results[0]["error"]["message"]


def test_datalake_only_source_returns_empty_when_lake_misses():
    moment = datetime(2026, 11, 6, 9, 15, tzinfo=UTC)
    service = _service(FakeWindowLoader(()), FakeBroker("dhan"), FakeBroker("upstox"))
    result = service.history(
        "NSE:RELIANCE",
        "1m",
        moment,
        moment + timedelta(minutes=1),
        source="datalake",
    )
    assert result["source"] == "none"
    assert result["degraded"] is True


def _recording_history(windows: list[tuple[datetime, datetime]]):
    def history(instrument, tf, start, end):
        windows.append((start, end))
        return HistoricalSeries(
            instrument=instrument, timeframe=tf, candles=[], start=start, end=end
        )

    return history


def test_dhan_uses_its_declared_90_day_chunk_cap():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=60)
    windows: list[tuple[datetime, datetime]] = []
    dhan = FakeBroker("dhan")
    dhan.capabilities = dataclasses.replace(dhan.capabilities, max_history_days=90)
    dhan.history_impl = _recording_history(windows)
    service = _service(FakeWindowLoader(()), dhan, FakeBroker("upstox"))
    service.history("IDX:NIFTY", "1m", start, end, broker="dhan")
    assert len(windows) == 1
    assert windows[0] == (start, end)


def test_upstox_still_chunks_at_its_30_day_cap():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=60)
    windows: list[tuple[datetime, datetime]] = []
    upstox = FakeBroker("upstox")
    upstox.capabilities = dataclasses.replace(upstox.capabilities, max_history_days=30)
    upstox.history_impl = _recording_history(windows)
    service = _service(FakeWindowLoader(()), FakeBroker("dhan"), upstox)
    service.history("IDX:NIFTY", "1m", start, end, broker="upstox")
    assert len(windows) == 2
    assert windows[0] == (start, start + timedelta(days=30))
    assert windows[1] == (start + timedelta(days=30), end)


class _Completed:
    """Future double for a callable that already ran."""

    def result(self, timeout=None):
        return None


class _InlineExecutor:
    """Executor double that runs each submitted callable immediately, here."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        fn(*args, **kwargs)
        return _Completed()


def test_batch_uses_the_injected_executor_without_a_nested_pool():
    items = [
        {
            "instrument_id": "IDX:NIFTY",
            "timeframe": "1m",
            "from_": "2026-11-06T09:15:00Z",
            "to": "2026-11-06T09:16:00Z",
            "broker": "upstox",
        },
        {
            "instrument_id": "IDX:NIFTY",
            "timeframe": "1m",
            "from_": "2026-11-06T09:17:00Z",
            "to": "2026-11-06T09:18:00Z",
            "broker": "upstox",
        },
    ]
    executor = _InlineExecutor()
    service = _service(
        FakeWindowLoader(()), FakeBroker("dhan"), FakeBroker("upstox"), executor=executor
    )
    results = service.batch(items)
    # Only the tail goes to the shared pool: item 0 runs inline on the calling
    # thread, which is what keeps a saturated pool making progress. A nested
    # ThreadPoolExecutor would leave this count at zero.
    assert len(executor.calls) == 1
    assert [item["broker"] for item in results] == ["upstox", "upstox"]
    assert [item["coverage"]["from"] for item in results] == [
        "2026-11-06T09:15:00+00:00",
        "2026-11-06T09:17:00+00:00",
    ]
