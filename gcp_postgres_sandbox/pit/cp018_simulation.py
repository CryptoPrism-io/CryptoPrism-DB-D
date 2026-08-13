"""CP-018 — realistic long/short simulation for d_pct_cvar (READ-ONLY).

Answers "does the CVaR signal make money net of costs on liquid assets?"
  - universe: top-N by trailing 30d volume each rebalance
  - signal:    d_pct_cvar (PIT); LONG lowest-CVaR quintile, SHORT highest-CVaR
  - rebalance: every 30 calendar days; holding = forward 30d return (from frozen closes)
  - costs:     turnover-aware, per-side cost (default 10 bps)
  - outputs:   gross/net annualized return, Sharpe, hit-rate, avg turnover

No DB writes. Report -> report/cp011-phase-d/cp018_simulation.json.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent.parent
SNAPSHOT = ROOT / "report" / "cp011-phase-d" / "frozen_ohlcv_cp011_v2.parquet"
OUT = ROOT / "report" / "cp011-phase-d" / "cp018_simulation.json"
SCHEMA = "pit_dmv_cp011_v2_20260812_1315"
METHOD = "cp011_pit_v2_current_main_full_regen"


def load_daily(snapshot: Path) -> pd.DataFrame:
    df = pd.read_parquet(snapshot, columns=["slug", "timestamp", "close", "volume"])
    df["date"] = pd.to_datetime(df["timestamp"]).dt.normalize()
    daily = (df.sort_values("timestamp")
             .groupby(["slug", "date"], sort=False)
             .agg(close=("close", "last"), volume=("volume", "sum"))
             .reset_index())
    return daily.sort_values(["slug", "date"]).reset_index(drop=True)


async def load_cvar(conn, method: str) -> pd.DataFrame:
    rows = await conn.fetch(
        f"""SELECT slug, date, d_pct_cvar, d_pct_var FROM "{SCHEMA}".dmv_rows
            WHERE methodology_version=$1 AND incomplete=FALSE AND d_pct_cvar IS NOT NULL
            ORDER BY slug, date""", method)
    return pd.DataFrame([dict(r) for r in rows])


def simulate(daily: pd.DataFrame, cvar: pd.DataFrame, top_n: int = 200,
             cost_per_side: float = 0.001) -> dict:
    cvar["date"] = pd.to_datetime(cvar["date"])
    daily = daily.merge(cvar[["slug", "date", "d_pct_cvar"]], on=["slug", "date"], how="inner")

    # trailing liquidity + forward 30d return per slug
    daily = daily.sort_values(["slug", "date"]).reset_index(drop=True)
    g = daily.groupby("slug", sort=False)
    daily["vol_30d"] = g["volume"].transform(lambda s: s.rolling(30, min_periods=10).mean())
    daily["ret30"] = g["close"].transform(lambda s: s.shift(-30) / s - 1.0)

    dates = sorted(daily["date"].unique())
    rebal = dates[::30]  # every 30th calendar date
    periods: list[dict] = []
    gross_list: list[float] = []
    net_list: list[float] = []
    turnover_list: list[float] = []
    prev_long: set[str] = set()
    prev_short: set[str] = set()
    for d in rebal:
        sub = daily[daily["date"] == d].dropna(subset=["d_pct_cvar", "ret30", "vol_30d"])
        if len(sub) < 50:
            continue
        top = sub.nlargest(top_n, "vol_30d")
        if len(top) < 20:
            continue
        top = top.sort_values("d_pct_cvar")
        n_leg = max(1, int(round(len(top) * 0.2)))
        short = set(top.tail(n_leg)["slug"])   # highest CVaR
        long = set(top.head(n_leg)["slug"])    # lowest CVaR
        long_ret = float(top.head(n_leg)["ret30"].mean())
        short_ret = float(top.tail(n_leg)["ret30"].mean())
        spread_gross = long_ret - short_ret
        # turnover = fraction of names changed across the two legs
        chg = (len(long ^ prev_long) + len(short ^ prev_short)) / max(len(long) + len(short), 1)
        spread_net = spread_gross - chg * cost_per_side
        gross_list.append(spread_gross)
        net_list.append(spread_net)
        turnover_list.append(chg)
        periods.append({
            "date": str(d.date()), "universe": int(len(top)), "long": len(long), "short": len(short),
            "long_ret": round(long_ret, 5), "short_ret": round(short_ret, 5),
            "spread_gross": round(spread_gross, 5), "turnover": round(chg, 4),
            "cost": round(chg * cost_per_side, 5), "spread_net": round(spread_net, 5),
        })
        prev_long, prev_short = long, short

    if not gross_list:
        return {"n_periods": 0}
    gross = np.array(gross_list, dtype=float)
    net = np.array(net_list, dtype=float)
    n = len(gross_list)
    gross_ann = float(gross.mean()) * 12
    net_ann = float(net.mean()) * 12
    net_std = float(net.std(ddof=1)) if n > 1 else 0.0
    sharpe = net_ann / (net_std * math.sqrt(12)) if net_std > 0 else 0.0
    return {
        "n_periods": int(n),
        "periods": periods,
        "rebalance": "30d",
        "universe_top_n": int(top_n),
        "cost_per_side": cost_per_side,
        "spread_gross_mean": round(float(gross.mean()), 5),
        "spread_net_mean": round(float(net.mean()), 5),
        "spread_net_median": round(float(np.median(net)), 5),
        "annualized_gross": round(gross_ann, 4),
        "annualized_net": round(net_ann, 4),
        "annualized_sharpe": round(sharpe, 3),
        "hit_rate_net": round(float((net > 0).mean()), 3),
        "avg_turnover": round(float(np.mean(turnover_list)), 3),
    }


def main() -> int:
    import asyncio

    from pit.db import connect

    daily = load_daily(SNAPSHOT)
    print(f"daily: {len(daily)}")
    async def _load():
        conn = await connect("cp_backtest", timeout=300)
        try:
            return await load_cvar(conn, METHOD)
        finally:
            await conn.close()
    cvar = asyncio.run(_load())
    print(f"cvar rows: {len(cvar)}")
    res = simulate(daily, cvar)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "periods"}, indent=2))
    print(f"saved -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
