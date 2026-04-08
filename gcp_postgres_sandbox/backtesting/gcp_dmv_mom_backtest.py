import pandas as pd
import numpy as np
import logging
from sqlalchemy import create_engine, text
import time
import os
from dotenv import load_dotenv

# 🔹 Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

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

# 🔹 Create SQLAlchemy Engine
def create_db_engine_backtest():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}')

# 🔹 Fetch Data Efficiently (BACKTEST VERSION - NO TIMESTAMP FILTER)
def fetch_data_backtest(engine):
    logging.info("Fetching ALL historical data from PostgreSQL for backtest...")
    query = """    SELECT
  "public"."1K_coins_ohlcv".*
FROM
  "public"."1K_coins_ohlcv"
INNER JOIN
  "public"."crypto_listings_latest_1000"
ON
  "public"."1K_coins_ohlcv"."slug" = "public"."crypto_listings_latest_1000"."slug"
WHERE
  "public"."crypto_listings_latest_1000"."cmc_rank" <= 1000;"""

    
    try:
        with engine.connect() as connection:
            df = pd.read_sql_query(query, connection)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.sort_values(by=["slug", "timestamp"], inplace=True)
        logging.info(f"✅ ALL historical data fetched for backtest. Shape: {df.shape}")
        return df
    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        raise

# 🔹 Calculate RSI (Vectorized and using Exponential Smoothing)
def calculate_rsi(df, periods=[9, 18, 27, 54, 108]):
    logging.info(f"Calculating RSI for periods: {periods}")

    # Calculate price changes
    delta = df.groupby("slug")["close"].diff()

    # Calculate gains and losses
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    for period in periods:
        # Calculate average gain and average loss using Exponential Moving Average
        avg_gain = gains.groupby(df['slug']).transform(lambda x: x.ewm(span=period, adjust=False).mean())
        avg_loss = losses.groupby(df['slug']).transform(lambda x: x.ewm(span=period, adjust=False).mean())

        # Calculate RS (Relative Strength)
        rs = avg_gain / avg_loss

        # Calculate RSI (Relative Strength Index)
        df[f'm_mom_rsi_{period}'] = 100 - (100 / (1 + rs))
    return df


# 🔹 Calculate SMA (Vectorized)
def calculate_sma(df, column="close", period=14):
    logging.info(f"Calculating SMA for {column} with period {period}")
    df[f'sma_{period}'] = df.groupby("slug")[column].transform(lambda x: x.rolling(window=period, min_periods=1).mean())

    # --- Normalization (Removed as per analysis - unusual and potentially unnecessary) ---
    return df


# 🔹 Calculate ROC (Vectorized)
def calculate_roc(df, period=9):
    logging.info(f"Calculating ROC with period {period}")
    df['m_mom_roc'] = df.groupby('slug')['close'].transform(lambda x: (x - x.shift(period)) / x.shift(period) * 100)
    return df

# 🔹 Calculate Williams %R (Vectorized)
def calculate_williams_r(df, period=14):
    logging.info(f"Calculating Williams %R with period {period}")

    highest_high = df.groupby('slug')['high'].transform(lambda x: x.rolling(window=period, min_periods=1).max())
    lowest_low = df.groupby('slug')['low'].transform(lambda x: x.rolling(window=period, min_periods=1).min())
    df['m_mom_williams_%'] = (highest_high - df['close']) / (highest_high - lowest_low) * -100
    return df

# 🔹 Calculate SMI (Vectorized)
def calculate_smi(df, period=14, smooth_k=3, smooth_d=3):
    logging.info(f"Calculating SMI with period {period}, smooth_k {smooth_k}, smooth_d {smooth_d}")

    highest_high = df.groupby('slug')['high'].transform(lambda x: x.rolling(window=period, min_periods=1).max())
    lowest_low = df.groupby('slug')['low'].transform(lambda x: x.rolling(window=period, min_periods=1).min())

    percent_k = (df['close'] - lowest_low) / (highest_high - lowest_low) * 100
    smoothed_k = percent_k.groupby(df['slug']).transform(lambda x: x.rolling(window=smooth_k, min_periods=1).mean())
    percent_d = smoothed_k.groupby(df['slug']).transform(lambda x: x.rolling(window=smooth_d, min_periods=1).mean())

    df['m_mom_smi'] = smoothed_k - percent_d
    return df

