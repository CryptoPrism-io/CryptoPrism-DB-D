# Candlestick patterns (Zerodha Varsity Module 2)
# Phase 2 of the DMV indicator expansion.
#
# Implements 9 directional candlestick patterns as plain vectorised pandas/numpy:
#   1. Marubozu              (bullish +1 / bearish -1)
#   2. Hammer                (bullish +1)        -- paper umbrella after downtrend
#   3. Hanging Man           (bearish -1)        -- paper umbrella after uptrend
#   4. Shooting Star         (bearish -1)        -- inverted paper umbrella after uptrend
#   5. Engulfing             (bullish +1 / bearish -1)
#   6. Harami                (bullish +1 / bearish -1)
#   7. Piercing              (bullish +1)
#   8. Dark Cloud Cover      (bearish -1)
#   9. Star                  (morning +1 / evening -1)
#
# Each detector writes a single column m_cnd_<name>_bin in {-1, 0, +1}.
# Bins land in FE_CANDLESTICK_SIGNALS (PK slug, timestamp) and get pulled into
# FE_DMV_ALL via the FULL OUTER JOIN in gcp_dmv_core.py.
#
# Non-directional patterns (Doji, Spinning Top) are intentionally skipped:
# they convey indecision, not direction, and would distort DMV bullish/bearish
# counts if encoded as +-1.

import logging
import os
import time

import numpy as np
import pandas as pd
from dotenv import load_dotenv
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
    logger.info("Fetching OHLCV history from PostgreSQL...")
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
    logger.info(f"Fetched {len(df):,} rows across {df['slug'].nunique()} slugs")
    return df


# ---------- Helper: per-slug prior trend proxy ----------
# A simple, fast definition of "prior trend": the close 1 bar ago vs the close
# 5 bars ago. If close[t-1] > close[t-5], we call the prior trend up; else down.
# Conservative: requires at least 5 prior bars per slug; NaN otherwise.

def add_prior_trend_flags(df):
    prev_close   = df.groupby("slug")["close"].shift(1)
    close_5_back = df.groupby("slug")["close"].shift(5)
    df["_prior_up"]   = (prev_close > close_5_back)
    df["_prior_down"] = (prev_close < close_5_back)
    return df


# ---------- Per-bar geometry helpers ----------

def add_geometry(df):
    body          = (df["close"] - df["open"]).abs()
    upper_shadow  = df["high"] - df[["open", "close"]].max(axis=1)
    lower_shadow  = df[["open", "close"]].min(axis=1) - df["low"]
    rng           = df["high"] - df["low"]
    df["_body"]   = body
    df["_us"]     = upper_shadow
    df["_ls"]     = lower_shadow
    df["_rng"]    = rng
    df["_red"]    = df["close"] < df["open"]
    df["_blue"]   = df["close"] > df["open"]
    return df


# ---------- 1. Marubozu ----------
# Shadows must be tiny relative to range. Strict definition allows zero shadow;
# we accept up to 5% of the candle's range on each side (constraint #2 of the
# Zerodha module: "be flexible -- quantify and verify").

def detect_marubozu(df, shadow_tol=0.05):
    rng = df["_rng"]
    tol = shadow_tol * rng
    bullish = (df["_blue"]) & (df["_us"] <= tol) & (df["_ls"] <= tol) & (rng > 0)
    bearish = (df["_red"])  & (df["_us"] <= tol) & (df["_ls"] <= tol) & (rng > 0)
    return np.select([bullish, bearish], [1, -1], default=0).astype(np.int64)


# ---------- Paper-umbrella geometry (used by hammer + hanging man) ----------

def _is_paper_umbrella(df, body_ratio=2.0, upper_tol=0.3):
    body = df["_body"]
    return (
        (df["_ls"] >= body_ratio * body)
        & (df["_us"] <= upper_tol * body)
        & (body > 0)
    )


# ---------- 2. Hammer ----------

def detect_hammer(df):
    is_umbrella = _is_paper_umbrella(df)
    hammer = is_umbrella & df["_prior_down"].fillna(False)
    return np.where(hammer, 1, 0).astype(np.int64)


# ---------- 3. Hanging Man ----------

def detect_hanging_man(df):
    is_umbrella = _is_paper_umbrella(df)
    hanging = is_umbrella & df["_prior_up"].fillna(False)
    return np.where(hanging, -1, 0).astype(np.int64)


# ---------- Inverted-paper-umbrella geometry ----------

def _is_inverted_paper_umbrella(df, body_ratio=2.0, lower_tol=0.3):
    body = df["_body"]
    return (
        (df["_us"] >= body_ratio * body)
        & (df["_ls"] <= lower_tol * body)
        & (body > 0)
    )


# ---------- 4. Shooting Star ----------

def detect_shooting_star(df):
    is_inv = _is_inverted_paper_umbrella(df)
    star = is_inv & df["_prior_up"].fillna(False)
    return np.where(star, -1, 0).astype(np.int64)


# ---------- Multi-candle helpers ----------

def _prev(df, col, n=1):
    return df.groupby("slug")[col].shift(n)


# ---------- 5. Engulfing ----------

def detect_engulfing(df):
    p_open  = _prev(df, "open")
    p_close = _prev(df, "close")
    p_red   = p_close < p_open
    p_blue  = p_close > p_open

    bull = (
        p_red & df["_blue"]
        & (df["open"]  <= p_close)
        & (df["close"] >= p_open)
        & df["_prior_down"].fillna(False)
    )
    bear = (
        p_blue & df["_red"]
        & (df["open"]  >= p_close)
        & (df["close"] <= p_open)
        & df["_prior_up"].fillna(False)
    )
    return np.select([bull, bear], [1, -1], default=0).astype(np.int64)


