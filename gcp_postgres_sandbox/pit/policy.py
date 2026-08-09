"""CP-011 Phase B — central live/backfill signal + missing-data policy.

One declared set of signal families and columns is shared by live and backfill
computation. Coverage impact of the core set is measured (not assumed): the
``metrics`` family only has ~113 historical dates (2026-03-29..08-08), so it is
OPTIONAL until historical metrics are reconstructed.

Missing-data policy: a DMV row missing any required (core) signal bin is marked
incomplete (score NaN) — never neutral/zero-filled. VaR/CVaR before min history
is NULL, never zero-filled.
"""

from __future__ import annotations

METHODOLOGY_VERSION = "pit-dmv-v1"
VAR_CVAR_METHODOLOGY = "pit-trailing-365d-min252"
METRICS_METHODOLOGY = "pit-cumulative-ath-atl-v1"
UNIVERSE_METHODOLOGY = "pit-approx-v1"

# Never neutral-fill missing values.
NEUTRAL_FILL = False

# Missing-data policy (one line, shared live + backfill).
MISSING_DATA_POLICY = (
    "drop_or_mark_incomplete: rows missing any required core signal bin get NaN "
    "scores (marked incomplete); missing VaR/CVaR stays NULL; nothing is zero-filled."
)

# Declared signal families (columns are the approved bin columns, from the
# backfill signal definitions). Values are -1/0/1 bins.
SIGNAL_FAMILIES: dict[str, list[str]] = {
    "oscillators": [
        "m_osc_macd_crossover_bin", "m_osc_cci_bin", "m_osc_adx_bin",
        "m_osc_uo_bin", "m_osc_ao_bin", "m_osc_trix_bin",
    ],
    "momentum": [
        "m_mom_roc_bin", "m_mom_williams_%_bin", "m_mom_smi_bin",
        "m_mom_cmo_bin", "m_mom_mom_bin",
    ],
    "tvv": [
        "m_tvv_obv_1d_binary", "d_tvv_sma9_18", "d_tvv_ema9_18",
        "d_tvv_sma21_108", "d_tvv_ema21_108", "m_tvv_cmf",
    ],
    "ratios": [
        "m_rat_alpha_bin", "d_rat_beta_bin", "v_rat_sharpe_bin",
        "v_rat_sortino_bin", "v_rat_teynor_bin", "v_rat_common_sense_bin",
        "v_rat_information_bin", "v_rat_win_loss_bin", "m_rat_win_rate_bin",
        "m_rat_ror_bin", "d_rat_pain_bin",
    ],
    "metrics": [
        "m_pct_1d_signal", "d_pct_cum_ret_signal", "d_met_ath_month_signal",
        "d_market_cap_signal", "d_met_coin_age_y_signal",
    ],
}

# Core families (full historical coverage in cp_backtest) — used to score rows.
CORE_FAMILIES: list[str] = ["oscillators", "momentum", "tvv", "ratios"]

# Optional families (partial/no history today; measured for coverage impact).
OPTIONAL_FAMILIES: list[str] = ["metrics"]


def core_bin_columns() -> list[str]:
    cols: list[str] = []
    for fam in CORE_FAMILIES:
        cols.extend(SIGNAL_FAMILIES[fam])
    return cols


def all_bin_columns() -> list[str]:
    cols: list[str] = []
    for fam in SIGNAL_FAMILIES:
        cols.extend(SIGNAL_FAMILIES[fam])
    return cols


def family_by_family() -> dict[str, dict]:
    return {
        "core": {f: SIGNAL_FAMILIES[f] for f in CORE_FAMILIES},
        "optional": {f: SIGNAL_FAMILIES[f] for f in OPTIONAL_FAMILIES},
    }
