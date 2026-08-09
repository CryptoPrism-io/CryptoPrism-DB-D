"""CP-011 Phase B — PIT-safe trailing VaR / CVaR.

Replaces the full-sample ``calculate_var_cvar`` in ``gcp_dmv_pct.py``.

Contract:
  - For each slug, each output row at date t uses ONLY returns with
    timestamp <= t and within the trailing ``window_days`` calendar window.
  - d_pct_var   = 5th percentile of trailing window returns (confidence 0.95).
  - d_pct_cvar  = mean of trailing window returns <= d_pct_var.
  - Returns NULL (NaN) when fewer than ``min_obs`` valid returns are available
    (insufficient history) — never zero/neutral-filled.
  - Deterministic: explicit quantile interpolation = ``linear`` (numpy default);
    input is stably sorted by (slug, date); duplicate dates are all included in
    the same trailing window (searchsorted left boundary includes equal dates).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

VAR_CVAR_METHODOLOGY = "pit-trailing-365d-min252"
_DEFAULT_WINDOW_DAYS = 365
_DEFAULT_MIN_OBS = 252
_DEFAULT_CONFIDENCE = 0.95


def calculate_var_cvar_pit(
    df: pd.DataFrame,
    *,
    slug_col: str = "slug",
    date_col: str = "timestamp",
    return_col: str = "m_pct_1d",
    window_days: int = _DEFAULT_WINDOW_DAYS,
    min_obs: int = _DEFAULT_MIN_OBS,
    confidence: float = _DEFAULT_CONFIDENCE,
) -> pd.DataFrame:
    """Compute PIT-safe trailing VaR/CVaR per slug. Mutates no input.

    Adds columns ``d_pct_var`` and ``d_pct_cvar`` (float; NaN before min history).
    """
    if window_days <= 0 or min_obs <= 0:
        raise ValueError("window_days and min_obs must be > 0")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0,1)")

    df = df.sort_values([slug_col, date_col]).copy()
    out_parts: list[pd.DataFrame] = []
    for _slug, g in df.groupby(slug_col, sort=True):
        dates = pd.to_datetime(g[date_col]).dt.tz_localize(None).to_numpy()
        rets = g[return_col].to_numpy(dtype=float)
        n = len(g)
        starts = np.searchsorted(
            dates, dates - np.timedelta64(window_days, "D"), side="left"
        )
        var = np.full(n, np.nan, dtype=float)
        cvar = np.full(n, np.nan, dtype=float)
        for i in range(n):
            win = rets[starts[i] : i + 1]
            win = win[~np.isnan(win)]
            if win.size < min_obs:
                continue
            v = float(np.quantile(win, 1.0 - confidence, method="linear"))
            var[i] = v
            tail = win[win <= v]
            if tail.size:
                cvar[i] = float(tail.mean())
        g = g.copy()
        g["d_pct_var"] = var
        g["d_pct_cvar"] = cvar
        out_parts.append(g)

    out = pd.concat(out_parts) if out_parts else df.copy()
    out["d_pct_var"] = out["d_pct_var"]
    out["d_pct_cvar"] = out["d_pct_cvar"]
    return out


def var_cvar_old_fullsample(
    df: pd.DataFrame,
    *,
    slug_col: str = "slug",
    return_col: str = "m_pct_1d",
    confidence: float = _DEFAULT_CONFIDENCE,
) -> pd.DataFrame:
    """LEGACY full-sample VaR/CVaR (the Phase-A-flagged leak).

    Kept ONLY to produce old-vs-corrected comparisons in the sample and to prove
    the future-mutation test would catch it. Never used for production output.
    """
    var_df = (
        df.groupby(slug_col)[return_col]
        .quantile(1 - confidence)
        .reset_index(name="d_pct_var_old")
    )
    cvar_rows = []
    for slug, x in df.groupby(slug_col):
        q = float(x[return_col].quantile(1 - confidence))
        tail = x[return_col][x[return_col] <= q]
        cvar_rows.append({"slug": slug, "d_pct_cvar_old": float(tail.mean())})
    cvar_df = pd.DataFrame(cvar_rows)
    out = df.merge(var_df, on=slug_col, how="left").merge(cvar_df, on=slug_col, how="left")
    return out
