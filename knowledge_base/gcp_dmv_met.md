````markdown
# gcp_dmv_met.py - Cryptocurrency Metrics & Signal Generator

## Disclaimer
Note: All sensitive configuration values are redacted in this document.

## Overview
This script is the **fundamental metrics calculation engine** of the CryptoPrism-DB system, responsible for computing critical cryptocurrency metrics including All-Time High/Low analysis, coin age calculations, market cap categorization, cumulative returns, and generating binary trading signals for the Durability and Valuation components of the DMV framework.

## Detailed Functionality

### **Database Connection Architecture**

#### **1. Dual Database Setup**
```python
# Main production database
db_name = "dbcp"
gcp_engine = create_engine(f'postgresql+psycopg2://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}')

# Backtesting database  
db_name_bt = "cp_backtest"
gcp_engine_bt = create_engine(f'postgresql+psycopg2://{db_user}:{db_password}@{db_host}:{db_port}/{db_name_bt}')
```
- **Primary Database (`dbcp`)**: Stores latest metrics for real-time analysis
- **Backtest Database (`cp_backtest`)**: Historical metrics for backtesting and research
- **Connection Management**: SQLAlchemy engines for concurrent database operations

#### **2. Data Source Integration**
```python
query = 'SELECT * FROM "1K_coins_ohlcv"'
all_coins_ohlcv_filtered = pd.read_sql_query(query, connection)

query = "SELECT * FROM crypto_listings_latest_1000"
top_1000_cmc_rank = pd.read_sql_query(query, connection)
```
- **OHLCV Data**: Complete price and volume history from `1K_coins_ohlcv`
- **Market Rankings**: Latest top 1000 cryptocurrency rankings
- **Data Volume**: Processes historical data for comprehensive metric calculations

### **Core Metrics Calculation Engine**

#### **3. Time-Series Data Preparation**
```python
df = all_coins_ohlcv_filtered
df['timestamp'] = pd.to_datetime(df['timestamp'])
df.sort_values(by=['slug', 'timestamp'], inplace=True)
grouped = df.groupby('slug')
```
- **Temporal Indexing**: Ensures chronological data ordering
- **Grouping Strategy**: Per-cryptocurrency calculations using pandas groupby
- **Memory Efficiency**: Processes data in cryptocurrency-specific chunks

#### **4. Momentum & Return Calculations**

##### **Daily Percentage Change**
```python
df['m_pct_1d'] = grouped['close'].pct_change()
```
- **Purpose**: Day-over-day price change percentage
- **Usage**: Foundation for all return-based metrics
- **Signal Category**: Momentum (m_ prefix)

##### **Cumulative Returns**
```python
df['d_pct_cum_ret'] = (1 + df['m_pct_1d']).groupby(df['slug']).cumprod() - 1
```
- **Purpose**: Total return since first data point
- **Calculation**: Compound return using consecutive daily changes
- **Signal Category**: Durability (d_ prefix)

#### **5. All-Time High/Low Analysis**

##### **ATH/ATL Calculation**
```python
# All-time high tracking
df['v_met_ath'] = grouped['high'].cummax()

# All-time low tracking  
df['v_met_atl'] = grouped['low'].cummin()
```
- **ATH Logic**: Rolling maximum of historical high prices
- **ATL Logic**: Rolling minimum of historical low prices
- **Signal Category**: Valuation (v_ prefix)

##### **ATH/ATL Date Tracking**
```python
# Date when ATH was reached
df['ath_date'] = df.groupby('slug')['high'].transform(lambda x: x.idxmax())

# Date when ATL was reached
df['atl_date'] = df.groupby('slug')['low'].transform(lambda x: x.idxmin())
```
- **ATH Date**: Timestamp of maximum price occurrence
- **ATL Date**: Timestamp of minimum price occurrence
- **Timezone Handling**: UTC normalization for consistency

