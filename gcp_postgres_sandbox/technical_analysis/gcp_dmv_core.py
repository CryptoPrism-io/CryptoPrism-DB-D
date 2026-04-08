import pandas as pd
import numpy as np
import time
import logging
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

BATCH_SIZE = 10000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()]
)
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

start_time = time.time()

# Create database engine using environment variables
gcp_engine = create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')


query = """
SELECT *
FROM "FE_OSCILLATORS_SIGNALS" AS o
FULL OUTER JOIN "FE_MOMENTUM_SIGNALS" AS m USING (slug, timestamp)
FULL OUTER JOIN "FE_METRICS_SIGNAL" AS me USING (slug, timestamp)
FULL OUTER JOIN "FE_TVV_SIGNALS" AS t USING (slug, timestamp)
FULL OUTER JOIN "FE_RATIOS_SIGNALS" AS r USING (slug, timestamp);
"""

def fetch_data(query, engine):
    try:
        return pd.read_sql_query(query, engine)
    except SQLAlchemyError as e:
        logger.error(f"Error fetching data: {e}")
        return pd.DataFrame()

df = fetch_data(query, gcp_engine)

if df.empty:
    logger.warning("Fetched DataFrame is empty. Exiting.")
    exit(1)
df = (
    df.loc[:, ~df.columns.duplicated()]
      .pipe(lambda d: d[d['slug'].eq('bitcoin') | d.drop(columns='slug').notna().all(axis=1)])
      .fillna(0)
)

for col in ['bullish', 'bearish', 'neutral']:
    df[col] = 0

for index, row in df.iloc[:, 4:].iterrows():
    df.at[index, 'bullish'] = (row == 1).sum()
    df.at[index, 'bearish'] = (row == -1).sum()
    df.at[index, 'neutral'] = (row == 0).sum()

# TRUNCATE and INSERT for FE_DMV_ALL
with gcp_engine.connect() as conn:
    conn.execute(text('TRUNCATE TABLE "FE_DMV_ALL"'))
    conn.commit()
df.to_sql('FE_DMV_ALL', con=gcp_engine, if_exists='append', index=False)
logger.info("FE_DMV_ALL uploaded.")

Durability = df[['slug'] + [c for c in df.columns if c.startswith('d_')]]
Momentum = df[['slug'] + [c for c in df.columns if c.startswith('m_')]]
Valuation = df[['slug'] + [c for c in df.columns if c.startswith('v_')]]

Durability['Durability_Score'] = Durability.drop('slug', axis=1).sum(axis=1) / (Durability.shape[1] - 1) * 100
Momentum['Momentum_Score'] = Momentum.drop('slug', axis=1).sum(axis=1) / (Momentum.shape[1] - 1) * 100
Valuation['Valuation_Score'] = Valuation.drop('slug', axis=1).sum(axis=1) / (Valuation.shape[1] - 1) * 100

dmv_scores = pd.DataFrame({
    'slug': df['slug'],
    'timestamp': df['timestamp'],
    'Durability_Score': Durability['Durability_Score'],
    'Momentum_Score': Momentum['Momentum_Score'],
    'Valuation_Score': Valuation['Valuation_Score']
})

latest_timestamp = df['timestamp'].max()
df = df[df['timestamp'] == latest_timestamp]

if df['timestamp'].nunique() == 1:
    print("✅ Latest timestamp filtered.")
else:
    print("⚠️ Multiple timestamps still exist.")

logger.info(f"Remaining rows: {df.shape[0]}")

try:
    # TRUNCATE and INSERT for FE_DMV_SCORES
    with gcp_engine.connect() as conn:
        conn.execute(text('TRUNCATE TABLE "FE_DMV_SCORES"'))
        conn.commit()
    dmv_scores.to_sql('FE_DMV_SCORES', con=gcp_engine, if_exists='append', index=False, method='multi', chunksize=BATCH_SIZE)
    logger.info("FE_DMV_SCORES uploaded.")
except SQLAlchemyError as e:
    logger.error(f"Error uploading scores: {e}")
    exit(1)

# --- Push to Backtest Database (Append Mode) ---
gcp_engine_bt = create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}')
try:
    df.to_sql('FE_DMV_ALL', con=gcp_engine_bt, if_exists='append', index=False)
    logger.info("FE_DMV_ALL appended to cp_backtest.")
    dmv_scores.to_sql('FE_DMV_SCORES', con=gcp_engine_bt, if_exists='append', index=False, method='multi', chunksize=BATCH_SIZE)
    logger.info("FE_DMV_SCORES appended to cp_backtest.")
except SQLAlchemyError as e:
    logger.error(f"Error uploading to backtest: {e}")
finally:
    gcp_engine_bt.dispose()

logger.info(f"Done in {(time.time() - start_time) / 60:.2f} mins.")
gcp_engine.dispose()
logger.info("Database connection closed successfully.")
