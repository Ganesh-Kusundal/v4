"""Universe — load Nifty index constituent CSVs → Equity instruments.

CSV columns: Company Name, Industry, Symbol, Series, ISIN Code
Maps the ``Symbol`` column to :class:`Equity`, attaching CSV ISIN/series on
``meta`` for connect-time resolve.

Why this lives in ``tradex_domain`` and not ``tradex_market_data``
------------------------------------------------------------------
Loading a universe is a *pure domain* concern: it reads a checked-in CSV and
maps rows onto domain value objects. It touches no datalake, no parquet, no
broker, and no network. It used to live in ``tradex_market_data``, and that
placement was the only thing making ``strategy`` depend on the datalake
package just to get two plain functions::

    strategy.scanners.nifty500_technical -> tradex_market_data.universe

which closed the ring ``market_data -> replay -> strategy -> market_data``.
Extracting the module to domain removes the edge entirely, so
``tradex_market_data.universe`` survives only as a re-export shim for
existing importers. This is the same reason ``NSETradingCalendar`` moved.

Do not move it back: any package that reaches across into
``tradex_market_data`` for a pure function re-opens the cycle, and
``tests/test_import_boundaries.py::test_no_unknown_import_cycles_between_packages``
will fail. Import from ``tradex_domain.universe`` instead.
"""

from __future__ import annotations

import csv
from pathlib import Path

from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity, InstrumentMeta
from tradex_domain.value_objects import InstrumentId

_NSE = "NSE"

# ponytail: repo root is 4 levels up from this file
# (tradex_domain → src → domain → repo)
_DEFAULT_CSV_DIR = Path(__file__).resolve().parents[3] / "Dependencies"

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
