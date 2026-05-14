#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cron_job.py
Fetches latest CoinMarketCap data, flattens it to a DataFrame matching the specified schema,
formats dates, ensures unique column names, fills any missing columns with None to maintain 30-field schema,
filters for top 1000 coins by cmc_rank, and writes to a database.
"""

import os
import requests
import pandas as pd
from sqlalchemy import create_engine


def fetch_listings(api_key: str) -> dict:
    """
    Fetch latest listings from CoinMarketCap API.
    """
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    headers = {"X-CMC_PRO_API_KEY": api_key}
    params = {"start": 1, "limit": 5000, "convert": "USD"}
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def process_data(data: dict) -> pd.DataFrame:
    """
    Flatten JSON, clean column names, ensure unique names, format dates,
    rename to match schema, add ref_currency, ensure all 30 columns, and order them.
    """
    df = pd.json_normalize(data.get("data", []))

    # Clean quote prefix
    df.columns = [col.replace("quote.USD.", "") for col in df.columns]

    # Handle duplicate column names by adding suffixes
    cols = pd.Series(df.columns)
    for dup in cols[cols.duplicated()].unique():
        dup_idxs = cols[cols == dup].index.tolist()
        cols.loc[dup_idxs] = [f"{dup}_{i}" if i else dup for i in range(len(dup_idxs))]
    df.columns = cols

    # Format date-like columns to YYYY-MM-DD
    date_cols = [c for c in df.columns if any(k in c.lower() for k in ("date", "updated", "timestamp"))]
    for col in date_cols:
        try:
            df[col] = pd.to_datetime(df[col], errors='coerce').dt.strftime('%Y-%m-%d')
        except Exception:
            pass

    # Rename to match original schema
    df.rename(columns={
        'num_market_pairs': 'market_pair_count',
        'volume_24h': 'volume24h',
        'percent_change_1h': 'percent_change1h',
        'percent_change_24h': 'percent_change24h',
        'percent_change_7d': 'percent_change7d',
        'percent_change_30d': 'percent_change30d',
        'percent_change_60d': 'percent_change60d',
        'percent_change_90d': 'percent_change90d',
        'fully_diluted_market_cap': 'fully_dillutted_market_cap'
    }, inplace=True)

    # Add static reference currency
    df['ref_currency'] = 'USD'

    # Desired 30-field schema, in order
    schema = [
        'id', 'percent_change1h', 'percent_change24h', 'percent_change7d',
        'percent_change30d', 'percent_change60d', 'percent_change90d',
        'fully_dillutted_market_cap', 'tvl', 'cmc_rank',
        'market_pair_count', 'self_reported_circulating_supply', 'max_supply',
        'price', 'volume24h', 'market_cap', 'name', 'symbol', 'slug',
        'ref_currency', 'last_updated', 'circulating_supply', 'date_added',
        'total_supply', 'is_active', 'market_cap_by_total_supply', 'dominance',
        'turnover', 'ytd_price_change_percentage', 'percent_change1y'
    ]

    # Ensure all columns exist, fill missing with None
    for col in schema:
        if col not in df.columns:
            df[col] = None

    # Reorder and return
    return df[schema]


def filter_top_1000(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter DataFrame to keep only top 1000 coins by cmc_rank.
    """
    # Ensure cmc_rank is numeric
    df['cmc_rank'] = pd.to_numeric(df['cmc_rank'], errors='coerce')
    
    # Filter for top 1000 (cmc_rank <= 1000)
    filtered_df = df[df['cmc_rank'] <= 1000].copy()
    
    print(f"Original records: {len(df)}")
    print(f"Filtered to top 1000: {len(filtered_df)}")
    
    return filtered_df


def upload_to_db(df: pd.DataFrame, db_url: str, table: str):
    """
    Write DataFrame to the specified table in the database.
    """
    engine = create_engine(db_url)
    with engine.begin() as conn:
        df.to_sql(table, conn, if_exists="replace", index=False,
                  method="multi", chunksize=200)
    engine.dispose()


def main():
    api_key = os.getenv("CMC_API_KEY")
    db_url = os.getenv("DB_URL")
    table_name = os.getenv("DB_TABLE", "crypto_listings_latest_1000")

    if not api_key or not db_url:
        raise EnvironmentError("Required environment variables: CMC_API_KEY, DB_URL")

    # Fetch and process data
    data = fetch_listings(api_key)
    df = process_data(data)
    
    # Filter for top 1000 coins before uploading
    df_filtered = filter_top_1000(df)
    
    # Upload filtered data to database
    upload_to_db(df_filtered, db_url, table_name)
    
    print(f"Successfully uploaded {len(df_filtered)} records to {table_name}")


if __name__ == "__main__":
    main()