##### **Days Since ATH/ATL**
```python
current_date = pd.Timestamp.now().tz_localize(None)
df['d_met_ath_days'] = (current_date - df['ath_date']).dt.days
df['d_met_atl_days'] = (current_date - df['atl_date']).dt.days

# Convert to weeks and months
df['d_met_ath_week'] = df['d_met_ath_days'] // 7
df['d_met_ath_month'] = df['d_met_ath_days'] // 30
```
- **Time Analysis**: Days, weeks, months since ATH/ATL
- **Risk Assessment**: Recent ATH indicates potential overvaluation
- **Opportunity Identification**: Extended ATL periods may signal value opportunities

### **Market Intelligence & Categorization**

#### **6. Latest Data Filtering**
```python
# Keep only latest timestamp for each cryptocurrency
met_df1 = df.loc[df.groupby('slug')['timestamp'].idxmax()]

# Inner join with rankings for metadata enrichment
met_df1 = pd.merge(met_df1, top_1000_cmc_rank[['slug', 'date_added', 'last_updated']], on='slug', how='inner')
```
#### **7. Coin Age Analysis**
```python
df['date_added'] = pd.to_datetime(df['date_added'], errors='coerce').dt.tz_localize(None)
current_date = pd.Timestamp.now().normalize()

# Coin age calculations
df['d_met_coin_age_d'] = (current_date - df['date_added']).dt.days
df['d_met_coin_age_m'] = df['d_met_coin_age_d'] // 30  # Approximate months
df['d_met_coin_age_y'] = df['d_met_coin_age_d'] // 365  # Approximate years
```
- **Durability Indicator**: Older coins generally more established
- **Risk Assessment**: Newer projects may have higher volatility
- **Market Maturity**: Age categories help classify market position

#### **8. Market Cap Categorization**
```python
def categorize_market_cap(market_cap):
    if market_cap >= 1e12:
        return '1T-100B'      # Mega cap ($1T+)
    elif market_cap >= 1e11:
        return '100B-10B'     # Large cap ($100B-$1T)
    elif market_cap >= 1e10:
        return '10B-1B'       # Mid cap ($10B-$100B)
    elif market_cap >= 1e9:
        return '1B-100M'      # Small cap ($1B-$10B)
    elif market_cap >= 1e7:
        return '100M-1M'      # Micro cap ($100M-$1B)
    else:
        return 'Under1M'      # Nano cap (Under $100M)

df['m_cap_cat'] = df['market_cap'].apply(categorize_market_cap)
```
- **Size Classification**: 6-tier market cap categorization
- **Risk Profiling**: Larger caps generally more stable
- **Investment Strategy**: Categories align with institutional investment criteria

### **Signal Generation Framework**

#### **9. Binary Signal Calculation**

##### **Daily Return Signals**
```python
metrics['m_pct_1d_signal'] = np.where(metrics['m_pct_1d'] > 0, 1, -1)
```
- **Logic**: Positive daily returns = Buy signal (1), Negative = Sell signal (-1)
- **Application**: Short-term momentum indicator

##### **Cumulative Return Signals**  
```python
metrics['d_pct_cum_ret_signal'] = np.where(metrics['d_pct_cum_ret'] > 0, 1, -1)
```
- **Logic**: Positive total returns = Buy signal, Negative = Sell signal
- **Application**: Long-term performance indicator

##### **ATH Recency Signals**
```python
metrics['d_met_ath_month_signal'] = ((100 - metrics['d_met_ath_month']) * 2)/100
```
- **Logic**: Recent ATH (lower month count) = Lower signal score
- **Range**: 0-2 scale (older ATH = higher score)
- **Application**: Contrarian valuation indicator

##### **Market Cap Signals**
```python
market_cap_signal = {
    '100M-1M': 0.25,      # Micro cap
    '1B-100M': 0.4,       # Small cap  
    'Under1M': 0.1,       # Nano cap
    '10B-1B': 0.5,        # Mid cap
    '1T-100B': 1,         # Mega cap
    '100B-10B': 0.75      # Large cap
}
metrics['d_market_cap_signal'] = metrics['m_cap_cat'].map(market_cap_signal)
```
- **Logic**: Larger market caps receive higher durability scores
- **Scale**: 0.1-1.0 scoring system
- **Application**: Risk-adjusted position sizing

