# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
import time
import logging
from sqlalchemy import create_engine, text
import os
from dotenv import load_dotenv

# 🔹 Logging setup
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

# 🔹 Create Database Engine for Live Data
def create_db_engine():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')

# 🔹 Create Database Engine for Backtest Data
def create_db_engine_backtest():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}')

# 🔹 Fetch Data
def fetch_data(engine):
    logging.info("Fetching OHLCV data from PostgreSQL...")
    
    query = """SELECT
                  "public"."1K_coins_ohlcv".*
                FROM
                  "public"."1K_coins_ohlcv"
                INNER JOIN
                  "public"."crypto_listings_latest_1000"
                ON
                  "public"."1K_coins_ohlcv"."slug" = "public"."crypto_listings_latest_1000"."slug"
                WHERE
                  "public"."crypto_listings_latest_1000"."cmc_rank" <= 1000;
                  """
    
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)

    # Ensure proper formatting
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df.sort_values(by=["slug", "timestamp"], inplace=True)

    logging.info(f"✅ Data fetched. Shape: {df.shape}")
    return df

# 🔹 Calculate Percentage Change (Returns)
def calculate_pct_change(df):
    logging.info("Calculating Percentage Change and Cumulative Returns...")

    df["m_pct_1d"] = df.groupby("slug")["close"].pct_change()
    df["d_pct_cum_ret"] = (1 + df["m_pct_1d"]).groupby(df["slug"]).cumprod() - 1

    return df

# 🔹 Calculate VaR & CVaR
def calculate_var_cvar(df, confidence_level=0.95):
    logging.info("Calculating VaR & CVaR...")

    # Historical VaR
    var_df = df.groupby("slug")["m_pct_1d"].quantile(1 - confidence_level).reset_index(name="d_pct_var")

    # Conditional VaR (Expected Shortfall)
    cvar_df = df.groupby("slug").apply(lambda x: x["m_pct_1d"][x["m_pct_1d"] <= x["m_pct_1d"].quantile(1 - confidence_level)].mean()).reset_index(name="d_pct_cvar")

    # Merge results back to the main dataframe
    df = df.merge(var_df, on="slug", how="left")
    df = df.merge(cvar_df, on="slug", how="left")

    return df

# 🔹 Calculate Volume Percentage Changes
def calculate_volume_pct_change(df):
    logging.info("Calculating Volume Percentage Change...")

    df["d_pct_vol_1d"] = df.groupby("slug")["volume"].pct_change()

    return df

# 🔹 Keep Only Latest Timestamp Per Slug
def filter_latest_data(df):
    logging.info("Filtering latest data per slug...")
    
    latest_df = df[df["timestamp"] == df.groupby("slug")["timestamp"].transform("max")]
    return latest_df

# 🔹 Clean and Remove Infinite Values
def clean_data(df):
    logging.info("Cleaning Data (Handling Inf/NaN values)...")

    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df.dropna(inplace=True)

    return df

# 🔹 Ensure PCT_CHANGE Required Columns Exist
def ensure_required_pct_columns(df):
    REQUIRED_COLUMNS = [
        # 🔹 Identifiers
        "id", "slug","name", "timestamp",

        # 🔹 Returns & Risk Metrics
        "m_pct_1d", "d_pct_cum_ret",
        "d_pct_var", "d_pct_cvar",
        
        # 🔹 Volume-Based Metrics
        "d_pct_vol_1d"
    ]

    return df[[col for col in REQUIRED_COLUMNS if col in df.columns]]

# 🔹 Push Data to Live Database with TRUNCATE + INSERT
def push_to_db(df, table_name):
    engine = create_db_engine()
    # Use TRUNCATE + INSERT to preserve table structure
    with engine.connect() as conn:
        conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
        conn.commit()
    df.to_sql(table_name, con=engine, if_exists="append", index=False)
    engine.dispose()
    logging.info(f"✅ {table_name} uploaded successfully to LIVE DB!")

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

# 🔹 Main Execution
if __name__ == "__main__":
    engine = create_db_engine()
    df = fetch_data(engine)

    df = (df.pipe(calculate_pct_change)
           .pipe(calculate_var_cvar)
           .pipe(calculate_volume_pct_change)
           .pipe(filter_latest_data)
           .pipe(clean_data))

    # Ensure required columns exist **after** calculations
    df = ensure_required_pct_columns(df)

    # Keep only the latest timestamp records
    latest_timestamp  = df["timestamp"].max()
    df = df[df["timestamp"] == latest_timestamp]

    # Push to both Live & Backtest databases
    push_to_db(df, "FE_PCT_CHANGE")           # Live database (replace)
    push_to_db_backtest(df, "FE_PCT_CHANGE")  # Backtest database (append)

    logging.info(f"✅ Process completed in {round((time.time() - start_time) / 60, 2)} minutes!")
