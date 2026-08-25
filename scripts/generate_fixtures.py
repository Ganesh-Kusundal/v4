"""Deterministic fixture candles shared by the TS golden harness and pytest."""
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
OUT = Path(__file__).resolve().parents[1] / "trading" / "tests" / "analytics" / "goldens" / "fixtures.json"


def main() -> None:
    rng = random.Random(42)
    t0_day1 = int(datetime(2026, 7, 15, 9, 15, tzinfo=IST).timestamp())
    t0_day2 = int(datetime(2026, 7, 16, 9, 15, tzinfo=IST).timestamp())

    price = 100.0
    bars = []
    for i in range(300):
        ts = t0_day1 + i * 60 if i < 240 else t0_day2 + (i - 240) * 60
        shock = rng.gauss(0, 0.6)
        if i < 80:        # steady uptrend
            step = 0.15 + math.sin(i / 15.0) * 0.2
        elif i < 140:     # chop
            step = shock
        elif i < 170:     # flat run
            step = 0.0
        else:             # downtrend
            step = -0.12 + shock * 0.4
        close = max(1.0, price + step)
        o = price
        h = max(o, close) + abs(rng.gauss(0, 0.3))
        low = min(o, close) - abs(rng.gauss(0, 0.3))
        vol = 0.0 if i in (50, 51, 200) else float(rng.randint(1000, 5000))
        bars.append([ts, round(o, 4), round(h, 4), round(low, 4), round(close, 4), vol])
        price = close

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"bars": bars}))
    print(f"wrote {len(bars)} bars to {OUT}")


if __name__ == "__main__":
    main()
