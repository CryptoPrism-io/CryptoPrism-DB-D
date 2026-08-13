"""CP-011 full shadow rebuild — Core-4 signal regeneration from raw OHLCV.

Reuses the repo's TA functions (gcp_dmv_mom/osc/tvv/rat) — the same functions the
backfill uses — to regenerate the declared Core-4 signal BIN columns from raw
OHLCV, so the full OHLCV-observed universe (incl. delisted assets) can be covered
where supported. Only the DECLARED policy bin columns are selected for scoring.

Note: the current gcp_dmv_osc generator adds Supertrend/Aroon bins; we run the
full current pipeline (so Supertrend_Dir etc. exist) and select only the declared
oscillator bins.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_TA = str(Path(__file__).resolve().parent.parent / "technical_analysis")
if _TA not in sys.path:
    sys.path.insert(0, _TA)
# The TA modules require DB env vars present at import time (module-level guard)
# even though the functions themselves never use them.
for _v in ("DB_HOST", "DB_USER", "DB_PASSWORD"):
    os.environ.setdefault(_v, "pit-regenerate-local")
os.environ.setdefault("GITHUB_ACTIONS", "1")

from gcp_dmv_mom import (  # noqa: E402
    calculate_pct_change as mom_pct,
    calculate_rsi, calculate_sma, calculate_roc, calculate_williams_r,
    calculate_smi, calculate_cmo, calculate_mom, calculate_tsi,
    generate_binary_signals_momentum,
)
from gcp_dmv_osc import (  # noqa: E402
    calculate_pct_change as osc_pct,
    calculate_cum_ret, calculate_macd, calculate_cci, calculate_adx,
    calculate_ultimate_oscillator, calculate_awesome_oscillator, calculate_trix,
    calculate_supertrend, calculate_aroon,
    generate_binary_signals_oscillators, rename_columns_for_db,
)
from gcp_dmv_tvv import (  # noqa: E402
    calculate_obv, calculate_moving_averages, calculate_atr, calculate_channels,
    calculate_bollinger_bands, calculate_vwap, calculate_cmf,
    generate_binary_signals as tvv_binary,
)
from gcp_dmv_rat import (  # noqa: E402
    calculate_pct_change as rat_pct,
    calculate_benchmark_returns, calculate_alpha, calculate_beta,
    calculate_omega_ratio, calculate_sharpe_ratio, calculate_sortino_ratio,
    calculate_treynor_ratio, calculate_common_sense_ratio,
    calculate_information_ratio, calculate_winloss_ratio,
    calculate_win_rate, calculate_risk_of_ruin, calculate_gain_to_pain,
    generate_binary_signals_ratios,
)
from pit.policy import SIGNAL_FAMILIES  # noqa: E402

# Pre-bin raw ratio column names (the values the bins are derived from).
RATIO_RAW_COLS = [
    "m_rat_alpha", "d_rat_beta", "v_rat_sharpe", "v_rat_sortino",
    "v_rat_teynor", "v_rat_common_sense", "v_rat_information",
    "v_rat_win_loss", "m_rat_win_rate", "m_rat_ror", "d_rat_pain",
]

logging.getLogger().setLevel(logging.WARNING)  # silence TA info logs


def regenerate_momentum(df: pd.DataFrame) -> pd.DataFrame:
    out = (
        df.pipe(mom_pct).pipe(calculate_rsi).pipe(calculate_sma).pipe(calculate_roc)
        .pipe(calculate_williams_r).pipe(calculate_smi).pipe(calculate_cmo)
        .pipe(calculate_mom).pipe(calculate_tsi).pipe(generate_binary_signals_momentum)
    )
    cols = ["slug", "timestamp"] + SIGNAL_FAMILIES["momentum"]
    return out[[c for c in cols if c in out.columns]]


def regenerate_oscillators(df: pd.DataFrame) -> pd.DataFrame:
    out = (
        df.pipe(osc_pct).pipe(calculate_cum_ret).pipe(calculate_macd).pipe(calculate_cci)
        .pipe(calculate_adx).pipe(calculate_ultimate_oscillator)
        .pipe(calculate_awesome_oscillator).pipe(calculate_trix)
        .pipe(calculate_supertrend).pipe(calculate_aroon)
        .pipe(generate_binary_signals_oscillators)
    )
    out = rename_columns_for_db(out)
    cols = ["slug", "timestamp"] + SIGNAL_FAMILIES["oscillators"]
    return out[[c for c in cols if c in out.columns]]


def regenerate_tvv(df: pd.DataFrame) -> pd.DataFrame:
    out = (
        df.pipe(calculate_obv).pipe(calculate_moving_averages).pipe(calculate_atr)
        .pipe(calculate_channels).pipe(calculate_bollinger_bands).pipe(calculate_vwap)
        .pipe(calculate_cmf).pipe(tvv_binary)
    )
    cols = ["slug", "timestamp"] + SIGNAL_FAMILIES["tvv"]
    return out[[c for c in cols if c in out.columns]]


def regenerate_ratios(
    df: pd.DataFrame, benchmark_df: pd.DataFrame, *, include_raw: bool = False
) -> pd.DataFrame:
    """28-day trailing-window ratios (replicates backfill phase_ratios).

    The benchmark (bitcoin) is derived from each window's bitcoin rows, matching
    the backfill. ``benchmark_df`` is accepted for API compatibility.

    ``include_raw`` additionally keeps the 11 pre-bin raw ratio columns (used by
    the equivalence proof); the pipeline output is unchanged when False.
    """
    df = rat_pct(df)
    all_ratios: list[pd.DataFrame] = []
    for target_date in sorted(df["timestamp"].unique()):
        window_start = target_date - pd.Timedelta(days=28)
        window = df[(df["timestamp"] >= window_start) & (df["timestamp"] <= target_date)].copy()
        if window["timestamp"].nunique() < 5:
            continue
        bench = calculate_benchmark_returns(window)
        if bench.empty:
            continue
        bench_avg = bench.mean()
        beta_values: dict[str, float] = {}
        for slug, group in window.groupby("slug"):
            if slug != "bitcoin" and len(group) >= 3:
                try:
                    beta_values[slug] = float(calculate_beta(group, bench)["d_rat_beta"])
                except Exception:  # noqa: BLE001
                    continue
        for slug, group in window.groupby("slug"):
            if slug == "bitcoin" or len(group) < 3 or slug not in beta_values:
                continue
            try:
                combined = pd.concat([
                    calculate_alpha(group, bench_avg),
                    calculate_omega_ratio(group, bench),
                    calculate_sharpe_ratio(group),
                    calculate_sortino_ratio(group),
                    calculate_treynor_ratio(group, beta_values),
                    calculate_common_sense_ratio(group),
                    calculate_information_ratio(group, bench),
                    calculate_winloss_ratio(group),
                    calculate_win_rate(group),
                    calculate_risk_of_ruin(group),
                    calculate_gain_to_pain(group),
                ])
                row = pd.DataFrame(combined).transpose()
                row["slug"] = slug
                row["timestamp"] = target_date
                row["d_rat_beta"] = beta_values[slug]
                all_ratios.append(row)
            except Exception:  # noqa: BLE001
                continue
    if not all_ratios:
        empty = df[["slug", "timestamp"]].copy()
        for c in SIGNAL_FAMILIES["ratios"]:
            empty[c] = np.nan
        return empty
    ratios = pd.concat(all_ratios, ignore_index=True)
    ratios = generate_binary_signals_ratios(ratios)
    cols = ["slug", "timestamp"] + SIGNAL_FAMILIES["ratios"]
    if include_raw:
        cols = ["slug", "timestamp"] + RATIO_RAW_COLS + SIGNAL_FAMILIES["ratios"]
    return ratios[[c for c in cols if c in ratios.columns]]


def regenerate_core4(df: pd.DataFrame, benchmark_df: pd.DataFrame) -> pd.DataFrame:
    """Regenerate all Core-4 signal families from raw OHLCV; merge on (slug,timestamp)."""
    frames = [
        regenerate_momentum(df),
        regenerate_oscillators(df),
        regenerate_tvv(df),
        regenerate_ratios(df, benchmark_df),
    ]
    merged = None
    for f in frames:
        if f.empty:
            continue
        merged = f if merged is None else merged.merge(f, on=["slug", "timestamp"], how="outer")
    return merged if merged is not None else df[["slug", "timestamp"]].copy()
