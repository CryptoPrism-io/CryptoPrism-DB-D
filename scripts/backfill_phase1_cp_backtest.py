# Phase 1 historical backfill on cp_backtest.
# Pulls OHLC history from cp_backtest FE_TVV (single query, all 1.13M rows),
# computes BB / Supertrend / Aroon per slug, then UPDATE FROM staging
# back into the four FE_* tables.
#
# Idempotent: rerunning overwrites with the same values.
# Usage: python scripts/backfill_phase1_cp_backtest.py
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if not os.getenv("GITHUB_ACTIONS"):
    if Path(".env").exists():
        load_dotenv()
        logger.info(".env loaded.")

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME_BT = os.getenv("DB_NAME_BT", "cp_backtest")

missing = [v for v in ["DB_HOST", "DB_USER", "DB_PASSWORD"] if not os.getenv(v)]
if missing:
    logger.error(f"Missing env vars: {missing}")
    sys.exit(1)


def engine_bt():
    url = f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}"
    return create_engine(url, pool_pre_ping=True)


# ---------- Indicator math (mirrors the production scripts exactly) ----------

def calculate_bollinger_bands(df, period=20, num_std=2.0):
    df["BB_Mid"] = df.groupby("slug")["close"].transform(
        lambda s: s.rolling(period, min_periods=period).mean()
    )
    rolling_std = df.groupby("slug")["close"].transform(
        lambda s: s.rolling(period, min_periods=period).std()
    )
    df["BB_Upper"] = df["BB_Mid"] + num_std * rolling_std
    df["BB_Lower"] = df["BB_Mid"] - num_std * rolling_std
    df["BB_Width"] = (df["BB_Upper"] - df["BB_Lower"]) / df["BB_Mid"]
    df["BB_Pct_B"] = (df["close"] - df["BB_Lower"]) / (df["BB_Upper"] - df["BB_Lower"])
    return df


def calculate_supertrend(df, atr_period=10, multiplier=3.0):
    prev_close = df.groupby("slug")["close"].shift(1)
    tr_local = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs()
        )
    )
    atr_local = tr_local.groupby(df["slug"]).transform(
        lambda s: s.ewm(alpha=1.0 / atr_period, adjust=False, min_periods=atr_period).mean()
    )

    hl2 = (df["high"] + df["low"]) / 2.0
    basic_upper = hl2 + multiplier * atr_local
    basic_lower = hl2 - multiplier * atr_local

    st_line = np.full(len(df), np.nan)
    st_dir = np.full(len(df), np.nan)

    for slug, idx in df.groupby("slug", sort=False).indices.items():
        idx = list(idx)
        final_upper_prev = np.nan
        final_lower_prev = np.nan
        dir_prev = 1.0
        close_prev = np.nan
        first_valid = True

        for i in idx:
            bu = basic_upper.iloc[i]
            bl = basic_lower.iloc[i]
            close_i = df["close"].iloc[i]

            if np.isnan(bu) or np.isnan(bl):
                final_upper_prev = np.nan
                final_lower_prev = np.nan
                close_prev = close_i
                continue

            if np.isnan(final_upper_prev) or bu < final_upper_prev or close_prev > final_upper_prev:
                final_upper = bu
            else:
                final_upper = final_upper_prev

            if np.isnan(final_lower_prev) or bl > final_lower_prev or close_prev < final_lower_prev:
                final_lower = bl
            else:
                final_lower = final_lower_prev

            if first_valid:
                cur_dir = 1.0 if close_i >= hl2.iloc[i] else -1.0
                first_valid = False
            else:
                if close_i > final_upper_prev:
                    cur_dir = 1.0
                elif close_i < final_lower_prev:
                    cur_dir = -1.0
                else:
                    cur_dir = dir_prev

            st_line[i] = final_lower if cur_dir == 1.0 else final_upper
            st_dir[i] = cur_dir

            final_upper_prev = final_upper
            final_lower_prev = final_lower
            dir_prev = cur_dir
            close_prev = close_i

    df["Supertrend_Line"] = st_line
    df["Supertrend_Dir"] = st_dir
    return df


def calculate_aroon(df, period=25):
    df["Aroon_Up"] = df.groupby("slug")["high"].transform(
        lambda s: s.rolling(period + 1, min_periods=period + 1).apply(
            lambda w: 100.0 * w.argmax() / period, raw=True
        )
    )
    df["Aroon_Down"] = df.groupby("slug")["low"].transform(
        lambda s: s.rolling(period + 1, min_periods=period + 1).apply(
            lambda w: 100.0 * w.argmin() / period, raw=True
        )
    )
    df["Aroon_Osc"] = df["Aroon_Up"] - df["Aroon_Down"]
    return df


def compute_bins(df):
    # BB bin: keep NaN-propagation (FE_TVV_SIGNALS uses double precision and other
    # bins in that table can be NaN, e.g. np.sign of OBV diff).
    bb_bin = np.where(df["close"] <= df["BB_Lower"], 1,
              np.where(df["close"] >= df["BB_Upper"], -1, 0)).astype(float)
    bb_bin[df["BB_Mid"].isna().to_numpy()] = np.nan
    df["m_tvv_bb_bin"] = bb_bin

    # Supertrend bin: default 0 when source NaN (FE_OSCILLATORS_SIGNALS bigint
    # convention; other osc bins use np.select with default=0, never NaN).
    df["m_osc_supertrend_bin"] = np.select(
        [df["Supertrend_Dir"] > 0, df["Supertrend_Dir"] < 0],
        [1, -1],
        default=0
    ).astype(np.int64)

    # Aroon bin: same default-0 convention.
    df["m_osc_aroon_bin"] = np.select(
        [(df["Aroon_Up"] >= 70) & (df["Aroon_Down"] <= 30),
         (df["Aroon_Down"] >= 70) & (df["Aroon_Up"] <= 30)],
        [1, -1],
        default=0
    ).astype(np.int64)

    return df


