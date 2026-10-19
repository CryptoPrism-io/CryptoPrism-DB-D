"""
Retroactive Backfill Script for cp_backtest Database
=====================================================
Rebuilds ALL FE_ tables in cp_backtest from historical OHLCV data.

The daily pipeline was only saving the latest snapshot. This script
recomputes ALL indicators for every historical date in 1K_coins_ohlcv
and populates cp_backtest with the full history.

Every coin in 1K_coins_ohlcv is included, not just today's top 1000, so
cp_backtest keeps the history of coins that later dropped out (no survivorship bias).

Usage:
    python gcp_postgres_sandbox/backtesting/backfill_cp_backtest.py
    python gcp_postgres_sandbox/backtesting/backfill_cp_backtest.py --only momentum
    python gcp_postgres_sandbox/backtesting/backfill_cp_backtest.py --dry-run --slugs bitcoin,ethereum

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
import argparse
import numpy as np
import pandas as pd
from sqlalchemy import bindparam, create_engine, text
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

def create_engine_live():
    return create_engine(
        f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
    )


def create_engine_backtest():
    return create_engine(
        f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}'
    )


# Set by --dry-run: compute everything, log what would be written, write nothing.
DRY_RUN = False


def table_columns(engine, table_name):
    """Column names of a cp_backtest table in table order ([] if the table doesn't exist)."""
    q = text("SELECT column_name FROM information_schema.columns "
             "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position")
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(q, {"t": table_name})]


def _ts_key(s):
    """Timestamp column as UTC nanoseconds, so tz-aware and naive timestamps join."""
    return pd.to_datetime(s, utc=True).astype("int64")


def _carry_over(conn, df, table_name, missing):
    """Copy existing values of columns the backfill doesn't compute (e.g. FE_DMV_ALL's
    candlestick/Dow bins written by the daily DMV) into df by (slug, timestamp),
    so the rewrite keeps them instead of NULLing them."""
    keep = [c for c in missing
            if conn.execute(text(f'SELECT COUNT("{c}") FROM "{table_name}"')).scalar()]
    if not keep or not {"slug", "timestamp"} <= set(df.columns):
        return df, []
    col_sql = ", ".join(f'"{c}"' for c in keep)
    old = pd.read_sql_query(text(f'SELECT "slug", "timestamp", {col_sql} FROM "{table_name}"'), conn)
    old["_ts"] = _ts_key(old.pop("timestamp"))
    old = old.drop_duplicates(["slug", "_ts"], keep="last")
    df = df.assign(_ts=_ts_key(df["timestamp"])).merge(old, on=["slug", "_ts"], how="left").drop(columns="_ts")
    return df, keep


# Set by --allow-shrink: let a guarded rewrite drop existing (slug, timestamp) keys.
ALLOW_SHRINK = False
# A guarded rewrite must keep at least this share of the table's current rows.
SHRINK_MIN_ROWS = 0.95


def _prefer_existing(conn, df, table_name, cols):
    """Union df with the table's current rows. Where both have a (slug, timestamp) key the
    existing non-NULL values win (they are the real daily snapshots); df only fills their
    NULLs and adds the keys the table doesn't have yet."""
    col_sql = ", ".join(f'"{c}"' for c in cols)
    old = pd.read_sql_query(text(f'SELECT {col_sql} FROM "{table_name}"'), conn)
    if old.empty:
        return df, 0
    new = df[[c for c in cols if c in df.columns]].copy()
    old["_ts"] = _ts_key(old["timestamp"])
    new["_ts"] = _ts_key(new["timestamp"])
    old = old.drop_duplicates(["slug", "_ts"], keep="last").set_index(["slug", "_ts"])
    new = new.drop_duplicates(["slug", "_ts"], keep="last").set_index(["slug", "_ts"])
    overlap = int(old.index.isin(new.index).sum())
    out = old.combine_first(new).reset_index().drop(columns="_ts")
    return out, overlap


