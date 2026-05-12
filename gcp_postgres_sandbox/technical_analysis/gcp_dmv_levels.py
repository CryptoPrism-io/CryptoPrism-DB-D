# Fibonacci retracement levels + ATR bands (Zerodha Varsity Module 2 final indicators).
# Phase 5 of the DMV indicator expansion.
#
# Computes per slug:
#   - fib_swing_low / fib_swing_high  -- anchor points (latest swing low and high in lookback)
#   - fib_0_236 / fib_0_382 / fib_0_500 / fib_0_618 / fib_0_786
#   - atr_band_mid / atr_band_upper / atr_band_lower (mid = 5-SMA close; bands +/- 3*ATR14)
#   - m_lvl_fib_proximity_bin (bigint, default 0):
#       +1 if close is within 1% of fib_0_382 or fib_0_618 (classic buying retracement)
#       -1 if close is within 1% of fib_0_236 (shallow rejection -- short)
#   - m_lvl_atr_band_bin (bigint, default 0):
#       +1 if close < atr_band_lower (oversold envelope)
#       -1 if close > atr_band_upper (overbought envelope)
#
# Bins use FE_OSCILLATORS_SIGNALS default-0 convention.

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


SWING_DISTANCE = 5
LOOKBACK = 60
PROXIMITY_PCT = 0.01    # 1% band for fib_proximity signal
ATR_PERIOD = 14
SMA_PERIOD = 5
ATR_BAND_MULT = 3.0


def _detect_per_slug(g):
    g = g.reset_index(drop=True)
    n = len(g)
    out = {
        "fib_swing_low":  np.nan,
        "fib_swing_high": np.nan,
        "fib_0_236": np.nan, "fib_0_382": np.nan,
        "fib_0_500": np.nan, "fib_0_618": np.nan, "fib_0_786": np.nan,
        "atr_band_mid": np.nan, "atr_band_upper": np.nan, "atr_band_lower": np.nan,
        "m_lvl_fib_proximity_bin": 0,
        "m_lvl_atr_band_bin": 0,
    }
    if n < ATR_PERIOD + 2:
        return pd.Series(out)

    window = g.iloc[-LOOKBACK:].reset_index(drop=True) if n > LOOKBACK else g
    closes = window["close"].to_numpy()
    highs  = window["high"].to_numpy()
    lows   = window["low"].to_numpy()
    last_close = closes[-1]

    # Anchor fib levels at the most recent swing low and swing high within the window.
    high_idx, _ = find_peaks(closes,  distance=SWING_DISTANCE)
    low_idx,  _ = find_peaks(-closes, distance=SWING_DISTANCE)

    if len(high_idx) >= 1 and len(low_idx) >= 1:
        # Use the most recent peak and the most recent trough.
        anchor_high_idx = int(high_idx[-1])
        anchor_low_idx  = int(low_idx[-1])
        # Order them so high > low.
        if anchor_high_idx > anchor_low_idx:
            swing_low  = float(closes[anchor_low_idx])
            swing_high = float(closes[anchor_high_idx])
        else:
            swing_low  = float(closes[anchor_low_idx])
            swing_high = float(closes[anchor_high_idx])
        if swing_high > swing_low:
            out["fib_swing_low"]  = swing_low
            out["fib_swing_high"] = swing_high
            diff = swing_high - swing_low
            out["fib_0_236"] = swing_high - 0.236 * diff
            out["fib_0_382"] = swing_high - 0.382 * diff
            out["fib_0_500"] = swing_high - 0.500 * diff
            out["fib_0_618"] = swing_high - 0.618 * diff
            out["fib_0_786"] = swing_high - 0.786 * diff

            # Proximity bin: classic retracement buy zones at 38.2%/61.8%.
            for level_name, lev in [("fib_0_382", out["fib_0_382"]),
                                    ("fib_0_618", out["fib_0_618"])]:
                if lev and abs(last_close - lev) / max(lev, 1e-9) <= PROXIMITY_PCT:
                    out["m_lvl_fib_proximity_bin"] = 1
                    break
            if out["m_lvl_fib_proximity_bin"] == 0:
                lev = out["fib_0_236"]
                if lev and abs(last_close - lev) / max(lev, 1e-9) <= PROXIMITY_PCT:
                    out["m_lvl_fib_proximity_bin"] = -1

    # ATR band envelope.
    # Local ATR(14) on True Range computed within the window.
    if len(window) >= ATR_PERIOD + 1:
        prev_close = np.concatenate([[np.nan], closes[:-1]])
        tr = np.maximum.reduce([
            highs - lows,
            np.abs(highs - prev_close),
            np.abs(lows  - prev_close),
        ])
        atr_series = pd.Series(tr).ewm(alpha=1.0 / ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()
        atr_last = float(atr_series.iloc[-1]) if not np.isnan(atr_series.iloc[-1]) else np.nan
        sma_last = float(np.mean(closes[-SMA_PERIOD:]))
        if not np.isnan(atr_last):
            out["atr_band_mid"]   = sma_last
            out["atr_band_upper"] = sma_last + ATR_BAND_MULT * atr_last
            out["atr_band_lower"] = sma_last - ATR_BAND_MULT * atr_last
            if last_close < out["atr_band_lower"]:
                out["m_lvl_atr_band_bin"] = 1
            elif last_close > out["atr_band_upper"]:
                out["m_lvl_atr_band_bin"] = -1

    return pd.Series(out)


def compute_levels_per_slug(df):
    logger.info("Computing Fibonacci levels + ATR bands per slug...")
    t0 = time.time()
    grouped = df.groupby("slug", sort=False).apply(_detect_per_slug, include_groups=False)
    logger.info(f"  done in {time.time() - t0:.1f}s")
    return grouped.reset_index()


VALUE_COLS = [
    "fib_swing_low", "fib_swing_high",
    "fib_0_236", "fib_0_382", "fib_0_500", "fib_0_618", "fib_0_786",
    "atr_band_mid", "atr_band_upper", "atr_band_lower",
]
BIN_COLS = ["m_lvl_fib_proximity_bin", "m_lvl_atr_band_bin"]


def push_to_db(df, table_name, engine):
    with engine.connect() as conn:
        conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
        conn.commit()
    df.to_sql(table_name, con=engine, if_exists="append", index=False)
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
    df.to_sql(table_name, con=engine, if_exists="append", index=False)
    logger.info(f"{table_name} (cp_backtest) appended.")


if __name__ == "__main__":
    t0 = time.time()
    engine = create_db_engine()
    engine_bt = create_db_engine_backtest()

    df = fetch_data(engine)
    results = compute_levels_per_slug(df)

    latest_ts = df.loc[df.groupby("slug")["timestamp"].idxmax(), ["slug", "timestamp"]]
    out = latest_ts.merge(results, on="slug", how="left")

    for c in BIN_COLS:
        out[c] = out[c].astype("Int64")

    out["id"] = pd.NA
    out = out[["id", "slug", "timestamp"] + VALUE_COLS + BIN_COLS]

    push_to_db(out, "FE_PRICE_LEVELS", engine)
    push_to_db_backtest(out, "FE_PRICE_LEVELS", engine_bt)

    engine.dispose()
    engine_bt.dispose()
    logger.info(f"Done in {(time.time() - t0)/60:.2f} minutes.")
