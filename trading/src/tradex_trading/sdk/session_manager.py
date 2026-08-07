"""Session manager — registry of multiple trading sessions.

Supports multi-account and multi-broker workflows.
"""

from __future__ import annotations

from collections.abc import Iterator

from tradex_domain.execution import PortfolioSnapshot, Position

from tradex_trading.sdk.session import TradingSession


class SessionManager:
    """Registry of multiple trading sessions.

    Manages multiple sessions for multi-account or multi-broker workflows.

    Example
    -------
    ```python
    manager = SessionManager()
    manager.add("dhan", session_dhan)
    manager.add("upstox", session_upstox)

    # Aggregate positions across all sessions
    all_positions = manager.all_positions()

    # Access specific session
    dhan = manager.get("dhan")
    ```
    """

    def __init__(self) -> None:
        """Initialize empty session manager."""
        self._sessions: dict[str, TradingSession] = {}
        self._active: str | None = None

    def add(self, name: str, session: TradingSession) -> None:
        """Register a session.

        Parameters
        ----------
        name : str
            Session name (e.g., "dhan", "paper").
        session : TradingSession
            The trading session to register.
        """
        self._sessions[name] = session
        if self._active is None:
            self._active = name

    def remove(self, name: str) -> TradingSession | None:
        """Remove a session.

        Parameters
        ----------
        name : str
            Session name.

        Returns
        -------
        TradingSession | None
            The removed session, or None if not found.
        """
        session = self._sessions.pop(name, None)
        if session is not None and self._active == name:
            self._active = next(iter(self._sessions), None)
        return session

    def get(self, name: str) -> TradingSession:
        """Get a session by name.

        Parameters
        ----------
        name : str
            Session name.

        Returns
        -------
        TradingSession
            The requested session.

        Raises
        ------
        KeyError
            If session not found.
        """
        if name not in self._sessions:
            raise KeyError(f"Session {name!r} not registered. Available: {list(self._sessions)}")
        return self._sessions[name]

    @property
    def active(self) -> TradingSession:
        """Get the currently active session.

        Returns
        -------
        TradingSession
            The active session.

        Raises
        ------
        ValueError
            If no sessions are registered.
        """
        if self._active is None or self._active not in self._sessions:
            raise ValueError("No active session. Add a session first.")
        return self._sessions[self._active]

    def set_active(self, name: str) -> None:
        """Set the active session.

        Parameters
        ----------
        name : str
            Session name.
        """
        if name not in self._sessions:
            raise KeyError(f"Session {name!r} not registered")
        self._active = name

    @property
    def names(self) -> list[str]:
        """List all registered session names."""
        return list(self._sessions.keys())

    def __len__(self) -> int:
        return len(self._sessions)

    def __iter__(self) -> Iterator[str]:
        return iter(self._sessions)

    def all_positions(self) -> list[Position]:
        """Aggregate positions across all sessions.

        Returns
        -------
        list[Position]
            All positions from all sessions.
        """
        positions: list[Position] = []
        for session in self._sessions.values():
            try:
                positions.extend(session.portfolio.positions())
            except Exception:
                continue
        return positions

    def all_portfolios(self) -> dict[str, PortfolioSnapshot]:
        """Get portfolio snapshots from all sessions.

        Returns
        -------
        dict[str, PortfolioSnapshot]
            Session name → portfolio snapshot mapping.
        """
        result: dict[str, PortfolioSnapshot] = {}
        for name, session in self._sessions.items():
            try:
                result[name] = session.portfolio.portfolio()
            except Exception:
                continue
        return result

    def close_all(self) -> None:
        """Close all sessions."""
        for session in self._sessions.values():
            try:
                session.stop()
            except Exception:
                continue
        self._sessions.clear()
        self._active = None


__all__ = ["SessionManager"]
