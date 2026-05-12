# Phase 3 + Phase 5 historical backfill on cp_backtest.
# - Dow patterns (S/R, double/triple tops, range breakout, flag)
# - Fibonacci levels + ATR band envelope
#
# Bulk-loads via psycopg2 COPY. Idempotent: TRUNCATE then COPY.
#
# These detectors are per-slug stateful (peak-finding on the last 60 bars).
# We slide through each slug's history bar by bar, computing the detector at
# every timestamp using the prior 60 bars as context. That is the only way to
# reconstruct historical signals correctly.
import io
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent / "gcp_postgres_sandbox" / "technical_analysis"))
import gcp_dmv_dow as dow         # noqa: E402
import gcp_dmv_levels as levels   # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if Path(".env").exists():
    load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME_BT = os.getenv("DB_NAME_BT", "cp_backtest")


def engine_bt():
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}"
    return create_engine(url, pool_pre_ping=True)


def copy_dataframe(df, table_name, cols):
    """Stream a DataFrame to Postgres via COPY FROM STDIN using TEXT format.
    TEXT format natively treats `\\N` as NULL, which CSV format does not."""
    buf = io.StringIO()
    df[cols].to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)
    cn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dbname=DB_NAME_BT,
    )
    try:
        with cn, cn.cursor() as cur:
            cols_csv = '","'.join(cols)
            # FORMAT text (default), DELIMITER tab (default), NULL `\N` (default).
            cur.copy_expert(
                f'COPY "{table_name}" ("{cols_csv}") FROM STDIN',
                buf,
            )
    finally:
        cn.close()


def run_detector_per_slug(df_full, detector_fn):
    """Iterate slug-by-slug, bar-by-bar, applying the detector on a rolling
    window of the prior bars. Returns one row per (slug, timestamp)."""
    out_rows = []
    for slug, g in df_full.groupby("slug", sort=False):
        g = g.reset_index(drop=True)
        for i in range(len(g)):
            # Detector expects access to bars [0..i]
            sub = g.iloc[: i + 1]
            sig = detector_fn(sub)
            row = {"slug": slug, "timestamp": g.iloc[i]["timestamp"]}
            for k, v in sig.items():
                row[k] = v
            out_rows.append(row)
    return pd.DataFrame(out_rows)


def main():
    t0 = time.time()
    eng = engine_bt()

    logger.info("Fetching OHLCV history from cp_backtest FE_TVV...")
    df = pd.read_sql_query(
        text('SELECT slug, "timestamp", "open", high, low, close, volume '
             'FROM "FE_TVV" ORDER BY slug, "timestamp"'),
        eng,
    )
    logger.info(f"Loaded {len(df):,} rows / {df['slug'].nunique()} slugs in {time.time()-t0:.1f}s")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    # ----- Phase 3: Dow patterns -----
    logger.info("Phase 3: detecting Dow patterns per slug, per bar...")
    t1 = time.time()
    dow_out = run_detector_per_slug(df, dow._detect_per_slug)
    logger.info(f"  done in {(time.time()-t1)/60:.2f} min, {len(dow_out):,} rows")

    DOW_BIN_COLS = [
        "m_dow_dbl_top_bin", "m_dow_dbl_bot_bin",
        "m_dow_trpl_top_bin", "m_dow_trpl_bot_bin",
        "m_dow_range_break_up_bin", "m_dow_range_break_dn_bin",
        "m_dow_flag_bin",
    ]
    DOW_VAL_COLS = ["m_dow_support", "m_dow_resistance"]
    DOW_ALL = DOW_VAL_COLS + DOW_BIN_COLS

    for c in DOW_BIN_COLS:
        dow_out[c] = dow_out[c].astype("Int64")

    logger.info("TRUNCATE FE_DOW_PATTERNS...")
    with eng.begin() as conn:
        conn.execute(text('TRUNCATE TABLE "FE_DOW_PATTERNS"'))

    logger.info("COPY FE_DOW_PATTERNS...")
    copy_dataframe(dow_out, "FE_DOW_PATTERNS", ["slug", "timestamp"] + DOW_ALL)

    # ----- Phase 5: Fibonacci levels + ATR bands -----
    logger.info("Phase 5: computing Fibonacci levels + ATR bands per slug, per bar...")
    t2 = time.time()
    lvl_out = run_detector_per_slug(df, levels._detect_per_slug)
    logger.info(f"  done in {(time.time()-t2)/60:.2f} min, {len(lvl_out):,} rows")

    LVL_BIN_COLS = ["m_lvl_fib_proximity_bin", "m_lvl_atr_band_bin"]
    LVL_VAL_COLS = [
        "fib_swing_low", "fib_swing_high",
        "fib_0_236", "fib_0_382", "fib_0_500", "fib_0_618", "fib_0_786",
        "atr_band_mid", "atr_band_upper", "atr_band_lower",
    ]
    LVL_ALL = LVL_VAL_COLS + LVL_BIN_COLS

    for c in LVL_BIN_COLS:
        lvl_out[c] = lvl_out[c].astype("Int64")

    logger.info("TRUNCATE FE_PRICE_LEVELS...")
    with eng.begin() as conn:
        conn.execute(text('TRUNCATE TABLE "FE_PRICE_LEVELS"'))

    logger.info("COPY FE_PRICE_LEVELS...")
    copy_dataframe(lvl_out, "FE_PRICE_LEVELS", ["slug", "timestamp"] + LVL_ALL)

    eng.dispose()
    logger.info(f"=== Phase 3 + 5 backfill complete in {(time.time()-t0)/60:.2f} minutes ===")


if __name__ == "__main__":
    main()
