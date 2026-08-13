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

# PIT_APPROX universe metadata — exact label and limitations.
# It is an OHLCV-OBSERVED ELIGIBILITY universe, NOT a reconstructed historical CMC
# top-1000 universe, and must never be presented as one (no historical rank or
# market-cap eligibility claim).
UNIVERSE_META = {
    "universe_method": "PIT_APPROX",
    "description": (
        "OHLCV-observed eligibility universe. NOT a reconstructed historical CMC "
        "top-1000 universe; makes NO historical rank or market-cap eligibility claim."
    ),
    "source_table": "1K_coins_ohlcv (cp_backtest)",
    "date_coverage": "2013-04-28 .. 2026-08-08",
    "interval_rule": (
        "asset eligible on date d iff first_seen(ohlcv) <= d <= last_seen(ohlcv)"
    ),
    "sparse_gap_limitation": (
        "interval membership may include dates where the asset has no OHLCV row "
        "(sparse gaps); it does not assert the asset was listed/ranked on those dates"
    ),
    "upgrade_path": "CMC_SNAPSHOT (dated CMC listing snapshots) when ingested",
    "no_historical_rank_or_mcap_claim": True,
}

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

# Measured coverage (read-only cp_backtest, distinct slug,date intersections):
#   per-family      ~1.19M each; metrics only 96,811 (113 dates -> history bottleneck)
#   all-8          94,714   (-92%)  -> too destructive: do NOT require all eight
#   core-4         1,177,746 (98.8%) -> least destructive defensible rule (CHOSEN)
#   core-4+metrics 95,713  (-92%)  -> metrics optional until history is reconstructed
COVERAGE_MEASURED = {
    "as_of": "2026-08-08",
    "per_family_distinct_slug_date": {
        "oscillators": 1187577, "momentum": 1187473, "metrics": 96811,
        "tvv": 1187577, "ratios": 1194961, "candlestick": 1186577,
        "dow": 1187577, "price_levels": 1187577,
    },
    "intersection_distinct_slug_date": {
        "all_8": 94714, "core_4": 1177746, "core_4_plus_metrics": 95713,
    },
    "decision": (
        "Required core = oscillators, momentum, tvv, ratios (intersection 1,177,746 = "
        "98.8% of the ~1.19M signal universe). All-8 and core+metrics collapse to ~95k "
        "because FE_METRICS_SIGNAL only has ~113 historical dates; do not require all "
        "eight until metrics history is reconstructed."
    ),
}


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
