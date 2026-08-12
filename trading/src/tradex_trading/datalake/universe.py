"""Universe — load Nifty index constituent CSVs → Equity instruments.

CSV columns: Company Name, Industry, Symbol, Series, ISIN Code
Maps the ``Symbol`` column to :class:`Equity` via ``Equity.of("NSE", symbol)``.

Adapted from nTrade's universe loader.
"""

from __future__ import annotations

import csv
from pathlib import Path

from tradex_domain.instruments import Equity

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

    Args:
        name: ``nifty50``, ``nifty100``, ``nifty200``, or ``nifty500``.
        csv_dir: Directory containing the CSV files
                 (default: ``<repo>/Dependencies``).

    Returns:
        A list of :class:`Equity` instruments, one per constituent.
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
        instruments.append(Equity.of(_NSE, symbol))
    return instruments


def available_universes(csv_dir: Path | str | None = None) -> list[str]:
    """Return the names of all universe CSVs available on disk."""
    csv_dir = Path(csv_dir) if csv_dir else _DEFAULT_CSV_DIR
    return sorted(
        p.stem.removesuffix("_list")
        for p in csv_dir.glob("nifty*_list.csv")
    )


__all__ = ["load_universe", "available_universes"]