def _guard_no_shrink(conn, out, table_name):
    """Refuse a rewrite that would drop any existing (slug, timestamp) key, or leave fewer
    than SHRINK_MIN_ROWS of the current rows, unless --allow-shrink is given."""
    old = pd.read_sql_query(text(f'SELECT "slug", "timestamp" FROM "{table_name}"'), conn)
    if old.empty:
        return
    old_keys = pd.MultiIndex.from_arrays([old["slug"], _ts_key(old["timestamp"])])
    new_keys = pd.MultiIndex.from_arrays([out["slug"], _ts_key(out["timestamp"])])
    lost = int((~old_keys.isin(new_keys)).sum())
    msg = (f"{table_name}: {len(out):,} rows vs {len(old):,} now, "
           f"{lost:,} existing (slug, timestamp) keys would be dropped")
    if lost == 0 and len(out) >= SHRINK_MIN_ROWS * len(old):
        logger.info(f"  Shrink guard OK: {msg}")
    elif ALLOW_SHRINK:
        logger.warning(f"  Shrink guard overridden by --allow-shrink: {msg}")
    elif DRY_RUN:
        logger.warning(f"  Shrink guard WOULD REFUSE this write: {msg}")
    else:
        raise RuntimeError(f"Refusing to shrink {msg}. Re-run with --allow-shrink if intended.")


