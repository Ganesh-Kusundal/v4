"""Single-source Indian market timezone, in every representation callers need.

``Asia/Kolkata`` was re-derived per package as an IANA string, a ``ZoneInfo``
and a bare fixed offset, so the same zone reached analytics, brokers and the
HTTP layer in three incompatible forms. This module owns the one copy.

Pick the representation by what the call site actually needs:

* ``IST_ZONE`` — the default. Use it for every date/time conversion. It is the
  only form carrying zone semantics, and India has no DST, so it is
  numerically identical to the fixed offset for every date this platform sees.
* ``IST_OFFSET_SECONDS`` — only for integer wall-clock arithmetic that never
  builds a ``tzinfo`` (second-of-day, day index, bucket alignment).
* ``IST_NAME`` — only where a *name* must survive (a configurable-timezone
  parameter fed to ``ZoneInfo(zone)``, a settings/JSON round-trip, a wire
  payload). Passing a name keeps that feature intact; passing ``IST_ZONE``
  there would not.

Ponytail: one file, stdlib only, no broker/trading import.

Note: ``market_calendar.IST`` is an older fixed-offset ``datetime.timezone``
copy of the same zone. It stays as-is for its existing importers; collapsing
it onto ``IST_ZONE`` is a separate follow-up because it would touch
``market_calendar.py`` and ``market.py``.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

#: IANA key for Indian Standard Time.
IST_NAME: str = "Asia/Kolkata"

#: Live IST zone for conversions (``astimezone``, ``fromtimestamp``,
#: ``replace(tzinfo=...)``).
IST_ZONE: ZoneInfo = ZoneInfo(IST_NAME)

#: Fixed +05:30 offset in seconds, for integer-only wall-clock math.
IST_OFFSET_SECONDS: int = 5 * 3600 + 30 * 60

__all__ = [
    "IST_NAME",
    "IST_OFFSET_SECONDS",
    "IST_ZONE",
]
