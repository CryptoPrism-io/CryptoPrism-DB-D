"""CP-011 Phase B — PIT-safe historical metrics (cumulative ATH/ATL).

Replaces the leaky ``gcp_dmv_met.py`` / ``backfill phase_metrics`` logic:
  - v_met_ath / v_met_atl are CUMULATIVE max/min as of each row date (PIT).
  - ATH/ATL date as of a row = date when the running max/min was last set,
    computed only from rows <= that date (no full-series idxmax/idxmin).
  - days-since-ATH/ATL is computed vs the ROW date, never ``now()``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

METRICS_METHODOLOGY = "pit-cumulative-ath-atl-v1"


def calculate_metrics_pit(
    df: pd.DataFrame,
    *,
    slug_col: str = "slug",
    date_col: str = "timestamp",
    high_col: str = "high",
    low_col: str = "low",
) -> pd.DataFrame:
    """Compute cumulative ATH/ATL + row-date days-since. Mutates no input.

    Adds: v_met_ath, v_met_atl, ath_date_pit, atl_date_pit,
          d_met_ath_days, d_met_atl_days, d_met_ath_week, d_met_ath_month,
          d_met_atl_week, d_met_atl_month, d_met_coin_age_d (row-date minus the
          asset's first valid OHLCV date — PIT approximate listing age).
    """
    df = df.sort_values([slug_col, date_col]).copy()
    first_seen = df.groupby(slug_col)[date_col].transform("min")
    out_parts: list[pd.DataFrame] = []
    for _slug, g in df.groupby(slug_col, sort=True):
        highs = g[high_col].to_numpy(dtype=float)
        lows = g[low_col].to_numpy(dtype=float)
        dates = pd.to_datetime(g[date_col]).dt.tz_localize(None).to_numpy()
        n = len(g)
        ath = np.full(n, np.nan, dtype=float)
        atl = np.full(n, np.nan, dtype=float)
        ath_date = np.empty(n, dtype="datetime64[D]")
        atl_date = np.empty(n, dtype="datetime64[D]")
        cur_max = -np.inf
        cur_min = np.inf
        max_date = None
        min_date = None
        for i in range(n):
            if highs[i] > cur_max:
                cur_max = highs[i]
                max_date = dates[i]
            if lows[i] < cur_min:
                cur_min = lows[i]
                min_date = dates[i]
            ath[i] = cur_max
            atl[i] = cur_min
            ath_date[i] = (
                np.datetime64(max_date, "D") if max_date is not None else np.datetime64("NaT")
            )
            atl_date[i] = (
                np.datetime64(min_date, "D") if min_date is not None else np.datetime64("NaT")
            )
        g = g.copy()
        g[date_col] = pd.to_datetime(g[date_col]).dt.tz_localize(None)
        g["first_seen"] = pd.to_datetime(first_seen.loc[g.index]).dt.tz_localize(None)
        g["v_met_ath"] = ath
        g["v_met_atl"] = atl
        g["ath_date_pit"] = pd.to_datetime(ath_date)
        g["atl_date_pit"] = pd.to_datetime(atl_date)
        g["d_met_ath_days"] = (g[date_col] - g["ath_date_pit"]).dt.days
        g["d_met_atl_days"] = (g[date_col] - g["atl_date_pit"]).dt.days
        g["d_met_ath_week"] = g["d_met_ath_days"] // 7
        g["d_met_ath_month"] = g["d_met_ath_days"] // 30
        g["d_met_atl_week"] = g["d_met_atl_days"] // 7
        g["d_met_atl_month"] = g["d_met_atl_days"] // 30
        g["d_met_coin_age_d"] = (g[date_col] - g["first_seen"]).dt.days
        out_parts.append(g)

    return pd.concat(out_parts) if out_parts else df.copy()


def metrics_old_fullsample(
    df: pd.DataFrame,
    *,
    slug_col: str = "slug",
    date_col: str = "timestamp",
    high_col: str = "high",
    low_col: str = "low",
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """LEGACY full-series ATH/ATL + now()-based days-since (the Phase-A leak).

    Kept ONLY for old-vs-corrected comparison and to prove the mutation test
    catches it. Never used for production output.
    """
    now_ts = as_of or pd.Timestamp.now()
    out = df.copy()
    full_dates: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for slug, g in df.groupby(slug_col):
        ath_date = g.loc[g[high_col].idxmax(), date_col]
        atl_date = g.loc[g[low_col].idxmin(), date_col]
        full_dates[slug] = (pd.Timestamp(ath_date), pd.Timestamp(atl_date))
    out["d_met_ath_days_old"] = out[slug_col].map(lambda s: (now_ts - full_dates[s][0]).days)
    out["d_met_atl_days_old"] = out[slug_col].map(lambda s: (now_ts - full_dates[s][1]).days)
    return out