# ---------- Staging-based bulk update ----------

STAGING_SPECS = {
    "FE_TVV": {
        "staging": "_phase1_bb_staging",
        "cols": ["BB_Mid", "BB_Upper", "BB_Lower", "BB_Width", "BB_Pct_B"],
        "types": "double precision",
    },
    "FE_TVV_SIGNALS": {
        "staging": "_phase1_bb_bin_staging",
        "cols": ["m_tvv_bb_bin"],
        "types": "double precision",
    },
    "FE_OSCILLATOR": {
        "staging": "_phase1_osc_staging",
        "cols": ["Supertrend_Line", "Supertrend_Dir", "Aroon_Up", "Aroon_Down", "Aroon_Osc"],
        "types": "double precision",
    },
    "FE_OSCILLATORS_SIGNALS": {
        "staging": "_phase1_osc_bin_staging",
        "cols": ["m_osc_supertrend_bin", "m_osc_aroon_bin"],
        "types": "bigint",
    },
}


def _create_staging(conn, spec):
    cols_sql = ", ".join([f'"{c}" {spec["types"]}' for c in spec["cols"]])
    conn.execute(text(f'DROP TABLE IF EXISTS "{spec["staging"]}"'))
    conn.execute(text(
        f'CREATE TABLE "{spec["staging"]}" '
        f'(slug text NOT NULL, "timestamp" timestamptz NOT NULL, {cols_sql}, '
        f'PRIMARY KEY (slug, "timestamp"))'
    ))


def _drop_staging(conn, spec):
    conn.execute(text(f'DROP TABLE IF EXISTS "{spec["staging"]}"'))


def _update_target_from_staging(conn, target_table, spec):
    set_clause = ", ".join([f'"{c}" = s."{c}"' for c in spec["cols"]])
    sql = (
        f'UPDATE "{target_table}" t SET {set_clause} '
        f'FROM "{spec["staging"]}" s '
        f'WHERE t.slug = s.slug AND t."timestamp" = s."timestamp"'
    )
    result = conn.execute(text(sql))
    return result.rowcount


def write_table(engine, target_table, df_all):
    spec = STAGING_SPECS[target_table]
    cols = spec["cols"]
    staging_df = df_all[["slug", "timestamp"] + cols].copy()

    # For bigint signal columns, pandas float NaN -> SQL NULL via pg8000 needs Int64 dtype
    if spec["types"] == "bigint":
        for c in cols:
            staging_df[c] = staging_df[c].astype("Int64")

    # pg8000 binds parameters as 16-bit ints in the BIND message (limit 65535).
    # method="multi" packs chunksize * num_columns params into a single INSERT.
    # Compute the largest chunk that stays under the limit, with a safety margin.
    num_cols = 2 + len(cols)  # slug + timestamp + value cols
    safe_chunksize = max(500, (65000 // num_cols))

    logger.info(f"  [{target_table}] writing {len(staging_df):,} rows to staging "
                f"{spec['staging']} (chunksize={safe_chunksize}, cols={num_cols})...")
    with engine.begin() as conn:
        _create_staging(conn, spec)

    staging_df.to_sql(spec["staging"], con=engine, if_exists="append",
                      index=False, chunksize=safe_chunksize, method="multi")

    with engine.begin() as conn:
        logger.info(f"  [{target_table}] applying UPDATE FROM {spec['staging']}...")
        n = _update_target_from_staging(conn, target_table, spec)
        logger.info(f"  [{target_table}] {n:,} rows updated")
        _drop_staging(conn, spec)


def main():
    t0 = time.time()
    engine = engine_bt()

    logger.info("Fetching all OHLC history from cp_backtest FE_TVV...")
    df = pd.read_sql_query(
        text('SELECT slug, "timestamp", high, low, close FROM "FE_TVV" '
             'ORDER BY slug, "timestamp"'),
        engine,
    )
    logger.info(f"Loaded {len(df):,} rows across {df['slug'].nunique()} slugs "
                f"in {time.time() - t0:.1f}s")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    logger.info("Computing Bollinger Bands...")
    t1 = time.time()
    df = calculate_bollinger_bands(df)
    logger.info(f"  done in {time.time() - t1:.1f}s")

    logger.info("Computing Supertrend (per-slug stateful loop)...")
    t1 = time.time()
    df = calculate_supertrend(df)
    logger.info(f"  done in {time.time() - t1:.1f}s")

    logger.info("Computing Aroon...")
    t1 = time.time()
    df = calculate_aroon(df)
    logger.info(f"  done in {time.time() - t1:.1f}s")

    logger.info("Computing bin signals...")
    df = compute_bins(df)

    logger.info("Writing to FE_TVV (BB values)...")
    write_table(engine, "FE_TVV", df)

    logger.info("Writing to FE_TVV_SIGNALS (bb bin)...")
    write_table(engine, "FE_TVV_SIGNALS", df)

    logger.info("Writing to FE_OSCILLATOR (Supertrend + Aroon values)...")
    write_table(engine, "FE_OSCILLATOR", df)

    logger.info("Writing to FE_OSCILLATORS_SIGNALS (supertrend + aroon bins)...")
    write_table(engine, "FE_OSCILLATORS_SIGNALS", df)

    engine.dispose()
    logger.info(f"=== Backfill complete in {(time.time() - t0) / 60:.2f} minutes ===")


if __name__ == "__main__":
    main()
