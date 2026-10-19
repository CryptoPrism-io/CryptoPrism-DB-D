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


def truncate_and_insert(df, table_name, engine):
    """Replace a table's rows with df in ONE transaction, in safe chunks for pg8000.

    Writes exactly the columns the table has, so indicators added to the daily DMV
    scripts get backfilled too. Table columns df doesn't compute keep their existing
    values (matched on slug + timestamp); if they hold no data they stay NULL.
    If any insert fails the TRUNCATE rolls back and the old rows stay.
    """
    cols = table_columns(engine, table_name)
    if not cols:
        if DRY_RUN:
            logger.warning(f"  {table_name} does not exist; would be created (dry run).")
            return
        raise LookupError(f"{table_name} does not exist")
    missing = [c for c in cols if c not in df.columns]
    try:
        with engine.begin() as conn:
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
RATIO_WINDOW_DAYS = 28   # window = [target_date - 28 days, target_date]
RATIO_MIN_DATES = 5      # skip a target date whose window has fewer unique dates
RATIO_MIN_ROWS = 3       # skip a slug with fewer rows than this in the window
RATIO_BENCHMARK = "bitcoin"


def _masked_mean(x, mask):
    """Row-wise mean over cells where mask is True, like pandas Series.mean() (skipna).
    NaN when a row has no cells; inf propagates the same way pandas' sum does."""
    n = mask.sum(axis=1)
    return np.where(mask, x, 0.0).sum(axis=1) / n


def _masked_var(x, mask):
    """Row-wise variance (ddof=1) over cells where mask is True, two-pass like pandas
    nanvar. NaN when a row has fewer than 2 cells."""
    n = mask.sum(axis=1)
    m = np.where(mask, x, 0.0).sum(axis=1) / n
    sq = np.where(mask, (x - m[:, None]) ** 2, 0.0).sum(axis=1)
    var = sq / (n - 1)
    var[n < 2] = np.nan
    return var


def _masked_std(x, mask):
    return np.sqrt(_masked_var(x, mask))


def _masked_cov(a, b, mask):
    """Row-wise covariance (ddof=1) of a and b over mask, like Series.cov (np.cov on the
    valid pairs). NaN when a row has fewer than 2 pairs."""
    n = mask.sum(axis=1)
    am = np.where(mask, a, 0.0).sum(axis=1) / n
    bm = np.where(mask, b, 0.0).sum(axis=1) / n
    c = np.where(mask, (a - am[:, None]) * (b - bm[:, None]), 0.0).sum(axis=1) / (n - 1)
    c[n < 2] = np.nan
    return c


