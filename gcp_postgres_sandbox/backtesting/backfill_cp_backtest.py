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
import resource
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

from dmv_ranges import compute_dmv_scores, risk_of_ruin  # noqa: E402

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
    _apply_guard(table_name, len(out), len(old), int((~old_keys.isin(new_keys)).sum()))


def _apply_guard(table_name, new_rows, old_rows, lost):
    """The shrink guard's decision, given the row counts and the number of lost keys."""
    if old_rows == 0:
        return
    msg = (f"{table_name}: {new_rows:,} rows vs {old_rows:,} now, "
           f"{lost:,} existing (slug, timestamp) keys would be dropped")
    if lost == 0 and new_rows >= SHRINK_MIN_ROWS * old_rows:
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


# Coins per batch in the batched phases (ratios, metrics); set by --batch-size.
BATCH_SLUGS = 250


def _batches(items, size=None):
    size = size or BATCH_SLUGS
    return [items[i:i + size] for i in range(0, len(items), size)]


def _quoted(cols, alias=None):
    prefix = f"{alias}." if alias else ""
    return ", ".join(f'{prefix}"{c}"' for c in cols)


class StagedTable:
    """Rewrites a table batch by batch, so peak memory is one batch instead of the table.

    add() appends each batch to an UNLOGGED staging copy, "_stg_<table>". finish() then
    does in SQL, in ONE transaction, what truncate_and_insert does in pandas: keep the
    existing values of columns the backfill doesn't compute, optionally keep the table's
    existing rows and values (prefer_existing), apply the shrink guard, then TRUNCATE the
    table and INSERT it from the staging copy. If that fails the table keeps its old rows.
    The staging copy is dropped after finish(); one left by a crashed run is dropped by
    the next run.

    In a dry run nothing is created: each batch is compared, read-only, with the table's
    rows in that batch's scope (its slugs or time window) to report what finish() would do.
    """

    def __init__(self, engine, table_name, prefer_existing=False, guard=False):
        self.engine, self.table, self.stg = engine, table_name, f"_stg_{table_name}"
        self.prefer_existing, self.guard = prefer_existing, guard
        self.cols = table_columns(engine, table_name)
        self.computed = set()
        self.rows = 0
        # Dry-run tallies over the scopes seen so far
        self.seen_old = self.overlap = self.unmatched_old = 0
        if not self.cols:
            if DRY_RUN:
                logger.warning(f"  {table_name} does not exist; would be created (dry run).")
                return
            raise LookupError(f"{table_name} does not exist")
        if not DRY_RUN:
            with engine.begin() as conn:
                conn.execute(text(f'DROP TABLE IF EXISTS "{self.stg}"'))
                conn.execute(text(f'CREATE UNLOGGED TABLE "{self.stg}" (LIKE "{table_name}" INCLUDING DEFAULTS)'))

    def add(self, df, scope):
        """Append one batch. scope = {"slugs": [...]} or {"start": a, "end": b}: the table
        rows this batch replaces, which the dry run compares it with."""
        if not self.cols or df.empty:
            return
        present = [c for c in self.cols if c in df.columns]
        self.computed.update(present)
        out = df[present].replace([np.inf, -np.inf], np.nan)
        self.rows += len(out)
        if DRY_RUN:
            self._dry_compare(out, scope)
            return
        # pg8000 limit: 65535 params per batch
        safe_chunk = max(1, 65000 // len(present))
        with self.engine.begin() as conn:
            out.to_sql(self.stg, con=conn, if_exists='append',
                       index=False, method='multi', chunksize=safe_chunk)

    def _dry_compare(self, out, scope):
        if "slugs" in scope:
            q = text(f'SELECT "slug", "timestamp" FROM "{self.table}" WHERE "slug" IN :slugs'
                     ).bindparams(bindparam("slugs", expanding=True))
            params = {"slugs": list(scope["slugs"])}
        else:
            q = text(f'SELECT "slug", "timestamp" FROM "{self.table}" '
                     f'WHERE "timestamp" >= :a AND "timestamp" < :b')
            params = {"a": scope["start"], "b": scope["end"]}
        with self.engine.connect() as conn:
            old = pd.read_sql_query(q, conn, params=params)
        if old.empty:
            return
        old_keys = pd.MultiIndex.from_arrays([old["slug"], _ts_key(old["timestamp"])])
        new_keys = pd.MultiIndex.from_arrays([out["slug"], _ts_key(out["timestamp"])])
        hit = old_keys.isin(new_keys)
        self.seen_old += len(old)
        self.overlap += int(hit.sum())
        self.unmatched_old += int((~hit).sum())

    def _not_computed(self, conn):
        """Columns no batch computed, split into those with data (kept) and empty ones."""
        missing = [c for c in self.cols if c not in self.computed]
        keep = [c for c in missing
                if conn.execute(text(f'SELECT COUNT("{c}") FROM "{self.table}"')).scalar()]
        if keep:
            logger.info(f"  {self.table}: keeping existing values of {len(keep)} column(s) "
                        f"the backfill doesn't compute: {keep}")
        still_null = [c for c in missing if c not in keep]
        if still_null:
            logger.warning(f"  {self.table}: {len(still_null)} column(s) not computed and "
                           f"currently empty, will stay NULL: {still_null}")
        return keep

    def finish(self):
        if not self.cols:
            return
        if DRY_RUN:
            self._dry_finish()
            return
        try:
            self._swap()
        except Exception as e:
            logger.error(f"  Error writing {self.table} (rolled back, old rows kept): {e}")
            raise
        finally:
            self.drop()

    def drop(self):
        """Drop the staging copy without touching the table."""
        if self.cols and not DRY_RUN:
            with self.engine.begin() as conn:
                conn.execute(text(f'DROP TABLE IF EXISTS "{self.stg}"'))

    def _dry_finish(self):
        with self.engine.connect() as conn:
            old_rows = conn.execute(text(f'SELECT COUNT(*) FROM "{self.table}"')).scalar()
            self._not_computed(conn)
        # Rows outside every batch's scope aren't recomputed either
        unmatched = self.unmatched_old + (old_rows - self.seen_old)
        if self.prefer_existing:
            logger.info(f"  {self.table}: would keep existing values for {self.overlap:,} "
                        f"(slug, timestamp) keys already in the table; computed rows fill the rest.")
            new_rows, lost = self.rows + unmatched, 0
        else:
            new_rows, lost = self.rows, unmatched
        if self.guard:
            _apply_guard(self.table, new_rows, old_rows, lost)
        logger.info(f"  Writing {new_rows} rows x {len(self.computed)} computed columns to "
                    f"{self.table} (dry run: nothing written)...")

    def _swap(self):
        t, s = self.table, self.stg
        key = 's."slug" = t."slug" AND s."timestamp" = t."timestamp"'
        not_in_stg = f'NOT EXISTS (SELECT 1 FROM "{s}" AS s WHERE {key})'
        with self.engine.begin() as conn:
            conn.execute(text(f'CREATE INDEX ON "{s}" ("slug", "timestamp")'))
            conn.execute(text(f'ANALYZE "{s}"'))
            keep = self._not_computed(conn)
            sets = [f'"{c}" = t."{c}"' for c in keep]
            if self.prefer_existing:
                sets += [f'"{c}" = COALESCE(t."{c}", s."{c}")' for c in self.cols
                         if c in self.computed and c not in ("slug", "timestamp")]
            if sets:
                overlap = conn.execute(text(
                    f'UPDATE "{s}" AS s SET {", ".join(sets)} FROM "{t}" AS t WHERE {key}')).rowcount
            if self.prefer_existing:
                added = conn.execute(text(
                    f'INSERT INTO "{s}" ({_quoted(self.cols)}) '
                    f'SELECT {_quoted(self.cols, "t")} FROM "{t}" AS t WHERE {not_in_stg}')).rowcount
                logger.info(f"  {t}: kept existing values for {overlap:,} (slug, timestamp) keys "
                            f"already in the table and {added:,} existing rows the backfill "
                            f"didn't compute; computed rows fill the rest.")
            new_rows = conn.execute(text(f'SELECT COUNT(*) FROM "{s}"')).scalar()
            if self.guard:
                old_rows = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
                lost = conn.execute(text(f'SELECT COUNT(*) FROM "{t}" AS t WHERE {not_in_stg}')).scalar()
                _apply_guard(t, new_rows, old_rows, lost)
            logger.info(f"  Writing {new_rows} rows x {len(self.cols)} columns to {t} from {s}...")
            conn.execute(text(f'TRUNCATE TABLE "{t}"'))
            conn.execute(text(f'INSERT INTO "{t}" ({_quoted(self.cols)}) SELECT {_quoted(self.cols)} FROM "{s}"'))
        logger.info(f"  {t}: {new_rows} rows written.")


def fetch_slugs(engine, slugs=None):
    """Every slug in 1K_coins_ohlcv (or `slugs`), sorted."""
    if slugs:
        return sorted(slugs)
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text('SELECT DISTINCT "slug" FROM "1K_coins_ohlcv" ORDER BY 1'))]


