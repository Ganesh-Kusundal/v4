"""The built-in catalogue must not move when its specs change files."""

import json
import subprocess
import sys
from pathlib import Path

from tradex_analytics.indicators import indicator_catalogue

SNAPSHOT = Path(__file__).parent / "data" / "indicator_catalogue.json"


def test_indicator_catalogue_matches_snapshot():
    live = json.loads(json.dumps(indicator_catalogue(), default=list))
    assert live == json.loads(SNAPSHOT.read_text())


def test_indicator_specs_importable_first():
    code = "import tradex_analytics.indicator_specs, tradex_analytics.indicators"
    subprocess.run([sys.executable, "-c", code], check=True)
