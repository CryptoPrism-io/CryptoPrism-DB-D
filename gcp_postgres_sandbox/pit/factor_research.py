"""Factor research — BTC single-asset time-series IC backtests (READ-ONLY).

Factors (the sibling's research-frame universe, loaded from dbcp):
  mvrv             onchain_utxo_metrics  chain='bitcoin'  metric='mvrv'
  realized_cap_usd onchain_utxo_metrics  chain='bitcoin'  metric='realized_cap_usd'
  cdd              onchain_daily_metrics chain='btc'      metric='coin_days_destroyed'
  sopr             onchain_daily_metrics chain='btc'      metric='sopr'
  news             FE_NEWS_SIGNALS       slug='bitcoin'   news_sentiment_1d (>=2024-04-01)

For each factor x forward-return horizon (1/7/30d) computes TIME-SERIES IC
(Pearson/Spearman of factor(t) vs return(t -> t+h)) with Fisher-z 95% CI,
p-value, BH-FDR across experiments, out-of-sample split and per-period buckets.

Output: report/cp011-phase-d/factor_research.json. No DB writes.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pit.cp012 import load_frozen_prices  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
SNAPSHOT = ROOT / "report" / "cp011-phase-d" / "frozen_ohlcv_cp011_v2.parquet"
OUT = ROOT / "report" / "cp011-phase-d" / "factor_research.json"

NEWS_CUTOFF = pd.Timestamp("2024-04-01")
HORIZONS = (1, 7, 30)


def _factor(name: str, rows) -> pd.DataFrame:
    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df[["date", "value"]].dropna().rename(columns={"value": name})


async def load_factors(conn) -> dict[str, pd.DataFrame]:
    out = {}
    out["mvrv"] = _factor("mvrv", await conn.fetch(
        "SELECT metric_date AS date, value FROM onchain_utxo_metrics "
        "WHERE chain='bitcoin' AND metric='mvrv' AND value IS NOT NULL ORDER BY metric_date"))
    out["realized_cap_usd"] = _factor("realized_cap_usd", await conn.fetch(
        "SELECT metric_date AS date, value FROM onchain_utxo_metrics "
        "WHERE chain='bitcoin' AND metric='realized_cap_usd' AND value IS NOT NULL ORDER BY metric_date"))
    out["cdd"] = _factor("cdd", await conn.fetch(
        "SELECT metric_date AS date, value FROM onchain_daily_metrics "
        "WHERE chain='btc' AND metric='coin_days_destroyed' AND value IS NOT NULL ORDER BY metric_date"))
    out["sopr"] = _factor("sopr", await conn.fetch(
        "SELECT metric_date AS date, value FROM onchain_daily_metrics "
        "WHERE chain='btc' AND metric='sopr' AND value IS NOT NULL ORDER BY metric_date"))
    out["news"] = _factor("news", await conn.fetch(
        "SELECT timestamp::date AS date, news_sentiment_1d AS value FROM \"FE_NEWS_SIGNALS\" "
        "WHERE slug='bitcoin' AND news_sentiment_1d IS NOT NULL ORDER BY timestamp"))
    return out


def _ic_series(x: pd.Series, y: pd.Series) -> dict:
    m = np.isfinite(x.to_numpy(dtype=float)) & np.isfinite(y.to_numpy(dtype=float))
    a = x.to_numpy(dtype=float)[m]
    b = y.to_numpy(dtype=float)[m]
    n = len(a)
    if n < 30 or np.std(a) == 0 or np.std(b) == 0:
        return {"n": int(n)}
    ic_p = float(np.corrcoef(a, b)[0, 1])
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    ic_s = float(np.corrcoef(ra, rb)[0, 1])
    # Fisher-z CI
    try:
        z = math.atanh(max(-0.9999, min(0.9999, ic_p)))
    except ValueError:
        z = 0.0
    se = 1.0 / math.sqrt(max(n - 3, 1))
    lo = math.tanh(z - 1.96 * se)
    hi = math.tanh(z + 1.96 * se)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / se / math.sqrt(2)))) if se > 0 else 1.0
    return {
        "n": int(n),
        "ic_pearson": round(ic_p, 4),
        "ic_spearman": round(ic_s, 4),
        "ci95_low": round(lo, 4),
        "ci95_high": round(hi, 4),
        "p_value": round(float(min(max(p, 0.0), 1.0)), 6),
    }


def _summarize_ic(ic: pd.Series) -> dict:
    if ic.empty:
        return {"n_dates": 0}
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) if len(ic) > 1 else 0.0
    se = std / math.sqrt(len(ic))
    return {
        "n_dates": int(len(ic)),
        "ic_mean": round(mean, 4),
        "ci95_low": round(mean - 1.96 * se, 4),
        "ci95_high": round(mean + 1.96 * se, 4),
        "pct_negative": round(float((ic < 0).mean()), 3),
    }


def run(btc_close: pd.Series, factors: dict[str, pd.DataFrame]) -> dict:
    base = btc_close.to_frame("close").sort_index()
    for name, f in factors.items():
        base = base.join(f.set_index("date"), how="outer")
    for h in HORIZONS:
        base[f"ret{h}"] = base["close"].shift(-h) / base["close"] - 1.0

    results = []
    for name in factors:
        for h in HORIZONS:
            col = name if name != "close" else name
            sub = base[[col, f"ret{h}"]].dropna()
            if name == "news":
                sub = sub[sub.index >= NEWS_CUTOFF]
            if sub.empty:
                results.append({"factor": name, "horizon": h, "n": 0})
                continue
            r = _ic_series(sub[col], sub[f"ret{h}"])
            # OOS split + periods for the time-series IC via rolling window of |IC|
            window = base[[f"ret{h}"]].join(base[col], how="inner").dropna()
            dates = window.index
            cut = int(len(dates) * 0.7)
            is_win = window.iloc[:cut]
            oos_win = window.iloc[cut:]
            r["in_sample_n"] = len(is_win)
            r["oos_n"] = len(oos_win)
            r["in_sample_ic"] = _ic_series(is_win[col], is_win[f"ret{h}"])
            r["out_of_sample_ic"] = _ic_series(oos_win[col], oos_win[f"ret{h}"])
            # period buckets
            buckets = {}
            thirds = pd.qcut(pd.Series(np.arange(len(window)), index=window.index),
                             3, labels=["p1", "p2", "p3"])
            for label in ["p1", "p2", "p3"]:
                w = window[thirds == label]
                buckets[f"period_{label}"] = _ic_series(w[col], w[f"ret{h}"])
            r["periods"] = buckets
            results.append({"factor": name, "horizon": h, **r})

    # BH-FDR
    finite: list[tuple[int, float]] = []
    for i, r in enumerate(results):
        pv = r.get("p_value")
        if isinstance(pv, (int, float)):
            finite.append((i, float(pv)))
    for r in results:
        r["bh_significant"] = False
    if finite:
        ordered = sorted(finite, key=lambda x: x[1])
        m = len(ordered)
        max_k = 0
        for k, (_, p) in enumerate(ordered, start=1):
            if p <= 0.05 * k / m:
                max_k = k
        rejected = {i for i, _ in ordered[:max_k]}
        for i, _ in ordered:
            results[i]["bh_significant"] = i in rejected
    for r in results:
        ic = r.get("ic_pearson")
        lo = r.get("ci95_low")
        hi = r.get("ci95_high")
        if isinstance(ic, (int, float)) and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            r["material"] = bool(abs(float(ic)) >= 0.1 and not (lo <= 0 <= hi))
        else:
            r["material"] = False
    return {"results": results, "coverage": {k: int(len(v)) for k, v in factors.items()}}


def main() -> int:
    import asyncio

    from pit.db import connect

    async def _load():
        conn = await connect("dbcp", timeout=120)
        try:
            return await load_factors(conn)
        finally:
            await conn.close()

    factors = asyncio.run(_load())
    for k, v in factors.items():
        print(f"factor {k}: {len(v)} rows, {v['date'].min().date()} .. {v['date'].max().date()}")

    btc = load_frozen_prices(SNAPSHOT)
    btc_close = btc[btc["slug"] == "bitcoin"].set_index("date")["close"].sort_index()
    print(f"btc closes: {len(btc_close)}")

    res = run(btc_close, factors)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")

    print("\n=== FACTOR RESEARCH (BTC time-series IC) ===")
    for r in res["results"]:
        ic = r.get("ic_pearson")
        oos = r.get("out_of_sample_ic", {}).get("ic_pearson")
        if ic is None:
            print(f"  {r['factor']:18s} h{r['horizon']:2d} n=0")
            continue
        print(f"  {r['factor']:18s} h{r['horizon']:2d} n={r['n']:5d} ic_p={ic:+.3f} "
              f"ic_s={r['ic_spearman']:+.3f} ci=[{r['ci95_low']:+.3f},{r['ci95_high']:+.3f}] "
              f"oos={oos and round(oos,3)} bh={r['bh_significant']} mat={r['material']}")
    print(f"saved -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
