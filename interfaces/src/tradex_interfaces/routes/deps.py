"""Shared FastAPI dependencies for the route modules."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from tradex_domain.execution import Account, PortfolioSnapshot, Position


def get_session(request: Request) -> Any | None:
    """Return ``app.state.session`` for the current request.

    ``None`` when the app is bound without a TradingSession (e.g. test
    scaffolding). Route handlers that need a session should treat
    ``None`` as "no data" rather than raising.
    """
    return request.app.state.session


def _empty_portfolio() -> PortfolioSnapshot:
    """The snapshot returned when nothing can be read at all."""
    return PortfolioSnapshot(positions=[], account=None)


def _portfolio_from_cache(positions: Any, account: Any) -> PortfolioSnapshot:
    """Build a snapshot from the OMS book plus the broker account."""
    account_snapshot: Account | None
    if isinstance(account, Account):
        account_snapshot = account
    elif account is None:
        account_snapshot = None
    else:
        # A duck-typed account object (a MagicMock in the route tests, or an
        # adapter that returns its own type) is still readable through its
        # Money-typed fields. Anything unreadable degrades to "no account
        # section" rather than 500ing a position read.
        try:
            account_snapshot = Account(
                account_id=getattr(account, "account_id", None),
                balance=getattr(account, "balance", None),
                margin=getattr(account, "margin", None),
                equity=getattr(account, "equity", None),
            )
        except Exception:
            account_snapshot = None
    try:
        position_list: list[Position] = list(positions)
    except TypeError:
        position_list = []
    return PortfolioSnapshot(positions=position_list, account=account_snapshot)


def get_session_portfolio(session: Any | None) -> PortfolioSnapshot:
    """The live portfolio the portfolio routes must serve.

    **Source of truth.** The book the running session actually holds is the
    OMS cache, reached at ``session.engine.cache.all_positions()``
    (``execution/src/tradex_execution/trading_cache.py:107``). That dict is
    the one object every component that books a position writes to: the
    engine's fill path, the live fill bridge, the quote-driven
    ``MarkToMarketService`` (which re-writes each row's mark and unrealized
    P&L in place under the cache's per-instrument lock) and event-store
    recovery all write here and nowhere else. ``session.broker.get_positions()``
    is *not* that book: for paper it is the broker's own parallel dict, which
    the session's fill source never writes (``position_projection_owned is
    False`` for the paper fill path, ``execution/src/tradex_execution/fill_sources.py:206``),
    and for live it is a REST snapshot that goes stale between reads.

    This is the wiring that was missing. The routes read
    ``session.engine.cache.all_positions()`` directly, which is correct for
    the position rows but gives the caller no account section at all, so a
    UI that asked "what do I hold, and what is it worth" got the holdings
    and nothing to value them against. The session-scoped accessor this
    replaces was never consumed by anything.

    The account half comes from the broker (the only object that knows the
    balance), so one read gives the caller the book *and* the money. Both
    halves degrade independently and by intent: a broker whose account call
    raises still serves the live book, because refusing a position read over
    a funds-call failure reports a working book as empty. ``None`` session
    means no snapshot rather than an empty one, and the caller decides
    whether that is ``[]`` or an error.
    """
    if session is None:
        return _empty_portfolio()
    cache = getattr(getattr(session, "engine", None), "cache", None)
    if cache is None or not hasattr(cache, "all_positions"):
        return _empty_portfolio()
    try:
        positions = cache.all_positions()
    except Exception:
        return _empty_portfolio()
    broker = getattr(session, "broker", None)
    get_account = getattr(broker, "get_account", None)
    if not callable(get_account):
        return _portfolio_from_cache(positions, None)
    try:
        account = get_account()
    except Exception:
        account = None
    return _portfolio_from_cache(positions, account)


__all__ = ["get_session", "get_session_portfolio"]
