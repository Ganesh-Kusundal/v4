"""TradeX v4 interface package.

Provides CLI and HTTP API.

The HTTP API lives in ``tradex_interfaces.fastapi_app`` (FastAPI +
uvicorn) and is intentionally *not* imported here so the base package stays
importable without the optional ``api`` extra.  It is reached lazily by the
``tradex serve`` CLI command.
"""

from tradex_interfaces.cli import main as cli_main

__all__ = [
    "cli_main",
]