# 🔹 Calculate CMO (Vectorized)
def calculate_cmo(df, period=14):
    logging.info(f"Calculating CMO with period {period}")

    delta = df.groupby("slug")["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    sum_gain = gains.groupby(df['slug']).transform(lambda x: x.rolling(window=period, min_periods=1).sum())
    sum_loss = losses.groupby(df['slug']).transform(lambda x: x.rolling(window=period, min_periods=1).sum())

    df['m_mom_cmo'] = (sum_gain - sum_loss) / (sum_gain + sum_loss) * 100
    return df

# 🔹 Calculate Momentum (MOM) (Vectorized)
def calculate_mom(df, period=10):
    logging.info(f"Calculating Momentum with period {period}")
    df['m_mom_mom'] = df.groupby('slug')['close'].transform(lambda x: x - x.shift(period))
    return df

# 🔹 Calculate TSI (Vectorized)
def calculate_tsi(df, short_period=13, long_period=25):
    logging.info(f"Calculating TSI with short period {short_period} and long period {long_period}")
    delta = df.groupby("slug")["close"].diff()

    smoothed_delta_short = delta.groupby(df['slug']).transform(lambda x: x.ewm(span=short_period, adjust=False).mean())
    smoothed_delta_long = delta.groupby(df['slug']).transform(lambda x: x.ewm(span=long_period, adjust=False).mean())

    df['m_mom_tsi'] = 100 * (smoothed_delta_short.ewm(span=short_period, adjust=False).mean() /
                              smoothed_delta_long.ewm(span=long_period, adjust=False).mean())
    return df

# 🔹 Calculate 1 Day Percentage Change
def calculate_pct_change(df):
    logging.info("Calculating 1-day percentage change...")
    df['m_pct_1d'] = df.groupby('slug')['close'].transform(lambda x: x.pct_change())
    return df

# 🔹 Generate Binary Signals (Momentum)
def generate_binary_signals_momentum(df):
    logging.info("Generating binary signals for Momentum indicators...")

    df['m_mom_roc_bin'] = np.select([df['m_mom_roc'] > 0, df['m_mom_roc'] < 0], [1, -1], default=0)
    df['m_mom_williams_%_bin'] = np.select([df['m_mom_williams_%'] > -50, df['m_mom_williams_%'] < -50], [1, -1], default=0)
    df['m_mom_smi_bin'] = np.select([df['m_mom_smi'] >= 25, df['m_mom_smi'] <= -25], [1, -1], default=0)
    df['m_mom_cmo_bin'] = np.select([df['m_mom_cmo'] > 40, df['m_mom_cmo'] < -40], [1, -1], default=0)
    df['m_mom_mom_bin'] = np.select([df['m_mom_mom'] > 4000, df['m_mom_mom'] < -4000], [1, -1], default=0)

    return df

# Push Data to Database (BACKTEST VERSION - TRUNCATE + INSERT to preserve table structure)
def push_to_db_backtest(df, table_name, engine):
    try:
        with engine.connect() as conn:
            conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
            conn.commit()
        df.to_sql(table_name, con=engine, if_exists="append", index=False)
        logging.info(f"{table_name} rebuilt in backtest database (TRUNCATE+INSERT).")
    except Exception as e:
        logging.error(f"Error pushing data to {table_name}: {e}")
        raise

# 🔹 Main Execution Block (BACKTEST VERSION)
if __name__ == "__main__":
    start_time = time.time()

    engine = create_db_engine()
    df = fetch_data_backtest(engine)  # Uses the backtest version that fetches ALL data

       # Calculate all momentum indicators
    df = (df.pipe(calculate_pct_change)
            .pipe(calculate_rsi)
            .pipe(calculate_sma)
            .pipe(calculate_roc)
            .pipe(calculate_williams_r)
            .pipe(calculate_smi)
            .pipe(calculate_cmo)
            .pipe(calculate_mom)
            .pipe(calculate_tsi)
            .pipe(generate_binary_signals_momentum)
          )
    
    # --- NO TIMESTAMP FILTERING - KEEP ALL HISTORICAL DATA ---
    # All data is preserved for backtesting purposes

    # --- Column Selection (GUARANTEED TO MATCH YOUR LIST) ---
    momentum_cols = [
        # 🔹 Identifiers
        "id", "slug", "name", "timestamp",

        # 🔹 Market Data (OHLCV + Market Cap)
        "open", "high", "low", "close", "volume", "market_cap",

        # 🔹 Momentum Indicators
        "m_pct_1d",  # 1-day percentage change
        "m_mom_rsi_9", "m_mom_rsi_18", "m_mom_rsi_27",  # RSI with different periods
        "m_mom_rsi_54", "m_mom_rsi_108",  # Extended-period RSI values
        "sma_14",  # 14-period Simple Moving Average (SMA)

        # 🔹 Rate of Change & Other Momentum Indicators
        "m_mom_roc",  # Rate of Change (ROC)
        "m_mom_williams_%",  # Williams %R
        "m_mom_smi",  # Stochastic Momentum Index (SMI)
        "m_mom_cmo",  # Chande Momentum Oscillator (CMO)
        "m_mom_mom",  # Standard Momentum
        "m_mom_tsi"  # True Strength Index (TSI)
    ]

    # --- Binary Momentum Signal Columns ---
    momentum_signals_cols = [
        # 🔹 Identifiers
        "id", "slug", "name", "timestamp",

        # 🔹 Binary Signals (Momentum-Based)
        "m_mom_roc_bin",  # Rate of Change Signal
        "m_mom_williams_%_bin",  # Williams %R Signal
        "m_mom_smi_bin",  # Stochastic Momentum Index (SMI) Signal
        "m_mom_cmo_bin",  # Chande Momentum Oscillator (CMO) Signal
        "m_mom_mom_bin"  # Standard Momentum Signal
    ]


    # Create separate DataFrames (ALL DATA - NO FILTERING)
    momentum_df = df[momentum_cols]
    momentum_signals_df = df[momentum_signals_cols]

      # --- Handle Infinite Values ---
    momentum_df = momentum_df.replace([np.inf, -np.inf], np.nan)
    momentum_signals_df = momentum_signals_df.replace([np.inf, -np.inf], np.nan)


    # --- Push to Backtest Database ONLY (REPLACE ALL DATA) ---
    engine_bt = create_db_engine_backtest()
    push_to_db_backtest(momentum_df, "FE_MOMENTUM", engine_bt)  # REPLACE all data
    push_to_db_backtest(momentum_signals_df, "FE_MOMENTUM_SIGNALS", engine_bt)  # REPLACE all data

    engine.dispose()
    engine_bt.dispose()

    end_time = time.time()
    elapsed_time = (end_time - start_time) / 60
    logging.info(f"BACKTEST PROCESS completed in {elapsed_time:.2f} minutes")
    logging.info(f"ALL HISTORICAL DATA has been processed and REPLACED in backtest database") 