def fetch_ohlcv_dates(engine):
    """Every distinct timestamp in 1K_coins_ohlcv, sorted, typed as fetch_all_ohlcv types them."""
    with engine.connect() as conn:
        ts = pd.read_sql_query(text('SELECT DISTINCT "timestamp" FROM "1K_coins_ohlcv"'), conn)['timestamp']
    return pd.DatetimeIndex(pd.to_datetime(ts)).sort_values()


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


def compute_ratios_vectorised(df, dates=None):
    """FE_RATIOS rows for every (target date, slug), from df that already has m_pct_1d.

    Same semantics as the per-date/per-slug loop over gcp_dmv_rat's calculate_* functions
    (window, skips, bitcoin benchmark ffill-aligned to each slug's dates, skipna/ddof rules),
    but each target date is one vectorised step over a dense slug x date matrix instead of
    a pandas groupby per slug. Returns None if nothing qualifies.

    dates: the target dates (default: df's). A slug's rows depend only on its own returns,
    bitcoin's and the target dates, so a batch of slugs plus bitcoin, given every coin's
    dates, gets exactly the rows the full run would.
    """
    df = df[['slug', 'name', 'timestamp', 'm_pct_1d']]
    dups = df.duplicated(['slug', 'timestamp'], keep='last')
    if dups.any():
        logger.warning(f"  {int(dups.sum())} duplicate (slug, timestamp) rows; keeping the last of each.")
        df = df[~dups]

    slugs = pd.Index(sorted(df['slug'].unique()))          # groupby('slug') order
    if dates is None:
        dates = pd.DatetimeIndex(df['timestamp'].unique()).sort_values()
    if RATIO_BENCHMARK not in slugs:
        return None
    S, D = len(slugs), len(dates)
    si = slugs.get_indexer(df['slug'])
    di = dates.get_indexer(df['timestamp'])
    if (di < 0).any():
        raise ValueError("df has timestamps that aren't in `dates`")

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
            # No returns in the window is no data, not certain ruin
            ror = np.where(valid.any(axis=1), risk_of_ruin(win_rate, n), np.nan)
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