##### **Coin Age Signals**
```python
metrics['d_met_coin_age_y_signal'] = np.where(
    metrics['d_met_coin_age_y'] < 1, 0,
    np.where(metrics['d_met_coin_age_y'] >= 1, 1 - (1 / metrics['d_met_coin_age_y']), 0)
)
```
- **Logic**: Asymptotic function favoring older coins
- **Formula**: 1 - (1/age_years) for coins >= 1 year old
- **Application**: Stability and longevity assessment

### **Database Output Architecture**

#### **10. Dual Table Strategy**

##### **FE_METRICS Table** (Complete Metrics)
```python
metrics.to_sql('FE_METRICS', con=gcp_engine, if_exists='replace', index=False)
```
- **Content**: All calculated metrics and raw data
- **Purpose**: Comprehensive dataset for analysis and research
- **Update Mode**: `replace` for fresh data each run

##### **FE_METRICS_SIGNAL Table** (Binary Signals Only)
```python
metrics_signal = metrics.drop(metrics.columns[3:22], axis=1)
metrics_signal.to_sql('FE_METRICS_SIGNAL', con=gcp_engine, if_exists='replace', index=False)
```
- **Content**: Only identifiers and binary signal columns
- **Purpose**: Optimized for DMV core aggregation
- **Data Reduction**: Drops intermediate calculation columns

#### **11. Backtest Database Replication**
```python
# Replicate to backtest database
metrics.to_sql('FE_METRICS', con=gcp_engine_bt, if_exists='replace', index=False)
metrics_signal.to_sql('FE_METRICS_SIGNAL', con=gcp_engine_bt, if_exists='replace', index=False)
```
- **Purpose**: Historical analysis and backtesting capabilities
- **Consistency**: Identical schema across both databases
- **Research**: Enables historical signal performance analysis

### **Data Quality & Performance**

#### **12. Duplicate Prevention**
```python
# Check for duplicates in the 'slug' column
duplicate_slugs = metrics[metrics.duplicated(subset=['slug'], keep=False)]
if not duplicate_slugs.empty:
    print("Duplicate slugs found:", duplicate_slugs['slug'].unique())

# Remove duplicates, keeping first occurrence
metrics_no_duplicates = metrics.drop_duplicates(subset=['slug'], keep='first')
```
- **Validation**: Ensures one record per cryptocurrency
- **Data Integrity**: Prevents signal calculation errors
- **Quality Control**: Identifies and resolves data issues

#### **13. Performance Monitoring**
```python
start_time = time.time()
# ... processing ...
end_time = time.time()
elapsed_time_minutes = (end_time - start_time) / 60
print(f"Cell execution time: {elapsed_time_minutes:.2f} minutes")
```
- **Runtime Tracking**: Monitors processing performance
- **Optimization**: Identifies performance bottlenecks
- **Scaling**: Ensures system can handle increased data volume

### **Integration Points**
- **Upstream Dependencies**: 
  - `1K_coins_ohlcv` table (from `gcp_108k_1kcoins.R`)
  - `crypto_listings_latest_1000` table (from `cmc_listings.py`)
- **Downstream Integration**: 
  - `FE_METRICS_SIGNAL` feeds into `gcp_dmv_core.py`
- **Pipeline Position**: **Stage 3.2** in DMV workflow
- **Data Flow**: OHLCV → Metrics Calculation → Signal Generation → DMV Aggregation

## Usage

### **Manual Execution**
```bash
# Direct script execution
python gcp_postgres_sandbox/gcp_dmv_met.py

# Expected runtime: ~2-4 minutes for 1000 cryptocurrencies
```

### **Automated Workflow**
```yaml
# GitHub Actions integration
- name: Calculate Cryptocurrency Metrics
  run: python gcp_postgres_sandbox/gcp_dmv_met.py
  # Runs after data collection, before signal aggregation
```

## Dependencies

### **Core Libraries**
- **pandas>=2.2.2** - Data manipulation and time series analysis
- **numpy>=1.26.4** - Numerical computations and array operations
- **matplotlib>=3.8.0** - Data visualization (imported but not actively used)
- **seaborn>=0.13.0** - Statistical plotting (imported but not actively used)
- **sqlalchemy>=2.0.32** - Database connectivity and ORM
- **psycopg2-binary>=2.9.9** - PostgreSQL adapter
- **warnings** - Built-in module for suppressing warnings
- **time** - Built-in module for performance monitoring

