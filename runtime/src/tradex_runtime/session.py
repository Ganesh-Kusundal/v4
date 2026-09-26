"""Composition façade for :class:`TradingSession`.

``TradingSession`` is still implemented under ``tradex_trading.sdk``; callers
outside the composition root should import from here instead of the shim path.
"""

from tradex_trading.sdk.session import SessionState, TradingSession

__all__ = ["SessionState", "TradingSession"]
