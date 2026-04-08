# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
import time
import logging
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

# 🔹 Logging setup for debugging and monitoring
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 🔹 Start Timer
start_time = time.time()

# Load .env file ONLY if running locally (not in GitHub Actions)
if not os.getenv("GITHUB_ACTIONS"):
    env_file = ".env"
    if os.path.exists(env_file):
        load_dotenv()
        logger.info("✅ .env file loaded successfully.")
    else:
        logger.error("❌ .env file is missing! Please create one for local testing.")
else:
    logger.info("🔹 Running in GitHub Actions: Using GitHub Secrets.")

# Fetch credentials (Works for both local and GitHub Actions)
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")  # Default to 5432 if not set
DB_NAME = os.getenv("DB_NAME", "dbcp")  # Default database
DB_NAME_BT = os.getenv("DB_NAME_BT", "cp_backtest")  # Backtest database

# Validate required environment variables
missing_vars = [var for var in ["DB_HOST", "DB_USER", "DB_PASSWORD"] if not globals()[var]]
if missing_vars:
    logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    raise SystemExit("❌ Terminating: Missing required credentials.")

# Log only necessary info (DO NOT log DB_PASSWORD for security)
logger.info(f"✅ Database Configuration Loaded: DB_HOST={DB_HOST}, DB_PORT={DB_PORT}")

# 🔹 Create SQLAlchemy Engine
def create_db_engine():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')

# 🔹 Fetch Data Efficiently
def fetch_data(engine):
    logging.info("Fetching data from PostgreSQL...")

    query = """SELECT
              "public"."1K_coins_ohlcv".*
            FROM
              "public"."1K_coins_ohlcv"
            INNER JOIN
              "public"."crypto_listings_latest_1000"
            ON
              "public"."1K_coins_ohlcv"."slug" = "public"."crypto_listings_latest_1000"."slug"
            WHERE
              "public"."crypto_listings_latest_1000"."cmc_rank" <= 1000
              AND "public"."1K_coins_ohlcv"."timestamp" >= NOW() - INTERVAL '110 days';
              """

    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values(by=["slug", "timestamp"], inplace=True)

    logging.info(f"✅ Data fetched. Shape: {df.shape}")
    return df

# 🔹 OBV Calculation (Vectorized)
def calculate_obv(df):
    logging.info("Calculating OBV...")
    
    def obv_calc(group):
        obv = np.zeros(len(group))
        volume_changes = np.where(group["close"].diff().fillna(0) > 0, group["volume"], 
                                  np.where(group["close"].diff().fillna(0) < 0, -group["volume"], 0))
        obv[1:] = np.cumsum(volume_changes[:-1])  
        return obv

    df["obv"] = df.groupby("slug", group_keys=False).apply(lambda g: pd.Series(obv_calc(g), index=g.index))
    df["m_tvv_obv_1d"] = df.groupby("slug")["obv"].pct_change()

    return df

# 🔹 Moving Averages
def calculate_moving_averages(df):
    logging.info("Calculating Moving Averages...")
    
    df = df.assign(
        SMA9=df.groupby("slug")["close"].transform(lambda x: x.rolling(9, min_periods=1).mean()),
        SMA18=df.groupby("slug")["close"].transform(lambda x: x.rolling(18, min_periods=1).mean()),
        EMA9=df.groupby("slug")["close"].transform(lambda x: x.ewm(span=9, adjust=False).mean()),
        EMA18=df.groupby("slug")["close"].transform(lambda x: x.ewm(span=18, adjust=False).mean()),
        SMA21=df.groupby("slug")["close"].transform(lambda x: x.rolling(21, min_periods=1).mean()),
        SMA108=df.groupby("slug")["close"].transform(lambda x: x.rolling(108, min_periods=1).mean()),
        EMA21=df.groupby("slug")["close"].transform(lambda x: x.ewm(span=21, adjust=False).mean()),
        EMA108=df.groupby("slug")["close"].transform(lambda x: x.ewm(span=108, adjust=False).mean())
    )
    
    return df

