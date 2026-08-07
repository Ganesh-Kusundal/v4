"""Make the v4 namespace packages importable without installation.

v4 is three separate hatchling projects (domain/brokers/trading); the root
venv can't install them (rx>=7.0 dependency conflict). Prepend their src
layouts so the whole suite runs in place with one command:

    pytest domain/tests brokers/tests trading/tests
"""

import sys
from pathlib import Path

_V4 = Path(__file__).resolve().parent

for _src in ("domain/src", "brokers/src", "trading/src", "brokers/tests"):
    _path = str(_V4 / _src)
    if _path not in sys.path:
        sys.path.insert(0, _path)
