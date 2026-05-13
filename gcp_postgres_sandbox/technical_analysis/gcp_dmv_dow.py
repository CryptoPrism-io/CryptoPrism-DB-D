# Dow Theory price-action patterns (Zerodha Varsity Module 2)
# Phase 3 of the DMV indicator expansion.
#
# Detects:
#   - m_dow_support           (double precision)  most recent twice-tested swing low
#   - m_dow_resistance        (double precision)  most recent twice-tested swing high
#   - m_dow_dbl_top_bin       (bigint)   -1 / 0  -- two swing highs ~equal price
#   - m_dow_dbl_bot_bin       (bigint)   +1 / 0  -- two swing lows  ~equal price
#   - m_dow_trpl_top_bin      (bigint)   -1 / 0  -- three swing highs ~equal price
#   - m_dow_trpl_bot_bin      (bigint)   +1 / 0
#   - m_dow_range_break_up_bin (bigint)  +1 / 0  -- close > prior resistance + above-avg volume
#   - m_dow_range_break_dn_bin (bigint)  -1 / 0
#   - m_dow_flag_bin          (bigint)  +1 / -1 / 0  -- sharp move then tight consolidation
#
# Uses scipy.signal.find_peaks for swing detection. All bigint bins default to 0
# (FE_OSCILLATORS_SIGNALS convention) so dmv_core's drop-rows-with-NaN filter
# does not penalise short-history slugs.
import logging
import os
import time

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from scipy.signal import find_peaks
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

if not os.getenv("GITHUB_ACTIONS"):
    if os.path.exists(".env"):
        load_dotenv()
        logger.info(".env loaded.")

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "dbcp")
DB_NAME_BT = os.getenv("DB_NAME_BT", "cp_backtest")

missing = [v for v in ["DB_HOST", "DB_USER", "DB_PASSWORD"] if not os.getenv(v)]
if missing:
    logger.error(f"Missing env vars: {missing}")
    raise SystemExit(1)


def create_db_engine():
    return create_engine(
        f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
        pool_pre_ping=True,
    )


def create_db_engine_backtest():
    return create_engine(
        f"postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}",
        pool_pre_ping=True,
    )


def fetch_data(engine):
    logger.info("Fetching OHLCV history (110 days)...")
    query = """
        SELECT "public"."1K_coins_ohlcv".*
        FROM "public"."1K_coins_ohlcv"
        INNER JOIN "public"."crypto_listings_latest_1000"
          ON "public"."1K_coins_ohlcv"."slug" = "public"."crypto_listings_latest_1000"."slug"
        WHERE "public"."crypto_listings_latest_1000"."cmc_rank" <= 1000
          AND "public"."1K_coins_ohlcv"."timestamp" >= NOW() - INTERVAL '110 days';
    """
    with engine.connect() as conn:
        df = pd.read_sql_query(query, conn)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values(["slug", "timestamp"], inplace=True)
    df.reset_index(drop=True, inplace=True)
    logger.info(f"Fetched {len(df):,} rows / {df['slug'].nunique()} slugs")
    return df


# ---------- Swing detection ----------
#
# A swing high at index i is a local maximum of `high` (or `close`) with at
# least `distance` bars on either side that are strictly lower.
# Similarly for swing low. We use scipy.signal.find_peaks on close.

DISTANCE = 5                # min separation between peaks (bars)
PRICE_TOLERANCE = 0.02      # 2% tolerance when comparing two peak/trough prices
BREAKOUT_VOL_RATIO = 1.3    # volume must be at least 1.3x 10-day avg for a breakout
LOOKBACK_BARS = 60          # window of bars used for pattern detection per slug


