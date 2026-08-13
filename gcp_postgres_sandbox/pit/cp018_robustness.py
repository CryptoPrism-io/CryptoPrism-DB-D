"""CP-018 promotion-approval — robustness pass for candidate factors (READ-ONLY).

Tests whether the CP-012 candidates (primary: d_pct_cvar h30) survive:

  1. Out-of-sample split       IC in first 70% vs last 30% of dates (+ CI)
  2. Regime stability          IC per calendar bucket and per BTC bull/bear +
                               high/low-vol regime
  3. Quintile spread           mean forward return of the signal's top vs bottom
                               quintile (per date, averaged), and net-of-cost
                               spread vs an assumed round-trip cost

Output: report/cp011-phase-d/cp018_robustness.json. No DB writes; recommendation
only. Promotion into production still requires a separate explicit approval.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pit.cp012 import build_panel, load_frozen_prices  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
SNAPSHOT = ROOT / "report" / "cp011-phase-d" / "frozen_ohlcv_cp011_v2.parquet"
OUT = ROOT / "report" / "cp011-phase-d" / "cp018_robustness.json"

FACTORS = ["d_pct_cvar", "d_pct_var"]
HORIZONS = [7, 30]
MIN_ASSETS = 5
COST_30D = 0.002  # 20 bps assumed round-trip cost per 30d holding


def _per_date_ic(df: pd.DataFrame, factor: str, ret: str) -> pd.Series:
    out = {}
    for date, d in df.groupby("date"):
        v = d[factor].to_numpy(dtype=float)
        r = d[ret].to_numpy(dtype=float)
        m = np.isfinite(v) & np.isfinite(r)
        v = v[m]
        r = r[m]
        if len(v) < MIN_ASSETS or np.std(v) == 0 or np.std(r) == 0:
            continue
        out[date] = float(np.corrcoef(v, r)[0, 1])
    return pd.Series(out)


def _summarize(ic: pd.Series) -> dict:
    if len(ic) == 0:
        return {"n_dates": 0}
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) if len(ic) > 1 else 0.0
    se = std / math.sqrt(len(ic)) if len(ic) > 1 else 0.0
    return {
        "n_dates": int(len(ic)),
        "ic_mean": round(mean, 4),
        "ci95_low": round(mean - 1.96 * se, 4),
        "ci95_high": round(mean + 1.96 * se, 4),
        "pct_positive": round(float((ic > 0).mean()), 3),
    }


def _quintile_spread(df: pd.DataFrame, factor: str, ret: str) -> dict:
    """Top-vs-bottom quintile mean forward return, per date, averaged.

    Signal = higher factor value (worse tail risk) predicts LOWER forward return,
    so the profitable side is LONG bottom-quintile / SHORT top-quintile.
    """
    spreads: list[float] = []
    q_returns: dict[str, list[float]] = {"bottom": [], "top": []}
    for date, d in df.groupby("date"):
        sub = d[[factor, ret]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(sub) < 10:
            continue
        q = sub[factor].quantile([0.2, 0.8])
        lo, hi = q.iloc[0], q.iloc[1]
        if lo == hi:
            continue
        bottom = sub.loc[sub[factor] <= lo, ret].mean()
        top = sub.loc[sub[factor] >= hi, ret].mean()
        spreads.append(bottom - top)
        q_returns["bottom"].append(bottom)
        q_returns["top"].append(top)
    if not spreads:
        return {"n_dates": 0}
    gross = float(np.mean(spreads))
    h = int(ret.rsplit("_", 1)[1][:-1])  # horizon from "return_30d"
    cost = COST_30D * h / 30
    return {
        "n_dates": int(len(spreads)),
        "spread_gross_mean": round(gross, 5),
        "spread_net_of_cost": round(gross - cost, 5),
        "assumed_cost": cost,
        "bottom_quintile_mean_ret": round(float(np.mean(q_returns["bottom"])), 5),
        "top_quintile_mean_ret": round(float(np.mean(q_returns["top"])), 5),
    }


def _regime_breakdown(panel: pd.DataFrame, factor: str, horizon: int) -> dict:
    ret = f"return_{horizon}d"
    ic_daily = _per_date_ic(panel, factor, ret)
    if ic_daily.empty:
        return {"error": "no IC dates"}

    # calendar buckets
    dates = pd.Series(ic_daily.index, index=ic_daily.index)
    thirds = pd.qcut(dates.rank(method="first"), 3, labels=["p1", "p2", "p3"])
    buckets = {}
    for label in ["p1", "p2", "p3"]:
        buckets[f"period_{label}"] = _summarize(ic_daily[thirds == label])

    # BTC regime (bull/bear by 200d high, high/low vol) using btc daily closes
    btc = load_frozen_prices(SNAPSHOT)
    btc = btc[btc["slug"] == "bitcoin"].set_index("date")["close"].sort_index()
    roll_max = btc.rolling(200, min_periods=30).max()
    vol = btc.pct_change().rolling(30, min_periods=10).std()
    bull_mask = ic_daily.index.to_series().map(
        lambda d: bool(np.isfinite(roll_max.get(d, np.nan)) and btc.get(d, np.nan) >= 0.8 * roll_max.get(d)))
    highvol_mask = ic_daily.index.to_series().map(
        lambda d: bool(np.isfinite(vol.get(d, np.nan)) and vol.get(d, np.nan) >= float(vol.median())))
    buckets["bull"] = _summarize(ic_daily[bull_mask])
    buckets["bear"] = _summarize(ic_daily[~bull_mask])
    buckets["high_vol"] = _summarize(ic_daily[highvol_mask])
    buckets["low_vol"] = _summarize(ic_daily[~highvol_mask])
    return buckets


def run(panel: pd.DataFrame) -> dict:
    out = {}
    for factor in FACTORS:
        for horizon in HORIZONS:
            ret = f"return_{horizon}d"
            ic_daily = _per_date_ic(panel, factor, ret)
            if ic_daily.empty:
                continue
            full = _summarize(ic_daily)
            # out-of-sample split on dates
            idx = ic_daily.index.sort_values()
            cut = int(len(idx) * 0.7)
            in_sample = ic_daily[idx[:cut]]
            oos = ic_daily[idx[cut:]]
            out[f"{factor}_h{horizon}"] = {
                "full": full,
                "in_sample_70pct": _summarize(in_sample),
                "out_of_sample_30pct": _summarize(oos),
                "regime": _regime_breakdown(panel, factor, horizon),
                "quintile_spread": _quintile_spread(panel, factor, ret),
                "verdict": "ROBUST" if (_summarize(oos).get("ic_mean", 0) < 0) else "WEAK_OOS",
            }
    return out


def main() -> int:
    import asyncio

    from pit.db import connect

    async def _load():
        conn = await connect("cp_backtest", timeout=300)
        try:
            rows = await conn.fetch(
                """SELECT slug, date, durability_score, momentum_score, valuation_score,
                          d_pct_var, d_pct_cvar, v_met_ath, v_met_atl,
                          d_met_ath_days, d_met_atl_days, d_met_coin_age_d
                   FROM "pit_dmv_cp011_v2_20260812_1315".dmv_rows
                   WHERE methodology_version='cp011_pit_v2_current_main_full_regen'
                     AND incomplete=FALSE ORDER BY slug, date""")
            return pd.DataFrame([dict(r) for r in rows])
        finally:
            await conn.close()

    dmv = asyncio.run(_load())
    print(f"dmv: {len(dmv)}")
    panel = build_panel(dmv, load_frozen_prices(SNAPSHOT))
    print(f"panel: {len(panel)}")
    res = run(panel)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    for k, v in res.items():
        print(f"\n== {k} ==")
        print("  full:", v["full"])
        print("  is:", v["in_sample_70pct"], " oos:", v["out_of_sample_30pct"])
        print("  regimes:", json.dumps(v["regime"]))
        print("  spread:", v["quintile_spread"], "->", v["verdict"])
    print(f"saved -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