def compute_ratios_vectorised(df):
    """FE_RATIOS rows for every (target date, slug), from df that already has m_pct_1d.

    Same semantics as the per-date/per-slug loop over gcp_dmv_rat's calculate_* functions
    (window, skips, bitcoin benchmark ffill-aligned to each slug's dates, skipna/ddof rules),
    but each target date is one vectorised step over a dense slug x date matrix instead of
    a pandas groupby per slug. Returns None if nothing qualifies.
    """
    df = df[['slug', 'name', 'timestamp', 'm_pct_1d']]
    dups = df.duplicated(['slug', 'timestamp'], keep='last')
    if dups.any():
        logger.warning(f"  {int(dups.sum())} duplicate (slug, timestamp) rows; keeping the last of each.")
        df = df[~dups]

    slugs = pd.Index(sorted(df['slug'].unique()))          # groupby('slug') order
    dates = pd.DatetimeIndex(df['timestamp'].unique()).sort_values()
    if RATIO_BENCHMARK not in slugs:
        return None
    S, D = len(slugs), len(dates)
    si = slugs.get_indexer(df['slug'])
    di = dates.get_indexer(df['timestamp'])

    R = np.full((S, D), np.nan)              # m_pct_1d; NaN = no row or NaN return
    R[si, di] = df['m_pct_1d'].to_numpy(dtype=float)
    P = np.zeros((S, D), dtype=bool)         # a row exists for (slug, date)
    P[si, di] = True
    row_of = np.full((S, D), -1, dtype=np.int64)
    row_of[si, di] = np.arange(len(df))
    names = df['name'].to_numpy()
    btc = slugs.get_loc(RATIO_BENCHMARK)
    starts = dates.searchsorted(dates - pd.Timedelta(days=RATIO_WINDOW_DAYS), side='left')

    out = {k: [] for k in ['m_rat_alpha', 'm_rat_omega', 'v_rat_sharpe', 'v_rat_sortino',
                           'v_rat_teynor', 'v_rat_common_sense', 'v_rat_information',
                           'v_rat_win_loss', 'm_rat_win_rate', 'm_rat_ror', 'd_rat_pain',
                           'd_rat_beta', 'slug_i', 'date_i', 'row_i']}
    processed = 0
    with np.errstate(all='ignore'):
        for e in range(D):
            s = starts[e]
            W = e - s + 1
            if W < RATIO_MIN_DATES:
                continue                     # window has < 5 unique dates
            pb, rb = P[btc, s:e + 1], R[btc, s:e + 1]
            if not pb.any():
                continue                     # no benchmark rows in the window
            bvalid = (pb & ~np.isnan(rb))[None, :]
            b_mean = _masked_mean(rb[None, :], bvalid)[0]
            b_var = _masked_var(rb[None, :], bvalid)[0]
            # Benchmark reindexed to each date with ffill over the window's bitcoin rows
            last = np.maximum.accumulate(np.where(pb, np.arange(W), -1))
            ab = np.where(last >= 0, rb[np.clip(last, 0, None)], np.nan)[None, :]

            pw = P[:, s:e + 1]
            nrows = pw.sum(axis=1)
            elig = nrows >= RATIO_MIN_ROWS
            elig[btc] = False
            ii = np.flatnonzero(elig)
            processed += 1
            if processed % 500 == 0:
                logger.info(f"  Processed {processed}/{D} dates...")
            if not len(ii):
                continue
            r = R[ii, s:e + 1]
            n = nrows[ii]
            valid = ~np.isnan(r)

            mean_r = _masked_mean(r, valid)
            std_r = _masked_std(r, valid)
            beta = _masked_cov(r, np.broadcast_to(ab, r.shape), valid & ~np.isnan(ab))
            beta = beta / b_var if b_var != 0 else np.full(len(ii), np.nan)

            ex = r - ab                                  # excess / active returns
            gm, lm = ex > 0, ex < 0
            avg_gain = _masked_mean(ex, gm)
            avg_loss = np.abs(_masked_mean(ex, lm))
            omega = np.where(avg_loss != 0, avg_gain / avg_loss, np.inf)

            sharpe = np.where(std_r != 0, mean_r / std_r, np.nan)
            down_std = _masked_std(r, valid & (r < 0))
            sortino = np.where(down_std != 0, mean_r / down_std, np.nan)
            treynor = np.where(np.isnan(beta) | (beta == 0), np.nan, mean_r / beta)

            cum = np.nancumprod(1 + np.where(valid, r, np.nan), axis=1)
            cum[~valid] = np.nan
            peak = np.fmax.accumulate(cum, axis=1)
            dd = (cum - peak) / peak
            dd_ok = ~np.isnan(dd)
            max_dd = np.where(dd_ok.any(axis=1), np.where(dd_ok, dd, np.inf).min(axis=1), np.nan)
            common_sense = np.where(max_dd != 0, mean_r / np.abs(max_dd), np.nan)

            act_ok = ~np.isnan(ex)
            te = _masked_std(ex, act_ok)
            information = np.where(te != 0, _masked_mean(ex, act_ok) / te, np.nan)

            wins = (r > 0).sum(axis=1)
            losses = (r < 0).sum(axis=1)
            win_loss = np.where(losses != 0, wins / losses, np.inf)
            win_rate = wins / n
            bad_wr = np.isnan(win_rate) | (win_rate == 0) | (win_rate == np.inf)
            ror = np.where(bad_wr, np.nan, ((1 - win_rate) / win_rate) ** n)
            gain = np.where(r > 0, r, 0.0).sum(axis=1)
            pain = np.where(r < 0, np.abs(r), 0.0).sum(axis=1)
            gain_to_pain = np.where(pain != 0, gain / pain, np.nan)

            first = pw[ii].argmax(axis=1)                # name of the slug's first row in window
            for k, v in [('m_rat_alpha', mean_r - b_mean), ('m_rat_omega', omega),
                         ('v_rat_sharpe', sharpe), ('v_rat_sortino', sortino),
                         ('v_rat_teynor', treynor), ('v_rat_common_sense', common_sense),
                         ('v_rat_information', information), ('v_rat_win_loss', win_loss),
                         ('m_rat_win_rate', win_rate), ('m_rat_ror', ror),
                         ('d_rat_pain', gain_to_pain), ('d_rat_beta', beta),
                         ('slug_i', ii), ('date_i', np.full(len(ii), e)),
                         ('row_i', row_of[ii, s + first])]:
                out[k].append(v)

    if not out['slug_i']:
        return None
    cols = {k: np.concatenate(v) for k, v in out.items()}
    ratios_df = pd.DataFrame({k: cols[k] for k in list(out)[:11]})
    ratios_df['slug'] = slugs.take(cols['slug_i'])
    ratios_df['name'] = names[cols['row_i']]
    ratios_df['timestamp'] = dates.take(cols['date_i'])
    ratios_df['d_rat_beta'] = cols['d_rat_beta']
    return ratios_df


