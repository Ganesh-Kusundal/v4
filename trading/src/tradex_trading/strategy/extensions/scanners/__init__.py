"""User scanner definitions — import each scanner module here.

Objects listed in ``__all__`` are validated as ``ScannerDefinition`` instances
by ``extensions/__init__.py``. Add a new scanner by dropping a module in this
package and importing it below.
"""

from tradex_trading.strategy.extensions.scanners.momentum import (
    momentum_scanner,
)

__all__ = ["momentum_scanner"]
