"""Golden parity: every series transform must match the actual openalgo-charts
TypeScript implementation exactly (1e-9, None-aligned)."""
import json
from pathlib import Path

import pytest

from tradex_analytics.transforms import compute_transform

GOLDENS = Path(__file__).parent / "goldens" / "transforms"

FIXTURES = json.loads(
    (Path(__file__).parent / "goldens" / "fixtures.json").read_text()
)
BARS = [
    {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": v}
    for t, o, h, low, c, v in FIXTURES["bars"]
]

# transform id -> {TS setting key: backend param name}
PARAM_MAP = {
    "heikin-ashi": {},
    "renko": {"boxSize": "box_size"},
    "range-bars": {"range": "range"},
    "line-break": {"lines": "lines"},
    "point-figure": {"boxSize": "box_size", "reversal": "reversal"},
    "kagi": {"reversal": "reversal"},
}


def _load_golden(tid: str) -> dict:
    return json.loads((GOLDENS / f"{tid}.json").read_text())


@pytest.mark.parametrize("tid", sorted(_load_golden(f.stem)["id"] for f in GOLDENS.glob("*.json")))
def test_transform_matches_ts_golden(tid: str) -> None:
    golden = _load_golden(tid)
    params = {py: golden["settings"][ts] for ts, py in PARAM_MAP[tid].items()}
    got = compute_transform(tid, BARS, params)
    exp = golden["bars"]

    assert len(got) == len(exp), f"{tid}: bar count {len(got)} != {len(exp)}"
    for i, (g, e) in enumerate(zip(got, exp)):
        assert g["time"] == e[0], f"{tid}[{i}]: time {g['time']} != {e[0]}"
        for key, idx in (("open", 1), ("high", 2), ("low", 3), ("close", 4)):
            assert g[key] == pytest.approx(e[idx], abs=1e-9), f"{tid}[{i}].{key}"
        # volume: both may be 0/absent for kagi; compare when golden carries it
        gv = g.get("volume", 0)
        ev = e[5] if len(e) > 5 else 0
        assert gv == pytest.approx(ev, abs=1e-9), f"{tid}[{i}].volume"


def test_unknown_transform_id_fails_loudly() -> None:
    with pytest.raises(ValueError):
        compute_transform("no-such-transform", BARS, {})