def truncate_and_insert(df, table_name, engine, prefer_existing=False, guard=False):
    """Replace a table's rows with df in ONE transaction, in safe chunks for pg8000.

    Writes exactly the columns the table has, so indicators added to the daily DMV
    scripts get backfilled too. Table columns df doesn't compute keep their existing
    values (matched on slug + timestamp); if they hold no data they stay NULL.
    prefer_existing: keep the table's existing rows and non-NULL values, df fills the rest.
    guard: refuse the write if it would drop existing keys (see _guard_no_shrink).
    If any insert fails the TRUNCATE rolls back and the old rows stay.
    """
    cols = table_columns(engine, table_name)
    if not cols:
        if DRY_RUN:
            logger.warning(f"  {table_name} does not exist; would be created (dry run).")
            return
        raise LookupError(f"{table_name} does not exist")
    try:
        with engine.begin() as conn:
            if prefer_existing:
                df, overlap = _prefer_existing(conn, df, table_name, cols)
                logger.info(f"  {table_name}: kept existing values for {overlap:,} (slug, timestamp) "
                            f"keys already in the table; computed rows fill the rest.")
            missing = [c for c in cols if c not in df.columns]
            kept = []
            if missing:
                df, kept = _carry_over(conn, df, table_name, missing)
                if kept:
                    logger.info(f"  {table_name}: keeping existing values of {len(kept)} column(s) "
                                f"the backfill doesn't compute: {kept}")
                still_null = [c for c in missing if c not in kept]
                if still_null:
                    logger.warning(f"  {table_name}: {len(still_null)} column(s) not computed and "
                                   f"currently empty, will stay NULL: {still_null}")
            present = [c for c in cols if c in df.columns]
            out = df[present].replace([np.inf, -np.inf], np.nan)
            if guard:
                _guard_no_shrink(conn, out, table_name)
            logger.info(f"  Writing {len(out)} rows x {len(present)} columns to {table_name}"
                        f"{' (dry run: nothing written)' if DRY_RUN else ''}...")
            if DRY_RUN:
                return
            # pg8000 limit: 65535 params per batch. Safe chunk = 65535 / num_columns
            safe_chunk = max(1, 65000 // len(present))
            conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
            out.to_sql(table_name, con=conn, if_exists='append',
                       index=False, method='multi', chunksize=safe_chunk)
        logger.info(f"  {table_name}: {len(out)} rows written (chunks of {safe_chunk}).")
    except Exception as e:
        logger.error(f"  Error writing {table_name} (rolled back, old rows kept): {e}")
        raise


def fetch_all_ohlcv(engine, slugs=None):
    """Fetch ALL OHLCV data (no time filter) for every coin, or only `slugs` if given."""
    query = 'SELECT o.* FROM "public"."1K_coins_ohlcv" o'
    params = {}
    if slugs:
        query += ' WHERE o."slug" IN :slugs'
        params = {"slugs": list(slugs)}
    query = text(query + ' ORDER BY o."slug", o."timestamp"')
    if slugs:
        query = query.bindparams(bindparam("slugs", expanding=True))
    logger.info(f"Fetching ALL OHLCV data (no time filter){f' for {len(slugs)} slugs' if slugs else ''}...")
    with engine.connect() as conn:
        df = pd.read_sql_query(query, conn, params=params)
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

    # Each table takes the columns it has (see truncate_and_insert)
    truncate_and_insert(df, "FE_MOMENTUM", engine_bt)
    truncate_and_insert(df, "FE_MOMENTUM_SIGNALS", engine_bt)
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
        calculate_supertrend, calculate_aroon,
        generate_binary_signals_oscillators, rename_columns_for_db
    )

    df = ohlcv_df.copy()
    # Same order as gcp_dmv_osc.main(); the signals step needs Supertrend + Aroon
    df = (df.pipe(osc_pct_change)
            .pipe(calculate_cum_ret)
            .pipe(calculate_macd)
            .pipe(calculate_cci)
            .pipe(calculate_adx)
            .pipe(calculate_ultimate_oscillator)
            .pipe(calculate_awesome_oscillator)
            .pipe(calculate_trix)
            .pipe(calculate_supertrend)
            .pipe(calculate_aroon)
            .pipe(generate_binary_signals_oscillators))

    # The osc module renames columns in push_to_db; replicate here
    df = rename_columns_for_db(df)

    truncate_and_insert(df, "FE_OSCILLATOR", engine_bt)
    truncate_and_insert(df, "FE_OSCILLATORS_SIGNALS", engine_bt)
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
        calculate_vwap, calculate_cmf, calculate_bollinger_bands,
        generate_binary_signals
    )

    df = ohlcv_df.copy()
    # Same order as gcp_dmv_tvv.main()
    df = (df.pipe(calculate_obv)
            .pipe(calculate_moving_averages)
            .pipe(calculate_atr)
            .pipe(calculate_channels)
            .pipe(calculate_vwap)
            .pipe(calculate_cmf)
            .pipe(calculate_bollinger_bands)
            .pipe(generate_binary_signals))

    truncate_and_insert(df, "FE_TVV", engine_bt)
    truncate_and_insert(df, "FE_TVV_SIGNALS", engine_bt)
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

    truncate_and_insert(df, "FE_PCT_CHANGE", engine_bt)
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

    truncate_and_insert(ratios_df, "FE_RATIOS", engine_bt)
    truncate_and_insert(ratios_df, "FE_RATIOS_SIGNALS", engine_bt)
    logger.info("  Ratios backfill complete.")


# ============================================================
# PHASE 7: METRICS (ATH/ATL, Coin Age, Market Cap Category)
# ============================================================
MARKET_CAP_SIGNAL = {
    '100M-1M': 0.25, '1B-100M': 0.4, 'Under1M': 0.1,
    '10B-1B': 0.5, '1T-100B': 1, '100B-10B': 0.75
}


def fetch_date_added(engine_live, engine_bt):
    """slug -> CMC date_added (tz-naive). dbcp's current listings win over cp_backtest's
    accumulated copy (~2.3k slugs), which also covers coins that left the top 1000."""
    out = {}
    for engine in (engine_bt, engine_live):
        with engine.connect() as conn:
            rows = pd.read_sql_query(text('SELECT slug, date_added FROM crypto_listings_latest_1000'), conn)
        rows['date_added'] = pd.to_datetime(rows['date_added'], errors='coerce', utc=True).dt.tz_localize(None)
        out.update(rows.dropna(subset=['date_added']).drop_duplicates('slug', keep='last')
                   .set_index('slug')['date_added'].to_dict())
    return out


