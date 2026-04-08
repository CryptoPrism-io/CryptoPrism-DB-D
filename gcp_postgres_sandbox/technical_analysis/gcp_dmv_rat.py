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

# 🔹 Create SQLAlchemy Engine for Backtesting
def create_db_engine_backtest():
    return create_engine(f'postgresql+pg8000://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME_BT}')

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
  AND "public"."1K_coins_ohlcv"."timestamp" >= NOW() - INTERVAL '30 days';
            """

# Use consistent table name
    try:
        with engine.connect() as connection:
            df = pd.read_sql_query(query, connection)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.sort_values(by=["slug", "timestamp"], inplace=True)
        logging.info(f"✅ Data fetched. Shape: {df.shape}")
        return df
    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        raise

# 🔹 Calculate 1 Day Percentage Change
def calculate_pct_change(df):
    logging.info("Calculating 1-day percentage change...")
    df['m_pct_1d'] = df.groupby('slug')['close'].transform(lambda x: x.pct_change())
    return df

# 🔹 Define Risk-Free Rate (Centralized Configuration)
RISK_FREE_RATE = 0.0  # Consistent with original notebook

# 🔹 Calculate Benchmark Returns (Bitcoin)
def calculate_benchmark_returns(df):
    logging.info("Calculating benchmark (Bitcoin) returns...")
    benchmark_df = df[df['slug'] == 'bitcoin'].copy()
    if benchmark_df.empty:
        logging.warning("Bitcoin data not found.  Benchmark-dependent ratios will be NaN.")
        return pd.Series(dtype='float64')  # Return empty Series, but with correct dtype
    return benchmark_df.set_index('timestamp')['m_pct_1d']

# 🔹 Calculate Alpha (Vectorized)
def calculate_alpha(group, benchmark_avg_return):
    # No slug needed, use group directly and iloc for safety
    logging.info(f"Calculating Alpha for: {group['slug'].iloc[0]}")
    avg_return = group['m_pct_1d'].mean()
    return pd.Series({'m_rat_alpha': avg_return - benchmark_avg_return})

# 🔹 Calculate Beta (Vectorized)
def calculate_beta(group, benchmark_returns):
    logging.info(f"Calculating Beta for: {group['slug'].iloc[0]}")
    group = group.set_index('timestamp')
    # Align benchmark returns, handling potential NaNs and zero variance
    aligned_benchmark = benchmark_returns.reindex(group.index, method='ffill')
    covariance = group['m_pct_1d'].cov(aligned_benchmark)
    benchmark_variance = benchmark_returns.var()
    beta = covariance / benchmark_variance if benchmark_variance != 0 else np.nan
    return pd.Series({'d_rat_beta': beta})

# 🔹 Calculate Omega Ratio (Vectorized)
def calculate_omega_ratio(group, benchmark_returns):
    logging.info(f"Calculating Omega Ratio for: {group['slug'].iloc[0]}")
    group = group.set_index('timestamp')
    returns = group['m_pct_1d']
    aligned_benchmark = benchmark_returns.reindex(group.index, method='ffill')
    excess_returns = returns - aligned_benchmark
    avg_gain = excess_returns[excess_returns > 0].mean()
    avg_loss = abs(excess_returns[excess_returns < 0].mean())
    omega_ratio = avg_gain / avg_loss if avg_loss != 0 else np.inf
    return pd.Series({'m_rat_omega': omega_ratio})

# 🔹 Calculate Sharpe Ratio (Vectorized)
def calculate_sharpe_ratio(group, risk_free_rate=RISK_FREE_RATE):
    logging.info(f"Calculating Sharpe Ratio for: {group['slug'].iloc[0]}")
    returns = group.set_index('timestamp')['m_pct_1d']
    excess_returns = returns - risk_free_rate
    sharpe_ratio = excess_returns.mean() / excess_returns.std() if excess_returns.std() != 0 else np.nan
    return pd.Series({'v_rat_sharpe': sharpe_ratio})

# 🔹 Calculate Sortino Ratio (Vectorized)
def calculate_sortino_ratio(group, risk_free_rate=RISK_FREE_RATE):
    logging.info(f"Calculating Sortino Ratio for: {group['slug'].iloc[0]}")
    returns = group.set_index('timestamp')['m_pct_1d']
    excess_returns = returns - risk_free_rate
    downside_returns = excess_returns[excess_returns < 0]
    downside_deviation = downside_returns.std()
    sortino_ratio = excess_returns.mean() / downside_deviation if downside_deviation != 0 else np.nan
    return pd.Series({'v_rat_sortino': sortino_ratio})

# 🔹 Calculate Treynor Ratio (Vectorized)
def calculate_treynor_ratio(group, beta_values, risk_free_rate=RISK_FREE_RATE):
    logging.info(f"Calculating Treynor Ratio for: {group['slug'].iloc[0]}")
    slug = group['slug'].iloc[0]
    beta = beta_values.get(slug, np.nan)  # Safely get beta
    if pd.isna(beta):
        logging.warning(f"Beta value missing for {slug}, Treynor Ratio will be NaN.")
        return pd.Series({'v_rat_teynor': np.nan})

    returns = group.set_index('timestamp')['m_pct_1d']
    avg_return = returns.mean()
    treynor_ratio = (avg_return - risk_free_rate) / beta if beta != 0 else np.nan
    return pd.Series({'v_rat_teynor': treynor_ratio})

# 🔹 Calculate Common Sense Ratio (Vectorized)
def calculate_common_sense_ratio(group):
    logging.info(f"Calculating Common Sense Ratio for: {group['slug'].iloc[0]}")
    returns = group.set_index('timestamp')['m_pct_1d']
    cum_returns = (1 + returns).cumprod()
    peak = cum_returns.cummax()
    drawdown = (cum_returns - peak) / peak
    max_drawdown = drawdown.min()
    # Handle max_drawdown of 0
    common_sense_ratio = returns.mean() / abs(max_drawdown) if max_drawdown != 0 else np.nan
    return pd.Series({'v_rat_common_sense': common_sense_ratio})

# 🔹 Calculate Information Ratio (Vectorized)
def calculate_information_ratio(group, benchmark_returns):
    logging.info(f"Calculating Information Ratio for: {group['slug'].iloc[0]}")
    returns = group.set_index('timestamp')['m_pct_1d']
    aligned_benchmark = benchmark_returns.reindex(returns.index, method='ffill')
    active_returns = returns - aligned_benchmark
    tracking_error = active_returns.std()
    # Handle zero tracking_error
    information_ratio = active_returns.mean() / tracking_error if tracking_error != 0 else np.nan
    return pd.Series({'v_rat_information': information_ratio})

# 🔹 Calculate Win/Loss Ratio (Vectorized)
def calculate_winloss_ratio(group):
    logging.info(f"Calculating Win/Loss Ratio for: {group['slug'].iloc[0]}")
    returns = group.set_index('timestamp')['m_pct_1d']
    wins = (returns > 0).sum()
    losses = (returns < 0).sum()
    winloss_ratio = wins / losses if losses != 0 else np.inf  # Avoid division by zero
    return pd.Series({'v_rat_win_loss': winloss_ratio})

# 🔹 Calculate Win Rate (Vectorized)
def calculate_win_rate(group):
    logging.info(f"Calculating Win Rate for: {group['slug'].iloc[0]}")
    returns = group.set_index('timestamp')['m_pct_1d']
    wins = (returns > 0).sum()
    total = len(returns)
    win_rate = wins / total if total != 0 else np.nan  # Avoid division by zero
    return pd.Series({'m_rat_win_rate': win_rate})

# 🔹 Calculate Risk of Ruin (Vectorized)
def calculate_risk_of_ruin(group):
    logging.info(f"Calculating Risk of Ruin for: {group['slug'].iloc[0]}")
    win_rate = calculate_win_rate(group)['m_rat_win_rate']
    if pd.isna(win_rate) or win_rate == 0 or win_rate == np.inf:
        return pd.Series({'m_rat_ror': np.nan})  # Handle invalid win rates
    returns = group.set_index('timestamp')['m_pct_1d']
    n = len(returns)
    ror = ((1 - win_rate) / win_rate) ** n  # Use the calculated win_rate
    return pd.Series({'m_rat_ror': ror})

# 🔹 Calculate Gain to Pain Ratio (Vectorized)
def calculate_gain_to_pain(group):
    logging.info(f"Calculating Gain to Pain Ratio for: {group['slug'].iloc[0]}")
    returns = group.set_index('timestamp')['m_pct_1d']
    total_gain = returns[returns > 0].sum()
    total_pain = abs(returns[returns < 0]).sum()
    gain_to_pain_ratio = total_gain / total_pain if total_pain != 0 else np.nan # Handle zero pain
    return pd.Series({'d_rat_pain': gain_to_pain_ratio})

# 🔹 Generate Binary Signals for Ratios (Vectorized)
def generate_binary_signals_ratios(ratios_df):
    logging.info("Generating binary signals for ratios...")
    # Use np.sign for -1, 0, 1 signals, and fill NaN with 0
    for col in ['m_rat_alpha', 'd_rat_beta', 'v_rat_sharpe', 'v_rat_sortino', 'v_rat_teynor', 'v_rat_common_sense']:
        ratios_df[col + '_bin'] = np.sign(ratios_df[col]).fillna(0).astype(int)

    # Use np.select for more complex conditions
    ratios_df['v_rat_information_bin'] = np.select([ratios_df['v_rat_information'] < -0.1, ratios_df['v_rat_information'] >= -0.1], [-1, 1], default=0)
    ratios_df['v_rat_win_loss_bin'] = np.select([ratios_df['v_rat_win_loss'] < 1, ratios_df['v_rat_win_loss'] >= 1], [-1, 1], default=0)
    ratios_df['m_rat_win_rate_bin'] = np.select([ratios_df['m_rat_win_rate'] < 0.5, ratios_df['m_rat_win_rate'] >= 0.5], [-1, 1], default=0)
    ratios_df['m_rat_ror_bin'] = np.select([ratios_df['m_rat_ror'] > 0.1, ratios_df['m_rat_ror'] <= 0.1], [-1, 1], default=0)
    ratios_df['d_rat_pain_bin'] = np.select([ratios_df['d_rat_pain'] < 0.5, ratios_df['d_rat_pain'] >= 0.5], [-1, 1], default=0)

    return ratios_df

# Push Data to Database with TRUNCATE + INSERT (live) or DELETE + INSERT (backtest)
def push_to_db(df, table_name, engine, if_exists="replace"):
    try:
        if if_exists == "replace":
            # Live DB: TRUNCATE + INSERT to preserve table structure
            with engine.connect() as conn:
                conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))
                conn.commit()
            df.to_sql(table_name, con=engine, if_exists="append", index=False)
        else:
            # Backtest DB: DELETE matching timestamps first to prevent duplicates
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
        logging.info(f"{table_name} uploaded successfully!")
    except Exception as e:
        logging.error(f"Error pushing data to {table_name}: {e}")
        raise

# 🔹 Ensure Required Columns Exist and Fill NaN
def ensure_required_columns(df, required_columns):
    """Ensures all required columns exist, filling missing ones with NaN."""
    for col in required_columns:
        if col not in df.columns:
            logging.warning(f"Column {col} missing, filling with NaN")
            df[col] = np.nan
    return df

# 🔹 Main Execution Block
if __name__ == "__main__":
    start_time = time.time()

    # --- Database Engines ---
    engine = create_db_engine()
    engine_bt = create_db_engine_backtest()

    # --- Fetch Data ---
    df = fetch_data(engine)

    # --- Calculate prerequisite indicators ---
    df = calculate_pct_change(df)

    # --- Filter for last 30 days ---
    end_date = df['timestamp'].max()
    start_date = end_date - pd.Timedelta(days=28)
    df_filtered = df[(df['timestamp'] >= start_date) & (df['timestamp'] <= end_date)].copy()
    df_filtered = df_filtered.sort_values(by='timestamp', ascending=False)


    # --- Calculate Benchmark Returns ---
    benchmark_returns = calculate_benchmark_returns(df_filtered)
    benchmark_avg_return = benchmark_returns.mean()

    # --- Calculate Ratios ---
    # 1. Calculate Beta for all slugs *before* the main loop
    beta_values = {}
    for slug, group in df_filtered.groupby('slug'):
        if slug != 'bitcoin':  # Exclude benchmark slug
            beta_series = calculate_beta(group, benchmark_returns)
            beta_values[slug] = beta_series['d_rat_beta']

    # 2. Prepare an empty list to collect ratio dataframes
    ratios_list = []

    # 3. Iterate through groups (excluding Bitcoin) and calculate ratios
    for slug, group in df_filtered.groupby('slug'):
        if slug == 'bitcoin':
            continue  # Skip the benchmark slug

        # Calculate all ratios, passing necessary pre-calculated values
        alpha_series = calculate_alpha(group, benchmark_avg_return)
        omega_series = calculate_omega_ratio(group, benchmark_returns)
        sharpe_series = calculate_sharpe_ratio(group)
        sortino_series = calculate_sortino_ratio(group)
        treynor_series = calculate_treynor_ratio(group, beta_values)  # Pass beta_values
        common_sense_series = calculate_common_sense_ratio(group)
        information_ratio_series = calculate_information_ratio(group, benchmark_returns)
        winloss_series = calculate_winloss_ratio(group)
        win_rate_series = calculate_win_rate(group)
        risk_of_ruin_series = calculate_risk_of_ruin(group)
        gain_to_pain_series = calculate_gain_to_pain(group)

        # Combine all series into a single DataFrame row
        combined_series = pd.concat([
            alpha_series, omega_series, sharpe_series, sortino_series,
            treynor_series, common_sense_series, information_ratio_series,
            winloss_series, win_rate_series, risk_of_ruin_series,
            gain_to_pain_series
        ])
        ratios_df_temp = pd.DataFrame(combined_series).transpose()
        ratios_df_temp['slug'] = slug  # Add the slug
        # Get the first row of the group for common columns (id, name, timestamp)
        first_row = group.iloc[0]
        ratios_df_temp['id'] = first_row['id']
        ratios_df_temp['name'] = first_row['name']
        ratios_df_temp['timestamp'] = first_row['timestamp']
        ratios_df_temp['d_rat_beta'] = beta_values[slug]
        ratios_list.append(ratios_df_temp)

    # 4. Concatenate all individual ratio dataframes
    ratios_df = pd.concat(ratios_list, ignore_index=True)

    # --- Generate Binary Signals ---
    ratios_df = generate_binary_signals_ratios(ratios_df)

    # --- Column Selection (Define your desired columns here) ---
    ratios_cols = [
        'slug', 'name', 'timestamp',
        'm_rat_alpha', 'd_rat_beta', 'm_rat_omega', 'v_rat_sharpe',
        'v_rat_sortino', 'v_rat_teynor', 'v_rat_common_sense',
        'v_rat_information', 'v_rat_win_loss', 'm_rat_win_rate',
        'm_rat_ror', 'd_rat_pain'
    ]
    ratios_signals_cols = [
        'slug', 'name', 'timestamp',
        'm_rat_alpha_bin', 'd_rat_beta_bin', 'v_rat_sharpe_bin',
        'v_rat_sortino_bin', 'v_rat_teynor_bin', 'v_rat_common_sense_bin',
        'v_rat_information_bin', 'v_rat_win_loss_bin', 'm_rat_win_rate_bin',
        'm_rat_ror_bin', 'd_rat_pain_bin'
    ]
        # --- Handle Coloums --
    latest_ratios_df=ratios_df[ratios_cols]
    latest_ratios_signals_df=ratios_df[ratios_signals_cols]

    # --- Handle Infinite Values ---
    latest_ratios_df = latest_ratios_df.replace([np.inf, -np.inf], np.nan)
    latest_ratios_signals_df = latest_ratios_signals_df.replace([np.inf, -np.inf], np.nan)

    # --- Push to Database ---
    push_to_db(latest_ratios_df, "FE_RATIOS", engine, if_exists="replace")
    push_to_db(latest_ratios_signals_df, "FE_RATIOS_SIGNALS", engine, if_exists="replace")

        # --- Push to Backtest Database ---
    engine_bt = create_db_engine_backtest()
    push_to_db(latest_ratios_df, "FE_RATIOS", engine_bt, if_exists="append")
    push_to_db(latest_ratios_signals_df, "FE_RATIOS_SIGNALS", engine_bt, if_exists="append")

    engine.dispose()
    engine_bt.dispose()

    end_time = time.time()
    elapsed_time = (end_time - start_time) / 60
    logging.info(f"Process completed in {elapsed_time:.2f} minutes")
