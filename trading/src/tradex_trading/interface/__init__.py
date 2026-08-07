"""TradeX v4 interface package.

Provides CLI, HTTP API, TUI, and connectivity probe.

The HTTP API lives in ``tradex_trading.interface.fastapi_app`` (FastAPI +
uvicorn) and is intentionally *not* imported here so the base package stays
importable without the optional ``api`` extra.  It is reached lazily by the
``tradex serve`` CLI command.
"""

from tradex_trading.interface.check_connection import check_connection
from tradex_trading.interface.cli import main as cli_main
from tradex_trading.interface.tui import TUI

__all__ = [
    "TUI",
    "check_connection",
    "cli_main",
]
