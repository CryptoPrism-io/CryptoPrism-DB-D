# gcp_dmv_pct.py - Risk Analytics & Return Calculation Engine

## Overview
This script is the **risk assessment and return analysis module** of the CryptoPrism-DB system, calculating sophisticated risk metrics including Value-at-Risk (VaR), Conditional Value-at-Risk (CVaR), percentage returns, cumulative returns, and volume analysis for comprehensive risk profiling of the top 1000 cryptocurrencies.

## Detailed Functionality

### **Core Return Calculations**

#### **1. Daily Percentage Returns**
```python
def calculate_pct_change(df):
    df["m_pct_1d"] = df.groupby("slug")["close"].pct_change()
    df["d_pct_cum_ret"] = (1 + df["m_pct_1d"]).groupby(df["slug"]).cumprod() - 1
```
- **Daily Returns**: Day-over-day percentage change in closing prices
- **Cumulative Returns**: Compound returns from inception
- **Application**: Foundation for all risk calculations

#### **2. Value-at-Risk (VaR) Analysis**
```python
def calculate_var_cvar(df, confidence_level=0.95):
    # Historical VaR at 95% confidence
    var_df = df.groupby("slug")["m_pct_1d"].quantile(1 - confidence_level).reset_index(name="d_pct_var")
```
- **Definition**: Maximum expected loss at 95% confidence level
- **Method**: Historical simulation using empirical distribution
- **Risk Management**: Portfolio risk budgeting and position sizing

#### **3. Conditional Value-at-Risk (CVaR)**
```python
# Expected Shortfall - average loss beyond VaR threshold
cvar_df = df.groupby("slug").apply(
    lambda x: x["m_pct_1d"][x["m_pct_1d"] <= x["m_pct_1d"].quantile(1 - confidence_level)].mean()
).reset_index(name="d_pct_cvar")
```
- **Definition**: Expected loss given that loss exceeds VaR threshold
- **Superior Metric**: Captures tail risk better than VaR
- **Application**: Extreme risk scenario planning

#### **4. Volume Analysis**
```python
def calculate_volume_pct_change(df):
    df["d_pct_vol_1d"] = df.groupby("slug")["volume"].pct_change()
```
- **Volume Changes**: Daily percentage change in trading volume
- **Liquidity Assessment**: Identifies liquidity risk patterns
- **Market Activity**: Volume spikes often precede price movements

### **Data Quality & Processing**

#### **5. Latest Data Filtering**
```python
def filter_latest_data(df):
    latest_df = df[df["timestamp"] == df.groupby("slug")["timestamp"].transform("max")]
    return latest_df
```
- **Purpose**: Ensures only the most recent data point per cryptocurrency
- **Efficiency**: Reduces dataset from time series to cross-sectional
- **Consistency**: Standardizes timestamp across all cryptocurrencies

#### **6. Data Cleaning Pipeline**
```python
def clean_data(df):
    # Replace infinite values with NaN
    df = df.replace([np.inf, -np.inf], np.nan)
    
    # Handle missing values
    df = df.fillna(method='ffill')
    
    return df
```
- **Infinite Value Handling**: Replaces mathematical infinities
- **Missing Data**: Forward-fill for continuity
- **Data Integrity**: Ensures clean datasets for downstream analysis

### **Risk Signal Generation**

#### **7. Binary Risk Signals**
```python
# Return-based signals
df['m_pct_1d_signal'] = np.where(df['m_pct_1d'] > 0, 1, -1)
df['d_pct_cum_ret_signal'] = np.where(df['d_pct_cum_ret'] > 0, 1, -1)

# Risk-based signals
df['d_pct_var_signal'] = np.where(df['d_pct_var'] < -0.05, -1,  # High risk
                                 np.where(df['d_pct_var'] > -0.02, 1, 0))  # Low risk

df['d_pct_cvar_signal'] = np.where(df['d_pct_cvar'] < -0.10, -1,  # Extreme risk
                                  np.where(df['d_pct_cvar'] > -0.05, 1, 0))  # Moderate risk
```

### **Database Architecture**

#### **8. Dual Database Strategy**
```python
# Production database (latest data)
engine = create_db_engine()
df.to_sql('FE_PCT', con=engine, if_exists='replace', index=False)

# Backtest database (historical append)
engine_bt = create_db_engine_backtest()
df.to_sql('FE_PCT', con=engine_bt, if_exists='append', index=False)
```
- **Production**: Real-time risk metrics for current analysis
- **Backtest**: Historical risk data for backtesting and research
- **Consistency**: Identical schemas across both databases

### **Performance & Monitoring**

#### **9. Execution Tracking**
```python
start_time = time.time()
# ... processing ...
end_time = time.time()
elapsed_time = (end_time - start_time) / 60
logging.info(f"Process completed in {elapsed_time:.2f} minutes")
```
- **Runtime Monitoring**: Tracks processing performance
- **Scalability**: Ensures system can handle growing datasets
- **Optimization**: Identifies processing bottlenecks

### **Integration Points**
- **Upstream**: `1K_coins_ohlcv` and `crypto_listings_latest_1000` tables
- **Downstream**: `FE_PCT_SIGNALS` feeds into `gcp_dmv_core.py`
- **Pipeline Position**: **Stage 3.1** in DMV workflow (first technical analysis step)
- **Data Flow**: Raw OHLCV → Risk Calculations → Signal Generation → DMV Aggregation

## Usage
```bash
python gcp_postgres_sandbox/gcp_dmv_pct.py
# Runtime: ~2-4 minutes for risk analysis of 1000 cryptocurrencies
```

## Dependencies
- **pandas>=2.2.2** - Advanced data manipulation and statistical functions
- **numpy>=1.26.4** - Mathematical operations and quantile calculations
- **sqlalchemy>=2.0.32** - Database operations with transaction management
- **logging** - Comprehensive progress tracking and error reporting
- **time** - Performance measurement and optimization

## Output Schema
```sql
CREATE TABLE FE_PCT (
    slug VARCHAR(255),
    timestamp TIMESTAMP,
    m_pct_1d DECIMAL(10,6),          -- Daily percentage change
    d_pct_cum_ret DECIMAL(15,6),     -- Cumulative returns
    d_pct_var DECIMAL(10,6),         -- Value-at-Risk (95% confidence)
    d_pct_cvar DECIMAL(10,6),        -- Conditional Value-at-Risk
    d_pct_vol_1d DECIMAL(10,6),      -- Daily volume change
    
    -- Binary Signals
    m_pct_1d_signal INTEGER,         -- Daily return signal (-1,0,1)
    d_pct_cum_ret_signal INTEGER,    -- Cumulative return signal (-1,0,1)
    d_pct_var_signal INTEGER,        -- VaR risk signal (-1,0,1)
    d_pct_cvar_signal INTEGER        -- CVaR risk signal (-1,0,1)
);
```

## Key Features
1. **Advanced Risk Metrics**: VaR and CVaR calculations for tail risk assessment
2. **Return Analysis**: Daily and cumulative return calculations
3. **Volume Intelligence**: Trading volume analysis and liquidity assessment
4. **Risk Signal Generation**: Binary signals based on risk thresholds
5. **Data Quality Assurance**: Comprehensive cleaning and validation pipeline
6. **Dual Database Architecture**: Production and backtesting data separation
7. **Performance Monitoring**: Runtime tracking and optimization capabilities

## Recent Changes
- **v4.8.2** (2026-05-13): Both `to_sql` calls (FE_PCT to dbcp + cp_backtest) now use `method='multi', chunksize=200` -- ~10x faster writes.
