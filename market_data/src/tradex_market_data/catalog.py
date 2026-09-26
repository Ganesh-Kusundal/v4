"""Data catalog — file-backed catalog for OHLCV bars."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tradex_domain import Candle

from tradex_market_data.paths import datalake_root


def _safe_symbol(symbol: str) -> str:
    """Sanitise a symbol for use as a filename component."""
    return symbol.strip().upper().replace(":", "_").replace("/", "_")


def _parse_ts(value: str | datetime) -> datetime:
    """Parse an ISO timestamp string or pass through a datetime."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DataCatalog:
    """File-backed data catalog for OHLCV bars."""

    def __init__(self, root: str | Path | None = None) -> None:
        """Initialize catalog.

        Args:
            root: Root directory for data storage. Defaults to the
                repo-anchored datalake root (``paths.datalake_root()``), so
                the catalog opens the same lake as ``ParquetStorage`` from
                any working directory. Resolved here rather than as a
                signature default, which would freeze it at import time.
        """
        # Previously the default was the relative literal "data/lake" — a
        # third spelling that matched neither the other consumers' "data/" nor
        # the anchored root, and that no directory, config or test in the repo
        # ever referenced. Anything wanting a nested sub-layout passes it.
        self._root = Path(root or datalake_root())
        self._root.mkdir(parents=True, exist_ok=True)

    def write_bar(self, instrument, timeframe, candle: Candle) -> None:
        """Write a candle to the catalog.

        Args:
            instrument: Instrument instance
            timeframe: Timeframe enum
            candle: Candle to write
        """
        # Create directory structure: root/exchange/symbol/timeframe/
        symbol_dir = self._root / instrument.exchange / instrument.symbol / timeframe.value
        symbol_dir.mkdir(parents=True, exist_ok=True)

        # Write candle as JSON
        filename = f"{candle.timestamp.strftime('%Y%m%d_%H%M%S')}.json"
        filepath = symbol_dir / filename

        data = {
            'timestamp': candle.timestamp.isoformat(),
            'open': str(candle.ohlc.open.value),
            'high': str(candle.ohlc.high.value),
            'low': str(candle.ohlc.low.value),
            'close': str(candle.ohlc.close.value),
            'volume': str(candle.volume.value),
        }

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

    def read_bars(self, instrument, timeframe, start=None, end=None) -> list:
        """Read candles from the catalog.

        Args:
            instrument: Instrument instance
            timeframe: Timeframe enum
            start: Start datetime (optional)
            end: End datetime (optional)

        Returns:
            List of Candle instances
        """
        from decimal import Decimal

        from tradex_domain import OHLC, Price, Quantity

        symbol_dir = self._root / instrument.exchange / instrument.symbol / timeframe.value

        if not symbol_dir.exists():
            return []

        candles = []
        for filepath in sorted(symbol_dir.glob('*.json')):
            with open(filepath) as f:
                data = json.load(f)

            timestamp = datetime.fromisoformat(data['timestamp'])

            # Apply time filters
            if start and timestamp < start:
                continue
            if end and timestamp > end:
                continue

            candle = Candle(
                instrument=instrument,
                timeframe=timeframe,
                ohlc=OHLC(
                    open=Price(Decimal(data['open'])),
                    high=Price(Decimal(data['high'])),
                    low=Price(Decimal(data['low'])),
                    close=Price(Decimal(data['close'])),
                ),
                volume=Quantity(Decimal(data['volume'])),
                timestamp=timestamp,
            )
            candles.append(candle)

        return candles

    def list_instruments(self) -> list:
        """List all instruments in the catalog.

        Returns:
            List of (exchange, symbol) tuples
        """
        instruments = []

        for exchange_dir in self._root.iterdir():
            if exchange_dir.is_dir():
                for symbol_dir in exchange_dir.iterdir():
                    if symbol_dir.is_dir():
                        instruments.append((exchange_dir.name, symbol_dir.name))

        return instruments

    # -- v3-compatible flat-file interface ------------------------------------

    def _path(self, symbol: str) -> Path:
        """Return the JSONL path for a flat symbol store."""
        return self._root / f"{_safe_symbol(symbol)}.jsonl"

    def write_bars(self, symbol: str, bars: list[dict[str, Any]]) -> None:
        """Upsert bars by ISO timestamp; sorted, single-file rewrite."""
        index = {row["timestamp"]: row for row in self._read_all(symbol)}
        for bar in bars:
            index[bar["timestamp"]] = bar
        rows = sorted(index.values(), key=lambda row: row["timestamp"])
        with self._path(symbol).open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")

    def query_bars(
        self,
        symbol: str,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
    ) -> list[dict[str, Any]]:
        """JSON-safe rows (ISO timestamps) for query surfaces."""
        s = _parse_ts(start) if isinstance(start, str) else start
        e = _parse_ts(end) if isinstance(end, str) else end
        rows = self._read_bars_parsed(symbol, start=s, end=e)
        return [
            {
                **{k: v for k, v in row.items() if k != "timestamp"},
                "timestamp": row["timestamp"].isoformat(),
            }
            for row in rows
        ]

    def has_bars(self, symbol: str) -> bool:
        """Return True when the flat symbol file exists and is non-empty."""
        path = self._path(symbol)
        return path.exists() and path.stat().st_size > 0

    def list_tables(self) -> list[str]:
        """Return sorted symbol names from flat JSONL files."""
        return sorted(path.stem for path in self._root.glob("*.jsonl"))

    def get_schema(self) -> list[str]:
        """Return the canonical bar column names."""
        return ["timestamp", "open", "high", "low", "close", "volume"]

    # -- survivorship-bias protection -----------------------------------------

    def _delisted_path(self) -> Path:
        """Return the path to the delisting ledger."""
        return self._root / "_delisted.json"

    def _load_delisted(self) -> dict[str, dict[str, str]]:
        """Load the delisting ledger. Returns {symbol: {delisted_on, reason}}."""
        path = self._delisted_path()
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save_delisted(self, ledger: dict[str, dict[str, str]]) -> None:
        """Persist the delisting ledger."""
        path = self._delisted_path()
        with path.open("w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2, sort_keys=True)

    def mark_delisted(
        self,
        symbol: str,
        delisted_on: str,
        reason: str = "",
    ) -> None:
        """Mark a symbol as delisted on the given date.

        Historical bars remain readable — the ledger lets backtests
        reconstruct past universes without survivorship bias.
        """
        ledger = self._load_delisted()
        ledger[_safe_symbol(symbol)] = {
            "delisted_on": delisted_on,
            "reason": reason,
        }
        self._save_delisted(ledger)

    def is_delisted(self, symbol: str) -> bool:
        """Return True if the symbol has been marked as delisted."""
        return _safe_symbol(symbol) in self._load_delisted()

    def delisted_instruments(self) -> dict[str, dict[str, str]]:
        """Return the full delisting ledger {symbol: {delisted_on, reason}}."""
        return self._load_delisted()

    def active_symbols(self) -> list[str]:
        """Return symbols with bars that have NOT been delisted."""
        delisted = set(self._load_delisted())
        return [s for s in self.list_tables() if s not in delisted]

    # -- internals -----------------------------------------------------------

    def _read_all(self, symbol: str) -> list[dict[str, Any]]:
        """Read all raw JSON dicts from the flat symbol file."""
        path = self._path(symbol)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _read_bars_parsed(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Read bars with optional datetime filtering, parsing timestamps."""
        out: list[dict[str, Any]] = []
        for row in self._read_all(symbol):
            ts = _parse_ts(row["timestamp"])
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            row = dict(row)
            row["timestamp"] = ts
            out.append(row)
        return out


__all__ = ["DataCatalog"]
