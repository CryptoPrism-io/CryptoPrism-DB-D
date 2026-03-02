import requests
import json
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import create_engine
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

# Validate required environment variables
missing_vars = [var for var in ["DB_HOST", "DB_USER", "DB_PASSWORD", "API_KEY"] if not globals()[var]]
if missing_vars:
    logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    raise SystemExit("❌ Terminating: Missing required credentials.")

# Log only necessary info (DO NOT log DB_PASSWORD for security)
logger.info(f"✅ Database Configuration Loaded: DB_HOST={DB_HOST}, DB_PORT={DB_PORT}")

# 🔹 Create SQLAlchemy Engine
def create_db_engine():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')

# 🔹 Create SQLAlchemy Engine for Backtest Database
def create_db_engine_backtest():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}')

# API Key - loaded from environment (CMC_API_KEY secret in GitHub Actions, or .env locally)
API_KEY = os.getenv("CMC_API_KEY")

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


def fetch_full_year_data(api_key):
    """ Fetches Fear & Greed data for 1 year using pagination. """
    all_data = []
    start = 1  # Start index for pagination
    limit = 500  # Max per request

    while True:
        data = fetch_fear_and_greed_data(api_key, limit, start)
        if not data or "data" not in data or not data["data"]:
            break  # Stop if no more data

        all_data.extend(data["data"])
        start += limit  # Move to the next batch

        # Convert first and last timestamp to check if we reached 1 year
        timestamps = [int(entry["timestamp"]) for entry in data["data"]]
        first_date = datetime.utcfromtimestamp(timestamps[0])
        last_date = datetime.utcfromtimestamp(timestamps[-1])
        one_year_ago = datetime.utcnow() - timedelta(days=365)

        if last_date < one_year_ago:
            break  # Stop fetching if we have 1 year of data

    return all_data


def process_fear_greed_data(data):
    """ Converts JSON data into a Pandas DataFrame. """
    if data:
        df = pd.DataFrame(data)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="s")
        df.rename(columns={"value": "fear_greed_index", "value_classification": "sentiment"}, inplace=True)
        df = df[df["timestamp"] >= datetime.utcnow() - timedelta(days=365)]  # Keep only last 1 year
        return df
    else:
        return pd.DataFrame()


def push_data_to_db(df, table_name="FE_FEAR_GREED_CMC"):
    """Pushes the Fear & Greed data to the database."""
    engine = create_db_engine()  # Connect to primary database
    with engine.begin() as connection:
        df.to_sql(table_name, connection, if_exists="replace", index=False)
    print(f"Data successfully pushed to {table_name}")


if __name__ == "__main__":
    api_key = API_KEY
    full_year_data = fetch_full_year_data(api_key)
    df = process_fear_greed_data(full_year_data)

    if not df.empty:
        push_data_to_db(df)
