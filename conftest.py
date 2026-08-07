"""Make the v4 namespace packages importable in-place for the combined root suite.

v4 is three separate hatchling projects (domain/brokers/trading) that run
without installation. This conftest prepends their src layouts — plus
brokers/tests, for the ``support.fake_fetch`` test helper — so the whole
suite runs in place with one command:

    pytest domain/tests brokers/tests trading/tests

Each package now carries its own ``[tool.pytest.ini_options]`` ``pythonpath``
in its pyproject.toml, so running pytest from inside a package directory
(e.g. ``cd trading && python -m pytest``) resolves without this file; the
conftest exists for the repo-root combined run.
"""

import sys
from pathlib import Path

_V4 = Path(__file__).resolve().parent

for _src in ("domain/src", "brokers/src", "trading/src", "brokers/tests"):
    _path = str(_V4 / _src)
    if _path not in sys.path:
        sys.path.insert(0, _path)
