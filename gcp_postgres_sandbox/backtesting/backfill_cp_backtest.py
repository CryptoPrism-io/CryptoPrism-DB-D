"""
Retroactive Backfill Script for cp_backtest Database
=====================================================
Rebuilds ALL FE_ tables in cp_backtest from historical OHLCV data.

The daily pipeline was only saving the latest snapshot. This script
recomputes ALL indicators for every historical date in 1K_coins_ohlcv
and populates cp_backtest with the full history.

Usage:
    python gcp_postgres_sandbox/backtesting/backfill_cp_backtest.py

Phases:
    1. Diagnose current cp_backtest state
    2. Backfill FE_MOMENTUM + FE_MOMENTUM_SIGNALS
    3. Backfill FE_OSCILLATOR + FE_OSCILLATORS_SIGNALS
    4. Backfill FE_TVV + FE_TVV_SIGNALS
    5. Backfill FE_PCT_CHANGE
    6. Backfill FE_RATIOS + FE_RATIOS_SIGNALS
    7. Backfill FE_METRICS + FE_METRICS_SIGNAL
    8. Backfill FE_DMV_ALL + FE_DMV_SCORES
"""

import os
import sys
import time
import logging
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv

# --- Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("backfill.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

if not os.getenv("GITHUB_ACTIONS"):
    env_file = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
    if os.path.exists(env_file):
        load_dotenv(env_file)
    elif os.path.exists('.env'):
        load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "dbcp")
DB_NAME_BT = os.getenv("DB_NAME_BT", "cp_backtest")

missing_vars = [v for v in ["DB_HOST", "DB_USER", "DB_PASSWORD"] if not os.getenv(v)]
if missing_vars:
    logger.error(f"Missing env vars: {', '.join(missing_vars)}")
    raise SystemExit("Missing required credentials.")

# --- Add TA modules to path for imports ---
TA_DIR = os.path.join(os.path.dirname(__file__), '..', 'technical_analysis')
sys.path.insert(0, os.path.abspath(TA_DIR))

CHUNK_SIZE = 10000


def create_engine_live():
    return create_engine(
        f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
    )


def create_engine_backtest():
    return create_engine(
        f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}'
    )


def truncate_and_insert(df, table_name, engine):
    """TRUNCATE table then INSERT all rows in chunks."""
    logger.info(f"  Writing {len(df)} rows to {table_name}...")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
            conn.commit()
        df.to_sql(table_name, con=engine, if_exists='append',
                  index=False, method='multi', chunksize=CHUNK_SIZE)
        logger.info(f"  {table_name}: {len(df)} rows written.")
    except Exception as e:
        logger.error(f"  Error writing {table_name}: {e}")
        raise


def fetch_all_ohlcv(engine):
    """Fetch ALL OHLCV data (no time filter) joined with current top 1000."""
    query = """
        SELECT o.*
        FROM "public"."1K_coins_ohlcv" o
        INNER JOIN "public"."crypto_listings_latest_1000" c
            ON o."slug" = c."slug"
        WHERE c."cmc_rank" <= 1000
        ORDER BY o."slug", o."timestamp"
    """
    logger.info("Fetching ALL OHLCV data (no time filter)...")
    df = pd.read_sql_query(query, engine)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    logger.info(f"  Fetched {len(df)} rows, "
                f"{df['slug'].nunique()} coins, "
                f"{df['timestamp'].nunique()} unique dates "
                f"({df['timestamp'].min().date()} to {df['timestamp'].max().date()})")
    return df


