"""Named ``strftime``/``strptime`` datetime format contracts.

These are **storage and wire contracts**, not display preferences.  Every value
here is baked into a broker request body, a persisted column, or a file name
that already exists on disk.  A format string is never "simplified", "tidied",
or unified with a neighbouring one: a single changed character silently
reinterprets every date already written under the old reading, and the failure
is invisible until someone compares a stored expiry against a settlement.

Two dashed families live here, and they are genuinely different contracts:

``DASHED_DATETIME``
    ``"%Y-%m-%d %H:%M:%S"`` — a space-separated date-time.  Used by the Dhan
    intraday ``fromDate``/``toDate`` request fields and by the Dhan instrument
    master's ``SEM_EXPIRY_DATE`` column.  Also the common-space variant used in
    provider fallback parsing.

``DASHED_DATE``
    ``"%Y-%m-%d"`` — a bare ISO calendar date with no time component.  Used by
    the CLI (``--start``/``--end``), the Dhan historical chart range, and the
    datalake partition paths.

Deliberately NOT in this module
-------------------------------
The compact ``"%Y%m%d"`` instrument-key expiry format is a *separate* contract
and lives as ``INSTRUMENT_KEY_EXPIRY_FORMAT`` in
:mod:`tradex_domain.value_objects`.  It is the expiry segment of provider keys
(NSE_OPT|NIFTY:20260130:20000:CE) and has nothing to do with either dashed
form.  Do not merge the two, and do not move that constant here: the value is
frozen because external clients already hold the compact spelling.

Likewise the tolerant-parsing *fallback ladders* in broker adapters are not
lists of duplicates to be collapsed.  They are ordered lists of independently
valid wire shapes accepted from untrusted providers, and the ordering carries
meaning — an entry must only be removed if the provider is proven never to
emit that shape.

This module is deliberately stdlib-only (constants and a docstring); it pulls
in no third-party dependency and no sibling package.
"""

from __future__ import annotations

#: ``"%Y-%m-%d %H:%M:%S"`` — space-separated date-time wire format.
#: Dhan intraday chart ``fromDate``/``toDate``; the Dhan instrument-master
#: ``SEM_EXPIRY_DATE`` column; the dashed date-time slot in broker fallback
#: parsing ladders.
DASHED_DATETIME = "%Y-%m-%d %H:%M:%S"

#: ``"%Y-%m-%d"`` — bare ISO calendar date.
#: CLI ``--start``/``--end`` arguments, Dhan historical chart ranges, datalake
#: partition paths, and the date-only slot in broker fallback parsing ladders.
DASHED_DATE = "%Y-%m-%d"

__all__ = [
    "DASHED_DATE",
    "DASHED_DATETIME",
]