### **Installation**
```bash
pip install pandas numpy matplotlib seaborn sqlalchemy psycopg2-binary
```

## Output Schema

### **FE_METRICS Table Structure**
```sql
CREATE TABLE FE_METRICS (
    slug VARCHAR(255),              -- Cryptocurrency identifier
    timestamp TIMESTAMP,            -- Data timestamp
    
    -- Price & Volume Data
    open DECIMAL(15,8),
    high DECIMAL(15,8), 
    low DECIMAL(15,8),
    close DECIMAL(15,8),
    volume DECIMAL(20,2),
    market_cap DECIMAL(20,2),
    
    -- Momentum Metrics
    m_pct_1d DECIMAL(10,6),         -- Daily percentage change
    d_pct_cum_ret DECIMAL(15,6),    -- Cumulative returns
    
    -- ATH/ATL Analysis
    v_met_ath DECIMAL(15,8),        -- All-time high price
    v_met_atl DECIMAL(15,8),        -- All-time low price  
    ath_date TIMESTAMP,             -- ATH date
    atl_date TIMESTAMP,             -- ATL date
    d_met_ath_days INTEGER,         -- Days since ATH
    d_met_atl_days INTEGER,         -- Days since ATL
    d_met_ath_week INTEGER,         -- Weeks since ATH
    d_met_ath_month INTEGER,        -- Months since ATH
    d_met_atl_week INTEGER,         -- Weeks since ATL
    d_met_atl_month INTEGER,        -- Months since ATL
    
    -- Coin Maturity Metrics
    date_added DATE,                -- Launch date
    last_updated TIMESTAMP,         -- Last update
    d_met_coin_age_d INTEGER,       -- Coin age in days
    d_met_coin_age_m INTEGER,       -- Coin age in months
    d_met_coin_age_y INTEGER,       -- Coin age in years
    
    -- Market Classification
    m_cap_cat VARCHAR(50)           -- Market cap category
);
```

### **FE_METRICS_SIGNAL Table Structure**
```sql
CREATE TABLE FE_METRICS_SIGNAL (
    slug VARCHAR(255),                    -- Cryptocurrency identifier
    timestamp TIMESTAMP,                  -- Signal timestamp
    name VARCHAR(255),                    -- Cryptocurrency name
    
    -- Binary Trading Signals (-1, 0, 1)
    m_pct_1d_signal INTEGER,              -- Daily return signal
    d_pct_cum_ret_signal INTEGER,         -- Cumulative return signal
    d_met_ath_month_signal DECIMAL(4,2),  -- ATH recency signal (0-2 scale)
    d_market_cap_signal DECIMAL(4,2),     -- Market cap durability signal (0.1-1.0)
    d_met_coin_age_y_signal DECIMAL(6,4)  -- Coin age signal (0-1 scale)
);
```

## Key Features Summary

1. **Comprehensive Metric Calculation**: 20+ fundamental cryptocurrency metrics
2. **All-Time Analysis**: ATH/ATL tracking with temporal analysis
3. **Market Classification**: 6-tier market cap categorization system
4. **Coin Maturity Assessment**: Age-based durability scoring
5. **Binary Signal Generation**: 5 standardized trading signals
6. **Dual Database Architecture**: Production and backtesting data separation
7. **Data Quality Assurance**: Duplicate detection and removal
8. **Performance Monitoring**: Runtime tracking and optimization
9. **Time Series Processing**: Efficient grouped calculations for 1000+ cryptocurrencies
10. **DMV Framework Integration**: Feeds Durability, Momentum, and Valuation signals

## Recent Changes
- **v4.8.2** (2026-05-13): All 4 `to_sql` calls (FE_METRICS + FE_METRICS_SIGNAL, dbcp + cp_backtest) now use `method='multi', chunksize=200` -- ~10x faster writes over a high-latency link to Cloud SQL postgres.