# ============================================================
# PHASE 1: DIAGNOSE
# ============================================================
def phase_diagnose(engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 1: Diagnosing cp_backtest state")
    logger.info("=" * 60)

    tables = [
        "FE_MOMENTUM", "FE_MOMENTUM_SIGNALS",
        "FE_OSCILLATOR", "FE_OSCILLATORS_SIGNALS",
        "FE_TVV", "FE_TVV_SIGNALS",
        "FE_PCT_CHANGE",
        "FE_RATIOS", "FE_RATIOS_SIGNALS",
        "FE_METRICS", "FE_METRICS_SIGNAL",
        "FE_DMV_ALL", "FE_DMV_SCORES",
    ]
    for tbl in tables:
        try:
            q = f'SELECT COUNT(*) as cnt, COUNT(DISTINCT "timestamp") as dates FROM "{tbl}"'
            result = pd.read_sql_query(q, engine_bt)
            cnt = result['cnt'].iloc[0]
            dates = result['dates'].iloc[0]
            logger.info(f"  {tbl:30s} | {cnt:>8} rows | {dates:>5} distinct dates")
        except Exception:
            logger.info(f"  {tbl:30s} | TABLE NOT FOUND")


# ============================================================
# PHASE 2: MOMENTUM
# ============================================================
def phase_momentum(ohlcv_df, engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 2: Backfilling FE_MOMENTUM + FE_MOMENTUM_SIGNALS")
    logger.info("=" * 60)

    from gcp_dmv_mom import (
        calculate_pct_change, calculate_rsi, calculate_sma,
        calculate_roc, calculate_williams_r, calculate_smi,
        calculate_cmo, calculate_mom, calculate_tsi,
        generate_binary_signals_momentum
    )

    df = ohlcv_df.copy()
    df = (df.pipe(calculate_pct_change)
            .pipe(calculate_rsi)
            .pipe(calculate_sma)
            .pipe(calculate_roc)
            .pipe(calculate_williams_r)
            .pipe(calculate_smi)
            .pipe(calculate_cmo)
            .pipe(calculate_mom)
            .pipe(calculate_tsi)
            .pipe(generate_binary_signals_momentum))

    momentum_cols = [
        "id", "slug", "name", "timestamp",
        "open", "high", "low", "close", "volume", "market_cap",
        "m_pct_1d", "m_mom_rsi_9", "m_mom_rsi_18", "m_mom_rsi_27",
        "m_mom_rsi_54", "m_mom_rsi_108", "sma_14",
        "m_mom_roc", "m_mom_williams_%", "m_mom_smi",
        "m_mom_cmo", "m_mom_mom", "m_mom_tsi"
    ]
    signals_cols = [
        "id", "slug", "name", "timestamp",
        "m_mom_roc_bin", "m_mom_williams_%_bin", "m_mom_smi_bin",
        "m_mom_cmo_bin", "m_mom_mom_bin"
    ]

    # Keep only columns that exist
    mom_cols_avail = [c for c in momentum_cols if c in df.columns]
    sig_cols_avail = [c for c in signals_cols if c in df.columns]

    momentum_df = df[mom_cols_avail].replace([np.inf, -np.inf], np.nan)
    signals_df = df[sig_cols_avail].replace([np.inf, -np.inf], np.nan)

    truncate_and_insert(momentum_df, "FE_MOMENTUM", engine_bt)
    truncate_and_insert(signals_df, "FE_MOMENTUM_SIGNALS", engine_bt)
    logger.info("  Momentum backfill complete.")


# ============================================================
# PHASE 3: OSCILLATORS
# ============================================================
def phase_oscillators(ohlcv_df, engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 3: Backfilling FE_OSCILLATOR + FE_OSCILLATORS_SIGNALS")
    logger.info("=" * 60)

    from gcp_dmv_osc import (
        calculate_pct_change as osc_pct_change,
        calculate_cum_ret, calculate_macd, calculate_cci,
        calculate_adx, calculate_ultimate_oscillator,
        calculate_awesome_oscillator, calculate_trix,
        generate_binary_signals_oscillators, rename_columns_for_db
    )

    df = ohlcv_df.copy()
    df = (df.pipe(osc_pct_change)
            .pipe(calculate_cum_ret)
            .pipe(calculate_macd)
            .pipe(calculate_cci)
            .pipe(calculate_adx)
            .pipe(calculate_ultimate_oscillator)
            .pipe(calculate_awesome_oscillator)
            .pipe(calculate_trix)
            .pipe(generate_binary_signals_oscillators))

    # The osc module renames columns in push_to_db; replicate here
    df = rename_columns_for_db(df)

    osc_cols = [
        "id", "slug", "name", "timestamp",
        "open", "high", "low", "close", "volume", "market_cap",
        "m_pct_1d", "d_pct_cum_ret",
        "EMA_12", "EMA_26", "MACD", "Signal",
        "TP", "SMA_TP", "MAD", "CCI",
        "TR", "+DM", "-DM", "Smoothed_TR", "Smoothed_+DM", "Smoothed_-DM",
        "+DI", "-DI", "DX", "ADX",
        "prev_close", "BP",
        "Avg_BP_short", "Avg_TR_short",
        "Avg_BP_intermediate", "Avg_TR_intermediate",
        "Avg_BP_long", "Avg_TR_long", "UO",
        "MP", "SMA_5", "SMA_34", "AO",
        "EMA1", "EMA2", "EMA3", "TRIX"
    ]
    sig_cols = [
        "id", "slug", "name", "timestamp",
        "m_osc_macd_crossover_bin", "m_osc_cci_bin",
        "m_osc_adx_bin", "m_osc_uo_bin",
        "m_osc_ao_bin", "m_osc_trix_bin"
    ]

    osc_avail = [c for c in osc_cols if c in df.columns]
    sig_avail = [c for c in sig_cols if c in df.columns]

    osc_df = df[osc_avail].replace([np.inf, -np.inf], np.nan)
    sig_df = df[sig_avail].replace([np.inf, -np.inf], np.nan)

    truncate_and_insert(osc_df, "FE_OSCILLATOR", engine_bt)
    truncate_and_insert(sig_df, "FE_OSCILLATORS_SIGNALS", engine_bt)
    logger.info("  Oscillators backfill complete.")


# ============================================================
# PHASE 4: TVV (Trend/Volume/Volatility)
# ============================================================
def phase_tvv(ohlcv_df, engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 4: Backfilling FE_TVV + FE_TVV_SIGNALS")
    logger.info("=" * 60)

    from gcp_dmv_tvv import (
        calculate_obv, calculate_moving_averages,
        calculate_atr, calculate_channels,
        calculate_vwap, calculate_cmf,
        generate_binary_signals
    )

    df = ohlcv_df.copy()
    df = (df.pipe(calculate_obv)
            .pipe(calculate_moving_averages)
            .pipe(calculate_atr)
            .pipe(calculate_channels)
            .pipe(calculate_vwap)
            .pipe(calculate_cmf)
            .pipe(generate_binary_signals))

    tvv_cols = [
        "id", "slug", "name", "timestamp",
        "open", "high", "low", "close", "volume", "market_cap",
        "obv", "m_tvv_obv_1d",
        "SMA9", "SMA18", "EMA9", "EMA18",
        "SMA21", "SMA108", "EMA21", "EMA108",
        "prev_close", "tr1", "tr2", "tr3", "TR", "ATR",
        "Keltner_Upper", "Keltner_Lower",
        "Donchian_Upper", "Donchian_Lower",
        "typical_price", "cum_price_volume", "cum_volume", "VWAP",
        "ADL", "cum_adl", "CMF"
    ]
    sig_cols = [
        "id", "slug", "timestamp",
        "m_tvv_obv_1d_binary", "d_tvv_sma9_18", "d_tvv_ema9_18",
        "d_tvv_sma21_108", "d_tvv_ema21_108", "m_tvv_cmf"
    ]

    tvv_avail = [c for c in tvv_cols if c in df.columns]
    sig_avail = [c for c in sig_cols if c in df.columns]

    tvv_df = df[tvv_avail].replace([np.inf, -np.inf], np.nan)
    sig_df = df[sig_avail].replace([np.inf, -np.inf], np.nan)

    truncate_and_insert(tvv_df, "FE_TVV", engine_bt)
    truncate_and_insert(sig_df, "FE_TVV_SIGNALS", engine_bt)
    logger.info("  TVV backfill complete.")


# ============================================================
# PHASE 5: PCT (Percentage Change / Risk Metrics)
# ============================================================
def phase_pct(ohlcv_df, engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 5: Backfilling FE_PCT_CHANGE")
    logger.info("=" * 60)

    from gcp_dmv_pct import (
        calculate_pct_change as pct_pct_change,
        calculate_var_cvar, calculate_volume_pct_change,
        clean_data
    )

    df = ohlcv_df.copy()
    # Run pipeline WITHOUT filter_latest_data (keep all dates)
    df = (df.pipe(pct_pct_change)
            .pipe(calculate_var_cvar)
            .pipe(calculate_volume_pct_change)
            .pipe(clean_data))

    pct_cols = [
        "id", "slug", "name", "timestamp",
        "m_pct_1d", "d_pct_cum_ret", "d_pct_var", "d_pct_cvar", "d_pct_vol_1d"
    ]
    pct_avail = [c for c in pct_cols if c in df.columns]
    pct_df = df[pct_avail].replace([np.inf, -np.inf], np.nan)

    truncate_and_insert(pct_df, "FE_PCT_CHANGE", engine_bt)
    logger.info("  PCT backfill complete.")


# ============================================================
# PHASE 6: RATIOS (rolling 28-day window per unique date)
# ============================================================
def phase_ratios(ohlcv_df, engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 6: Backfilling FE_RATIOS + FE_RATIOS_SIGNALS")
    logger.info("=" * 60)

    from gcp_dmv_rat import (
        calculate_pct_change as rat_pct_change,
        calculate_benchmark_returns, calculate_alpha, calculate_beta,
        calculate_omega_ratio, calculate_sharpe_ratio, calculate_sortino_ratio,
        calculate_treynor_ratio, calculate_common_sense_ratio,
        calculate_information_ratio, calculate_winloss_ratio,
        calculate_win_rate, calculate_risk_of_ruin, calculate_gain_to_pain,
        generate_binary_signals_ratios
    )

    df = ohlcv_df.copy()
    df = rat_pct_change(df)

    # Get all unique dates, sorted
    all_dates = sorted(df['timestamp'].unique())
    logger.info(f"  {len(all_dates)} unique dates in OHLCV data.")

    # Process in rolling 28-day windows, stepping by 1 day
    all_ratios = []
    processed = 0

    for target_date in all_dates:
        window_start = target_date - pd.Timedelta(days=28)
        window = df[(df['timestamp'] >= window_start) & (df['timestamp'] <= target_date)].copy()

        if window['timestamp'].nunique() < 5:
            continue  # Need at least 5 days for meaningful ratios

        benchmark_returns = calculate_benchmark_returns(window)
        if benchmark_returns.empty:
            continue
        benchmark_avg_return = benchmark_returns.mean()

        # Calculate beta for all slugs
        beta_values = {}
        for slug, group in window.groupby('slug'):
            if slug != 'bitcoin' and len(group) >= 3:
                beta_series = calculate_beta(group, benchmark_returns)
                beta_values[slug] = beta_series['d_rat_beta']

        # Calculate all ratios per slug
        for slug, group in window.groupby('slug'):
            if slug == 'bitcoin' or len(group) < 3:
                continue
            if slug not in beta_values:
                continue

            try:
                combined = pd.concat([
                    calculate_alpha(group, benchmark_avg_return),
                    calculate_omega_ratio(group, benchmark_returns),
                    calculate_sharpe_ratio(group),
                    calculate_sortino_ratio(group),
                    calculate_treynor_ratio(group, beta_values),
                    calculate_common_sense_ratio(group),
                    calculate_information_ratio(group, benchmark_returns),
                    calculate_winloss_ratio(group),
                    calculate_win_rate(group),
                    calculate_risk_of_ruin(group),
                    calculate_gain_to_pain(group),
                ])
                row = pd.DataFrame(combined).transpose()
                row['slug'] = slug
                row['name'] = group['name'].iloc[0]
                row['timestamp'] = target_date
                row['d_rat_beta'] = beta_values[slug]
                all_ratios.append(row)
            except Exception:
                continue

        processed += 1
        if processed % 100 == 0:
            logger.info(f"  Processed {processed}/{len(all_dates)} dates, "
                        f"{len(all_ratios)} ratio rows so far...")

    if not all_ratios:
        logger.warning("  No ratio data computed. Skipping.")
        return

    ratios_df = pd.concat(all_ratios, ignore_index=True)
    ratios_df = generate_binary_signals_ratios(ratios_df)

    ratios_cols = [
        'slug', 'name', 'timestamp',
        'm_rat_alpha', 'd_rat_beta', 'm_rat_omega', 'v_rat_sharpe',
        'v_rat_sortino', 'v_rat_teynor', 'v_rat_common_sense',
        'v_rat_information', 'v_rat_win_loss', 'm_rat_win_rate',
        'm_rat_ror', 'd_rat_pain'
    ]
    signals_cols = [
        'slug', 'name', 'timestamp',
        'm_rat_alpha_bin', 'd_rat_beta_bin', 'v_rat_sharpe_bin',
        'v_rat_sortino_bin', 'v_rat_teynor_bin', 'v_rat_common_sense_bin',
        'v_rat_information_bin', 'v_rat_win_loss_bin', 'm_rat_win_rate_bin',
        'm_rat_ror_bin', 'd_rat_pain_bin'
    ]

    r_avail = [c for c in ratios_cols if c in ratios_df.columns]
    s_avail = [c for c in signals_cols if c in ratios_df.columns]

    r_df = ratios_df[r_avail].replace([np.inf, -np.inf], np.nan)
    s_df = ratios_df[s_avail].replace([np.inf, -np.inf], np.nan)

    truncate_and_insert(r_df, "FE_RATIOS", engine_bt)
    truncate_and_insert(s_df, "FE_RATIOS_SIGNALS", engine_bt)
    logger.info("  Ratios backfill complete.")


# ============================================================
# PHASE 7: METRICS (ATH/ATL, Coin Age, Market Cap Category)
# ============================================================
def phase_metrics(engine_live, engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 7: Backfilling FE_METRICS + FE_METRICS_SIGNAL")
    logger.info("=" * 60)

    # Fetch data (replicating gcp_dmv_met.py logic)
    with engine_live.connect() as conn:
        ohlcv = pd.read_sql_query('SELECT * FROM "1K_coins_ohlcv"', conn)
        listings = pd.read_sql_query("SELECT * FROM crypto_listings_latest_1000", conn)

    df = ohlcv.copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.sort_values(by=['slug', 'timestamp'], inplace=True)
    grouped = df.groupby('slug')

    # Percentage change and cumulative returns
    df['m_pct_1d'] = grouped['close'].pct_change()
    df['d_pct_cum_ret'] = (1 + df['m_pct_1d']).groupby(df['slug']).cumprod() - 1

    # ATH / ATL
    df['v_met_ath'] = grouped['high'].cummax()
    df['v_met_atl'] = grouped['low'].cummin()

    # ATH/ATL dates (same approach as gcp_dmv_met.py)
    df.set_index('timestamp', inplace=True)
    df['ath_date'] = df.groupby('slug')['high'].transform(lambda x: x.idxmax())
    df['atl_date'] = df.groupby('slug')['low'].transform(lambda x: x.idxmin())
    df.reset_index(inplace=True)

    df['ath_date'] = pd.to_datetime(df['ath_date'])
    df['atl_date'] = pd.to_datetime(df['atl_date'])

    # Days since ATH/ATL
    current_date = pd.Timestamp.now().tz_localize(None)
    df['ath_date'] = df['ath_date'].dt.tz_localize(None)
    df['atl_date'] = df['atl_date'].dt.tz_localize(None)
    df['d_met_ath_days'] = (current_date - df['ath_date']).dt.days
    df['d_met_atl_days'] = (current_date - df['atl_date']).dt.days
    df['d_met_ath_week'] = df['d_met_ath_days'] // 7
    df['d_met_ath_month'] = df['d_met_ath_days'] // 30
    df['d_met_atl_week'] = df['d_met_atl_days'] // 7
    df['d_met_atl_month'] = df['d_met_atl_days'] // 30

    # Keep latest per slug (same as gcp_dmv_met.py line 154)
    met_latest = df.loc[df.groupby('slug')['timestamp'].idxmax()].copy()
    met_latest = pd.merge(
        met_latest,
        listings[['slug', 'date_added', 'last_updated']],
        on='slug', how='inner'
    )

    met_latest['date_added'] = pd.to_datetime(met_latest['date_added'], errors='coerce').dt.tz_localize(None)
    met_latest['last_updated'] = pd.to_datetime(met_latest['last_updated'], errors='coerce').dt.tz_localize(None)
    met_latest = met_latest.dropna(subset=['date_added', 'last_updated'])

    current_date = pd.Timestamp.now().normalize()
    met_latest['d_met_coin_age_d'] = (current_date - met_latest['date_added']).dt.days
    met_latest['d_met_coin_age_m'] = met_latest['d_met_coin_age_d'] // 30
    met_latest['d_met_coin_age_y'] = met_latest['d_met_coin_age_d'] // 365

    def categorize_market_cap(mc):
        if mc >= 1e12: return '1T-100B'
        elif mc >= 1e11: return '100B-10B'
        elif mc >= 1e10: return '10B-1B'
        elif mc >= 1e9: return '1B-100M'
        elif mc >= 1e7: return '100M-1M'
        else: return 'Under1M'

    met_latest['m_cap_cat'] = met_latest['market_cap'].apply(categorize_market_cap)
    met_latest = met_latest.drop_duplicates(subset=['slug'], keep='first')
    metrics = met_latest.drop(met_latest.columns[4:10], axis=1)

    # Signals
    metrics['m_pct_1d_signal'] = np.where(metrics['m_pct_1d'] > 0, 1, -1)
    metrics['d_pct_cum_ret_signal'] = np.where(metrics['d_pct_cum_ret'] > 0, 1, -1)
    metrics['d_met_ath_month_signal'] = ((100 - metrics['d_met_ath_month']) * 2) / 100

    market_cap_signal = {
        '100M-1M': 0.25, '1B-100M': 0.4, 'Under1M': 0.1,
        '10B-1B': 0.5, '1T-100B': 1, '100B-10B': 0.75
    }
    metrics['d_market_cap_signal'] = metrics['m_cap_cat'].map(market_cap_signal)
    metrics['d_met_coin_age_y_signal'] = np.where(
        metrics['d_met_coin_age_y'] < 1, 0,
        np.where(metrics['d_met_coin_age_y'] >= 1,
                 1 - (1 / metrics['d_met_coin_age_y']), 0)
    )

    metrics_signal = metrics.drop(metrics.columns[3:22], axis=1)

    truncate_and_insert(metrics, "FE_METRICS", engine_bt)
    truncate_and_insert(metrics_signal, "FE_METRICS_SIGNAL", engine_bt)
    logger.info("  Metrics backfill complete.")


# ============================================================
# PHASE 8: CORE (DMV_ALL + DMV_SCORES from signal tables)
# ============================================================
def phase_core(engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 8: Backfilling FE_DMV_ALL + FE_DMV_SCORES")
    logger.info("=" * 60)

    # Join ALL signal tables from cp_backtest (now populated with history)
    query = """
    SELECT *
    FROM "FE_OSCILLATORS_SIGNALS" AS o
    FULL OUTER JOIN "FE_MOMENTUM_SIGNALS" AS m USING (slug, timestamp)
    FULL OUTER JOIN "FE_METRICS_SIGNAL" AS me USING (slug, timestamp)
    FULL OUTER JOIN "FE_TVV_SIGNALS" AS t USING (slug, timestamp)
    FULL OUTER JOIN "FE_RATIOS_SIGNALS" AS r USING (slug, timestamp)
    """

    logger.info("  Joining signal tables from cp_backtest...")
    df = pd.read_sql_query(query, engine_bt)

    if df.empty:
        logger.warning("  No signal data found. Skipping core.")
        return

    # Dedup columns, filter rows, fill NaN
    df = (
        df.loc[:, ~df.columns.duplicated()]
          .pipe(lambda d: d[d['slug'].eq('bitcoin') | d.drop(columns='slug').notna().all(axis=1)])
          .fillna(0)
    )

    # Count bullish/bearish/neutral per row
    for col in ['bullish', 'bearish', 'neutral']:
        df[col] = 0

    signal_cols = df.iloc[:, 4:]  # Skip slug, timestamp, id, name
    for index, row in signal_cols.iterrows():
        df.at[index, 'bullish'] = (row == 1).sum()
        df.at[index, 'bearish'] = (row == -1).sum()
        df.at[index, 'neutral'] = (row == 0).sum()

    # DMV Scores
    d_cols = [c for c in df.columns if c.startswith('d_')]
    m_cols = [c for c in df.columns if c.startswith('m_')]
    v_cols = [c for c in df.columns if c.startswith('v_')]

    durability = df[['slug'] + d_cols]
    momentum = df[['slug'] + m_cols]
    valuation = df[['slug'] + v_cols]

    d_score = durability.drop('slug', axis=1).sum(axis=1) / max(durability.shape[1] - 1, 1) * 100
    m_score = momentum.drop('slug', axis=1).sum(axis=1) / max(momentum.shape[1] - 1, 1) * 100
    v_score = valuation.drop('slug', axis=1).sum(axis=1) / max(valuation.shape[1] - 1, 1) * 100

    dmv_scores = pd.DataFrame({
        'slug': df['slug'],
        'timestamp': df['timestamp'],
        'Durability_Score': d_score,
        'Momentum_Score': m_score,
        'Valuation_Score': v_score
    })

    truncate_and_insert(df, "FE_DMV_ALL", engine_bt)
    truncate_and_insert(dmv_scores, "FE_DMV_SCORES", engine_bt)
    logger.info("  Core backfill complete.")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    overall_start = time.time()
    logger.info("BACKFILL CP_BACKTEST - Starting full rebuild")

    engine_live = create_engine_live()
    engine_bt = create_engine_backtest()

    # Phase 1: Diagnose
    phase_diagnose(engine_bt)

    # Fetch OHLCV once, reuse for phases 2-6
    ohlcv_df = fetch_all_ohlcv(engine_live)

    # Phase 2-5: Time-series indicators (full OHLCV history)
    phase_momentum(ohlcv_df, engine_bt)
    phase_oscillators(ohlcv_df, engine_bt)
    phase_tvv(ohlcv_df, engine_bt)
    phase_pct(ohlcv_df, engine_bt)

    # Phase 6: Ratios (rolling 28-day window for each date)
    phase_ratios(ohlcv_df, engine_bt)

    # Phase 7: Metrics (uses separate data sources)
    phase_metrics(engine_live, engine_bt)

    # Phase 8: Core (joins signal tables already populated above)
    phase_core(engine_bt)

    # Final diagnosis
    logger.info("")
    logger.info("FINAL STATE:")
    phase_diagnose(engine_bt)

    engine_live.dispose()
    engine_bt.dispose()

    elapsed = (time.time() - overall_start) / 60
    logger.info(f"BACKFILL COMPLETE in {elapsed:.1f} minutes.")
