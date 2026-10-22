"""Root test bootstrap.

`shared` is not a workspace member yet; every other package is installed
editable by `uv sync`. Task 3 folds `shared` into `tradex_domain` and removes
this block entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

_V4 = Path(__file__).resolve().parent

for _src in ("shared/src",):
    _path = str(_V4 / _src)
    if _path not in sys.path:
        sys.path.insert(0, _path)
