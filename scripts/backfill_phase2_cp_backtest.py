# Phase 2 historical backfill on cp_backtest.
# Computes 9 candlestick pattern bins on all 1.13M historical OHLCV rows
# stored in cp_backtest FE_TVV, then bulk-loads into FE_CANDLESTICK_SIGNALS
# via psycopg2 COPY (10-100x faster than pandas to_sql / pg8000 multi-insert).
# Idempotent: TRUNCATE then COPY.
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

# Reuse the production detectors. Add the candlestick module to the path.
THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent / "gcp_postgres_sandbox" / "technical_analysis"))
import gcp_dmv_candle as candle  # noqa: E402

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


BIN_COLS = [c for c, _ in candle.PATTERN_FUNCS]


def main():
    t0 = time.time()
    eng = engine_bt()

    logger.info("Fetching OHLC history from cp_backtest FE_TVV...")
    df = pd.read_sql_query(
        text('SELECT slug, "timestamp", "open", high, low, close '
             'FROM "FE_TVV" ORDER BY slug, "timestamp"'),
        eng,
    )
    logger.info(f"Loaded {len(df):,} rows across {df['slug'].nunique()} slugs in {time.time() - t0:.1f}s")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    logger.info("Computing candlestick patterns...")
    t1 = time.time()
    df = candle.compute_all_patterns(df)
    logger.info(f"  patterns done in {time.time() - t1:.1f}s")

    out = df[["slug", "timestamp"] + BIN_COLS].copy()
    # bigint nullable; bins are already 0/+/-1 ints
    for c in BIN_COLS:
        out[c] = out[c].astype("Int64")

    logger.info("TRUNCATE FE_CANDLESTICK_SIGNALS on cp_backtest...")
    with eng.begin() as conn:
        conn.execute(text('TRUNCATE TABLE "FE_CANDLESTICK_SIGNALS"'))
    eng.dispose()

    # Bulk-load via psycopg2 COPY FROM STDIN — orders of magnitude faster than
    # pandas to_sql with pg8000. Build an in-memory CSV buffer and stream it.
    logger.info(f"Streaming {len(out):,} rows via COPY...")
    buf = io.StringIO()
    out.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
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
            cols_csv = '","'.join(["slug", "timestamp"] + BIN_COLS)
            cur.copy_expert(
                f'COPY "FE_CANDLESTICK_SIGNALS" ("{cols_csv}") '
                f"FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t', NULL '\\\\N')",
                buf,
            )
    finally:
        cn.close()

    logger.info(f"=== Backfill complete in {(time.time() - t0)/60:.2f} minutes ===")


if __name__ == "__main__":
    main()
