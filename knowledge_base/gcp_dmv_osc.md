# gcp_dmv_osc.py - Technical Oscillator Indicators Engine

## Overview
This script is the **advanced technical analysis module** of the CryptoPrism-DB system, specializing in calculating 6 sophisticated oscillator indicators and generating binary momentum signals. It processes 110 days of OHLCV data to compute MACD, CCI, ADX, Ultimate Oscillator, Awesome Oscillator, and TRIX indicators for comprehensive momentum analysis.

## Detailed Functionality

### **Technical Oscillator Calculations**

#### **1. MACD (Moving Average Convergence Divergence)**
```python
def calculate_macd(df, short_period=12, long_period=26, signal_period=9):
    df['EMA_12'] = df['close'].ewm(span=short_period, adjust=False).mean()
    df['EMA_26'] = df['close'].ewm(span=long_period, adjust=False).mean()
    df['MACD'] = df['EMA_12'] - df['EMA_26']
    df['Signal'] = df['MACD'].ewm(span=signal_period, adjust=False).mean()
```
- **Components**: 12-day EMA, 26-day EMA, 9-day signal line
- **Signal Generation**: MACD line crossovers with signal line
- **Momentum Assessment**: Trend strength and direction changes

#### **2. CCI (Commodity Channel Index)**
```python
def calculate_cci(df, period=20):
    df['TP'] = (df['high'] + df['low'] + df['close']) / 3  # Typical Price
    df['SMA_TP'] = df['TP'].rolling(window=period).mean()
    df['MAD'] = (df['TP'] - df['SMA_TP']).abs().rolling(window=period).mean()
    df['CCI'] = (df['TP'] - df['SMA_TP']) / (0.015 * df['MAD'])
```
- **Range**: Typically -300 to +300
- **Overbought**: Above +108
- **Oversold**: Below -108
- **Application**: Identifies cyclical price movements

#### **3. ADX (Average Directional Index) System**
```python
def calculate_adx(df, period=14):
    # True Range calculation
    df['TR'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['prev_close']), 
                  abs(df['low'] - df['prev_close']))
    )
    
    # Directional Movement
    df['+DM'] = np.where(((df['high'] - df['high'].shift(1)) > 
                         (df['low'].shift(1) - df['low'])),
                        np.maximum(df['high'] - df['high'].shift(1), 0), 0)
    
    # Wilder's smoothing and ADX calculation
    df['ADX'] = df['DX'].ewm(alpha=1/period, adjust=False).mean()
```
- **Components**: +DI, -DI, DX, ADX
- **Trend Strength**: ADX >= 20 indicates strong trend
- **Direction**: +DI > -DI = uptrend, -DI > +DI = downtrend

#### **4. Ultimate Oscillator**
```python
def calculate_ultimate_oscillator(df, short_period=7, intermediate_period=14, long_period=28):
    df['BP'] = df['close'] - np.minimum(df['low'], df['prev_close'])  # Buying Pressure
    df['UO'] = 100 * (
        (4 * df['Avg_BP_short'] + 2 * df['Avg_BP_intermediate'] + df['Avg_BP_long']) /
        (4 * df['Avg_TR_short'] + 2 * df['Avg_TR_intermediate'] + df['Avg_TR_long'])
    )
```
- **Multi-timeframe**: Combines 7, 14, and 28-day periods
- **Weighted Formula**: 4:2:1 weighting for timeframes
- **Overbought**: Above 67, Oversold: Below 33

#### **5. Awesome Oscillator (AO)**
```python
def calculate_awesome_oscillator(df):
    df['MP'] = (df['high'] + df['low']) / 2  # Median Price
    df['SMA_5'] = df['MP'].rolling(window=5).mean()
    df['SMA_34'] = df['MP'].rolling(window=34).mean()
    df['AO'] = df['SMA_5'] - df['SMA_34']
```
- **Calculation**: 5-period SMA minus 34-period SMA of median price
- **Momentum Gauge**: Positive values indicate bullish momentum
- **Zero Line**: Crossovers signal momentum shifts

#### **6. TRIX (Triple Exponential Moving Average)**
```python
def calculate_trix(df, period=15):
    # Triple smoothed exponential moving average
    df['EMA1'] = df['close'].ewm(span=period, adjust=False).mean()
    df['EMA2'] = df['EMA1'].ewm(span=period, adjust=False).mean()
    df['EMA3'] = df['EMA2'].ewm(span=period, adjust=False).mean()
    df['TRIX'] = df['EMA3'].pct_change() * 100
```
- **Smoothing**: Triple exponential smoothing reduces noise
- **Signal**: Rate of change of triple-smoothed EMA
- **Trend Identification**: Positive TRIX = uptrend, Negative = downtrend

#### **7. Supertrend (v4.7.0)**
```python
def calculate_supertrend(df, atr_period=10, multiplier=3.0):
    # hl2 = (high + low) / 2; basic upper = hl2 + multiplier * ATR(10)
    # The "final" upper/lower bands carry forward across bars with a flip rule
    # tied to close vs prior final band.
```
- **Components**: 10-period ATR; basic and final upper/lower bands; direction line
- **Output columns**: `Supertrend_Line` (double precision), `Supertrend_Dir` (double precision)
- **Bin signal**: `m_osc_supertrend_bin` in `FE_OSCILLATORS_SIGNALS` -- +1 when price closes above Supertrend line, -1 when below
- **Per-slug stateful loop**: required because final bands depend on prior bar's final band and close

