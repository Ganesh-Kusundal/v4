"""Tests for SourceSelectionPolicy."""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_trading.datalake.source_selection import DataSourceKind, SourceSelectionPolicy


class TestSourceSelectionPolicy:
    """Tests for SourceSelectionPolicy.select()."""

    def test_default_returns_preferred_source(self) -> None:
        """Without catalog or historical range, returns preferred_source."""
        policy = SourceSelectionPolicy(preferred_source="broker")
        result = policy.select(instrument="AAPL", timeframe="1m")
        assert result == "broker"

    def test_with_catalog_that_has_bars_returns_datalake(self) -> None:
        """When catalog has bars for the instrument, returns DATALAKE."""
        catalog = MagicMock()
        catalog.has_bars.return_value = True
        policy = SourceSelectionPolicy(preferred_source="broker", _catalog=catalog)
        result = policy.select(instrument="AAPL", timeframe="1m")
        assert result == DataSourceKind.DATALAKE
        catalog.has_bars.assert_called_once_with("AAPL", "1m")

    def test_with_catalog_that_has_no_bars_falls_through(self) -> None:
        """When catalog doesn't have bars, falls through to preferred."""
        catalog = MagicMock()
        catalog.has_bars.return_value = False
        policy = SourceSelectionPolicy(preferred_source="broker", _catalog=catalog)
        result = policy.select(instrument="AAPL", timeframe="1m")
        assert result == "broker"

    def test_with_historical_range_returns_broker_historical(self) -> None:
        """When start/end provided, returns BROKER_HISTORICAL."""
        from datetime import UTC, datetime

        policy = SourceSelectionPolicy(preferred_source="broker")
        start = datetime(2024, 1, 1, tzinfo=UTC)
        result = policy.select(instrument="AAPL", timeframe="1m", start=start)
        assert result == DataSourceKind.BROKER_HISTORICAL

    def test_with_end_only_returns_broker_historical(self) -> None:
        """When only end provided, returns BROKER_HISTORICAL."""
        from datetime import UTC, datetime

        policy = SourceSelectionPolicy(preferred_source="broker")
        end = datetime(2024, 12, 31, tzinfo=UTC)
        result = policy.select(instrument="AAPL", timeframe="1m", end=end)
        assert result == DataSourceKind.BROKER_HISTORICAL

    def test_catalog_exception_falls_through(self) -> None:
        """When catalog raises, falls through gracefully."""
        catalog = MagicMock()
        catalog.has_bars.side_effect = RuntimeError("catalog error")
        policy = SourceSelectionPolicy(preferred_source="broker", _catalog=catalog)
        result = policy.select(instrument="AAPL", timeframe="1m")
        assert result == "broker"
