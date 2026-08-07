"""Datalake tests — catalog, quality, corporate actions, source selection (F16/F28).

Ported from v3 ``test_analytics_datalake.py`` (datalake parts only).

v4 API differences:
- ``DataCatalog.write_bar(instrument, timeframe, candle)`` (v3: ``write_bars(symbol, [bars])``)
- ``DataCatalog.read_bars(instrument, timeframe, start, end)`` → list[Candle]
- ``DataQualityEngine.check_gaps(bars, timeframe)`` → list[dict]
- ``DataQualityEngine.check_duplicates(bars)`` → list[timestamp]
- ``CorporateActionStore.add(action)`` / ``.get(instrument, start, end)`` — no split adjustment
- ``SourceSelectionPolicy`` is a frozen dataclass with ``select()`` → str
- ``MCPDataLakeServer`` is a stub (start/stop only)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    Price,
    Quantity,
    Timeframe,
)

from tradex_trading.datalake import (
    CorporateActionStore,
    DataCatalog,
    DataQualityEngine,
    DataSourceKind,
    MCPDataLakeServer,
    SourceSelectionPolicy,
)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(close: float, ts: datetime) -> Candle:
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(close - 1))),
            high=Price(value=Decimal(str(close + 1))),
            low=Price(value=Decimal(str(close - 1))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


# ---------------------------------------------------------------------------
# DataCatalog — write/read round-trip
# ---------------------------------------------------------------------------


class TestDataCatalog:
    """DataCatalog file-backed OHLCV storage."""

    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        ts = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        candle = _candle(104.0, ts)
        catalog.write_bar(_eq(), Timeframe.D1, candle)

        bars = catalog.read_bars(_eq(), Timeframe.D1)
        assert len(bars) == 1
        assert bars[0].ohlc.close.value == Decimal("104.0")

    def test_read_empty_returns_empty(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        bars = catalog.read_bars(_eq(), Timeframe.D1)
        assert bars == []

    def test_read_with_time_filter(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        now = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        for i in range(5):
            catalog.write_bar(
                _eq(), Timeframe.D1,
                _candle(100.0 + i, now + timedelta(days=i)),
            )

        # Filter to days 1-2 (inclusive on both ends)
        start = now + timedelta(days=1)
        end = now + timedelta(days=2)
        bars = catalog.read_bars(_eq(), Timeframe.D1, start=start, end=end)
        assert len(bars) == 2

    def test_list_instruments(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        ts = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        catalog.write_bar(_eq(), Timeframe.D1, _candle(100.0, ts))

        instruments = catalog.list_instruments()
        assert len(instruments) == 1
        assert instruments[0] == ("NSE", "RELIANCE")


# ---------------------------------------------------------------------------
# DataQualityEngine — gap and duplicate detection
# ---------------------------------------------------------------------------


class TestDataQualityEngine:
    """DataQualityEngine data quality checks."""

    def test_check_gaps_detects_missing_bars(self) -> None:
        engine = DataQualityEngine()
        now = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        bars = [
            _candle(100.0, now),
            _candle(101.0, now + timedelta(days=1)),
            _candle(102.0, now + timedelta(days=5)),  # gap of 4 days
        ]
        gaps = engine.check_gaps(bars, Timeframe.D1)
        assert len(gaps) == 1
        assert gaps[0]["expected_count"] >= 2

    def test_check_gaps_no_gaps(self) -> None:
        engine = DataQualityEngine()
        now = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        bars = [
            _candle(100.0, now),
            _candle(101.0, now + timedelta(days=1)),
        ]
        gaps = engine.check_gaps(bars, Timeframe.D1)
        assert gaps == []

    def test_check_gaps_empty(self) -> None:
        engine = DataQualityEngine()
        assert engine.check_gaps([], Timeframe.D1) == []

    def test_check_duplicates(self) -> None:
        engine = DataQualityEngine()
        now = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        bars = [
            _candle(100.0, now),
            _candle(101.0, now),  # duplicate timestamp
            _candle(102.0, now + timedelta(days=1)),
        ]
        dupes = engine.check_duplicates(bars)
        assert len(dupes) == 1
        assert dupes[0] == now

    def test_check_duplicates_empty(self) -> None:
        engine = DataQualityEngine()
        assert engine.check_duplicates([]) == []


# ---------------------------------------------------------------------------
# CorporateActionStore
# ---------------------------------------------------------------------------


class TestCorporateActionStore:
    """CorporateActionStore — add/get actions."""

    def test_add_and_get(self) -> None:
        store = CorporateActionStore()
        eq = _eq()
        action = {
            "instrument": eq,
            "action_type": "dividend",
            "date": datetime(2026, 7, 15, tzinfo=UTC),
            "details": {"amount": 10.0},
        }
        store.add(action)
        results = store.get(eq)
        assert len(results) == 1
        assert results[0]["action_type"] == "dividend"

    def test_get_with_date_filter(self) -> None:
        store = CorporateActionStore()
        eq = _eq()
        store.add({
            "instrument": eq,
            "action_type": "dividend",
            "date": datetime(2026, 7, 15, tzinfo=UTC),
        })
        store.add({
            "instrument": eq,
            "action_type": "split",
            "date": datetime(2026, 8, 1, tzinfo=UTC),
        })

        results = store.get(
            eq,
            start=datetime(2026, 7, 20, tzinfo=UTC),
        )
        assert len(results) == 1
        assert results[0]["action_type"] == "split"

    def test_get_empty_store(self) -> None:
        store = CorporateActionStore()
        assert store.get(_eq()) == []

    def test_record_split_and_adjust_by_symbol(self) -> None:
        store = CorporateActionStore()
        store.record_split("RELIANCE", 2.0)
        assert store.adjust_price(100.0, symbol="reliance") == pytest.approx(50.0)

    def test_record_split_rejects_non_positive(self) -> None:
        store = CorporateActionStore()
        with pytest.raises(ValueError):
            store.record_split("X", 0.0)

    def test_adjust_price_explicit_ratio(self) -> None:
        store = CorporateActionStore()
        assert store.adjust_price(200.0, ratio=4.0) == pytest.approx(50.0)

    def test_adjust_price_missing_symbol_raises(self) -> None:
        store = CorporateActionStore()
        with pytest.raises(KeyError):
            store.adjust_price(100.0, symbol="UNKNOWN")

    def test_adjust_price_no_symbol_no_ratio_raises(self) -> None:
        store = CorporateActionStore()
        with pytest.raises(ValueError):
            store.adjust_price(100.0)


# ---------------------------------------------------------------------------
# SourceSelectionPolicy
# ---------------------------------------------------------------------------


class TestSourceSelectionPolicy:
    """SourceSelectionPolicy — data source selection."""

    def test_default_preferred_source(self) -> None:
        policy = SourceSelectionPolicy()
        assert policy.select(_eq(), Timeframe.D1) == "broker"

    def test_custom_preferred_source(self) -> None:
        policy = SourceSelectionPolicy(preferred_source="datalake")
        assert policy.select(_eq(), Timeframe.D1) == "datalake"

    def test_fallback_source(self) -> None:
        policy = SourceSelectionPolicy(preferred_source="broker", fallback_source="datalake")
        assert policy.fallback_source == "datalake"


class TestDataSourceKind:
    """DataSourceKind enum."""

    def test_enum_values(self) -> None:
        assert DataSourceKind.DATALAKE == "DATALAKE"
        assert DataSourceKind.BROKER_HISTORICAL == "BROKER_HISTORICAL"
        assert DataSourceKind.LIVE == "LIVE"
        assert DataSourceKind.REPLAY == "REPLAY"

    def test_is_str(self) -> None:
        assert isinstance(DataSourceKind.DATALAKE, str)


# ---------------------------------------------------------------------------
# MCPDataLakeServer — stub
# ---------------------------------------------------------------------------


class TestMCPDataLakeServer:
    """MCPDataLakeServer is a stub with start/stop."""

    def test_start_and_stop(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        server = MCPDataLakeServer(catalog)
        server.start()
        server.stop()


# ---------------------------------------------------------------------------
# DataCatalog — v3-compatible flat-file interface
# ---------------------------------------------------------------------------


class TestDataCatalogFlatFile:
    """DataCatalog.list_tables, has_bars, write_bars, query_bars, get_schema."""

    def test_write_and_has_bars(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        assert catalog.has_bars("TESTSYM") is False
        catalog.write_bars("TESTSYM", [
            {"timestamp": "2026-01-01T00:00:00+00:00", "close": 100.0},
        ])
        assert catalog.has_bars("TESTSYM") is True

    def test_list_tables(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        catalog.write_bars("AAA", [{"timestamp": "2026-01-01T00:00:00+00:00", "close": 1.0}])
        catalog.write_bars("BBB", [{"timestamp": "2026-01-01T00:00:00+00:00", "close": 2.0}])
        assert catalog.list_tables() == ["AAA", "BBB"]

    def test_query_bars_returns_iso_timestamps(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        catalog.write_bars("SYM", [
            {"timestamp": "2026-01-01T00:00:00+00:00", "close": 10.0},
            {"timestamp": "2026-01-02T00:00:00+00:00", "close": 11.0},
        ])
        rows = catalog.query_bars("SYM")
        assert len(rows) == 2
        assert rows[0]["timestamp"].startswith("2026-01-01")

    def test_query_bars_with_string_window(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        catalog.write_bars("SYM", [
            {"timestamp": "2026-01-01T00:00:00+00:00", "close": 10.0},
            {"timestamp": "2026-01-02T00:00:00+00:00", "close": 11.0},
            {"timestamp": "2026-01-03T00:00:00+00:00", "close": 12.0},
        ])
        rows = catalog.query_bars(
            "SYM",
            start="2026-01-02T00:00:00+00:00",
            end="2026-01-02T23:59:59+00:00",
        )
        assert len(rows) == 1
        assert rows[0]["close"] == 11.0

    def test_get_schema(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        assert catalog.get_schema() == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_write_bars_upserts_by_timestamp(self, tmp_path: Path) -> None:
        catalog = DataCatalog(tmp_path)
        catalog.write_bars("SYM", [
            {"timestamp": "2026-01-01T00:00:00+00:00", "close": 10.0},
        ])
        catalog.write_bars("SYM", [
            {"timestamp": "2026-01-01T00:00:00+00:00", "close": 99.0},
        ])
        rows = catalog.query_bars("SYM")
        assert len(rows) == 1
        assert rows[0]["close"] == 99.0