#### **8. Aroon (v4.7.0)**
```python
def calculate_aroon(df, period=14):
    # Aroon_Up   = 100 * (period - bars_since_highest_high) / period
    # Aroon_Down = 100 * (period - bars_since_lowest_low) / period
    # Aroon_Osc  = Aroon_Up - Aroon_Down
```
- **Output columns**: `Aroon_Up`, `Aroon_Down`, `Aroon_Osc` (all double precision)
- **Range**: 0-100 each; oscillator -100 to +100
- **Bin signal**: `m_osc_aroon_bin` in `FE_OSCILLATORS_SIGNALS` -- +1 when Aroon_Osc > 50, -1 when < -50, 0 otherwise (`np.select(default=0)`)

### **Binary Signal Generation**

#### **Signal Logic Framework**
```python
def generate_binary_signals_oscillators(df):
    # MACD Crossover Signal
    df['m_osc_macd_crossover_bin'] = np.select(
        [df['MACD'] > df['Signal'], df['MACD'] < df['Signal']], 
        [1, -1], default=0
    )
    
    # CCI Overbought/Oversold Signal
    df['m_osc_cci_bin'] = np.select(
        [df['CCI'] > 108, df['CCI'] < -108], 
        [1, -1], default=0
    )
    
    # ADX Trend Strength Signal
    df['m_osc_adx_bin'] = np.select(
        [(df['+DI'] > df['-DI']) & (df['ADX'] >= 20),
         (df['-DI'] > df['+DI']) & (df['ADX'] >= 20)],
        [1, -1], default=0
    )
```

### **Database Architecture**

#### **Dual Table Output System**
- **FE_OSCILLATOR**: Complete indicator calculations (40+ columns)
- **FE_OSCILLATORS_SIGNALS**: Binary signals only (6 signals)

#### **Dual Database Strategy**
- **Production Database (`dbcp`)**: Latest data (`if_exists='replace'`)
- **Backtest Database (`cp_backtest`)**: Historical data (`if_exists='append'`)

### **Performance Optimizations**
- **Vectorized Calculations**: Pandas groupby operations for 1000+ cryptocurrencies
- **Memory Management**: Efficient data pipeline with method chaining
- **Infinite Value Handling**: Automatic replacement of inf/-inf with NaN

### **Integration Points**
- **Upstream**: Depends on `1K_coins_ohlcv` table
- **Downstream**: Feeds `FE_OSCILLATORS_SIGNALS` to `gcp_dmv_core.py`
- **Pipeline Position**: **Stage 3.3** in DMV workflow

## Usage
```bash
python gcp_postgres_sandbox/gcp_dmv_osc.py
# Runtime: ~3-5 minutes for 1000 cryptocurrencies
```

## Dependencies
- **pandas>=2.2.2** - Data manipulation and time series
- **numpy>=1.26.4** - Mathematical operations and signal generation  
- **sqlalchemy>=2.0.32** - Database connectivity
- **logging** - Progress monitoring and error tracking

## Output Schema
```sql
-- FE_OSCILLATORS_SIGNALS table
CREATE TABLE FE_OSCILLATORS_SIGNALS (
    slug VARCHAR(255),
    timestamp TIMESTAMP,
    m_osc_macd_crossover_bin INTEGER,  -- MACD crossover signal (-1,0,1)
    m_osc_cci_bin INTEGER,             -- CCI overbought/oversold (-1,0,1)
    m_osc_adx_bin INTEGER,             -- ADX trend strength (-1,0,1)
    m_osc_uo_bin INTEGER,              -- Ultimate Oscillator signal (-1,0,1)
    m_osc_ao_bin INTEGER,              -- Awesome Oscillator signal (-1,0,1)
    m_osc_trix_bin INTEGER             -- TRIX trend signal (-1,0,1)
    , m_osc_supertrend_bin INTEGER     -- Supertrend signal (-1,0,1) -- v4.7.0
    , m_osc_aroon_bin INTEGER          -- Aroon oscillator signal (-1,0,1) -- v4.7.0
);
```

## Recent Changes
- **v4.8.2** (2026-05-13): Both `to_sql` calls (FE_OSCILLATORS_SIGNALS to dbcp + cp_backtest) now use `method='multi', chunksize=200` -- ~10x faster writes.
- **v4.7.1** (2026-05-11): Bigint bins (`m_osc_supertrend_bin`, `m_osc_aroon_bin`) use `np.select(default=0)` so they default to 0 (neutral) rather than NULL. Matches the long-standing bigint-bin convention; aligns with `dmv_core.py`'s NaN-handling.
- **v4.7.0** (2026-05-11): Added Supertrend (`Supertrend_Line/Dir` + `m_osc_supertrend_bin`) and Aroon (`Aroon_Up/Down/Osc` + `m_osc_aroon_bin`). See sections "7. Supertrend" and "8. Aroon" above.