def phase_ratios(engine_live, engine_bt, all_slugs):
    logger.info("=" * 60)
    logger.info("PHASE 6: Backfilling FE_RATIOS + FE_RATIOS_SIGNALS")
    logger.info("=" * 60)

    from gcp_dmv_rat import (
        calculate_pct_change as rat_pct_change,
        generate_binary_signals_ratios
    )

    dates = fetch_ohlcv_dates(engine_live)
    logger.info(f"  {len(dates)} unique dates in OHLCV data; {len(all_slugs):,} coins "
                f"in batches of {BATCH_SLUGS}.")
    stages = [StagedTable(engine_bt, "FE_RATIOS"), StagedTable(engine_bt, "FE_RATIOS_SIGNALS")]
    start, total = time.time(), 0
    for i, batch in enumerate(_batches(all_slugs), 1):
        # Every batch fetches bitcoin too: it's the benchmark (and never gets a row itself)
        df = rat_pct_change(fetch_all_ohlcv(engine_live, sorted(set(batch) | {RATIO_BENCHMARK})))
        ratios_df = compute_ratios_vectorised(df, dates)
        if ratios_df is None or ratios_df.empty:
            continue
        ratios_df = generate_binary_signals_ratios(ratios_df[ratios_df['slug'].isin(batch)])
        for st in stages:
            st.add(ratios_df, {"slugs": batch})
        total += len(ratios_df)
        logger.info(f"  Batch {i}: {total:,} ratio rows so far ({(time.time() - start) / 60:.1f} min).")
    if not total:
        logger.warning("  No ratio data computed. Skipping.")
        for st in stages:
            st.drop()
        return
    for st in stages:
        st.finish()
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


