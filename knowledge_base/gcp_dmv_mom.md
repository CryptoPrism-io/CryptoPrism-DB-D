# gcp_dmv_mom.py - Momentum Indicators Calculator

## Overview
This script is the **momentum analysis engine** of the CryptoPrism-DB system, calculating 21 different momentum-based technical indicators and generating binary trading signals for the top 1000 cryptocurrencies.

## Detailed Functionality

### **Data Collection Process**
1. **Database Connection**: Connects to PostgreSQL using hardcoded credentials (dbcp database)
2. **Data Filtering**: 
   - Fetches OHLCV data from `1K_coins_ohlcv` table
   - Inner joins with `crypto_listings_latest_1000` to ensure top 1000 coins only
   - Filters last 110 days of data for sufficient calculation periods
3. **Data Validation**: Removes duplicates and ensures chronological ordering

### **Momentum Indicators Calculated (21 Total)**

#### **RSI Family (5 indicators)**
```python
# Multiple period RSI using exponential smoothing
rsi_9, rsi_18, rsi_27, rsi_54, rsi_108
```
- **Formula**: Uses Wilder's exponential smoothing method
- **Periods**: 9, 18, 27, 54, 108 days
- **Range**: 0-100 (overbought >70, oversold <30)

#### **Rate of Change (ROC)**
```python
roc_9 = ((close - close.shift(9)) / close.shift(9)) * 100
```
- **Purpose**: Measures price change percentage over 9 periods
- **Usage**: Momentum strength and direction indicator

#### **Williams %R**
```python
williams_r_14 = ((highest_high - close) / (highest_high - lowest_low)) * -100
```
- **Period**: 14 days
- **Range**: -100 to 0 (overbought >-20, oversold <-80)

#### **Stochastic Momentum Index (SMI)**
```python
# Double smoothed momentum oscillator
smi_value, smi_signal = calculate_smi(high, low, close, k_period=14, d_period=3)
```
- **Components**: SMI line and signal line
- **Smoothing**: Double EMA smoothing for noise reduction

#### **Chande Momentum Oscillator (CMO)**
```python
cmo_14 = ((sum_gains - sum_losses) / (sum_gains + sum_losses)) * 100
```
- **Period**: 14 days
- **Range**: -100 to +100
- **Purpose**: Momentum with overbought/oversold levels

#### **True Strength Index (TSI)**
```python
# Double smoothed price momentum
tsi = 100 * (double_smoothed_pc / double_smoothed_apc)
```
- **Smoothing**: 25-period and 13-period double smoothing
- **Purpose**: Trend direction and momentum strength

#### **Standard Momentum**
```python
momentum_10 = close - close.shift(10)
```
- **Period**: 10 days
- **Purpose**: Simple price difference momentum

### **Binary Signal Generation (5 signals)**
```python
# RSI-based signals
m_rsi_9_signal = np.where(rsi_9 > 70, 1, np.where(rsi_9 < 30, -1, 0))
m_rsi_54_signal = np.where(rsi_54 > 70, 1, np.where(rsi_54 < 30, -1, 0))

# Williams %R signals  
m_williams_r_signal = np.where(williams_r_14 > -20, 1, np.where(williams_r_14 < -80, -1, 0))

# ROC signals
m_roc_signal = np.where(roc_9 > 5, 1, np.where(roc_9 < -5, -1, 0))

# Momentum signals
m_momentum_signal = np.where(momentum_10 > 0, 1, np.where(momentum_10 < 0, -1, 0))
```

### **Data Output Process**

#### **Dual Database Architecture**
1. **Live Database (`dbcp`)**:
   - `FE_MOMENTUM`: Latest timestamp data only
   - `FE_MOMENTUM_SIGNALS`: Binary signals for current analysis

2. **Backtest Database (`cp_backtest`)**:
   - Same tables but in append mode for historical analysis
   - Preserves all timestamps for backtesting

#### **Performance Optimization**
- **Vectorized Calculations**: Uses pandas groupby for efficient computation
- **Batch Processing**: 10,000 record batches for database uploads  
- **Memory Management**: Processes data in chunks to handle 1000+ cryptocurrencies

### **Error Handling & Logging**
```python
# Comprehensive logging system
logging.basicConfig(level=logging.INFO, 
                   handlers=[logging.FileHandler("app.log"), logging.StreamHandler()])

# Database error handling
try:
    df.to_sql('FE_MOMENTUM', con=engine, if_exists='replace', method='multi')
    logger.info("FE_MOMENTUM uploaded successfully.")
except SQLAlchemyError as e:
    logger.error(f"Database upload failed: {e}")
```

### **Integration Points**
- **Upstream**: Depends on `gcp_108k_1kcoins.R` for OHLCV data
- **Downstream**: Feeds into `gcp_dmv_core.py` for signal aggregation  
- **Pipeline Position**: Stage 3.5 in the DMV workflow

## Usage
```bash
# Manual execution
python gcp_postgres_sandbox/gcp_dmv_mom.py

# Automated via GitHub Actions (DMV workflow)
# Runs after gcp_dmv_pct.py in the pipeline
```

## Dependencies
- pandas>=2.2.2 - Data manipulation and analysis
- numpy>=1.26.4 - Numerical computations  
- sqlalchemy>=2.0.32 - Database connectivity
- psycopg2-binary>=2.9.9 - PostgreSQL adapter
- logging - Progress tracking and error reporting

## Output Tables Schema
```sql
-- FE_MOMENTUM table (21 momentum indicators)
slug, timestamp, rsi_9, rsi_18, rsi_27, rsi_54, rsi_108,
roc_9, williams_r_14, smi, smi_signal, cmo_14, 
tsi, momentum_10, [additional momentum metrics...]

-- FE_MOMENTUM_SIGNALS table (5 binary signals)  
slug, timestamp, m_rsi_9_signal, m_rsi_54_signal,
m_williams_r_signal, m_roc_signal, m_momentum_signal
```

## Recent Changes
- **v4.8.2** (2026-05-13): Both `to_sql` calls (FE_MOMENTUM_SIGNALS to dbcp + cp_backtest) now use `method='multi', chunksize=200` -- ~10x faster writes.
