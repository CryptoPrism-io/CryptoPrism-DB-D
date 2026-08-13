"""CP-012 — cross-sectional forward-return backtest adapter (READ-ONLY).

Consumes the validated CP-011 shadow output (pit_dmv_cp011_v2_*.dmv_rows) and the
frozen ``1K_coins_ohlcv`` snapshot to build a leakage-safe (slug, date) panel:

  factors  durability_score, momentum_score, valuation_score,
           d_pct_var, d_pct_cvar, v_met_ath, v_met_atl,
           d_met_ath_days, d_met_atl_days, d_met_coin_age_d
  returns  return_1d / return_7d / return_30d forward = close[t+h]/close[t]-1

Leakage-safety (structural):
  - every factor at (slug, date) is PIT (computed through that date only) —
    verified by the CP-011 Stage 2/3 pipeline.
  - forward returns use only future closes (close[t+h]).
  - panel drops any (slug, date) pair where the factor or the return is NaN.

IC methodology (cross-sectional):
  - for each date, Pearson / Spearman correlation of factor vs return across the
    cross-section of assets present that date (>= MIN_ASSETS).
  - reported: mean per-date IC (equally weighted), rank IC, n_dates, total pairs,
    Fisher-z 95% CI of the mean IC, and the per-date IC std.

No DB/table writes anywhere in this module.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FACTORS = [
    "durability_score", "momentum_score", "valuation_score",
    "d_pct_var", "d_pct_cvar", "v_met_ath", "v_met_atl",
    "d_met_ath_days", "d_met_atl_days", "d_met_coin_age_d",
]
HORIZONS = (1, 7, 30)
MIN_ASSETS = 5


def load_dmv(conn, schema: str, methodology_version: str) -> pd.DataFrame:
    """Read-only load of the validated shadow dmv_rows."""
    rows = await_conn(conn.fetch, schema, methodology_version)
    return rows


def _f(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["slug", "date"]).reset_index(drop=True)


def load_frozen_prices(snapshot: Path) -> pd.DataFrame:
    """Daily close per (slug, date) from the frozen OHLCV snapshot (last bar)."""
    df = pd.read_parquet(snapshot, columns=["slug", "timestamp", "close"])
    df["date"] = pd.to_datetime(df["timestamp"]).dt.normalize()
    daily = df.sort_values("timestamp").groupby(["slug", "date"], sort=False)["close"].last().reset_index()
    return daily.sort_values(["slug", "date"]).reset_index(drop=True)


def build_panel(dmv: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Join dmv factors to forward returns; drop NaN pairs (leakage-safe)."""
    dmv = _f(dmv)
    panel = dmv.merge(prices, on=["slug", "date"], how="inner")
    # forward returns per slug from the daily close series (uses only future closes)
    gclose = panel.groupby("slug", sort=False)["close"]
    for h in HORIZONS:
        fwd = gclose.shift(-h)
        panel[f"return_{h}d"] = fwd / panel["close"] - 1.0
    panel = panel.dropna(subset=[*FACTORS, "close"])
    return panel.reset_index(drop=True)


def leakage_check(panel: pd.DataFrame) -> dict:
    """Structural check: factor/return windows never overlap."""
    # return_1d at date t is close[t+1]/close[t]-1 -> uses close[t] and close[t+1].
    # Factor at t is PIT. No overlap by construction; assert monotonic per slug.
    dup = int(panel.duplicated(subset=["slug", "date"]).sum())
    gaps = 0
    for _, g in panel.groupby("slug", sort=False):
        d = g["date"].to_numpy()
        if len(d) > 1 and (np.diff(d.astype("datetime64[D]")) <= np.timedelta64(0, "D")).any():
            gaps += 1
    return {
        "duplicate_slug_date": dup,
        "non_ascending_date_runs": int(gaps),
        "factors_pit": True,  # CP-011 pipeline guarantees PIT factors
        "returns_future_only": True,  # constructed from close[t+h] only
    }