def phase_metrics(engine_live, engine_bt, all_slugs):
    logger.info("=" * 60)
    logger.info("PHASE 7: Backfilling FE_METRICS + FE_METRICS_SIGNAL")
    logger.info("=" * 60)

    date_added = fetch_date_added(engine_live, engine_bt)
    # The existing rows are the real daily snapshots: keep their values and only fill gaps
    # (their signal columns are NULL, as gcp_dmv_met.py writes FE_METRICS before signals).
    stages = [StagedTable(engine_bt, "FE_METRICS", prefer_existing=True, guard=True),
              StagedTable(engine_bt, "FE_METRICS_SIGNAL", prefer_existing=True, guard=True)]
    rows = coins = no_listing = 0
    # Every metric is per coin, so batches of coins give the same rows as one pass
    for i, batch in enumerate(_batches(all_slugs), 1):
        metrics = compute_metrics(fetch_all_ohlcv(engine_live, batch), date_added)
        for st in stages:
            st.add(metrics, {"slugs": batch})
        batch_slugs = metrics['slug'].unique()
        rows, coins = rows + len(metrics), coins + len(batch_slugs)
        no_listing += int((~pd.Series(batch_slugs).isin(date_added)).sum())
        logger.info(f"  Batch {i}: {rows:,} rows so far.")
    logger.info(f"  {rows:,} point-in-time rows for {coins:,} coins; "
                f"{no_listing:,} coins have no CMC date_added "
                f"(first OHLCV date used as a lower bound for coin age).")
    for st in stages:
        st.finish()
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
    """FULL OUTER JOIN of the signal tables USING (slug, timestamp) for one time window.

    The join runs here, not in Postgres: each table's window is a plain range read on its
    "timestamp" index, then pandas outer-merges them. On a small RDS instance the 8-way
    join in SQL ran the server out of memory. Columns come out as SQL's SELECT * ... USING
    orders them (slug, timestamp, then each table's other columns), except that a name an
    earlier table already has is skipped: core keeps only the first copy anyway.
    """
    where = '"timestamp" >= :a AND "timestamp" < :b' + (' AND slug IN :slugs' if slugs else '')
    params = {"a": start, "b": end}
    if slugs:
        params["slugs"] = list(slugs)
    keys = ["slug", "timestamp"]
    out = None
    with engine_bt.connect() as conn:
        for t in tables:
            query = text(f'SELECT * FROM "{t}" WHERE {where}')
            if slugs:
                query = query.bindparams(bindparam("slugs", expanding=True))
            df = pd.read_sql_query(query, conn, params=params)
            df = df.loc[:, ~df.columns.duplicated()]
            # Same key types in every frame, even an empty one, so the merges line up
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df["slug"] = df["slug"].astype(object)
            if out is None:
                out = df[keys + [c for c in df.columns if c not in keys]]
                continue
            new = [c for c in df.columns if c not in out.columns]
            out = out.merge(df[keys + new], on=keys, how="outer")
    return out


def iter_core(engine_bt, slugs=None, since=None):
    """FE_DMV_ALL + FE_DMV_SCORES for every (slug, timestamp) any signal table has, one
    calendar month at a time: yields (start, end, rows, scores) per non-empty month.

    Mirrors gcp_dmv_core.py: FULL OUTER JOIN, drop duplicate columns, fillna(0) (a pillar
    with no row for that key counts as 0), bullish/bearish/neutral = how many of
    columns[4:] equal 1/-1/0 (counted before the 3 new zero columns get values, so
    neutral includes them), then keep FE_DMV_ALL's columns and score each pillar as
    mean(d_/m_/v_ columns) * 100. Every value is per row, so months are independent.
    """
    tables = [t for t in CORE_SIGNAL_TABLES if table_columns(engine_bt, t)]
    absent = [t for t in CORE_SIGNAL_TABLES if t not in tables]
    if absent:
        logger.warning(f"  Signal tables missing from cp_backtest, treated as all-0: {absent}")
    dmv_cols = table_columns(engine_bt, "FE_DMV_ALL")

    first = pd.Timestamp(since if since is not None else "2013-01-01", tz="UTC")
    last = pd.Timestamp(f"{pd.Timestamp.now().year + 2}-01-01", tz="UTC")
    edges = [first] + [m for m in pd.date_range(first.replace(day=1), last, freq="MS") if m > first]
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

        scores = pd.concat([df[['slug', 'timestamp']], compute_dmv_scores(df)], axis=1)
        logger.info(f"  {start.date()}..{end.date()}: {len(df):,} rows")
        yield start, end, df, scores