def compute_metrics(ohlcv_df, date_added):
    """Point-in-time FE_METRICS + FE_METRICS_SIGNAL for every (slug, timestamp) row.

    Each row equals what gcp_dmv_met.py writes when it runs the morning after that row's
    candle (T = the row's timestamp, run date = T + 1 day), using only data up to T:
      - ATH/ATL: running max/min, dated at the first time it was reached
        (= gcp_dmv_met's idxmax over all history at run time)
      - *_days: (run time - ath_date).days, which equals (T - ath_date).days because
        every candle is stamped 23:59:59
      - coin age: (T.date + 1 day) - date_added, as gcp_dmv_met's now().normalize()
    The daily script drops coins with no date_added. Here they keep their rows, and their
    first OHLCV date stands in for date_added. That's a lower bound on age, as
    1K_coins_ohlcv often starts long after the CMC listing.
    """
    df = ohlcv_df[['id', 'slug', 'timestamp', 'high', 'low', 'close', 'market_cap']].copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df = df.sort_values(['slug', 'timestamp']).reset_index(drop=True)
    by_slug = df.groupby('slug', sort=False)
    ts = df['timestamp'].dt.tz_localize(None)

    df['m_pct_1d'] = by_slug['close'].pct_change()
    df['d_pct_cum_ret'] = (1 + df['m_pct_1d']).groupby(df['slug']).cumprod() - 1

    df['v_met_ath'] = by_slug['high'].cummax()
    df['v_met_atl'] = by_slug['low'].cummin()
    prev_ath = df.groupby('slug', sort=False)['v_met_ath'].shift()
    prev_atl = df.groupby('slug', sort=False)['v_met_atl'].shift()
    new_ath = df['high'].notna() & (prev_ath.isna() | (df['high'] > prev_ath))
    new_atl = df['low'].notna() & (prev_atl.isna() | (df['low'] < prev_atl))
    df['ath_date'] = ts.where(new_ath).groupby(df['slug']).ffill()
    df['atl_date'] = ts.where(new_atl).groupby(df['slug']).ffill()

    df['d_met_ath_days'] = (ts - df['ath_date']).dt.days
    df['d_met_atl_days'] = (ts - df['atl_date']).dt.days
    df['d_met_ath_week'] = df['d_met_ath_days'] // 7
    df['d_met_ath_month'] = df['d_met_ath_days'] // 30
    df['d_met_atl_week'] = df['d_met_atl_days'] // 7
    df['d_met_atl_month'] = df['d_met_atl_days'] // 30

    first_seen = ts.groupby(df['slug']).transform('min')
    df['date_added'] = df['slug'].map(date_added).fillna(first_seen)
    run_date = ts.dt.normalize() + pd.Timedelta(days=1)
    df['last_updated'] = run_date
    df['d_met_coin_age_d'] = (run_date - df['date_added']).dt.days
    df['d_met_coin_age_m'] = df['d_met_coin_age_d'] // 30
    df['d_met_coin_age_y'] = df['d_met_coin_age_d'] // 365

    mc = df['market_cap']
    df['m_cap_cat'] = np.select(
        [mc >= 1e12, mc >= 1e11, mc >= 1e10, mc >= 1e9, mc >= 1e7],
        ['1T-100B', '100B-10B', '10B-1B', '1B-100M', '100M-1M'], default='Under1M')

    df['m_pct_1d_signal'] = np.where(df['m_pct_1d'] > 0, 1, -1)
    df['d_pct_cum_ret_signal'] = np.where(df['d_pct_cum_ret'] > 0, 1, -1)
    df['d_met_ath_month_signal'] = ((100 - df['d_met_ath_month']) * 2) / 100
    df['d_market_cap_signal'] = df['m_cap_cat'].map(MARKET_CAP_SIGNAL)
    age_y = df['d_met_coin_age_y']
    df['d_met_coin_age_y_signal'] = np.where(age_y < 1, 0, np.where(age_y >= 1, 1 - (1 / age_y), 0))

    # gcp_dmv_met.py loses `symbol` (its set_index) so it's NULL in both tables; leaving it
    # out keeps it NULL, which also keeps core's neutral count identical to the daily.
    return df.drop(columns=['high', 'low', 'close'])


