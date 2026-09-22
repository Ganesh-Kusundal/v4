"""Backtest the app's volume-profile execution layer across lake sessions.

The app trades each 09:45-scanner pick against its OWN 09:15→09:50 volume
profile (VAH/VAL/POC + HVN/LVN). This script runs that exact simulator
(``_simulate_vp`` imported from the Streamlit app, so there is one
implementation, not two) over many sessions and reports what a real account
would have done — net of trading costs.

Usage:
    .venv/bin/python poc/research/vp_strategy_backtest.py [sessions] [tf]

Outputs a per-day CSV to /tmp/vp_backtest.csv plus a summary on stdout.
"""

from __future__ import annotations

import importlib.util
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

CAP = 10_000_000.0  # ₹1 cr
K = 3               # scanner picks per day


def _load_app():
    """Import apps/top_gainers/app.py as a module (it is a Streamlit script)."""
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "appmod", root / "apps" / "top_gainers" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _log(msg: str, t0: float) -> None:
    print(f"[{time.time() - t0:7.1f}s] {msg}", flush=True)


def detail(m, d, picks, tf: str, target: str, t0: float) -> None:
    """Print one day's trades and profiles, to eyeball the rules."""
    _log(f"detail: {d} picks={picks}", t0)
    for s in picks:
        prof = m._vp_profile(s, d)
        if prof is None:
            _log(f"  {s}: no pre-window profile", t0)
            continue
        lv = m._vp_levels(prof, m.VP_BUF_PCT, target)
        _log(
            f"  {s}: VAL {lv['val']:.2f} · POC {lv['poc']:.2f} · VAH {lv['vah']:.2f} "
            f"· buf {lv['buf']:.2f} · HVN up {lv['up']} dn {lv['dn']}",
            t0,
        )
    tr, fl, eq, profs = m._simulate_vp(
        picks, d, tf, m.VP_BUF_PCT, m.VP_CONFIRM, target, CAP, 2.0,
        cost_bps=5.0,
    )
    _log(f"  regimes: { {k: v['regime'] for k, v in profs.items()} }", t0)
    if tr.empty:
        _log("  no round-trips", t0)
        return
    print(
        tr[["symbol", "setup", "side", "entry_time", "entry", "stop", "target",
            "exit_time", "exit", "qty", "pnl", "cost", "exit_reason"]].to_string()
    )
    _log(
        f"  net {tr.pnl.sum():,.0f} · cost {tr.cost.sum():,.0f} · "
        f"identity_ok={abs(eq.equity.iloc[-1] - (CAP + tr.pnl.sum())) < 1.0}",
        t0,
    )


def main() -> int:
    t0 = time.time()
    n_days = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    tf = sys.argv[2] if len(sys.argv) > 2 else "5m"
    m = _load_app()
    _log("app loaded", t0)

    days = m.lake_dates()[:n_days]
    _log(f"{len(days)} sessions, tf={tf}, k={K}, capital ₹{CAP:,.0f}", t0)

    picks_by_day: dict = {}
    for d in days:
        scan = m.scanner_0945(d)
        if scan.empty:
            continue
        picks_by_day[d] = scan.nlargest(K, "score_movers").symbol.tolist()
    _log(f"picks built for {len(picks_by_day)} sessions", t0)

    detail(m, days[0], picks_by_day.get(days[0], []), tf, m.VP_TARGET_VA, t0)

    # Sweep: the three target modes × cost levels, plus an all-setups-off
    # (opening entry only) arm to isolate what each rule contributes.
    arms = {
        "all · VA target": dict(target=m.VP_TARGET_VA),
        "all · HVN target": dict(target=m.VP_TARGET_HVN),
        "all · EOD only": dict(target=m.VP_TARGET_EOD),
        "opening only": dict(target=m.VP_TARGET_VA, fade=False, brk=False, rentry=False),
        "fade only": dict(target=m.VP_TARGET_VA, open_=False, brk=False, rentry=False),
        "break only": dict(target=m.VP_TARGET_HVN, open_=False, fade=False, rentry=False),
        "re-entry only": dict(target=m.VP_TARGET_VA, open_=False, fade=False, brk=False),
    }
    rows: list[dict] = []
    for arm, cfg in arms.items():
        for cost in (0.0, 5.0):
            tot = 0.0
            to = 0.0
            n = 0
            wins = 0
            per_day: list[float] = []
            for d, picks in picks_by_day.items():
                tr, _fl, _eq, _pr = m._simulate_vp(
                    picks, d, tf, m.VP_BUF_PCT, m.VP_CONFIRM, cfg["target"],
                    CAP, 2.0,
                    allow_fade=cfg.get("fade", True),
                    allow_break=cfg.get("brk", True),
                    allow_reentry=cfg.get("rentry", True),
                    open_entry=cfg.get("open_", True),
                    cost_bps=cost,
                )
                if tr.empty:
                    per_day.append(0.0)
                    continue
                tot += float(tr.pnl.sum())
                to += float((tr.entry * tr.qty).sum())
                n += len(tr)
                wins += int((tr.pnl > 0).sum())
                per_day.append(float(tr.pnl.sum()))
            arr = np.asarray(per_day)
            rows.append({
                "arm": arm, "cost_bps": cost, "trades": n,
                "win_pct": 100.0 * wins / max(n, 1),
                "net_pnl": tot, "ret_pct": 100.0 * tot / CAP,
                "ex_best_pct": 100.0 * (tot - arr.max()) / CAP if len(arr) else 0.0,
                "median_day": float(np.median(arr)) if len(arr) else 0.0,
                "days_pos_pct": 100.0 * float((arr > 0).mean()) if len(arr) else 0.0,
                "turnover_cr": to / 1e7,
            })
            _log(f"{arm:18s} cost={cost:>4.0f} → net {100.0 * tot / CAP:+7.2f}%", t0)

    res = pd.DataFrame(rows)
    res.to_csv("/tmp/vp_backtest.csv", index=False)
    print("\n=== volume-profile strategy, "
          f"{len(picks_by_day)} sessions, k={K}, {tf}, risk 2%, 4.75× ===")
    print(res.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    _log("done", t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