def _detect_per_slug(g):
    """Compute all Dow pattern values + bins for one slug's history."""
    g = g.reset_index(drop=True)
    n = len(g)
    out = {
        "m_dow_support": np.nan,
        "m_dow_resistance": np.nan,
        "m_dow_dbl_top_bin": 0,
        "m_dow_dbl_bot_bin": 0,
        "m_dow_trpl_top_bin": 0,
        "m_dow_trpl_bot_bin": 0,
        "m_dow_range_break_up_bin": 0,
        "m_dow_range_break_dn_bin": 0,
        "m_dow_flag_bin": 0,
    }
    if n < DISTANCE * 2 + 1:
        return pd.Series(out)

    # Restrict to a recent window for performance and relevance.
    window = g.iloc[-LOOKBACK_BARS:].reset_index(drop=True) if n > LOOKBACK_BARS else g
    closes = window["close"].to_numpy()
    highs  = window["high"].to_numpy()
    lows   = window["low"].to_numpy()
    vols   = window["volume"].to_numpy()
    nW = len(window)
    last_close = closes[-1]
    last_vol   = vols[-1]
    vol_avg_10 = float(np.nanmean(vols[-10:])) if nW >= 1 else np.nan

    # Swing highs / lows from close-series
    high_idx, _ = find_peaks(closes,  distance=DISTANCE)
    low_idx,  _ = find_peaks(-closes, distance=DISTANCE)

    swing_highs = closes[high_idx] if len(high_idx) else np.array([])
    swing_lows  = closes[low_idx]  if len(low_idx)  else np.array([])

    # Resistance: most recent swing high that has at least one prior peak within tolerance.
    if len(swing_highs) >= 2:
        last_peak = swing_highs[-1]
        for prev in swing_highs[-6:-1][::-1]:                       # look back up to 5 prior peaks
            if abs(prev - last_peak) / max(prev, 1e-9) <= PRICE_TOLERANCE:
                out["m_dow_resistance"] = (prev + last_peak) / 2.0
                # Double top: exactly two tested peaks (no third yet)
                out["m_dow_dbl_top_bin"] = -1
                break

    # Support: same logic on lows.
    if len(swing_lows) >= 2:
        last_trough = swing_lows[-1]
        for prev in swing_lows[-6:-1][::-1]:
            if abs(prev - last_trough) / max(prev, 1e-9) <= PRICE_TOLERANCE:
                out["m_dow_support"] = (prev + last_trough) / 2.0
                out["m_dow_dbl_bot_bin"] = 1
                break

    # Triple top / bottom: three of the last <=5 peaks/troughs within tolerance.
    def _triple(series):
        if len(series) < 3:
            return False
        last3 = series[-3:]
        mid = float(np.mean(last3))
        return bool(np.all(np.abs(last3 - mid) / max(abs(mid), 1e-9) <= PRICE_TOLERANCE))

    if _triple(swing_highs):
        out["m_dow_trpl_top_bin"] = -1
        # if triple is detected, the double flag should NOT stand alone
        out["m_dow_dbl_top_bin"] = 0
    if _triple(swing_lows):
        out["m_dow_trpl_bot_bin"] = 1
        out["m_dow_dbl_bot_bin"] = 0

    # Range breakout: current close pierces the most recent twice-tested S/R level
    # AND volume is at least 1.3x the 10-day average.
    res = out["m_dow_resistance"]
    sup = out["m_dow_support"]
    if not np.isnan(vol_avg_10) and vol_avg_10 > 0:
        vol_strong = last_vol >= BREAKOUT_VOL_RATIO * vol_avg_10
        if vol_strong:
            if not np.isnan(res) and last_close > res:
                out["m_dow_range_break_up_bin"] = 1
            if not np.isnan(sup) and last_close < sup:
                out["m_dow_range_break_dn_bin"] = -1

    # Flag: a 10%+ move over the prior 15 bars, then a tight 5-bar range (<=3%).
    if nW >= 20:
        recent5_high = highs[-5:].max()
        recent5_low  = lows[-5:].min()
        recent5_range_pct = (recent5_high - recent5_low) / max(recent5_low, 1e-9)

        prior15_close_start = closes[-20]
        prior15_close_end   = closes[-6]
        prior_move_pct = (prior15_close_end - prior15_close_start) / max(abs(prior15_close_start), 1e-9)

        if recent5_range_pct <= 0.03 and abs(prior_move_pct) >= 0.10:
            out["m_dow_flag_bin"] = 1 if prior_move_pct > 0 else -1

    return pd.Series(out)


def compute_dow_per_slug(df):
    logger.info("Detecting Dow patterns per slug...")
    t0 = time.time()
    grouped = df.groupby("slug", sort=False).apply(_detect_per_slug, include_groups=False)
    logger.info(f"  done in {time.time() - t0:.1f}s")
    grouped = grouped.reset_index()
    return grouped


VALUE_COLS = ["m_dow_support", "m_dow_resistance"]
BIN_COLS = [
    "m_dow_dbl_top_bin", "m_dow_dbl_bot_bin",
    "m_dow_trpl_top_bin", "m_dow_trpl_bot_bin",
    "m_dow_range_break_up_bin", "m_dow_range_break_dn_bin",
    "m_dow_flag_bin",
]


def push_to_db(df, table_name, engine):
    with engine.connect() as conn:
        conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
        conn.commit()
    df.to_sql(table_name, con=engine, if_exists="append", index=False, method="multi", chunksize=200)
    logger.info(f"{table_name} (dbcp) uploaded.")


def push_to_db_backtest(df, table_name, engine):
    if "timestamp" in df.columns:
        timestamps = df["timestamp"].dropna().unique().tolist()
        with engine.connect() as conn:
            for ts in timestamps:
                conn.execute(
                    text(f'DELETE FROM "{table_name}" WHERE "timestamp" = :ts'),
                    {"ts": ts},
                )
            conn.commit()
    df.to_sql(table_name, con=engine, if_exists="append", index=False, method="multi", chunksize=200)
    logger.info(f"{table_name} (cp_backtest) appended.")


if __name__ == "__main__":
    t0 = time.time()
    engine = create_db_engine()
    engine_bt = create_db_engine_backtest()

    df = fetch_data(engine)
    results = compute_dow_per_slug(df)

    latest_ts = df.loc[df.groupby("slug")["timestamp"].idxmax(), ["slug", "timestamp"]]
    out = latest_ts.merge(results, on="slug", how="left")

    for c in BIN_COLS:
        out[c] = out[c].astype("Int64")

    out["id"] = pd.array([pd.NA] * len(out), dtype="Int64")
    out = out[["id", "slug", "timestamp"] + VALUE_COLS + BIN_COLS]

    push_to_db(out, "FE_DOW_PATTERNS", engine)
    push_to_db_backtest(out, "FE_DOW_PATTERNS", engine_bt)

    engine.dispose()
    engine_bt.dispose()
    logger.info(f"Done in {(time.time() - t0)/60:.2f} minutes.")
