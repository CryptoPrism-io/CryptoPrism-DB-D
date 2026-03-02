import requests
import json
import pandas as pd

from sqlalchemy import create_engine, text
import os
import logging
from dotenv import load_dotenv

# Configure logging
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
API_KEY = os.getenv("CMC_API_KEY")

# Validate required environment variables
missing_vars = [var for var in ["DB_HOST", "DB_USER", "DB_PASSWORD", "API_KEY"] if not globals()[var]]
if missing_vars:
    logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
    raise SystemExit("Terminating: Missing required credentials.")

# Log only necessary info (DO NOT log DB_PASSWORD for security)
logger.info(f"Database Configuration Loaded: DB_HOST={DB_HOST}, DB_PORT={DB_PORT}")

# Create SQLAlchemy Engine
def create_db_engine():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')

# Create SQLAlchemy Engine for Backtest Database
def create_db_engine_backtest():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}')

def fetch_fear_and_greed_data(api_key, limit=500, start=1):
    """ Fetches paginated Fear & Greed Index data from the API. """
    url = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/historical"

    headers = {
        "Accepts": "application/json",
        "X-CMC_PRO_API_KEY": api_key,
    }

    params = {
        "start": start,  # Start from record 1
        "limit": limit  # Max 500 records per request
    }

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")
        return None


def fetch_all_data(api_key):
    """ Fetches all available Fear & Greed historical data using pagination (back to Feb 2018). """
    all_data = []
    start = 1  # Start index for pagination
    limit = 500  # Max per request

    while True:
        data = fetch_fear_and_greed_data(api_key, limit, start)
        if not data or "data" not in data or not data["data"]:
            break  # Stop if no more data

        all_data.extend(data["data"])
        start += limit  # Move to the next batch

    logger.info(f"Fetched {len(all_data)} total Fear & Greed records.")
    return all_data


def process_fear_greed_data(data):
    """ Converts JSON data into a Pandas DataFrame. """
    if data:
        df = pd.DataFrame(data)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="s")
        df.rename(columns={"value": "fear_greed_index", "value_classification": "sentiment"}, inplace=True)
        return df
    else:
        return pd.DataFrame()


def push_data_to_db(df, table_name="FE_FEAR_GREED_CMC"):
    """Pushes the Fear & Greed data to the database using TRUNCATE + INSERT.
    Avoids DROP TABLE which would break dependent materialized views.
    """
    engine = create_db_engine()
    with engine.connect() as conn:
        conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
        conn.commit()
    df.to_sql(table_name, con=engine, if_exists="append", index=False)
    logger.info(f"Data successfully pushed to {table_name}")


if __name__ == "__main__":
    api_key = API_KEY
    full_year_data = fetch_all_data(api_key)
    df = process_fear_greed_data(full_year_data)

    if not df.empty:
        push_data_to_db(df)