def compute_core(engine_bt, slugs=None, since=None):
    """All of iter_core in two DataFrames (holds the whole result; for small runs)."""
    chunks = list(iter_core(engine_bt, slugs, since))
    if not chunks:
        return pd.DataFrame(), pd.DataFrame()
    return (pd.concat([c[2] for c in chunks], ignore_index=True),
            pd.concat([c[3] for c in chunks], ignore_index=True))


def _create_table(df, table_name, engine):
    logger.info(f"  {table_name} does not exist yet, creating...")
    safe_chunk = max(1, 65000 // len(df.columns))
    df.to_sql(table_name, con=engine, if_exists='replace',
              index=False, method='multi', chunksize=safe_chunk)
    logger.info(f"  {table_name} created with {len(df)} rows.")


def phase_core(engine_bt, slugs=None):
    logger.info("=" * 60)
    logger.info("PHASE 8: Backfilling FE_DMV_ALL + FE_DMV_SCORES")
    logger.info("=" * 60)

    try:
        stages = [StagedTable(engine_bt, "FE_DMV_ALL", guard=True),
                  StagedTable(engine_bt, "FE_DMV_SCORES", guard=True)]
    except LookupError:
        # A fresh cp_backtest without these tables: create them from the whole result.
        df, dmv_scores = compute_core(engine_bt, slugs)
        if df.empty:
            logger.warning("  No signal data found. Skipping core.")
            return
        for frame, name in [(df, "FE_DMV_ALL"), (dmv_scores, "FE_DMV_SCORES")]:
            if table_columns(engine_bt, name):
                truncate_and_insert(frame, name, engine_bt, guard=True)
            else:
                _create_table(frame, name, engine_bt)
        logger.info("  Core backfill complete.")
        return

    rows, coins, dates = 0, set(), 0
    for start, end, df, dmv_scores in iter_core(engine_bt, slugs):
        stages[0].add(df, {"start": start, "end": end})
        stages[1].add(dmv_scores, {"start": start, "end": end})
        rows += len(df)
        coins.update(df['slug'].unique())
        dates += df['timestamp'].nunique()
    if not rows:
        logger.warning("  No signal data found. Skipping core.")
        for st in stages:
            st.drop()
        return
    logger.info(f"  {rows:,} rows for {len(coins):,} coins over {dates:,} dates.")
    for st in stages:
        st.finish()
    logger.info("  Core backfill complete.")


# ============================================================
# MAIN
# ============================================================
ALL_PHASES = ["momentum", "oscillators", "tvv", "pct", "ratios", "metrics", "core"]
# Run only when named in --only:
#   ratios  - rewrites FE_RATIOS(+_SIGNALS) for all coins: ~0.5 min compute (vectorised),
#             then ~1 h of inserts for ~2.75M rows per table on the crypto VM
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
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SLUGS,
        help=f"Coins per batch for ratios and metrics (default {BATCH_SLUGS}); "
             "lower it to use less memory."
    )
    args = parser.parse_args()
    DRY_RUN = args.dry_run
    ALLOW_SHRINK = args.allow_shrink
    BATCH_SLUGS = max(1, args.batch_size)
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

    # The default phases take all OHLCV at once; ratios and metrics fetch it per batch
    ohlcv_phases = {"momentum", "oscillators", "tvv", "pct"}
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
    ohlcv_df = None
    if {"ratios", "metrics"} & set(selected):
        all_slugs = fetch_slugs(engine_live, slugs)
        if "ratios" in selected:
            phase_ratios(engine_live, engine_bt, all_slugs)
        if "metrics" in selected:
            phase_metrics(engine_live, engine_bt, all_slugs)
    if "core" in selected:
        phase_core(engine_bt, slugs)

    # Final diagnosis
    logger.info("")
    logger.info("FINAL STATE:")
    phase_diagnose(engine_bt)

    engine_live.dispose()
    engine_bt.dispose()

    elapsed = (time.time() - overall_start) / 60
    # ru_maxrss is KiB on Linux, bytes on macOS
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10)
    logger.info(f"BACKFILL COMPLETE in {elapsed:.1f} minutes (peak memory {peak:,.0f} MB).")