def phase_ratios(ohlcv_df, engine_bt):
    logger.info("=" * 60)
    logger.info("PHASE 6: Backfilling FE_RATIOS + FE_RATIOS_SIGNALS")
    logger.info("=" * 60)

    from gcp_dmv_rat import (
        calculate_pct_change as rat_pct_change,
        generate_binary_signals_ratios
    )

    df = rat_pct_change(ohlcv_df.copy())
    logger.info(f"  {df['timestamp'].nunique()} unique dates in OHLCV data.")
    start = time.time()
    ratios_df = compute_ratios_vectorised(df)
    if ratios_df is None or ratios_df.empty:
        logger.warning("  No ratio data computed. Skipping.")
        return
    logger.info(f"  {len(ratios_df)} ratio rows computed in {(time.time() - start) / 60:.1f} minutes.")
    ratios_df = generate_binary_signals_ratios(ratios_df)

    truncate_and_insert(ratios_df, "FE_RATIOS", engine_bt)
    truncate_and_insert(ratios_df, "FE_RATIOS_SIGNALS", engine_bt)
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

    # Drop raw OHLCV columns by NAME (not position -- positional drops break
    # when set_index/reset_index reorders columns)
    ohlcv_drop_cols = ['name', 'open', 'high', 'low', 'close', 'volume']
    metrics = met_latest.drop(columns=[c for c in ohlcv_drop_cols if c in met_latest.columns])

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

    # Select signal columns explicitly by name (not positional drop)
    signal_cols = [c for c in metrics.columns if c in
                   ['timestamp', 'id', 'symbol', 'slug',
                    'm_pct_1d_signal', 'd_pct_cum_ret_signal',
                    'd_met_ath_month_signal', 'd_market_cap_signal',
                    'd_met_coin_age_y_signal']]
    metrics_signal = metrics[signal_cols]

    if DRY_RUN:
        logger.info(f"  Would replace FE_METRICS ({len(metrics)} rows) and "
                    f"FE_METRICS_SIGNAL ({len(metrics_signal)} rows) (dry run: nothing written).")
        return

    # Use replace for metrics (snapshot data, avoids schema mismatch with existing table)
    logger.info(f"  Writing {len(metrics)} rows to FE_METRICS...")
    num_cols = len(metrics.columns)
    safe_chunk = max(1, 65000 // num_cols)
    metrics.to_sql('FE_METRICS', con=engine_bt, if_exists='replace',
                   index=False, method='multi', chunksize=safe_chunk)
    logger.info(f"  FE_METRICS: {len(metrics)} rows written.")

    logger.info(f"  Writing {len(metrics_signal)} rows to FE_METRICS_SIGNAL...")
    num_cols = len(metrics_signal.columns)
    safe_chunk = max(1, 65000 // num_cols)
    metrics_signal.to_sql('FE_METRICS_SIGNAL', con=engine_bt, if_exists='replace',
                          index=False, method='multi', chunksize=safe_chunk)
    logger.info(f"  FE_METRICS_SIGNAL: {len(metrics_signal)} rows written.")
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

    # FE_DMV_ALL and FE_DMV_SCORES may not exist yet in cp_backtest
    # Use if_exists='replace' for first creation, then backfill handles future runs.
    # Only on LookupError (table missing): any other write error has already rolled
    # back, and replacing the table then would throw its rows away.
    try:
        truncate_and_insert(df, "FE_DMV_ALL", engine_bt)
    except LookupError:
        logger.info("  FE_DMV_ALL does not exist yet, creating...")
        num_cols = len(df.columns)
        safe_chunk = max(1, 65000 // num_cols)
        df.to_sql("FE_DMV_ALL", con=engine_bt, if_exists='replace',
                  index=False, method='multi', chunksize=safe_chunk)
        logger.info(f"  FE_DMV_ALL created with {len(df)} rows.")

    try:
        truncate_and_insert(dmv_scores, "FE_DMV_SCORES", engine_bt)
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
#   ratios  - rewrites FE_RATIOS(+_SIGNALS) for all coins: ~0.5 min compute (vectorised),
#             then ~1 h of inserts for ~2.75M rows per table on the crypto VM
#   metrics - replaces the daily FE_METRICS snapshot history with a single current snapshot
#   core    - rebuilds FE_DMV_ALL from the inner join of all signal tables, which today
#             covers ~5.7k rows vs ~156k in the table (FE_METRICS_SIGNAL has only ~178 dates)
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
    args = parser.parse_args()
    DRY_RUN = args.dry_run
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
    ohlcv_phases = {"momentum", "oscillators", "tvv", "pct", "ratios"}
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
        phase_metrics(engine_live, engine_bt)
    if "core" in selected:
        phase_core(engine_bt)

    # Final diagnosis
    logger.info("")
    logger.info("FINAL STATE:")
    phase_diagnose(engine_bt)

    engine_live.dispose()
    engine_bt.dispose()

    elapsed = (time.time() - overall_start) / 60
    logger.info(f"BACKFILL COMPLETE in {elapsed:.1f} minutes.")