# ---------- 6. Harami ----------

def detect_harami(df, small_ratio=0.5):
    p_open  = _prev(df, "open")
    p_close = _prev(df, "close")
    p_body  = (p_close - p_open).abs()

    body_top    = df[["open", "close"]].max(axis=1)
    body_bottom = df[["open", "close"]].min(axis=1)
    p_body_top    = pd.concat([p_open, p_close], axis=1).max(axis=1)
    p_body_bottom = pd.concat([p_open, p_close], axis=1).min(axis=1)

    inside = (body_top <= p_body_top) & (body_bottom >= p_body_bottom)
    p_red  = p_close < p_open
    p_blue = p_close > p_open

    bull = (p_red & df["_blue"] & inside & (df["_body"] <= small_ratio * p_body)
            & df["_prior_down"].fillna(False))
    bear = (p_blue & df["_red"] & inside & (df["_body"] <= small_ratio * p_body)
            & df["_prior_up"].fillna(False))
    return np.select([bull, bear], [1, -1], default=0).astype(np.int64)


# ---------- 7. Piercing ----------

def detect_piercing(df):
    p_open  = _prev(df, "open")
    p_close = _prev(df, "close")
    p_red   = p_close < p_open
    p_mid   = (p_open + p_close) / 2.0

    cond = (
        p_red & df["_blue"]
        & (df["open"]  < p_close)
        & (df["close"] > p_mid)
        & (df["close"] < p_open)
        & df["_prior_down"].fillna(False)
    )
    return np.where(cond, 1, 0).astype(np.int64)


# ---------- 8. Dark Cloud Cover ----------

def detect_dark_cloud(df):
    p_open  = _prev(df, "open")
    p_close = _prev(df, "close")
    p_blue  = p_close > p_open
    p_mid   = (p_open + p_close) / 2.0

    cond = (
        p_blue & df["_red"]
        & (df["open"]  > p_close)
        & (df["close"] < p_mid)
        & (df["close"] > p_open)
        & df["_prior_up"].fillna(False)
    )
    return np.where(cond, -1, 0).astype(np.int64)


# ---------- 9. Star (Morning / Evening) ----------

def detect_star(df, small_ratio=0.3):
    p1_open  = _prev(df, "open",  2)
    p1_close = _prev(df, "close", 2)
    p2_open  = _prev(df, "open",  1)
    p2_close = _prev(df, "close", 1)
    p2_high  = _prev(df, "high",  1)
    p2_low   = _prev(df, "low",   1)

    p1_body = (p1_close - p1_open).abs()
    p2_body = (p2_close - p2_open).abs()
    p1_mid  = (p1_open + p1_close) / 2.0

    p1_red  = p1_close < p1_open
    p1_blue = p1_close > p1_open
    p2_small = p2_body <= small_ratio * p1_body

    morning = (
        p1_red & p2_small & df["_blue"]
        & (p2_high < p1_close)             # gap down separating P1 close and P2 high
        & (df["close"] > p1_mid)
        & df["_prior_down"].fillna(False)
    )
    evening = (
        p1_blue & p2_small & df["_red"]
        & (p2_low > p1_close)              # gap up
        & (df["close"] < p1_mid)
        & df["_prior_up"].fillna(False)
    )
    return np.select([morning, evening], [1, -1], default=0).astype(np.int64)


# ---------- Main pipeline ----------

PATTERN_FUNCS = [
    ("m_cnd_marubozu_bin",      detect_marubozu),
    ("m_cnd_hammer_bin",        detect_hammer),
    ("m_cnd_hanging_man_bin",   detect_hanging_man),
    ("m_cnd_shooting_star_bin", detect_shooting_star),
    ("m_cnd_engulfing_bin",     detect_engulfing),
    ("m_cnd_harami_bin",        detect_harami),
    ("m_cnd_piercing_bin",      detect_piercing),
    ("m_cnd_dark_cloud_bin",    detect_dark_cloud),
    ("m_cnd_star_bin",          detect_star),
]


def compute_all_patterns(df):
    df = add_geometry(df)
    df = add_prior_trend_flags(df)
    for col, fn in PATTERN_FUNCS:
        df[col] = fn(df)
        logger.info(f"  {col}: bull={(df[col] == 1).sum():,}, "
                    f"bear={(df[col] == -1).sum():,}, neutral={(df[col] == 0).sum():,}")
    return df


REQUIRED_COLUMNS = ["id", "slug", "timestamp"] + [c for c, _ in PATTERN_FUNCS]


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
    df = compute_all_patterns(df)

    # Per-slug latest timestamp (constraint #3)
    latest = df.loc[df.groupby("slug")["timestamp"].idxmax()].copy()

    # id column may not exist in source OHLCV; ensure it as nullable so to_sql does not fail
    if "id" not in latest.columns:
        latest["id"] = pd.NA

    out = latest[REQUIRED_COLUMNS].copy()

    push_to_db(out, "FE_CANDLESTICK_SIGNALS", engine)
    push_to_db_backtest(out, "FE_CANDLESTICK_SIGNALS", engine_bt)

    engine.dispose()
    engine_bt.dispose()
    logger.info(f"Done in {(time.time() - t0)/60:.2f} minutes.")