def phase_metrics(ohlcv_df, engine_live, engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 7: Backfilling FE_METRICS + FE_METRICS_SIGNAL")
    logger.info("=" * 60)

    date_added = fetch_date_added(engine_live, engine_bt)
    metrics = compute_metrics(ohlcv_df, date_added)
    with_listing = metrics['slug'].isin(date_added).groupby(metrics['slug']).first()
    logger.info(f"  {len(metrics):,} point-in-time rows for {metrics['slug'].nunique():,} coins; "
                f"{int((~with_listing).sum()):,} coins have no CMC date_added "
                f"(first OHLCV date used as a lower bound for coin age).")

    # The existing rows are the real daily snapshots: keep their values and only fill gaps
    # (their signal columns are NULL, as gcp_dmv_met.py writes FE_METRICS before signals).
    truncate_and_insert(metrics, "FE_METRICS", engine_bt, prefer_existing=True, guard=True)
    truncate_and_insert(metrics, "FE_METRICS_SIGNAL", engine_bt, prefer_existing=True, guard=True)
    logger.info("  Metrics backfill complete.")


# ============================================================
# PHASE 8: CORE (DMV_ALL + DMV_SCORES from signal tables)
# ============================================================
# Same tables and join order as gcp_dmv_core.py: the order fixes the column order,
# and the daily's bullish/bearish/neutral counts skip the first 4 columns by position.
CORE_SIGNAL_TABLES = [
    "FE_OSCILLATORS_SIGNALS", "FE_MOMENTUM_SIGNALS", "FE_METRICS_SIGNAL", "FE_TVV_SIGNALS",
    "FE_RATIOS_SIGNALS", "FE_CANDLESTICK_SIGNALS", "FE_DOW_PATTERNS", "FE_PRICE_LEVELS",
]


def _core_join_chunk(engine_bt, tables, start, end, slugs=None):
    """FULL OUTER JOIN of the signal tables USING (slug, timestamp) for one time window."""
    where = '"timestamp" >= :a AND "timestamp" < :b' + (' AND slug IN :slugs' if slugs else '')
    parts = [f'(SELECT * FROM "{t}" WHERE {where}) AS t{i}' for i, t in enumerate(tables)]
    query = text("SELECT * FROM " + " FULL OUTER JOIN ".join(
        p if i == 0 else f"{p} USING (slug, timestamp)" for i, p in enumerate(parts)))
    params = {"a": start, "b": end}
    if slugs:
        query = query.bindparams(bindparam("slugs", expanding=True))
        params["slugs"] = list(slugs)
    with engine_bt.connect() as conn:
        return pd.read_sql_query(query, conn, params=params)


def compute_core(engine_bt, slugs=None, since=None):
    """FE_DMV_ALL + FE_DMV_SCORES for every (slug, timestamp) any signal table has.

    Mirrors gcp_dmv_core.py: FULL OUTER JOIN, drop duplicate columns, fillna(0) (a pillar
    with no row for that key counts as 0), bullish/bearish/neutral = how many of
    columns[4:] equal 1/-1/0 (counted before the 3 new zero columns get values, so
    neutral includes them), then keep FE_DMV_ALL's columns and score each pillar as
    mean(d_/m_/v_ columns) * 100.
    """
    tables = [t for t in CORE_SIGNAL_TABLES if table_columns(engine_bt, t)]
    absent = [t for t in CORE_SIGNAL_TABLES if t not in tables]
    if absent:
        logger.warning(f"  Signal tables missing from cp_backtest, treated as all-0: {absent}")
    dmv_cols = table_columns(engine_bt, "FE_DMV_ALL")

    first_year = 2013 if since is None else pd.Timestamp(since).year
    edges = [pd.Timestamp(f"{y}-01-01", tz="UTC") for y in range(first_year, pd.Timestamp.now().year + 2)]
    if since is not None:
        edges[0] = pd.Timestamp(since, tz="UTC")
    all_rows, all_scores = [], []
    for start, end in zip(edges[:-1], edges[1:]):
        df = _core_join_chunk(engine_bt, tables, start, end, slugs)
        if df.empty:
            continue
        # cp_backtest's FE_METRICS_SIGNAL has a legacy, always-NULL `symbol` that dbcp's copy
        # (which the daily joins) lacks; as a 0 it would add one to every neutral count.
        df = (df.loc[:, ~df.columns.duplicated()]
                .drop(columns=['symbol'], errors='ignore')
                .dropna(subset=['slug', 'timestamp'])
                .fillna(0))
        for col in ['bullish', 'bearish', 'neutral']:
            df[col] = 0
        counted = df.iloc[:, 4:].copy()
        bull, bear, neut = counted.eq(1).sum(axis=1), counted.eq(-1).sum(axis=1), counted.eq(0).sum(axis=1)
        df['bullish'], df['bearish'], df['neutral'] = bull, bear, neut
        if dmv_cols:
            df = df[[c for c in df.columns if c in dmv_cols]]

        pillars = {}
        for name, prefix in [('Durability_Score', 'd_'), ('Momentum_Score', 'm_'), ('Valuation_Score', 'v_')]:
            pcols = [c for c in df.columns if c.startswith(prefix)]
            pillars[name] = df[pcols].sum(axis=1) / max(len(pcols), 1) * 100
        all_scores.append(pd.DataFrame({'slug': df['slug'], 'timestamp': df['timestamp'], **pillars}))
        all_rows.append(df)
        logger.info(f"  {start.date()}..{end.date()}: {len(df):,} rows")

    if not all_rows:
        return pd.DataFrame(), pd.DataFrame()
    return pd.concat(all_rows, ignore_index=True), pd.concat(all_scores, ignore_index=True)


def phase_core(engine_bt, slugs=None):
    logger.info("=" * 60)
    logger.info("PHASE 8: Backfilling FE_DMV_ALL + FE_DMV_SCORES")
    logger.info("=" * 60)

    df, dmv_scores = compute_core(engine_bt, slugs)
    if df.empty:
        logger.warning("  No signal data found. Skipping core.")
        return
    logger.info(f"  {len(df):,} rows for {df['slug'].nunique():,} coins over "
                f"{df['timestamp'].nunique():,} dates.")

    # FE_DMV_ALL and FE_DMV_SCORES may not exist yet in cp_backtest
    # Use if_exists='replace' for first creation, then backfill handles future runs.
    # Only on LookupError (table missing): any other write error has already rolled
    # back, and replacing the table then would throw its rows away.
    try:
        truncate_and_insert(df, "FE_DMV_ALL", engine_bt, guard=True)
    except LookupError:
        logger.info("  FE_DMV_ALL does not exist yet, creating...")
        num_cols = len(df.columns)
        safe_chunk = max(1, 65000 // num_cols)
        df.to_sql("FE_DMV_ALL", con=engine_bt, if_exists='replace',
                  index=False, method='multi', chunksize=safe_chunk)
        logger.info(f"  FE_DMV_ALL created with {len(df)} rows.")

    try:
        truncate_and_insert(dmv_scores, "FE_DMV_SCORES", engine_bt, guard=True)
    except LookupError:
        logger.info("  FE_DMV_SCORES does not exist yet, creating...")
        num_cols = len(dmv_scores.columns)
        safe_chunk = max(1, 65000 // num_cols)
        dmv_scores.to_sql("FE_DMV_SCORES", con=engine_bt, if_exists='replace',
                          index=False, method='multi', chunksize=safe_chunk)
        logger.info(f"  FE_DMV_SCORES created with {len(dmv_scores)} rows.")
    logger.info("  Core backfill complete.")


# ============================================================
# MAIN
# ============================================================
ALL_PHASES = ["momentum", "oscillators", "tvv", "pct", "ratios", "metrics", "core"]
# Run only when named in --only:
#   ratios  - loops every date x coin; ~48 h for all ~3,660 coins (needs vectorising)
#   metrics - point-in-time history for every coin; keeps the existing daily snapshot rows
#             and values (prefer_existing) and refuses to drop keys (shrink guard). Opt-in
#             because it rewrites FE_METRICS(+_SIGNAL), ~2.7M rows, and core reads it.
#   core    - FULL OUTER JOIN of the signal tables like gcp_dmv_core.py, guarded against
#             shrinking. Opt-in because it rewrites ~2.7M rows and its scores depend on
#             ratios/metrics/candlestick/Dow/levels, so run it after those are current.
OPT_IN_PHASES = ["ratios", "metrics", "core"]
DEFAULT_PHASES = [p for p in ALL_PHASES if p not in OPT_IN_PHASES]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill cp_backtest FE_ tables")
    parser.add_argument(
        "--only", type=str, default=None,
        help="Comma-separated phases to run. Options: " + ",".join(ALL_PHASES) +
             ". Default: " + ",".join(DEFAULT_PHASES) +
             " (" + ",".join(OPT_IN_PHASES) + " only run when named here)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute every selected phase and log what would be written, but write nothing."
    )
    parser.add_argument(
        "--slugs", type=str, default=None,
        help="Comma-separated slugs to limit the OHLCV fetch to (for testing; use with --dry-run)."
    )
    parser.add_argument(
        "--allow-shrink", action="store_true",
        help="Let metrics/core rewrites drop existing (slug, timestamp) keys or >5%% of rows "
             "(refused by default)."
    )
    args = parser.parse_args()
    DRY_RUN = args.dry_run
    ALLOW_SHRINK = args.allow_shrink
    slugs = [s.strip() for s in args.slugs.split(",")] if args.slugs else None
    if slugs and not DRY_RUN:
        print("--slugs would TRUNCATE each table down to just those coins; use it with --dry-run.")
        sys.exit(1)

    if args.only:
        selected = [p.strip().lower() for p in args.only.split(",")]
        invalid = [p for p in selected if p not in ALL_PHASES]
        if invalid:
            print(f"Invalid phases: {invalid}. Valid: {ALL_PHASES}")
            sys.exit(1)
    else:
        selected = DEFAULT_PHASES
        logger.info(f"No --only given: running {', '.join(DEFAULT_PHASES)}. "
                    f"Opt-in phases skipped: {', '.join(OPT_IN_PHASES)}.")

    overall_start = time.time()
    logger.info(f"BACKFILL CP_BACKTEST - Phases: {', '.join(selected)}"
                f"{' (DRY RUN: nothing will be written)' if DRY_RUN else ''}")

    engine_live = create_engine_live()
    engine_bt = create_engine_backtest()

    # Phase 1: Always diagnose
    phase_diagnose(engine_bt)

    # Fetch OHLCV if any OHLCV-based phase is selected
    ohlcv_phases = {"momentum", "oscillators", "tvv", "pct", "ratios", "metrics"}
    if ohlcv_phases & set(selected):
        ohlcv_df = fetch_all_ohlcv(engine_live, slugs)
    else:
        ohlcv_df = None

    if "momentum" in selected:
        phase_momentum(ohlcv_df, engine_bt)
    if "oscillators" in selected:
        phase_oscillators(ohlcv_df, engine_bt)
    if "tvv" in selected:
        phase_tvv(ohlcv_df, engine_bt)
    if "pct" in selected:
        phase_pct(ohlcv_df, engine_bt)
    if "ratios" in selected:
        phase_ratios(ohlcv_df, engine_bt)
    if "metrics" in selected:
        phase_metrics(ohlcv_df, engine_live, engine_bt)
    if "core" in selected:
        phase_core(engine_bt, slugs)

    # Final diagnosis
    logger.info("")
    logger.info("FINAL STATE:")
    phase_diagnose(engine_bt)

    engine_live.dispose()
    engine_bt.dispose()

    elapsed = (time.time() - overall_start) / 60
    logger.info(f"BACKFILL COMPLETE in {elapsed:.1f} minutes.")
