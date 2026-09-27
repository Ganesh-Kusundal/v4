"""Composition façade for :class:`TradingSession`.

The implementation lives in :mod:`tradex_runtime.session_impl`; callers
outside the composition root should import from here.
"""

from tradex_runtime.session_impl import SessionState, TradingSession

__all__ = ["SessionState", "TradingSession"]
