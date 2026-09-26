"""User scanner definitions — import each scanner module here.

Objects listed in ``__all__`` are validated as ``ScannerDefinition`` instances
by ``extensions/__init__.py``. Add a new scanner by dropping a module in this
package and importing it below.
"""

from tradex_strategy.extensions.scanners.momentum import (
    momentum_scanner,
)
from tradex_strategy.extensions.scanners.nifty500_technical import (
    nifty500_technical_scanner,
)
from tradex_strategy.extensions.scanners.pullback import pullback_scanner


__all__ = ["momentum_scanner", "nifty500_technical_scanner", "pullback_scanner"]