# 🔹 ATR Calculation
def calculate_atr(df, window=21):
    logging.info("Calculating ATR...")

    df["prev_close"] = df.groupby("slug")["close"].shift(1)
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = abs(df["high"] - df["prev_close"])
    df["tr3"] = abs(df["low"] - df["prev_close"])
    df["TR"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["ATR"] = df.groupby("slug")["TR"].transform(lambda x: x.rolling(window, min_periods=1).mean())

    return df

# 🔹 Keltner & Donchian Channels
def calculate_channels(df):
    logging.info("Calculating Keltner & Donchian Channels...")

    df["Keltner_Upper"] = df["EMA9"] + (df["ATR"] * 1.5)
    df["Keltner_Lower"] = df["EMA9"] - (df["ATR"] * 1.5)
    df["Donchian_Upper"] = df.groupby("slug")["high"].transform(lambda x: x.rolling(20, min_periods=1).max())
    df["Donchian_Lower"] = df.groupby("slug")["low"].transform(lambda x: x.rolling(20, min_periods=1).min())

    return df

# 🔹 VWAP Calculation
def calculate_vwap(df):
    logging.info("Calculating VWAP...")
    
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_price_volume"] = df.groupby("slug")["typical_price"].cumsum() * df.groupby("slug")["volume"].cumsum()
    df["cum_volume"] = df.groupby("slug")["volume"].cumsum()
    df["VWAP"] = df["cum_price_volume"] / df["cum_volume"]
    
    return df

# 🔹 CMF & ADL Calculation
def calculate_cmf(df, period=21):
    logging.info("Calculating CMF & ADL...")

    # Correct ADL Calculation
    df["ADL"] = ((df["close"] - df["low"] - (df["high"] - df["close"])) / (df["high"] - df["low"])) * df["volume"]

    # Cumulative ADL
    df["cum_adl"] = df.groupby("slug")["ADL"].cumsum()

    # CMF Calculation
    df["CMF"] = df.groupby("slug")["cum_adl"].transform(lambda x: x.rolling(period, min_periods=1).sum()) / \
                df.groupby("slug")["volume"].transform(lambda x: x.rolling(period, min_periods=1).sum())

    return df


# 🔹 Ensure 37 Required Columns Exist
def ensure_required_columns(df):
    REQUIRED_COLUMNS = [
    # 🔹 Identifiers
    "id", "slug", "name", "timestamp",
    
    # 🔹 Market Data
    "open", "high", "low", "close", "volume", "market_cap",

    # 🔹 On-Balance Volume (OBV)
    "obv", "m_tvv_obv_1d",

    # 🔹 Moving Averages
    "SMA9", "SMA18", "EMA9", "EMA18",
    "SMA21", "SMA108", "EMA21", "EMA108",

    # 🔹 True Range & ATR
    "prev_close", "tr1", "tr2", "tr3", "TR", "ATR",

    # 🔹 Keltner & Donchian Channels
    "Keltner_Upper", "Keltner_Lower",
    "Donchian_Upper", "Donchian_Lower",

    # 🔹 VWAP & CMF
    "typical_price", "cum_price_volume", "cum_volume", "VWAP",
    "ADL", "cum_adl", "CMF"
]


    return df[REQUIRED_COLUMNS]

# 🔹 TVV Binary Signals
def generate_binary_signals(df):
    logging.info("Generating Binary Signals...")

    df["m_tvv_obv_1d_binary"] = np.sign(df["m_tvv_obv_1d"])
    df["d_tvv_sma9_18"] = np.sign(df["SMA9"] - df["SMA18"])
    df["d_tvv_ema9_18"] = np.sign(df["EMA9"] - df["EMA18"])
    df["d_tvv_sma21_108"] = np.sign(df["SMA21"] - df["SMA108"])
    df["d_tvv_ema21_108"] = np.sign(df["EMA21"] - df["EMA108"])
    df["m_tvv_cmf"] = np.sign(df["CMF"])

    return df    

# 🔹 Ensure TVV Signals Required Columns Exist
def ensure_signals_columns(df):
    REQUIRED_SIGNAL_COLUMNS = [
        # 🔹 Identifiers
        "id", "slug", "timestamp",

        # 🔹 OBV-Based Signals
        "m_tvv_obv_1d_binary", 
        
        # 🔹 Moving Average Crossovers
        "d_tvv_sma9_18", "d_tvv_ema9_18",
        "d_tvv_sma21_108", "d_tvv_ema21_108",
        
        # 🔹 CMF-Based Signal
        "m_tvv_cmf"
    ]
    
    # Ensure only existing columns are selected (prevent KeyErrors)
    return df[[col for col in REQUIRED_SIGNAL_COLUMNS if col in df.columns]]


# 🔹 Push Data to Database with TRUNCATE + INSERT
def push_to_db(df, table_name):
    engine = create_db_engine()
    # Use TRUNCATE + INSERT to preserve table structure
    with engine.connect() as conn:
        conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
        conn.commit()
    df.to_sql(table_name, con=engine, if_exists="append", index=False)
    engine.dispose()
    logging.info(f"✅ {table_name} uploaded successfully!")

# 🔹 Create SQLAlchemy Engine
def create_db_engine_backtest():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}')

# Push Data to Backtest Database (DELETE + INSERT to prevent duplicates)
def push_to_db_backtest(df, table_name):
    engine = create_db_engine_backtest()
    if 'timestamp' in df.columns:
        timestamps = df['timestamp'].dropna().unique().tolist()
        with engine.connect() as conn:
            for ts in timestamps:
                conn.execute(
                    text(f'DELETE FROM "{table_name}" WHERE "timestamp" = :ts'),
                    {"ts": ts}
                )
            conn.commit()
    df.to_sql(table_name, con=engine, if_exists="append", index=False)
    engine.dispose()
    logging.info(f"{table_name} uploaded to backtest successfully!")

if __name__ == "__main__":
    engine = create_db_engine()
    df = fetch_data(engine)

    df = (df.pipe(calculate_obv)
           .pipe(calculate_moving_averages)
           .pipe(calculate_atr)
           .pipe(calculate_channels)      # ✅ Added Keltner & Donchian calculation
           .pipe(calculate_vwap)          # ✅ Added VWAP calculation
           .pipe(calculate_cmf)
           .pipe(generate_binary_signals))

    # Keep only the latest timestamp records
    latest_timestamp = df["timestamp"].max()
    latest_data = df[df["timestamp"] == latest_timestamp]

    # Ensure required columns exist **after** all calculations
    latest_data_cols = ensure_required_columns(latest_data)
    latest_data_signals_cols = ensure_signals_columns(latest_data) 

    # Separate TVV and TVV Signals
    tvv = latest_data_cols.copy()
    tvv_signals = latest_data_signals_cols.copy()

    # Push to database
    push_to_db(tvv, "FE_TVV")
    push_to_db(tvv_signals, "FE_TVV_SIGNALS")

    push_to_db_backtest(tvv, "FE_TVV")
    push_to_db_backtest(tvv_signals, "FE_TVV_SIGNALS")

    logging.info(f"✅ Process completed in {round((time.time() - start_time) / 60, 2)} minutes!")