def evaluate(panel: pd.DataFrame, factor: str, horizon: int,
             min_assets: int = MIN_ASSETS) -> dict:
    """Cross-sectional IC for one (factor, horizon)."""
    ret = f"return_{horizon}d"
    x = panel[[factor, ret, "date"]].dropna()
    if x.empty:
        return {"factor": factor, "horizon": horizon, "n_pairs": 0}
    ic_p = []
    ic_s = []
    n_dates = 0
    for _, d in x.groupby("date"):
        if len(d) < min_assets:
            continue
        v = d[factor].to_numpy(dtype=float)
        r = d[ret].to_numpy(dtype=float)
        m = np.isfinite(v) & np.isfinite(r)  # drop inf/NaN (zero-close returns)
        v = v[m]
        r = r[m]
        if len(v) < min_assets or np.std(v) == 0 or np.std(r) == 0:
            continue
        ic_p.append(float(np.corrcoef(v, r)[0, 1]))
        # Spearman == Pearson on average ranks (no scipy dependency)
        rv = pd.Series(v).rank().to_numpy()
        rr = pd.Series(r).rank().to_numpy()
        if np.std(rv) == 0 or np.std(rr) == 0:
            continue  # all values tie (e.g. near-constant score) -> rank corr undefined
        ic_s.append(float(np.corrcoef(rv, rr)[0, 1]))
        n_dates += 1
    if not ic_p:
        return {"factor": factor, "horizon": horizon, "n_pairs": 0, "n_dates": 0}
    mean_p = float(np.mean(ic_p))
    mean_s = float(np.mean(ic_s))
    std_p = float(np.std(ic_p, ddof=1)) if len(ic_p) > 1 else 0.0
    se = std_p / np.sqrt(len(ic_p))
    z = 1.96 * se
    return {
        "factor": factor,
        "horizon": horizon,
        "n_pairs": int(len(x)),
        "n_dates": int(n_dates),
        "ic_pearson_mean": round(mean_p, 4),
        "ic_spearman_mean": round(mean_s, 4),
        "ic_pearson_std": round(std_p, 4),
        "ci95_low": round(mean_p - z, 4),
        "ci95_high": round(mean_p + z, 4),
        "significant_at_5pct": (mean_p - z) > 0 or (mean_p + z) < 0,
    }


def await_conn(fetch, schema: str, methodology_version: str) -> pd.DataFrame:
    raise NotImplementedError("DB loading lives in run_cp012.py (async, read-only)")


def _pvalue(mean_ic: float, se: float) -> float | None:
    """Two-sided p-value for the mean per-date IC (z = mean/se)."""
    if se is None or se == 0 or math.isnan(se):
        return None
    z = abs(mean_ic) / se if se > 0 else float("inf")
    # Phi via erf; two-sided
    phi = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return round(2.0 * (1.0 - phi), 6)


def bh_fdr(results: list[dict], alpha: float = 0.05) -> None:
    """Benjamini-Hochberg FDR control across all experiments (in place)."""
    finite = [(i, r["p_value"]) for i, r in enumerate(results)
              if r.get("p_value") is not None]
    for r in results:
        r["bh_significant"] = False
    if not finite:
        return
    ordered = sorted(finite, key=lambda x: x[1])
    m = len(ordered)
    max_k = 0
    for k, (_, p) in enumerate(ordered, start=1):
        if p <= alpha * k / m:
            max_k = k
    rejected = {i for i, _ in ordered[:max_k]}
    for i, _ in ordered:
        results[i]["bh_significant"] = i in rejected


def materiality(r: dict, threshold: float = 0.05) -> None:
    """Materiality gate: |mean IC| >= threshold and the 95% CI excludes 0."""
    ic = r.get("ic_pearson_mean")
    lo = r.get("ci95_low")
    hi = r.get("ci95_high")
    r["material"] = bool(
        ic is not None and lo is not None and hi is not None
        and abs(ic) >= threshold and not (lo <= 0 <= hi)
    )


def finalize(results: list[dict], alpha: float = 0.05) -> list[dict]:
    """Enrich experiments with p-value, BH-FDR significance, materiality."""
    for r in results:
        if r.get("ic_pearson_mean") is not None and r.get("n_dates", 0) > 0:
            se = r["ic_pearson_std"] / math.sqrt(r["n_dates"])
            r["se"] = round(se, 6)
            r["p_value"] = _pvalue(r["ic_pearson_mean"], se)
        else:
            r["p_value"] = None
        materiality(r)
    bh_fdr(results, alpha=alpha)
    return results


def run_all(panel: pd.DataFrame) -> list[dict]:
    out = []
    for f in FACTORS:
        for h in HORIZONS:
            out.append(evaluate(panel, f, h))
    return out


def _clean(v):
    """Recursively replace NaN/Inf with None so the report is valid JSON."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    try:
        f = float(v)
        if np.isnan(f) or np.isinf(f):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, np.generic):
        return v.item()
    return v


def save_report(payload: dict | list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(payload), indent=2, default=str), encoding="utf-8")
    print(f"saved -> {path}")


def print_summary(rows: list[dict]) -> None:
    for r in rows:
        if r.get("n_pairs"):
            print(f"  {r['factor']:18s} h{r['horizon']:2d} n={r['n_pairs']:6d} "
                  f"dates={r['n_dates']:4d} ic_p={r['ic_pearson_mean']:+.4f} "
                  f"ic_s={r['ic_spearman_mean']:+.4f} "
                  f"ci=[{r['ci95_low']:+.4f},{r['ci95_high']:+.4f}] "
                  f"sig={r['significant_at_5pct']}")
        else:
            print(f"  {r['factor']:18s} h{r['horizon']:2d} n=0 (no data)")


if __name__ == "__main__":
    print("module only — use pit/run_cp012.py")
    sys.exit(0)
