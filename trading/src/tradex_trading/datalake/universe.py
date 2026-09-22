"""Universe — load Nifty index constituent CSVs → Equity instruments.

CSV columns: Company Name, Industry, Symbol, Series, ISIN Code
Maps the ``Symbol`` column to :class:`Equity`, attaching CSV ISIN/series on
``meta`` for connect-time resolve.
"""

from __future__ import annotations

import csv
from pathlib import Path

from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity, InstrumentMeta
from tradex_domain.value_objects import InstrumentId

_NSE = "NSE"

# ponytail: repo root is 5 levels up from this file
_DEFAULT_CSV_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "Dependencies"

# EQ = normal delivery, BE = trade-to-trade (both cash segment)
_CASH_SERIES = frozenset({"EQ", "BE"})


def _load_csv(path: Path) -> list[dict[str, str]]:
    """Read a Nifty constituent CSV into a list of row dicts."""
    rows: list[dict[str, str]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)
    return rows


def load_universe(
    name: str = "nifty50",
    csv_dir: Path | str | None = None,
) -> list[Equity]:
    """Load a Nifty index constituents CSV and return Equity instruments.

    ``meta.isin`` / ``meta.extra['series']`` come from the CSV when present.
    """
    csv_dir = Path(csv_dir) if csv_dir else _DEFAULT_CSV_DIR
    csv_path = csv_dir / f"{name}_list.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Universe CSV not found: {csv_path}")

    instruments: list[Equity] = []
    for row in _load_csv(csv_path):
        symbol = row["Symbol"].strip().upper()
        series = row.get("Series", "").strip().upper()
        if series not in _CASH_SERIES:
            continue
        isin = (row.get("ISIN Code") or row.get("ISIN") or "").strip().upper() or None
        instruments.append(Equity(
            instrument_id=InstrumentId.equity(_NSE, symbol),
            symbol=symbol,
            exchange=ExchangeId(_NSE),
            meta=InstrumentMeta(isin=isin, extra={"series": series} if series else {}),
        ))
    return instruments


def available_universes(csv_dir: Path | str | None = None) -> list[str]:
    """Return the names of all universe CSVs available on disk."""
    csv_dir = Path(csv_dir) if csv_dir else _DEFAULT_CSV_DIR
    return sorted(
        p.stem.removesuffix("_list")
        for p in csv_dir.glob("nifty*_list.csv")
    )


__all__ = ["load_universe", "available_universes"]
