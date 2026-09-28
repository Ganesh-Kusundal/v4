"""Instrument-master lifecycle vocabulary: what a security's status *means*.

Lane 3d (AC-12) needs one question answered with a canonical vocabulary: is
this symbol halted or delisted, so that a security which legitimately produces
no ticks can be exempted from the per-symbol cadence check? Three sites have to
agree on the answer and none of them owns the meaning:

* ``tradex_brokers.dhan`` / ``tradex_brokers.upstox`` read the vendor column
  out of a downloaded master (``SEM_STATUS``, ``status``),
* ``tradex_brokers.common.base`` puts it on the instrument registry,
* ``tradex_runtime.feed_monitor`` consumes it to decide the exemption.

So it lives here, next to the other one-definitions (``market_calendar``,
``timeframe``), and the layers import *downward*: domain ← brokers ← trading.
A private copy per layer would be three vocabularies that drift, and a drift
here is silent in the worst direction — one layer's "halted" spelling stops
matching another's, the exemption stops applying, and every halted stock on the
tape pages the operator forever until they learn to ignore the page.

The asymmetry that matters: **unrecognised is not exempt**. A status this table
does not know is a reason to keep watching a symbol, not a reason to stop. The
two exempt states are the only ones that are known to go quiet by design, and
guessing at a third word would be a blind spot wearing a permission's name.
"""

from __future__ import annotations

from typing import Any

#: The canonical states that exempt a symbol from the cadence contract. Every
#: spelling below collapses onto one of these two.
EXEMPT_STATES: frozenset[str] = frozenset({"halted", "delisted"})

#: Vendor spellings → canonical state. Vendor masters use the loose vocabulary
#: ("HALT", "SUSPENDED", "INACTIVE"); the rest of the platform uses the two
#: canonical words. Matched case-insensitively after stripping.
_STATUS_ALIASES: dict[str, str] = {
    "halt": "halted",
    "halted": "halted",
    "suspend": "halted",
    "suspended": "halted",
    "suspension": "halted",
    "paused": "halted",
    "delist": "delisted",
    "delisted": "delisted",
    "removed": "delisted",
    "inactive": "delisted",
    "inactive_after": "delisted",
}

#: The master column each vendor publishes the lifecycle state in. Both parsers
#: read their own through :func:`row_status`, so a vendor's column name is
#: stated exactly once, here, instead of in each parser.
STATUS_COLUMNS: dict[str, tuple[str, ...]] = {
    "dhan": ("SEM_STATUS",),
    "upstox": ("status",),
}

#: The key a *normalized* row carries the canonical state under, so a row that
#: has already been through a parser needs no vendor column name to be re-read.
NORMALIZED_KEY = "master_status"


def normalize_status(value: object) -> str | None:
    """Canonical lifecycle state for one status cell, or ``None``.

    ``None`` means "nothing usable was said", and therefore *not* exempt. A
    blank cell, ``"active"`` and ``"something new"`` all land here: the master
    has not claimed this security is quiet, so the symbol stays under the
    cadence contract.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    return _STATUS_ALIASES.get(text)


def is_exempt(value: object) -> bool:
    """Whether one status cell exempts its symbol from the cadence contract."""
    return normalize_status(value) in EXEMPT_STATES


def row_status(row: Any) -> str | None:
    """Canonical lifecycle state carried by a master *row*, or ``None``.

    Reads :data:`NORMALIZED_KEY` first (a row a parser already normalized), then
    every vendor column in :data:`STATUS_COLUMNS`, so callers can hand this
    either a raw CSV/JSON mapping or a normalized row without knowing which.
    """
    if not isinstance(row, dict):
        return None
    if NORMALIZED_KEY in row:
        return normalize_status(row[NORMALIZED_KEY])
    for columns in STATUS_COLUMNS.values():
        for column in columns:
            if column in row:
                return normalize_status(row[column])
    return None


def row_exempt(row: Any) -> bool:
    """Whether a master row's lifecycle state exempts its symbol."""
    return row_status(row) in EXEMPT_STATES


__all__ = [
    "EXEMPT_STATES",
    "NORMALIZED_KEY",
    "STATUS_COLUMNS",
    "is_exempt",
    "normalize_status",
    "row_exempt",
    "row_status",
]
